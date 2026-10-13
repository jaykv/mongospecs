"""Benchmark workloads B1-B8 (plan section 6). Each returns a :class:`Result`.

Everything that touches the network runs against real MongoDB. Numbers from a
``--quick`` run only prove the harness works; they are not for conclusions.
"""

from __future__ import annotations

import gc
import tracemalloc
import typing as t

import bson
import httpx

from mongospecs import Inc, Plugin, Q, meta_of, relations, tracking
from mongospecs.query import _encode_cursor
from mongospecs_bench.harness import Result, Settings, environment, measure, measure_sync
from mongospecs_bench.models import MODELS, SHAPES, model_for, raw_document
from mongospecs_web import Api, Resource

Workload = t.Callable[[Settings], t.Awaitable[Result]]


def _ratio(value: float, base: float) -> float:
    return round(value / base, 2) if base else 0.0


# -- B1 -------------------------------------------------------------------------------


async def b1(settings: Settings) -> Result:
    result = Result(
        "B1",
        "Materialise documents",
        "What does turning a raw document into a model cost, per backend, and how does it compare "
        "with the driver's own BSON decoding?",
    )
    n = settings.scale(10_000, 300)
    for shape in SHAPES:
        docs = [{"_id": bson.ObjectId(), **raw_document(shape, i)} for i in range(n)]
        encoded = [bson.encode(d) for d in docs]
        options: t.Any = bson.codec_options.CodecOptions(tz_aware=True)
        base = measure_sync(lambda: [bson.decode(b, options) for b in encoded], repeat=settings.repeat)
        result.add(
            shape=shape,
            backend="driver",
            mode="bson->dict",
            n=n,
            us_per_doc=round(base.median_ms * 1000 / n, 2),
            vs_bson=1.0,
        )
        for backend in settings.backends:
            meta = meta_of(model_for(backend, shape))
            modes: dict[str, t.Callable[[], t.Any]] = {
                "validated": lambda: [meta.decode(d, trusted=False) for d in docs],
                "trusted": lambda: [meta.decode(d, trusted=True) for d in docs],
                "tracked": lambda: [meta.encode(meta.decode(d, trusted=False)) for d in docs],
            }
            for mode, fn in modes.items():
                timing = measure_sync(fn, repeat=settings.repeat)
                result.add(
                    shape=shape,
                    backend=backend,
                    mode=mode,
                    n=n,
                    us_per_doc=round(timing.median_ms * 1000 / n, 2),
                    vs_bson=_ratio(timing.median_ms, base.median_ms),
                )
    result.notes.append("'tracked' is decode plus the encode that builds the change-tracking snapshot.")
    result.notes.append("'trusted' only differs for flat pydantic models; msgspec and attrs always convert.")
    return result


# -- B2 -------------------------------------------------------------------------------


async def b2(settings: Settings) -> Result:
    result = Result(
        "B2",
        "find() end to end",
        "How much does the ODM add on top of the raw driver for reads of 1, 100 and 10,000 documents?",
    )
    total = settings.scale(10_000, 300)
    sizes = [n for n in (1, 100, 10_000) if n <= total] or [total]
    async with environment(settings) as registry:
        for shape in ("small", "nested"):
            for backend in settings.backends:
                model = model_for(backend, shape)
                manager: t.Any = registry.manager(model)
                if backend == settings.backends[0]:
                    await manager.collection.insert_many([raw_document(shape, i) for i in range(total)])
                for n in sizes:
                    collection = manager.collection
                    raw = await measure(
                        lambda: collection.find({}, limit=n).to_list(), repeat=settings.repeat
                    )
                    cases = {
                        "odm raw()": lambda: manager.find().limit(n).raw().all(),
                        "odm lean()": lambda: manager.find().limit(n).lean().all(),
                        "odm tracked": lambda: manager.find().limit(n).all(),
                    }
                    if backend == settings.backends[0]:
                        result.add(
                            shape=shape,
                            backend="pymongo",
                            case="driver dicts",
                            n=n,
                            median_ms=round(raw.median_ms, 3),
                            vs_driver=1.0,
                        )
                    for case, fn in cases.items():
                        timing = await measure(fn, repeat=settings.repeat)
                        result.add(
                            shape=shape,
                            backend=backend,
                            case=case,
                            n=n,
                            median_ms=round(timing.median_ms, 3),
                            vs_driver=_ratio(timing.median_ms, raw.median_ms),
                        )
            await registry.manager(model_for(settings.backends[0], shape)).collection.drop()
    return result


