"""Stress scenarios X1-X7 (plan section 6). Each returns a :class:`Result` with a pass/fail verdict."""

from __future__ import annotations

import asyncio
import gc
import random
import threading
import typing as t

from mongospecs import (
    DocumentTooLarge,
    MongoSpecsError,
    PoolTimeout,
    Q,
    Registry,
    StaleDocumentError,
    tracking,
)
from mongospecs_bench.harness import Result, Settings, environment, uri_for
from mongospecs_bench.models import MODELS

Scenario = t.Callable[[Settings], t.Awaitable[Result]]


def _verdict(result: Result, passed: bool, summary: str) -> Result:
    result.conclusion = ("PASS: " if passed else "FAIL: ") + summary
    result.add(check="verdict", value="pass" if passed else "fail")
    return result


async def x1(settings: Settings) -> Result:
    result = Result(
        "X1",
        "Many tasks, small pool",
        "Do thousands of concurrent tasks finish on a pool of 5, and do pool waits surface as typed errors?",
    )
    tasks = settings.scale(3000, 200)
    ns = MODELS["msgspec"]
    async with environment(settings, maxPoolSize=5) as registry:
        manager = registry.manager(ns.Plain)

        async def work(i: int) -> int:
            doc = await manager.insert(ns.Plain(n=i, name=f"t{i}"))
            found = await manager.get(doc.id)
            return found.n if found else -1

        done = await asyncio.wait_for(asyncio.gather(*(work(i) for i in range(tasks))), timeout=300)
        count = await manager.count()
        result.add(check="tasks completed", value=len(done))
        result.add(check="documents written", value=count)
        ok = sorted(done) == list(range(tasks)) and count == tasks
    async with environment(settings, maxPoolSize=1, waitQueueTimeoutMS=20) as starved:
        manager = starved.manager(ns.Plain)
        await manager.insert(ns.Plain(n=0))
        from mongospecs import trusted

        slow = [manager.find(trusted({"$where": "sleep(300) || true"})).all() for _ in range(6)]
        outcomes = await asyncio.gather(*slow, return_exceptions=True)
        typed = sum(isinstance(o, PoolTimeout) for o in outcomes)
        untyped = [o for o in outcomes if isinstance(o, BaseException) and not isinstance(o, MongoSpecsError)]
        result.add(check="pool waits raised as PoolTimeout", value=typed)
        result.add(check="untyped errors", value=len(untyped))
        ok = ok and typed >= 1 and not untyped
    return _verdict(
        result, ok, f"{tasks} tasks completed on 5 connections; starved pool raised PoolTimeout {typed} times"
    )


async def x2(settings: Settings) -> Result:
    result = Result(
        "X2",
        "Transaction contention",
        "Do contended read-modify-write transactions lose updates, and are retries bounded?",
    )
    workers = settings.scale(40, 8)
    ns = MODELS["msgspec"]
    async with environment(settings) as registry:
        manager = registry.manager(ns.Plain)
        counter = await manager.insert(ns.Plain(n=0, name="counter"))
        attempts = 0

        async def bump(tx: t.Any) -> None:
            nonlocal attempts
            attempts += 1
            doc = await manager.find(Q.id == counter.id, session=tx).one()
            doc.n += 1
            await manager.save(doc, session=tx)

        await asyncio.gather(*(registry.transaction(bump) for _ in range(workers)))
        final = await manager.get(counter.id)
        assert final is not None
        result.add(check="workers", value=workers)
        result.add(check="final counter", value=final.n)
        result.add(check="callback runs (incl. retries)", value=attempts)
        ok = final.n == workers and attempts <= workers * 50
    return _verdict(result, ok, f"counter reached {final.n}/{workers} with {attempts - workers} retries")


