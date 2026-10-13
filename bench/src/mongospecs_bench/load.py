"""HTTP load benchmark against the storefront running as a real server process.

Virtual users mix browsing, product detail with reviews, and placing orders
over real TCP connections. The same mix is run against a hand-written
pymongo + Starlette implementation of the same endpoints, so the result shows
what mongospecs and mongospecs-web cost in a whole request, not in isolation.

After every run the stock invariant is checked against the database: a
benchmark that loses or oversells units is a failed run, whatever its speed.
"""

from __future__ import annotations

import asyncio
import random
import statistics
import time
import typing as t
import uuid

import httpx

from mongospecs import Registry
from mongospecs_bench.harness import Result, Settings, uri_for
from mongospecs_bench.storefront import models as shop_models
from mongospecs_bench.storefront.auth import Tokens
from mongospecs_bench.storefront.seed import CATEGORIES, Seeded, seed, stock_invariant
from mongospecs_bench.storefront.server import running_server

SECRET = "load-secret"
MIX = (("list", 60), ("detail", 30), ("order", 10))


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


async def _virtual_user(
    index: int,
    client: httpx.AsyncClient,
    seeded: Seeded,
    stop: asyncio.Event,
    samples: dict[str, list[float]],
    failures: dict[str, int],
    rng: random.Random,
) -> None:
    token = {"authorization": f"Bearer {seeded.user_tokens[index % len(seeded.user_tokens)]}"}
    actions = [name for name, weight in MIX for _ in range(weight)]
    cursor: str | None = None
    category = rng.choice(CATEGORIES)
    while not stop.is_set():
        action = rng.choice(actions)
        started = time.perf_counter()
        try:
            if action == "list":
                url = f"/api/products?category={category}&sort=price&limit=20"
                if cursor and rng.random() < 0.5:
                    url += f"&after={cursor}"
                else:
                    category = rng.choice(CATEGORIES)
                    url = f"/api/products?category={category}&sort=price&limit=20"
                response = await client.get(url)
                cursor = response.json()["page"]["next_cursor"] if response.status_code == 200 else None
            elif action == "detail":
                response = await client.get(
                    f"/api/products/{rng.choice(seeded.product_ids)}?include=reviews,review_count"
                )
            else:
                lines = [
                    {"sku": sku, "qty": rng.randint(1, 2)}
                    for sku in rng.sample(seeded.skus, k=rng.randint(1, 3))
                ]
                response = await client.post("/api/orders", json={"lines": lines}, headers=token)
            ok = response.status_code in (200, 201)
        except httpx.HTTPError:
            ok = False
        elapsed = time.perf_counter() - started
        if ok:
            samples[action].append(elapsed)
        else:
            failures[action] += 1


async def run_load(
    settings: Settings,
    *,
    app: str = "storefront",
    plug: str = "asgi",
    backend: str = "msgspec",
    users: int = 32,
    seconds: float = 15.0,
    products: int = 5000,
) -> list[dict[str, t.Any]]:
    """One load run. Returns rows per action plus an ``all`` row; raises if the stock invariant fails."""
    database = f"ms_load_{uuid.uuid4().hex[:10]}"
    uri = uri_for(settings.topology)
    m = shop_models.build(backend)
    label = "raw pymongo + starlette" if app == "raw" else f"mongospecs ({backend}, {plug})"
    async with Registry(uri, database=database) as registry:
        try:
            seeded = await seed(registry, m, Tokens(SECRET), products=products, users=max(users, 8))
            for model in m.ALL:  # both apps get the same indexes
                registry.bind(model)
            await registry.apply_indexes()
            async with running_server(
                app=app,
                database=database,
                mongo_uri=uri,
                secret=SECRET,
                plug=plug,
                backend=backend,
                projector=False,
            ) as server:
                limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
                async with httpx.AsyncClient(base_url=server.base_url, timeout=30, limits=limits) as client:
                    for warm in (2.0, seconds):  # a short warm-up, then the measured run
                        samples: dict[str, list[float]] = {name: [] for name, _ in MIX}
                        failures: dict[str, int] = {name: 0 for name, _ in MIX}
                        stop = asyncio.Event()
                        tasks = [
                            asyncio.create_task(
                                _virtual_user(
                                    i, client, seeded, stop, samples, failures, random.Random(1000 + i)
                                )
                            )
                            for i in range(users)
                        ]
                        started = time.perf_counter()
                        await asyncio.sleep(warm)
                        stop.set()
                        await asyncio.gather(*tasks)
                        elapsed = time.perf_counter() - started
            invariant = await stock_invariant(registry, m, seeded)
        finally:
            await registry.client.drop_database(database)
    if not invariant["ok"]:
        raise AssertionError(f"{label}: stock invariant violated under load: {invariant}")
    rows: list[dict[str, t.Any]] = []
    everything: list[float] = []
    for action, _ in MIX:
        values = samples[action]
        everything.extend(values)
        if not values:
            continue
        rows.append(
            {
                "app": label,
                "action": action,
                "requests": len(values),
                "rps": round(len(values) / elapsed, 1),
                "p50_ms": round(statistics.median(values) * 1000, 2),
                "p95_ms": round(_percentile(values, 0.95) * 1000, 2),
                "p99_ms": round(_percentile(values, 0.99) * 1000, 2),
                "failed": failures[action],
            }
        )
    rows.append(
        {
            "app": label,
            "action": "all",
            "requests": len(everything),
            "rps": round(len(everything) / elapsed, 1),
            "p50_ms": round(statistics.median(everything) * 1000, 2),
            "p95_ms": round(_percentile(everything, 0.95) * 1000, 2),
            "p99_ms": round(_percentile(everything, 0.99) * 1000, 2),
            "failed": sum(failures.values()),
        }
    )
    rows[-1]["orders_placed"] = invariant["orders_placed"]
    return rows


async def load(settings: Settings) -> Result:
    """Workload ``LOAD``: the raw baseline, then mongospecs on every plug and model backend."""
    result = Result(
        "LOAD",
        "Storefront under HTTP load",
        "What do mongospecs and mongospecs-web cost per request in a real server, against the same "
        "endpoints written by hand with pymongo?",
    )
    users = settings.scale(32, 6)
    seconds = float(settings.scale(15, 3))
    products = settings.scale(5000, 200)
    runs: list[tuple[str, str, str]] = [("raw", "asgi", "msgspec")]
    runs += [
        ("storefront", plug, "msgspec") for plug in ("asgi", "starlette", "fastapi", "litestar", "quart")
    ]
    runs += [("storefront", "asgi", backend) for backend in settings.backends if backend != "msgspec"]
    if settings.quick:
        runs = runs[:2]
    baseline: dict[str, float] = {}
    for app, plug, backend in runs:
        rows = await run_load(
            settings, app=app, plug=plug, backend=backend, users=users, seconds=seconds, products=products
        )
        for row in rows:
            if app == "raw":
                baseline[row["action"]] = row["rps"]
            row["rps_vs_raw"] = (
                round(row["rps"] / baseline[row["action"]], 2) if baseline.get(row["action"]) else ""
            )
            result.add(**row)
    result.notes.append(
        f"{users} virtual users for {seconds:.0f}s after a 2s warm-up; mix {dict(MIX)}; {products} products, "
        "3 reviews each; one uvicorn worker; load generator and server on the same machine."
    )
    result.notes.append(
        "The stock invariant (stock + units sold = initial stock, never negative) held after every run."
    )
    result.notes.append(
        "The raw baseline implements the same three endpoints by hand, including the order transaction."
    )
    return result