# -- B3 -------------------------------------------------------------------------------


async def b3(settings: Settings) -> Result:
    result = Result(
        "B3",
        "include(): $lookup vs batched $in",
        "Which strategy should include(strategy='auto') use, across fan-outs and topologies?",
    )
    plans = [(1, 400), (10, 200), (100, 50), (1000, 10)]
    if settings.quick:
        plans = [(1, 20), (10, 10)]
    ns = MODELS["msgspec"]
    async with environment(settings) as registry:
        parents, children = registry.manager(ns.Parent), registry.manager(ns.Child)
        if settings.topology == "sharded":
            db = parents.collection.database.name
            await registry.client.admin.command(
                "shardCollection", f"{db}.bench_children", key={"_id": "hashed"}
            )
        for fan_out, parent_count in plans:
            await parents.collection.delete_many({})
            await children.collection.delete_many({})
            child_docs = [
                {"_id": bson.ObjectId(), "n": i, "label": f"c{i}", "payload": "x" * 200}
                for i in range(fan_out * parent_count)
            ]
            await children.collection.insert_many(child_docs)
            await parents.collection.insert_many(
                [
                    {
                        "n": p,
                        "name": f"p{p}",
                        "children": [c["_id"] for c in child_docs[p * fan_out : (p + 1) * fan_out]],
                    }
                    for p in range(parent_count)
                ]
            )
            timings = {}
            strategy: t.Any
            for strategy in ("batch", "lookup"):
                timings[strategy] = await measure(
                    lambda: parents.find().lean().include("children", strategy=strategy).all(),
                    repeat=settings.repeat,
                )
            result.add(
                relation="list, distinct children",
                fan_out=fan_out,
                parents=parent_count,
                batch_ms=round(timings["batch"].median_ms, 3),
                lookup_ms=round(timings["lookup"].median_ms, 3),
                lookup_vs_batch=_ratio(timings["lookup"].median_ms, timings["batch"].median_ms),
            )
        # To-one with heavy sharing: many parents, few targets. $lookup repeats each target per parent.
        await parents.collection.delete_many({})
        await children.collection.delete_many({})
        owners = [
            {"_id": bson.ObjectId(), "n": i, "label": f"o{i}", "payload": "x" * 2000} for i in range(20)
        ]
        await children.collection.insert_many(owners)
        count = settings.scale(2000, 100)
        await parents.collection.insert_many(
            [{"n": p, "name": f"p{p}", "owner": owners[p % 20]["_id"]} for p in range(count)]
        )
        timings = {}
        for strategy in ("batch", "lookup"):
            timings[strategy] = await measure(
                lambda: parents.find().lean().include("owner", strategy=strategy).all(),
                repeat=settings.repeat,
            )
        result.add(
            relation="to-one, 20 shared targets",
            fan_out=1,
            parents=count,
            batch_ms=round(timings["batch"].median_ms, 3),
            lookup_ms=round(timings["lookup"].median_ms, 3),
            lookup_vs_batch=_ratio(timings["lookup"].median_ms, timings["batch"].median_ms),
        )
    ratios = [row["lookup_vs_batch"] for row in result.rows]
    wins = sum(1 for r in ratios if r < 0.95)
    result.conclusion = (
        f"$lookup was faster than batched $in in {wins} of {len(ratios)} cases on {settings.topology} "
        f"(lookup/batch ratios: {ratios}). Current default: {relations.DEFAULT_STRATEGY}."
    )
    result.notes.append(
        "Strategy choice does not depend on the model backend; measured with msgspec, lean reads."
    )
    return result


# -- B4 -------------------------------------------------------------------------------


