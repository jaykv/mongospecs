"""Workload B9: CPU cost of the hot paths, measured through public entry points only.

Wall-clock time on a laptop is noisy; CPU time of this process and the number of
commands sent are not. Every row here is one of those two, so a change to the
library can be judged by running this workload before and after it.
"""

from __future__ import annotations

import datetime as dt
import time
import typing as t

from mongospecs import Q, Registry
from mongospecs_bench.harness import Result, Settings, environment
from mongospecs_bench.storefront import models as shop_models
from mongospecs_web import Api, Cursor, Delete, Get, List, Resource


async def cpu_us(fn: t.Callable[[], t.Awaitable[t.Any]], calls: int, repeats: int) -> float:
    """CPU microseconds per call: the best of ``repeats`` batches of ``calls``."""
    for _ in range(min(calls, 20)):
        await fn()
    best = float("inf")
    for _ in range(repeats):
        start = time.process_time()
        for _ in range(calls):
            await fn()
        best = min(best, (time.process_time() - start) / calls)
    return round(best * 1e6, 1)


class Commands:
    """Counts the commands a block sends."""

    def __init__(self, registry: Registry) -> None:
        self.names: list[str] = []
        self._registry = registry

    def __call__(self, event: t.Any) -> None:
        if event.phase == "started":
            self.names.append(event.command_name)


def _api(m: t.Any, routes: int = 0) -> Api:
    resources: list[Resource[t.Any]] = [
        Resource(m.Product, name="products", ops=[List, Get, Delete], page=Cursor(default=20, max=100)),
        Resource(m.Review, name="reviews", ops=[List, Get], include=["product", "author"]),
    ]
    # extra mount points of the same model, to measure routing as an API grows
    resources += [Resource(m.Review, name=f"r{i}", ops=[List, Get]) for i in range(routes)]
    return Api(*resources, prefix="/api")


async def b9(settings: Settings) -> Result:
    result = Result(
        "B9",
        "Hot paths: CPU per call and commands per operation",
        "Where does the library spend CPU on the paths every request takes, and how many commands "
        "does each operation send?",
    )
    calls = settings.scale(300, 20)
    repeats = settings.scale(5, 2)
    products = settings.scale(400, 120)
    for backend in settings.backends:
        m = shop_models.build(backend)
        events = Commands.__new__(Commands)
        events.names = []
        async with environment(settings, m.ALL, on_command=events) as registry:
            await registry.apply_indexes()
            now = dt.datetime.now(dt.UTC)
            await m.Product.insert_many(
                [
                    m.Product(
                        sku=f"S{i:05d}",
                        name=f"Product {i}",
                        category="tools",
                        price=float(i),
                        stock=i,
                        tags=["a", "b", "c"],
                        description="x" * 200,
                        created_at=now,
                    )
                    for i in range(products)
                ]
            )
            users = (
                await m.User.insert_many([m.User(email=f"u{i}@x.io", name=f"u{i}") for i in range(20)])
            ).inserted
            items = await m.Product.find().sort("sku").all()
            await m.Review.insert_many(
                [
                    m.Review(product=items[i % 20], rating=1 + i % 5, body="fine", author=users[i % 20])
                    for i in range(2000)
                ]
            )
            first = items[0].id
            api = _api(m)

            async def listing() -> None:
                response = await api.handle("GET", "/api/products", query=[("limit", "100")])
                assert response.status == 200, response.body

            async def detail() -> None:
                assert (await api.handle("GET", f"/api/products/{first}")).status == 200

            async def model_get() -> None:
                assert await m.Product.get(first) is not None

            async def tracked_page() -> None:
                assert len(await m.Product.find().limit(100).all()) == 100

            async def lean_page() -> None:
                assert len(await m.Product.find().lean().limit(100).all()) == 100

            async def two_includes() -> None:
                assert len(await m.Review.find().limit(100).include("product").include("author").all()) == 100

            async def per_parent() -> None:
                query = m.Product.find(Q.sku < "S00020").include("reviews", per_parent_limit=2)
                assert len(await query.all()) == 20

            def add(path: str, unit: str, value: float, per: int = 1) -> None:
                result.add(backend=backend, path=path, unit=unit, value=round(value / per, 2))

            add("web: GET list of 100", "cpu us/doc", await cpu_us(listing, calls, repeats), 100)
            add("web: GET one", "cpu us/call", await cpu_us(detail, calls, repeats))
            add("Model.get(id)", "cpu us/call", await cpu_us(model_get, calls, repeats))
            add("find().all(), 100 tracked", "cpu us/doc", await cpu_us(tracked_page, calls, repeats), 100)
            add("find().lean().all(), 100", "cpu us/doc", await cpu_us(lean_page, calls, repeats), 100)
            add("include x2, 100 parents", "cpu us/call", await cpu_us(two_includes, calls // 4, repeats))
            add(
                "include reviews, per_parent_limit=2 of 100",
                "cpu us/call",
                await cpu_us(per_parent, calls // 4, repeats),
            )

            def count(name: str, *paths: str) -> None:
                add(name, "commands", float(len(events.names)))
                result.rows[-1]["detail"] = " ".join(events.names)

            for name, run in (
                ("web: GET one", detail),
                ("web: GET list of 100", listing),
                ("include x2, 100 parents", two_includes),
                ("include reviews, per_parent_limit=2 of 100", per_parent),
            ):
                events.names.clear()
                await run()
                count(name)
            victim = items[-1].id
            events.names.clear()
            assert (await api.handle("DELETE", f"/api/products/{victim}")).status == 204
            count("web: DELETE (soft)")

        if backend == settings.backends[0]:
            for routes in (25, 250, 1000):
                big = _api(m, routes // 2)
                path = f"/api/r{routes // 2 - 1}/000000000000000000000000"
                for _ in range(200):
                    big.match("GET", path)
                start = time.process_time()
                for _ in range(2000):
                    big.match("GET", path)
                result.add(
                    backend="-",
                    path=f"route match, last of {len(big.routes)} routes",
                    unit="cpu us/call",
                    value=round((time.process_time() - start) / 2000 * 1e6, 2),
                )
    result.notes.append(
        "CPU time of this process (best of several batches), so the database's own work and network waits "
        "are excluded. Rows with unit 'commands' are driver commands sent, listed in 'detail'."
    )
    return result