async def x3(settings: Settings) -> Result:
    result = Result(
        "X3",
        "Optimistic-lock storm",
        "With many writers on one revisioned document, does exactly one win per revision?",
    )
    writers = settings.scale(40, 8)
    ns = MODELS["msgspec"]
    async with environment(settings) as registry:
        manager = registry.manager(ns.Managed)
        doc = await manager.insert(ns.Managed(n=0, name="hot"))
        copies = [await manager.find(Q.id == doc.id).one() for _ in range(writers)]

        async def attempt(copy: t.Any) -> bool:
            copy.n += 1
            try:
                await manager.save(copy)
            except StaleDocumentError:
                return False
            return True

        first_round = await asyncio.gather(*(attempt(c) for c in copies))
        result.add(check="winners of the first round", value=sum(first_round))
        conflicts = 0

        async def persistent(_: int) -> None:
            nonlocal conflicts
            while True:
                fresh = await manager.find(Q.id == doc.id).one()
                fresh.n += 1
                try:
                    await manager.save(fresh)
                    return
                except StaleDocumentError:
                    conflicts += 1

        await asyncio.gather(*(persistent(i) for i in range(writers)))
        final = await manager.get(doc.id)
        assert final is not None
        result.add(check="final n", value=final.n)
        result.add(check="final rev", value=final.rev)
        result.add(check="conflicts retried", value=conflicts)
        ok = sum(first_round) == 1 and final.n == writers + 1 and final.rev == writers + 1
    return _verdict(
        result,
        ok,
        f"1 winner in the first round; after retries n={final.n}, rev={final.rev} for {writers} writers",
    )


async def x4(settings: Settings) -> Result:
    result = Result(
        "X4",
        "Cancellation",
        "Does cancelling tasks mid-operation and mid-transaction leak sessions, transactions or tracked state?",
    )
    count = settings.scale(200, 30)
    ns = MODELS["msgspec"]
    async with environment(settings) as registry:
        manager = registry.manager(ns.Plain)
        hot = await manager.insert(ns.Plain(n=0, name="hot"))
        gc.collect()
        tracked_before = tracking.tracked_count()

        async def in_transaction(i: int) -> None:
            async def body(tx: t.Any) -> None:
                await manager.insert(ns.Plain(n=i, name="tx"), session=tx)
                await asyncio.sleep(random.random() * 0.2)
                await manager.update(hot.id, session=tx, name=f"tx{i}")

            await registry.transaction(body)

        async def plain(i: int) -> None:
            docs = await manager.find().limit(50).all()
            await asyncio.sleep(random.random() * 0.2)
            await manager.insert(ns.Plain(n=i + len(docs), name="plain"))

        tasks = [asyncio.create_task(in_transaction(i) if i % 2 else plain(i)) for i in range(count)]
        await asyncio.sleep(0.05)
        cancelled = 0
        for task in tasks:
            if random.random() < 0.7 and not task.done():
                task.cancel()
                cancelled += 1
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        unexpected = [
            o
            for o in outcomes
            if isinstance(o, BaseException) and not isinstance(o, asyncio.CancelledError | MongoSpecsError)
        ]
        await asyncio.sleep(1)
        admin = registry.client.admin
        cursor = await admin.aggregate(
            [
                {"$currentOp": {"allUsers": True, "idleSessions": True}},
                {"$match": {"transaction": {"$exists": True}}},
            ]
        )
        open_transactions = len(await cursor.to_list())
        # locks are released: the hot document can be written immediately
        await asyncio.wait_for(manager.update(hot.id, name="after"), timeout=5)
        del tasks, outcomes
        gc.collect()
        result.add(check="tasks cancelled", value=cancelled)
        result.add(check="unexpected error types", value=len(unexpected))
        result.add(check="transactions still open on the server", value=open_transactions)
        result.add(
            check="tracked documents leaked", value=max(0, tracking.tracked_count() - tracked_before - 1)
        )
        ok = not unexpected and open_transactions == 0 and tracking.tracked_count() - tracked_before <= 1
    return _verdict(
        result,
        ok,
        f"{cancelled} of {count} tasks cancelled; {open_transactions} open transactions afterwards",
    )


async def x5(settings: Settings) -> Result:
    result = Result(
        "X5",
        "Result documents near 16MB",
        "What happens when a $lookup would build a document over the BSON limit?",
    )
    ns = MODELS["msgspec"]
    async with environment(settings) as registry:
        parents, children = registry.manager(ns.Parent), registry.manager(ns.Child)
        payload = "x" * (1024 * 1024)
        ids = []
        for i in range(18):  # 18 x 1MB children referenced by a single parent
            child = await children.insert(ns.Child(n=i, label=f"c{i}", payload=payload))
            ids.append(child.id)
        await parents.insert(ns.Parent(n=0, name="big", children=ids))
        try:
            await parents.find().include("children", strategy="lookup").all()
            lookup = "returned"
        except DocumentTooLarge:
            lookup = "DocumentTooLarge"
        except MongoSpecsError as exc:
            lookup = f"{type(exc).__name__}: {exc}"[:120]
        except Exception as exc:
            lookup = f"UNTYPED {type(exc).__name__}"
        loaded = await parents.find().include("children", strategy="batch").one()
        batch_loaded = sum(ref.loaded for ref in loaded.children)
        result.add(check="$lookup outcome", value=lookup)
        result.add(check="batch strategy loaded children", value=batch_loaded)
        ok = not lookup.startswith("UNTYPED") and lookup != "returned" and batch_loaded == 18
    return _verdict(
        result, ok, f"$lookup over the limit -> {lookup}; batch strategy loaded all {batch_loaded} children"
    )