async def b4(settings: Settings) -> Result:
    result = Result("B4", "Writes", "What does the ODM add to inserts, atomic patches, saves and upserts?")
    n = settings.scale(1000, 50)
    ops = settings.scale(200, 20)
    async with environment(settings) as registry:
        for backend in settings.backends:
            ns = MODELS[backend]
            plain, managed = registry.manager(ns.Plain), registry.manager(ns.Managed)
            collection = plain.collection

            async def reset() -> None:
                await collection.delete_many({})
                await managed.collection.delete_many({})

            raw_insert = await measure(
                lambda: collection.insert_many([{"n": i, "name": f"n{i}"} for i in range(n)]),
                repeat=settings.repeat,
                setup=reset,
            )
            cases: dict[str, t.Callable[[], t.Awaitable[t.Any]]] = {
                "insert_many (no plugins)": lambda: plain.insert_many(
                    [ns.Plain(n=i, name=f"n{i}") for i in range(n)]
                ),
                "insert_many (timestamps+revision)": lambda: managed.insert_many(
                    [ns.Managed(n=i, name=f"n{i}") for i in range(n)]
                ),
            }
            result.add(
                backend=backend,
                op=f"insert {n}",
                case="driver insert_many",
                median_ms=round(raw_insert.median_ms, 3),
                vs_driver=1.0,
            )
            for case, fn in cases.items():
                timing = await measure(fn, repeat=settings.repeat, setup=reset)
                result.add(
                    backend=backend,
                    op=f"insert {n}",
                    case=case,
                    median_ms=round(timing.median_ms, 3),
                    vs_driver=_ratio(timing.median_ms, raw_insert.median_ms),
                )

            await reset()
            docs = (await plain.insert_many([ns.Plain(n=i, name=f"n{i}") for i in range(ops)])).inserted
            ids = [d.id for d in docs]

            async def raw_patch() -> None:
                for _id in ids:
                    await collection.update_one({"_id": _id}, {"$inc": {"n": 1}})

            async def odm_patch() -> None:
                for _id in ids:
                    await plain.update(_id, Inc(n=1))

            async def odm_save() -> None:
                for doc in docs:
                    doc.n += 1
                    await plain.save(doc)

            async def raw_upsert() -> None:
                for i in range(ops):
                    await collection.update_one({"name": f"u{i}"}, {"$inc": {"n": 1}}, upsert=True)

            async def odm_upsert() -> None:
                for i in range(ops):
                    await plain.upsert(Q.name == f"u{i}", Inc(n=1))

            base = await measure(raw_patch, repeat=settings.repeat)
            result.add(
                backend=backend,
                op=f"patch x{ops}",
                case="driver update_one",
                median_ms=round(base.median_ms, 3),
                vs_driver=1.0,
            )
            for case, fn in (("Model.update (returns doc)", odm_patch), ("doc.save() delta", odm_save)):
                timing = await measure(fn, repeat=settings.repeat)
                result.add(
                    backend=backend,
                    op=f"patch x{ops}",
                    case=case,
                    median_ms=round(timing.median_ms, 3),
                    vs_driver=_ratio(timing.median_ms, base.median_ms),
                )
            base = await measure(raw_upsert, repeat=settings.repeat)
            timing = await measure(odm_upsert, repeat=settings.repeat)
            result.add(
                backend=backend,
                op=f"upsert x{ops}",
                case="driver update_one(upsert)",
                median_ms=round(base.median_ms, 3),
                vs_driver=1.0,
            )
            result.add(
                backend=backend,
                op=f"upsert x{ops}",
                case="Model.upsert (returns doc)",
                median_ms=round(timing.median_ms, 3),
                vs_driver=_ratio(timing.median_ms, base.median_ms),
            )
            await reset()
    result.notes.append(
        "Model.update and Model.upsert use findAndModify to return the document; the driver baseline does not."
    )
    return result


# -- B5 -------------------------------------------------------------------------------


