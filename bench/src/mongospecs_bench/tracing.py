"""End-to-end tracing check (plan node E3).

Runs a small mongospecs-web application with the Sentry SDK pointed at a local
rustrak server (``docker/compose.rustrak.yaml``), makes a few HTTP requests and
then asks rustrak what it received. The check passes when every request shows
up as one trace holding the HTTP transaction and the ``Model.op`` database spans
recorded by ``mongospecs.sentry_listener``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import typing as t
import uuid
from dataclasses import dataclass, field

import httpx

from mongospecs import CreatedAt, Policy, Ref, Registry, Revision, SpecConfig, sentry_listener
from mongospecs.msgspec import Spec
from mongospecs_web import Api, Resource

RUSTRAK_URL = os.environ.get("RUSTRAK_URL", "http://localhost:8787")
RUSTRAK_USER = os.environ.get("RUSTRAK_SUPERUSER", "admin@example.com:mongospecs-dev")
MONGO_URI = os.environ.get("MONGOSPECS_TEST_URI", "mongodb://localhost:27117/?replicaSet=rs0")


@dataclass
class TraceReport:
    project: str
    dsn: str
    requests: list[str] = field(default_factory=list)
    transactions: list[dict[str, t.Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def render(self) -> str:
        lines = [f"rustrak project {self.project!r}", f"dsn {self.dsn}", ""]
        for tx in self.transactions:
            lines.append(f"  {tx['name']}  trace={tx['trace_id'][:8]}  {tx['duration_ms']:.1f}ms")
            for span in tx["spans"]:
                lines.append(f"      {span['op']:<6} {span['description']:<22} {span['status']}")
        lines.append("")
        lines.extend(f"PROBLEM: {p}" for p in self.problems)
        lines.append("end-to-end tracing: " + ("OK" if self.ok else "FAILED"))
        return "\n".join(lines)


async def _rustrak_project(client: httpx.AsyncClient) -> tuple[int, str]:
    email, _, password = RUSTRAK_USER.partition(":")
    login = await client.post("/auth/login", json={"email": email, "password": password})
    login.raise_for_status()
    created = await client.post("/api/projects", json={"name": f"mongospecs-e2e-{uuid.uuid4().hex[:8]}"})
    created.raise_for_status()
    project = created.json()
    key = str(project["sentry_key"]).replace("-", "")
    host = RUSTRAK_URL.split("://", 1)[1]
    scheme = RUSTRAK_URL.split("://", 1)[0]
    return int(project["id"]), f"{scheme}://{key}@{host}/{project['id']}"


class Writer(Spec):
    __spec__ = SpecConfig(collection="writers")
    name: str = ""


class Story(Spec):
    __spec__ = SpecConfig(collection="stories")
    title: t.Annotated[str, Policy(filter=("eq",), sort=True)] = ""
    words: int = 0
    writer: Ref[Writer] | None = None
    created_at: t.Annotated[dt.datetime | None, CreatedAt] = None
    rev: t.Annotated[int, Revision] = 0


def _build_app(registry: t.Any) -> t.Any:
    registry.bind(Writer, Story)
    return Api(Resource(Writer), Resource(Story, include=["writer"]), prefix="/api").asgi()


async def run_trace_check(*, wait_seconds: float = 30.0) -> TraceReport:
    import sentry_sdk
    from sentry_sdk.integrations.asgi import SentryAsgiMiddleware

    async with httpx.AsyncClient(base_url=RUSTRAK_URL, timeout=10) as rustrak:
        project_id, dsn = await _rustrak_project(rustrak)
        report = TraceReport(f"project {project_id}", dsn)
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=1.0,
            default_integrations=False,
            auto_enabling_integrations=False,
            release="mongospecs-bench",
        )
        database = f"ms_trace_{uuid.uuid4().hex[:8]}"
        try:
            async with Registry(MONGO_URI, database=database, on_command=sentry_listener()) as registry:
                inner = _build_app(registry)
                app = SentryAsgiMiddleware(inner, transaction_style="url")
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://app"
                ) as client:
                    writer = (await client.post("/api/writers", json={"name": "Ann"})).json()["data"]
                    story = (
                        await client.post("/api/stories", json={"title": "Traced", "writer": writer["id"]})
                    ).json()["data"]
                    await client.get("/api/stories?include=writer&sort=title")
                    await client.patch(f"/api/stories/{story['id']}", json={"words": 120})
                    report.requests = [
                        "POST writers",
                        "POST stories",
                        "GET stories?include",
                        "PATCH stories/{id}",
                    ]
                await registry.client.drop_database(database)
            sentry_sdk.flush(timeout=10)
        finally:
            sentry_sdk.init(dsn=None, default_integrations=False, auto_enabling_integrations=False)

        # rustrak ingests in two phases; poll until the transactions are queryable.
        deadline = asyncio.get_running_loop().time() + wait_seconds
        items: list[dict[str, t.Any]] = []
        while asyncio.get_running_loop().time() < deadline:
            listed = await rustrak.get(f"/api/projects/{project_id}/transactions")
            listed.raise_for_status()
            body = listed.json()
            items = body.get("items", body if isinstance(body, list) else [])
            if len(items) >= len(report.requests):
                break
            await asyncio.sleep(0.5)
        for item in items:
            detail = await rustrak.get(f"/api/projects/{project_id}/transactions/{item['id']}")
            detail.raise_for_status()
            report.transactions.append(_summarise(detail.json()))

    report.transactions.sort(key=lambda tx: tx["start"])
    _verify(report)
    return report


def _summarise(detail: dict[str, t.Any]) -> dict[str, t.Any]:
    data = detail.get("data") or detail.get("event") or detail
    trace = (data.get("contexts") or {}).get("trace") or {}
    spans = data.get("spans") or detail.get("spans") or []
    return {
        "name": detail.get("transaction") or detail.get("name") or data.get("transaction") or "?",
        "trace_id": detail.get("trace_id") or trace.get("trace_id") or "",
        "duration_ms": float(detail.get("duration_ms") or detail.get("duration") or 0.0),
        "start": str(
            detail.get("start_timestamp") or data.get("start_timestamp") or detail.get("timestamp") or ""
        ),
        "spans": [
            {
                "op": span.get("op") or "",
                "description": span.get("description") or span.get("name") or "",
                "status": span.get("status") or "",
                "trace_id": span.get("trace_id") or "",
            }
            for span in spans
        ],
    }


EXPECTED: list[tuple[str, list[str]]] = [
    ("writers", ["Writer.insert"]),
    ("stories", ["Story.insert"]),
    ("stories", ["Story.find", "Writer.find"]),
    ("stories", ["Story.update"]),
]


def _verify(report: TraceReport) -> None:
    if len(report.transactions) != len(EXPECTED):
        report.problems.append(
            f"expected {len(EXPECTED)} transactions in rustrak, found {len(report.transactions)}"
        )
        return
    for tx, (path_part, wanted) in zip(report.transactions, EXPECTED, strict=True):
        if path_part not in tx["name"]:
            report.problems.append(f"transaction {tx['name']!r} does not look like a request to {path_part}")
        db = [s["description"] for s in tx["spans"] if s["op"] == "db"]
        if db != wanted:
            report.problems.append(f"{tx['name']}: database spans {db} != expected {wanted}")
        if any(s["trace_id"] and s["trace_id"] != tx["trace_id"] for s in tx["spans"]):
            report.problems.append(f"{tx['name']}: a span belongs to a different trace")
        if any(s["status"] not in ("ok", "") for s in tx["spans"] if s["op"] == "db"):
            report.problems.append(f"{tx['name']}: a database span did not finish ok")
    if len({tx["trace_id"] for tx in report.transactions}) != len(report.transactions):
        report.problems.append("requests did not get one trace each")


def main() -> int:
    report = asyncio.run(run_trace_check())
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