async def x6(settings: Settings) -> Result:
    result = Result(
        "X6",
        "Change stream across a primary step-down",
        "Does a consumer see every event exactly once across a step-down?",
    )
    ns = MODELS["msgspec"]
    async with environment(settings) as registry:
        manager = registry.manager(ns.Plain)
        await manager.insert(ns.Plain(n=-1, name="create collection"))
        seen: list[int] = []
        stream = manager.watch([{"$match": {"operationType": "insert"}}], max_await_time_ms=500)
        await stream._open()

        async def consume(expected: int) -> None:
            async for event in stream:
                seen.append(event.document.n)
                if len(seen) == expected:
                    return

        consumer = asyncio.create_task(consume(20))
        for i in range(10):
            await manager.insert(ns.Plain(n=i, name="before"))
        stepped = "not attempted"
        if settings.topology == "rs":
            try:
                await registry.client.admin.command("replSetStepDown", 2, force=True)
                stepped = "stepped down"
            except Exception as exc:  # the command drops connections; that is the point
                stepped = f"stepped down ({type(exc).__name__})"
        inserted_after = 0
        deadline = asyncio.get_running_loop().time() + 90
        while inserted_after < 10 and asyncio.get_running_loop().time() < deadline:
            try:
                await manager.insert(ns.Plain(n=10 + inserted_after, name="after"))
                inserted_after += 1
            except MongoSpecsError:
                await asyncio.sleep(0.5)  # no primary yet
        try:
            await asyncio.wait_for(consumer, timeout=60)
        except TimeoutError:
            consumer.cancel()
        await stream.close()
        result.add(check="step-down", value=stepped)
        result.add(check="events seen", value=len(seen))
        result.add(check="duplicates", value=len(seen) - len(set(seen)))
        ok = seen == list(range(20))
    return _verdict(result, ok, f"{len(seen)} of 20 events seen in order across the step-down ({stepped})")


async def x7(settings: Settings) -> Result:
    import sys

    result = Result(
        "X7",
        "Threads with their own loops and registries",
        "Is any state shared across registries when each thread runs its own event loop?",
    )
    threads = settings.scale(8, 3)
    per_thread = settings.scale(200, 20)
    ns = MODELS["msgspec"]
    uri = uri_for(settings.topology)
    errors: list[str] = []
    counts: dict[int, int] = {}

    def worker(index: int) -> None:
        async def main() -> None:
            name = f"ms_x7_{index}_{random.randrange(10**9)}"
            async with Registry(uri, database=name) as registry:
                manager = registry.manager(ns.Managed)  # explicit manager: no global binding involved
                try:
                    docs = (
                        await manager.insert_many(
                            [ns.Managed(n=i, name=f"t{index}") for i in range(per_thread)]
                        )
                    ).inserted
                    for doc in docs[:20]:
                        doc.n += 1000
                        await manager.save(doc)
                    loaded = await manager.find().all()
                    if any(d.name != f"t{index}" for d in loaded):
                        errors.append(f"thread {index} saw another thread's documents")
                    counts[index] = len(loaded)
                finally:
                    await registry.client.drop_database(name)

        try:
            asyncio.run(main())
        except Exception as exc:
            errors.append(f"thread {index}: {type(exc).__name__}: {exc}")

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    free_threaded = not getattr(sys, "_is_gil_enabled", lambda: True)()
    result.add(check="free-threaded build", value=free_threaded)
    result.add(check="threads", value=threads)
    result.add(check="errors", value=len(errors))
    result.notes.extend(errors[:5])
    ok = not errors and all(counts.get(i) == per_thread for i in range(threads))
    return _verdict(
        result,
        ok,
        f"{threads} threads x {per_thread} documents, each with its own loop and registry; free-threaded={free_threaded}",
    )


SCENARIOS: dict[str, Scenario] = {"X1": x1, "X2": x2, "X3": x3, "X4": x4, "X5": x5, "X6": x6, "X7": x7}