async def b5(settings: Settings) -> Result:
    result = Result(
        "B5",
        "Pagination",
        "How do offset and cursor pagination compare at depth, and should a total come from $facet or a count?",
    )
    total = settings.scale(50_000, 2_000)
    size = 20
    ns = MODELS["msgspec"]
    async with environment(settings) as registry:
        manager = registry.manager(ns.Small)
        await manager.collection.insert_many([raw_document("small", i) for i in range(total)])
        await manager.collection.create_index([("n", 1)])
        for page in (1, 100, total // size // 2, total // size - 1):
            if page < 1 or page * size > total:
                continue
            skip = (page - 1) * size
            offset = await measure(
                lambda: manager.find().sort("n").lean().page(size, number=page), repeat=settings.repeat
            )
            if skip:
                last = await manager.collection.find_one({}, sort=[("n", 1)], skip=skip - 1)
                assert last is not None
                token: str | None = _encode_cursor([last["n"], last["_id"]])
            else:
                token = None
            cursor = await measure(
                lambda: manager.find().sort("n").lean().page(size, after=token), repeat=settings.repeat
            )
            result.add(
                page=page,
                skip=skip,
                offset_ms=round(offset.median_ms, 3),
                cursor_ms=round(cursor.median_ms, 3),
                offset_vs_cursor=_ratio(offset.median_ms, cursor.median_ms),
            )
        flt = {"n": {"$gte": total // 4}}
        facet_pipeline: list[dict[str, t.Any]] = [
            {"$match": flt},
            {"$facet": {"items": [{"$sort": {"n": 1}}, {"$limit": size}], "total": [{"$count": "n"}]}},
        ]

        async def facet() -> t.Any:
            cursor_ = await manager.collection.aggregate(facet_pipeline)
            return await cursor_.to_list()

        concurrent = await measure(
            lambda: manager.find(Q.n >= total // 4).sort("n").lean().page(size, number=1, total=True),
            repeat=settings.repeat,
        )
        faceted = await measure(facet, repeat=settings.repeat)
        result.add(
            page="total: concurrent count",
            skip=0,
            offset_ms=round(concurrent.median_ms, 3),
            cursor_ms="",
            offset_vs_cursor="",
        )
        result.add(
            page="total: $facet (raw driver)",
            skip=0,
            offset_ms=round(faceted.median_ms, 3),
            cursor_ms="",
            offset_vs_cursor="",
        )
        result.notes.append(f"Collection of {total} documents, page size {size}, sorted on an indexed field.")
        result.notes.append(
            "$facet returns the page and the total as one document, so the page is bound by the 16MB limit."
        )
    return result


# -- B6 -------------------------------------------------------------------------------


class _Noop(Plugin):
    def __init__(self, index: int) -> None:
        self.name = f"noop{index}"


async def b6(settings: Settings) -> Result:
    result = Result("B6", "Plugin overhead", "What does each active plugin cost per operation?")
    ops = settings.scale(300, 20)
    ns = MODELS["msgspec"]
    baseline: dict[str, float] = {}
    for count in (0, 3, 10):
        async with environment(settings, plugins=[_Noop(i) for i in range(count)]) as registry:
            manager = registry.manager(ns.Plain)

            async def inserts() -> None:
                for i in range(ops):
                    await manager.insert(ns.Plain(n=i))

            docs = (await manager.insert_many([ns.Plain(n=i) for i in range(ops)])).inserted

            async def saves() -> None:
                for doc in docs:
                    doc.n += 1
                    await manager.save(doc)

            async def finds() -> None:
                for doc in docs:
                    await manager.get(doc.id)

            for op, fn in (("insert", inserts), ("save", saves), ("get", finds)):
                timing = await measure(fn, repeat=settings.repeat)
                per_op = timing.median_ms * 1000 / ops
                baseline.setdefault(op, per_op)
                result.add(
                    plugins=f"{count} no-op",
                    op=op,
                    us_per_op=round(per_op, 1),
                    vs_no_plugins=_ratio(per_op, baseline[op]),
                )
    async with environment(settings) as registry:
        manager = registry.manager(ns.Managed)

        async def managed_inserts() -> None:
            for i in range(ops):
                await manager.insert(ns.Managed(n=i))

        timing = await measure(managed_inserts, repeat=settings.repeat)
        per_op = timing.median_ms * 1000 / ops
        result.add(
            plugins="timestamps+revision",
            op="insert",
            us_per_op=round(per_op, 1),
            vs_no_plugins=_ratio(per_op, baseline["insert"]),
        )
    result.notes.append(
        "Each operation is one round trip to MongoDB, which dominates; plugin calls are in-process."
    )
    return result


# -- B7 -------------------------------------------------------------------------------


def _apps(api: Api) -> dict[str, t.Any]:
    from fastapi import FastAPI
    from litestar import Litestar
    from quart import Quart
    from starlette.applications import Starlette

    from mongospecs_web.plugs import fastapi as fastapi_plug
    from mongospecs_web.plugs import quart as quart_plug
    from mongospecs_web.plugs import starlette as starlette_plug
    from mongospecs_web.plugs.litestar import MongospecsPlugin

    fast = FastAPI()
    fastapi_plug.mount(fast, api)
    quart = Quart(__name__)
    quart_plug.init_app(quart, api)
    return {
        "asgi": api.asgi(),
        "starlette": Starlette(routes=starlette_plug.routes(api)),
        "fastapi": fast,
        "litestar": Litestar(plugins=[MongospecsPlugin(api)], openapi_config=None),
        "quart": quart,
    }


async def b7(settings: Settings) -> Result:
    result = Result(
        "B7",
        "HTTP request to response",
        "What does each plug add over calling the API core directly, per model backend?",
    )
    calls = settings.scale(200, 10)
    async with environment(settings) as registry:
        for backend in settings.backends:
            ns = MODELS[backend]
            manager = registry.manager(ns.Small)
            await manager.collection.delete_many({})
            await manager.collection.insert_many([raw_document("small", i) for i in range(200)])
            first = await manager.collection.find_one({})
            assert first is not None
            from mongospecs_web.backend import MongoBackend

            api = Api(
                Resource(ns.Small, name="small", backend=MongoBackend(ns.Small, manager)), prefix="/api"
            )
            one, many = f"/api/small/{first['_id']}", "/api/small?limit=20"

            async def direct(path: str) -> None:
                for _ in range(calls):
                    await api.handle(
                        "GET", path.split("?")[0], query=[("limit", "20")] if "?" in path else []
                    )

            base = {
                "get one": await measure(lambda: direct(one), repeat=settings.repeat),
                "list 20": await measure(lambda: direct(many), repeat=settings.repeat),
            }
            for case, timing in base.items():
                result.add(
                    backend=backend,
                    plug="(core, no HTTP)",
                    case=case,
                    us_per_request=round(timing.median_ms * 1000 / calls, 1),
                    vs_core=1.0,
                )
            for plug, app in _apps(api).items():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://bench"
                ) as client:

                    async def hit(path: str) -> None:
                        for _ in range(calls):
                            response = await client.get(path)
                            assert response.status_code == 200, response.text

                    for case, path in (("get one", one), ("list 20", many)):
                        timing = await measure(lambda: hit(path), repeat=settings.repeat)
                        per = timing.median_ms * 1000 / calls
                        result.add(
                            backend=backend,
                            plug=plug,
                            case=case,
                            us_per_request=round(per, 1),
                            vs_core=_ratio(per, base[case].median_ms * 1000 / calls),
                        )
    result.notes.append(
        "Requests go through httpx's in-process ASGI transport, so client overhead is included equally for every plug."
    )
    return result


# -- B8 -------------------------------------------------------------------------------


async def b8(settings: Settings) -> Result:
    result = Result(
        "B8",
        "Cost of change tracking",
        "What do tracked reads cost in time and memory over lean and raw reads, and what does a delta save send?",
    )
    n = settings.scale(5000, 200)
    async with environment(settings) as registry:
        for backend in settings.backends:
            manager: t.Any = registry.manager(model_for(backend, "nested"))
            await manager.collection.delete_many({})
            await manager.collection.insert_many([raw_document("nested", i) for i in range(n)])
            timings = {
                "raw()": await measure(lambda: manager.find().raw().all(), repeat=settings.repeat),
                "lean()": await measure(lambda: manager.find().lean().all(), repeat=settings.repeat),
                "tracked": await measure(lambda: manager.find().all(), repeat=settings.repeat),
            }
            memory: dict[str, float] = {}
            for mode, query in (
                ("raw()", manager.find().raw()),
                ("lean()", manager.find().lean()),
                ("tracked", manager.find()),
            ):
                gc.collect()
                tracemalloc.start()
                before = tracemalloc.get_traced_memory()[0]
                held = await query.all()
                memory[mode] = (tracemalloc.get_traced_memory()[0] - before) / len(held)
                tracemalloc.stop()
                del held
            for mode, timing in timings.items():
                result.add(
                    backend=backend,
                    mode=mode,
                    n=n,
                    median_ms=round(timing.median_ms, 2),
                    vs_lean=_ratio(timing.median_ms, timings["lean()"].median_ms),
                    bytes_per_doc=round(memory[mode]),
                )
            doc = await manager.find().first()
            assert doc is not None
            full = bson.encode(
                {"$set": {k: v for k, v in meta_of(type(doc)).encode(doc).items() if k != "_id"}}
            )
            t.cast(t.Any, doc).customer = "changed"
            delta = bson.encode(t.cast(t.Any, doc).changes())
            result.add(
                backend=backend,
                mode="save payload: full $set",
                n=1,
                median_ms="",
                vs_lean="",
                bytes_per_doc=len(full),
            )
            result.add(
                backend=backend,
                mode="save payload: delta",
                n=1,
                median_ms="",
                vs_lean="",
                bytes_per_doc=len(delta),
            )
        gc.collect()
        result.notes.append(
            f"{tracking.tracked_count()} tracked documents remained after the run (should be ~0)."
        )
        result.notes.append(
            "bytes_per_doc for reads is Python heap per document held; for save payloads it is the BSON update size."
        )
    return result


WORKLOADS: dict[str, Workload] = {
    "B1": b1,
    "B2": b2,
    "B3": b3,
    "B4": b4,
    "B5": b5,
    "B6": b6,
    "B7": b7,
    "B8": b8,
}
