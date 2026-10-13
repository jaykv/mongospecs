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
import contextlib
import json
import os
import pathlib
import random
import statistics
import subprocess
import time
import typing as t
import uuid

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


class Connection:
    """A minimal HTTP/1.1 keep-alive client connection.

    The load generator must not be the bottleneck. A general-purpose client
    spends more CPU per request than the server under test does; this one writes
    a request and reads a length-delimited response, nothing else.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()

    async def request(
        self, method: str, path: str, *, token: str | None = None, body: bytes = b""
    ) -> tuple[int, bytes]:
        assert self.reader is not None and self.writer is not None
        head = [f"{method} {path} HTTP/1.1", f"host: {self.host}:{self.port}"]
        if token:
            head.append(f"authorization: Bearer {token}")
        if body:
            head.append("content-type: application/json")
            head.append(f"content-length: {len(body)}")
        self.writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + body)
        await self.writer.drain()
        raw = await self.reader.readuntil(b"\r\n\r\n")
        lines = raw.split(b"\r\n")
        status = int(lines[0].split(b" ", 2)[1])
        length, chunked = 0, False
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            lowered = name.lower()
            if lowered == b"content-length":
                length = int(value)
            elif lowered == b"transfer-encoding" and b"chunked" in value.lower():
                chunked = True
        if chunked:
            parts = []
            while True:
                size = int((await self.reader.readline()).strip() or b"0", 16)
                if size == 0:
                    await self.reader.readline()
                    break
                parts.append(await self.reader.readexactly(size))
                await self.reader.readline()
            return status, b"".join(parts)
        return status, await self.reader.readexactly(length) if length else b""


def process_cpu_seconds(pid: int) -> float:
    """CPU time (user + system) a process has used so far."""
    stat = pathlib.Path(f"/proc/{pid}/stat")
    if stat.exists():
        fields = stat.read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    out = subprocess.run(
        ["ps", "-o", "cputime=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    total = 0.0
    for part in out.replace("-", ":").split(":"):
        total = total * 60 + float(part)
    return total


async def _virtual_user(
    index: int,
    host: str,
    port: int,
    seeded: Seeded,
    stop: asyncio.Event,
    samples: dict[str, list[float]],
    failures: dict[str, int],
    rng: random.Random,
) -> None:
    token = seeded.user_tokens[index % len(seeded.user_tokens)]
    actions = [name for name, weight in MIX for _ in range(weight)]
    cursor: str | None = None
    category = rng.choice(CATEGORIES)
    connection = Connection(host, port)
    await connection.open()
    try:
        while not stop.is_set():
            action = rng.choice(actions)
            started = time.perf_counter()
            try:
                if action == "list":
                    if cursor and rng.random() < 0.5:
                        path = f"/api/products?category={category}&sort=price&limit=20&after={cursor}"
                    else:
                        category = rng.choice(CATEGORIES)
                        path = f"/api/products?category={category}&sort=price&limit=20"
                    status, payload = await connection.request("GET", path)
                    cursor = json.loads(payload)["page"]["next_cursor"] if status == 200 else None
                elif action == "detail":
                    product = rng.choice(seeded.product_ids)
                    status, payload = await connection.request(
                        "GET", f"/api/products/{product}?include=reviews,review_count"
                    )
                else:
                    lines = [
                        {"sku": sku, "qty": rng.randint(1, 2)}
                        for sku in rng.sample(seeded.skus, k=rng.randint(1, 3))
                    ]
                    status, payload = await connection.request(
                        "POST", "/api/orders", token=token, body=json.dumps({"lines": lines}).encode()
                    )
                ok = status in (200, 201)
            except (OSError, asyncio.IncompleteReadError, ValueError):
                ok = False
                await connection.close()
                await connection.open()
            elapsed = time.perf_counter() - started
            if ok:
                samples[action].append(elapsed)
            else:
                failures[action] += 1
    finally:
        await connection.close()


async def _phase(
    server: t.Any, seeded: Seeded, users: int, seconds: float
) -> tuple[dict[str, list[float]], dict[str, int], float, float]:
    """Run ``users`` virtual users for ``seconds``. Returns samples, failures, elapsed and server CPU seconds."""
    host, port = server.base_url.split("://", 1)[1].split(":")
    samples: dict[str, list[float]] = {name: [] for name, _ in MIX}
    failures: dict[str, int] = {name: 0 for name, _ in MIX}
    stop = asyncio.Event()
    cpu_before = process_cpu_seconds(server.process.pid)
    started = time.perf_counter()
    tasks = [
        asyncio.create_task(
            _virtual_user(i, host, int(port), seeded, stop, samples, failures, random.Random(1000 + i))
        )
        for i in range(users)
    ]
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - started
    return samples, failures, elapsed, process_cpu_seconds(server.process.pid) - cpu_before


async def run_load(
    settings: Settings,
    *,
    app: str = "storefront",
    plug: str = "asgi",
    backend: str = "msgspec",
    users: int = 64,
    seconds: float = 15.0,
    products: int = 5000,
) -> list[dict[str, t.Any]]:
    """One load run: a light phase for latency, then a saturating phase for capacity.

    Raises if the stock invariant does not hold afterwards.
    """
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
                await _phase(server, seeded, 4, 2.0)  # warm-up
                light = await _phase(server, seeded, 2, seconds / 3)
                heavy = await _phase(server, seeded, users, seconds)
            invariant = await stock_invariant(registry, m, seeded)
        finally:
            await registry.client.drop_database(database)
    if not invariant["ok"]:
        raise AssertionError(f"{label}: stock invariant violated under load: {invariant}")

    samples, failures, elapsed, cpu = heavy
    total = sum(len(v) for v in samples.values())
    row: dict[str, t.Any] = {
        "app": label,
        "server_cpu_ms_per_request": round(cpu / total * 1000, 3) if total else 0.0,
        "rps_at_saturation": round(total / elapsed, 1),
        "server_cpu_utilisation": round(cpu / elapsed, 2),
    }
    quiet = light[0]
    for action, _ in MIX:
        values = quiet[action]
        row[f"{action}_p50_ms"] = round(statistics.median(values) * 1000, 2) if values else ""
        row[f"{action}_p95_ms"] = round(_percentile(values, 0.95) * 1000, 2) if values else ""
    busy = [v for values in samples.values() for v in values]
    row["saturated_p99_ms"] = round(_percentile(busy, 0.99) * 1000, 1) if busy else ""
    row["failed"] = sum(failures.values()) + sum(light[1].values())
    row["orders_placed"] = invariant["orders_placed"]
    return [row]


async def load(settings: Settings) -> Result:
    """Workload ``LOAD``: the raw baseline, then mongospecs on every plug and model backend."""
    result = Result(
        "LOAD",
        "Storefront under HTTP load",
        "What do mongospecs and mongospecs-web cost per request in a real server, against the same "
        "endpoints written by hand with pymongo?",
    )
    users = settings.scale(64, 8)
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
        for row in await run_load(
            settings, app=app, plug=plug, backend=backend, users=users, seconds=seconds, products=products
        ):
            if app == "raw":
                baseline = {"cpu": row["server_cpu_ms_per_request"], "rps": row["rps_at_saturation"]}
            row["cpu_vs_raw"] = (
                round(row["server_cpu_ms_per_request"] / baseline["cpu"], 2) if baseline.get("cpu") else ""
            )
            row["rps_vs_raw"] = (
                round(row["rps_at_saturation"] / baseline["rps"], 2) if baseline.get("rps") else ""
            )
            result.add(**row)
    result.notes.append(
        f"Mix {dict(MIX)}; {products} products with 3 reviews each; one uvicorn worker. Latency columns: 2 virtual "
        f"users for {seconds / 3:.0f}s (an unloaded server). Capacity columns: {users} virtual users for {seconds:.0f}s."
    )
    result.notes.append(
        "server_cpu_ms_per_request is CPU time of the server process divided by requests served: it does not "
        "depend on the load generator, which runs on the same machine."
    )
    result.notes.append(
        "The stock invariant (stock + units sold = initial stock, never negative) held after every run."
    )
    result.notes.append(
        "The raw baseline implements the same three endpoints by hand, including the order transaction."
    )
    return result
