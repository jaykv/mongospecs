"""Timing, environment and result-file plumbing shared by workloads and stress scenarios."""

from __future__ import annotations

import asyncio
import datetime as dt
import gc
import json
import os
import pathlib
import platform
import statistics
import subprocess
import sys
import time
import typing as t
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from mongospecs import Registry

RS_URI = os.environ.get("MONGOSPECS_TEST_URI", "mongodb://localhost:27117/?replicaSet=rs0")
SHARDED_URI = os.environ.get("MONGOSPECS_TEST_SHARDED_URI", "mongodb://localhost:27118")
RESULTS = pathlib.Path(__file__).resolve().parents[2] / "results"


def uri_for(topology: str) -> str:
    return SHARDED_URI if topology == "sharded" else RS_URI


@dataclass
class Timing:
    samples: list[float]
    """Seconds per call."""

    @property
    def median_ms(self) -> float:
        return statistics.median(self.samples) * 1000

    @property
    def min_ms(self) -> float:
        return min(self.samples) * 1000

    @property
    def p95_ms(self) -> float:
        ordered = sorted(self.samples)
        return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))] * 1000

    def as_dict(self) -> dict[str, float]:
        return {
            "median_ms": round(self.median_ms, 4),
            "min_ms": round(self.min_ms, 4),
            "p95_ms": round(self.p95_ms, 4),
            "runs": len(self.samples),
        }


async def measure(
    fn: t.Callable[[], t.Awaitable[t.Any]],
    *,
    repeat: int = 15,
    warmup: int = 2,
    setup: t.Callable[[], t.Awaitable[t.Any]] | None = None,
) -> Timing:
    """Time an async callable. ``setup`` runs before each call and is not timed."""
    for _ in range(warmup):
        if setup is not None:
            await setup()
        await fn()
    samples: list[float] = []
    gc.collect()
    for _ in range(repeat):
        if setup is not None:
            await setup()
        start = time.perf_counter()
        await fn()
        samples.append(time.perf_counter() - start)
    return Timing(samples)


def measure_sync(fn: t.Callable[[], t.Any], *, repeat: int = 15, warmup: int = 2) -> Timing:
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    gc.collect()
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return Timing(samples)


@dataclass
class Settings:
    topology: str = "rs"
    quick: bool = False
    """Small sizes and few repetitions: for checking the harness, not for publishing numbers."""
    backends: tuple[str, ...] = ("msgspec", "pydantic", "attrs")

    @property
    def repeat(self) -> int:
        return 3 if self.quick else 15

    def scale(self, full: int, quick: int) -> int:
        return quick if self.quick else full


@dataclass
class Result:
    workload: str
    title: str
    question: str
    rows: list[dict[str, t.Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    conclusion: str = ""

    def add(self, **row: t.Any) -> None:
        self.rows.append(row)


@asynccontextmanager
async def environment(
    settings: Settings, models: t.Iterable[type] = (), **registry_options: t.Any
) -> t.AsyncIterator[Registry]:
    """An open registry on a fresh database of the chosen topology, dropped afterwards."""
    name = f"ms_bench_{uuid.uuid4().hex[:10]}"
    async with Registry(
        uri_for(settings.topology), database=name, models=models, **registry_options
    ) as registry:
        if settings.topology == "sharded":
            await registry.client.admin.command("enableSharding", name)
        try:
            yield registry
        finally:
            await registry.client.drop_database(name)


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, cwd=RESULTS.parent
        ).stdout.strip()
    except Exception:
        return "unknown"


async def metadata(settings: Settings) -> dict[str, t.Any]:
    import msgspec
    import pydantic
    import pymongo

    import mongospecs

    async with Registry(uri_for(settings.topology), database="admin") as registry:
        info = await registry.client.server_info()
    return {
        "git_sha": _git("rev-parse", "--short", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "timestamp": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "topology": settings.topology,
        "quick": settings.quick,
        "mongodb": info.get("version"),
        "python": sys.version.split()[0],
        "free_threaded": not getattr(sys, "_is_gil_enabled", lambda: True)(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "versions": {
            "mongospecs": mongospecs.__version__,
            "pymongo": pymongo.version,
            "msgspec": msgspec.__version__,
            "pydantic": pydantic.VERSION,
        },
    }


def write_result(result: Result, meta: dict[str, t.Any]) -> pathlib.Path:
    """Write one result file under ``bench/results/<git sha>/``."""
    folder = RESULTS / str(meta["git_sha"])
    folder.mkdir(parents=True, exist_ok=True)
    suffix = "" if meta["topology"] == "rs" else f"-{meta['topology']}"
    path = folder / f"{result.workload}{suffix}{'-quick' if meta['quick'] else ''}.json"
    payload = {
        "meta": meta,
        "workload": result.workload,
        "title": result.title,
        "question": result.question,
        "rows": result.rows,
        "notes": result.notes,
        "conclusion": result.conclusion,
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return path


def table(rows: list[dict[str, t.Any]]) -> str:
    """Render rows as a fixed-width text table."""
    if not rows:
        return "(no rows)"
    columns = list(dict.fromkeys(key for row in rows for key in row))
    cells = [[_cell(row.get(c, "")) for c in columns] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(columns)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    lines.extend("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)) for row in cells)
    return "\n".join(lines)


def _cell(value: t.Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 100 else f"{value:.1f}"
    return str(value)


def run(coro: t.Coroutine[t.Any, t.Any, t.Any]) -> t.Any:
    return asyncio.run(coro)
