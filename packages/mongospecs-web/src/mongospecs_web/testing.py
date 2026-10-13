"""Test support: an in-memory backend and the plug contract suite.

``FakeBackend`` implements :class:`~mongospecs_web.backend.ResourceBackend`
without a database. It evaluates only the bounded filter operators a Resource
can produce, so it stays a test double rather than a database emulator.

``run_contract`` drives a mounted API through an ``httpx.AsyncClient`` and
returns a transcript. Every plug must produce the same transcript for the same
API, which is how a third-party plug proves it behaves like the built-in ones::

    api = contract_api("msgspec")
    app = my_framework_app(api)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        transcript = await run_contract(client, api)
    assert transcript == await reference_transcript("msgspec")
"""

from __future__ import annotations

import re
import typing as t

from bson import ObjectId

from mongospecs import (
    DocumentNotFound,
    DuplicateKeyError,
    Page,
    Policy,
    Q,
    QueryError,
    Revision,
    SpecConfig,
    StaleDocumentError,
    meta_of,
)
from mongospecs.expr import compile_filter
from mongospecs_web.api import Api
from mongospecs_web.backend import ListQuery
from mongospecs_web.errors import Unauthorized
from mongospecs_web.filters import Offset
from mongospecs_web.policy import view_of
from mongospecs_web.resource import Create, Ctx, Delete, Get, List, Patch, Request, Resource

__all__ = ["FakeBackend", "contract_api", "reference_transcript", "run_contract"]

T = t.TypeVar("T")


def _matches(doc: t.Mapping[str, t.Any], flt: t.Mapping[str, t.Any]) -> bool:
    for key, cond in flt.items():
        if key == "$and":
            if not all(_matches(doc, c) for c in cond):
                return False
        elif key == "$or":
            if not any(_matches(doc, c) for c in cond):
                return False
        elif key == "$nor":
            if any(_matches(doc, c) for c in cond):
                return False
        elif not _field(doc.get(key), key in doc, cond):
            return False
    return True


def _eq(value: t.Any, expected: t.Any) -> bool:
    return value == expected or (isinstance(value, list) and expected in value)


def _field(value: t.Any, present: bool, cond: t.Any) -> bool:
    if not (isinstance(cond, dict) and any(str(k).startswith("$") for k in cond)):
        return _eq(value, cond)
    for op, arg in cond.items():
        if op == "$eq":
            ok = _eq(value, arg)
        elif op == "$ne":
            ok = not _eq(value, arg)
        elif op == "$in":
            ok = any(_eq(value, a) for a in arg)
        elif op == "$nin":
            ok = not any(_eq(value, a) for a in arg)
        elif op in ("$gt", "$gte", "$lt", "$lte"):
            if value is None:
                ok = False
            else:
                ok = {"$gt": value > arg, "$gte": value >= arg, "$lt": value < arg, "$lte": value <= arg}[op]
        elif op == "$exists":
            ok = present == bool(arg)
        elif op == "$regex":
            ok = isinstance(value, str) and re.search(arg, value) is not None
        elif op == "$options":
            ok = True
        else:
            raise QueryError(f"FakeBackend does not evaluate {op}")
        if not ok:
            return False
    return True


class FakeBackend(t.Generic[T]):
    """In-memory ``ResourceBackend``. Ids are sequential so transcripts are reproducible."""

    def __init__(self, model: type[T], *, unique: t.Sequence[str] = ()) -> None:
        self.model = model
        self.meta = meta_of(model)
        self.view = view_of(model)
        self.unique = tuple(unique)
        self.docs: dict[t.Any, T] = {}
        self._next = 1

    def _visible(self, doc: T, scope: t.Any) -> bool:
        return scope is None or _matches(self.meta.encode(doc), compile_filter(scope, self.meta))

    def _find(self, id: t.Any, scope: t.Any) -> T | None:
        try:
            key = self.meta.cast("id", id)
        except QueryError:
            return None
        doc = self.docs.get(key)
        return doc if doc is not None and self._visible(doc, scope) else None

    def _check_unique(self, doc: T, skip: t.Any = None) -> None:
        for name in self.unique:
            value = getattr(doc, name)
            if value is None:
                continue  # like a sparse unique index
            for other_id, other in self.docs.items():
                if other_id != skip and getattr(other, name) == value:
                    raise DuplicateKeyError(f"duplicate {name}", code=11000, fields={name: value})

    async def get(
        self, id: t.Any, *, scope: t.Any, include: t.Sequence[str] = (), read_only: bool = False
    ) -> T | None:
        return self._find(id, scope)

    async def list(self, query: ListQuery, *, scope: t.Any) -> Page[T]:
        conditions = [compile_filter(f.to_expr(), self.meta) for f in query.filters]
        rows = [(self.meta.encode(d), d) for d in self.docs.values() if self._visible(d, scope)]
        rows = [(enc, d) for enc, d in rows if all(_matches(enc, c) for c in conditions)]
        keys = [(self.meta.stored_path(name), direction) for name, direction in query.sort]
        if not any(k == "_id" for k, _ in keys):
            keys.append(("_id", 1))
        for key, direction in reversed(keys):
            rows.sort(key=lambda row: (row[0].get(key) is not None, row[0].get(key)), reverse=direction == -1)
        total = len(rows) if query.total else None
        if query.number is not None:
            start = (query.number - 1) * query.size
        elif query.after is not None:
            ids = [str(enc["_id"]) for enc, _ in rows]
            if query.after not in ids:
                raise QueryError("invalid pagination cursor")
            start = ids.index(query.after) + 1
        else:
            start = 0
        chunk = rows[start : start + query.size + 1]
        has_next = len(chunk) > query.size
        items = [d for _, d in chunk[: query.size]]
        cursor = str(chunk[query.size - 1][0]["_id"]) if has_next and query.number is None else None
        return Page(items, query.size, has_next, cursor, query.number, total)

    async def create(self, data: dict[str, t.Any]) -> T:
        stored = {self.meta.fields[k].stored: v for k, v in data.items()}
        doc = self.meta.decode(stored, stored=False)
        self._check_unique(doc)
        doc.id = ObjectId(f"{self._next:024x}")
        self._next += 1
        self.docs[doc.id] = doc
        return t.cast(T, doc)

    async def patch(self, id: t.Any, data: dict[str, t.Any], *, scope: t.Any, if_rev: int | None = None) -> T:
        doc = self._find(id, scope)
        if doc is None:
            raise DocumentNotFound("not found")
        revision = self.view.revision
        if revision is not None and if_rev is not None and getattr(doc, revision) != if_rev:
            raise StaleDocumentError("stale")
        values = {k: self.meta.codec.validate_field(self.model, k, v) for k, v in data.items()}
        if values:
            before = {k: getattr(doc, k) for k in values}
            for key, value in values.items():
                setattr(doc, key, value)
            try:
                self._check_unique(doc, skip=t.cast(t.Any, doc).id)
            except DuplicateKeyError:
                for key, value in before.items():
                    setattr(doc, key, value)
                raise
            if revision is not None:
                setattr(doc, revision, getattr(doc, revision) + 1)
        return doc

    async def delete(self, id: t.Any, *, scope: t.Any) -> None:
        doc = self._find(id, scope)
        if doc is None:
            raise DocumentNotFound("not found")
        del self.docs[t.cast(t.Any, doc).id]


# -- the contract API ----------------------------------------------------------------


def _widget_model(backend: str) -> type:
    """The model every plug is checked against, declared on the requested backend."""
    fields: dict[str, t.Any] = {
        "name": (t.Annotated[str, Policy(filter=("eq", "prefix", "in"), sort=True)], ...),
        "price": (t.Annotated[float, Policy(filter=True, sort=True)], 0.0),
        "stock": (t.Annotated[int, Policy(filter=("eq", "gte", "lte"))], 0),
        "tags": (list[str], None),
        "owner": (t.Annotated[str, Policy(update=False, filter=("eq",))], "anonymous"),
        "sku": (t.Annotated[str | None, Policy(filter=("eq", "exists"))], None),
        "internal_note": (t.Annotated[str | None, Policy(private=True)], None),
        "audit": (t.Annotated[str | None, Policy(insert=False, update=False)], None),
        "rev": (t.Annotated[int, Revision], 0),
    }
    config = SpecConfig(collection="widgets")
    if backend == "msgspec":
        import msgspec

        from mongospecs.msgspec import Spec as MsgspecSpec

        spec_fields: list[t.Any] = []
        for name, (tp, default) in fields.items():
            if default is ...:
                spec_fields.append((name, tp))
            elif name == "tags":
                spec_fields.append((name, tp, msgspec.field(default_factory=list)))
            else:
                spec_fields.append((name, tp, default))
        return msgspec.defstruct(
            "Widget", spec_fields, bases=(MsgspecSpec,), kw_only=True, namespace={"__spec__": config}
        )
    if backend == "pydantic":
        import pydantic

        from mongospecs.pydantic import Spec as PydanticSpec

        definitions: dict[str, t.Any] = {}
        for name, (tp, default) in fields.items():
            if name == "tags":
                definitions[name] = (tp, pydantic.Field(default_factory=list))
            else:
                definitions[name] = (tp, default)
        model = pydantic.create_model("Widget", __base__=PydanticSpec, **definitions)
        model.__spec__ = config
        return model
    if backend == "attrs":
        import attrs

        from mongospecs.attrs import Spec as AttrsSpec

        attributes: dict[str, t.Any] = {}
        for name, (tp, default) in fields.items():
            if default is ...:
                attributes[name] = attrs.field(type=tp)
            elif name == "tags":
                attributes[name] = attrs.field(type=tp, factory=list)
            else:
                attributes[name] = attrs.field(type=tp, default=default)
        klass = attrs.make_class("Widget", attributes, bases=(AttrsSpec,), kw_only=True)
        t.cast(t.Any, klass).__spec__ = config
        return klass
    raise ValueError(f"unknown backend {backend!r}")


class Principal(t.NamedTuple):
    name: str
    admin: bool


def _authenticate(request: Request) -> Principal | None:
    token = request.headers.get("authorization", "")
    if not token:
        return None
    if not token.startswith("Bearer "):
        raise Unauthorized("Malformed credentials.", headers={"www-authenticate": "Bearer"})
    name = token[len("Bearer ") :]
    return Principal(name, name == "root")


def contract_api(backend: str = "msgspec") -> Api:
    """The API the contract suite runs against: one model, in memory, with auth, scope and guards."""
    model = _widget_model(backend)
    store: FakeBackend[t.Any] = FakeBackend(model, unique=["sku"])

    def require_login(ctx: Ctx[t.Any]) -> bool:
        if ctx.principal is None:
            raise Unauthorized("Log in first.", headers={"www-authenticate": "Bearer"})
        return True

    def admin_only(ctx: Ctx[t.Any]) -> bool:
        return bool(ctx.principal and ctx.principal.admin)

    def own_rows(ctx: Ctx[t.Any]) -> t.Any:
        # admins see everything; everyone else only what they own or what is public
        if ctx.principal is not None and ctx.principal.admin:
            return None
        owners = ["anonymous"] + ([ctx.principal.name] if ctx.principal is not None else [])
        return Q.owner.in_(owners)

    widgets: Resource[t.Any] = Resource(
        model,
        backend=store,
        ops=[List, Get, Create, Patch, Delete],
        scope=own_rows,
        guards={Create: require_login, Patch: require_login, Delete: [require_login, admin_only]},
    )

    @widgets.before(Create)
    def stamp_owner(ctx: Ctx[t.Any]) -> None:
        ctx.state["creator"] = ctx.principal.name

    @widgets.after(Create)
    def record(ctx: Ctx[t.Any], doc: t.Any) -> None:
        doc.audit = f"created by {ctx.state['creator']}"

    @widgets.op("restock", method="POST", summary="Add stock to a widget")
    async def restock(ctx: Ctx[t.Any]) -> t.Any:
        amount = (ctx.body() or {}).get("amount", 1)
        doc = await ctx.load()
        doc.stock += int(amount)
        return doc

    @widgets.op("stats", method="GET", path="/stats")
    async def stats(ctx: Ctx[t.Any]) -> dict[str, t.Any]:
        page = await ctx.backend.list(ListQuery(size=1000), scope=await ctx.scope())
        return {"count": len(page.items), "stock": sum(d.stock for d in page.items)}

    pages: Resource[t.Any] = Resource(
        model, name="widget-pages", backend=store, ops=[List], page=Offset(default=2, max=3, total=True)
    )
    return Api(widgets, pages, prefix="/api", title="Contract", version="1.0.0", authenticate=_authenticate)


Transcript = list[dict[str, t.Any]]

_HEADERS = ("etag", "location", "www-authenticate", "content-type")
_FRAMEWORK_ROUTED = frozenset({"method not allowed", "unknown route", "unsupported method"})


async def run_contract(client: t.Any, api: Api | None = None) -> Transcript:
    """Drive the contract API through ``client`` (an ``httpx.AsyncClient``) and record what happened.

    The fixed sequence covers CRUD, validation, field policy, filters, sorting,
    both pagination styles, authentication, guards, row scope, ETags and
    conditional updates, custom operations, error documents and routing errors.
    """
    transcript: Transcript = []
    ann = {"authorization": "Bearer ann"}
    bob = {"authorization": "Bearer bob"}
    root = {"authorization": "Bearer root"}

    async def call(method: str, path: str, *, label: str, **kwargs: t.Any) -> t.Any:
        response = await client.request(method, "/api" + path, **kwargs)
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = response.text
        headers = {k: response.headers[k] for k in _HEADERS if k in response.headers}
        if "content-type" in headers:
            headers["content-type"] = headers["content-type"].split(";")[0]
        entry = {"label": label, "status": response.status_code, "headers": headers, "body": body}
        if label in _FRAMEWORK_ROUTED:
            # The framework answers these itself (no route matched), so only the status is contractual.
            entry = {"label": label, "status": response.status_code, "framework": True}
        transcript.append(entry)
        return body

    # -- create ------------------------------------------------------------------
    await call("POST", "/widgets", label="create needs login", json={"name": "a"})
    await call(
        "POST", "/widgets", label="malformed credentials", json={"name": "a"}, headers={"authorization": "x"}
    )
    await call(
        "POST",
        "/widgets",
        label="create",
        json={"name": "anvil", "price": 10.5, "stock": 3, "tags": ["heavy"], "owner": "ann", "sku": "A-1"},
        headers=ann,
    )
    await call(
        "POST",
        "/widgets",
        label="create second",
        json={"name": "bolt", "price": 0.5, "owner": "bob"},
        headers=bob,
    )
    await call(
        "POST", "/widgets", label="create public", json={"name": "cog", "price": 2, "stock": 9}, headers=ann
    )
    await call(
        "POST", "/widgets", label="create duplicate sku", json={"name": "dup", "sku": "A-1"}, headers=ann
    )
    await call("POST", "/widgets", label="create missing required", json={"price": 1}, headers=ann)
    await call(
        "POST", "/widgets", label="create wrong type", json={"name": "x", "price": "cheap"}, headers=ann
    )
    await call(
        "POST", "/widgets", label="create unknown field", json={"name": "x", "colour": "red"}, headers=ann
    )
    await call(
        "POST", "/widgets", label="create read-only field", json={"name": "x", "audit": "forged"}, headers=ann
    )
    await call(
        "POST",
        "/widgets",
        label="create private field",
        json={"name": "x", "internal_note": "n"},
        headers=ann,
    )
    await call("POST", "/widgets", label="create managed field", json={"name": "x", "rev": 9}, headers=ann)
    await call("POST", "/widgets", label="create with id", json={"name": "x", "id": "0" * 24}, headers=ann)
    await call("POST", "/widgets", label="create not an object", json=["a"], headers=ann)
    await call(
        "POST",
        "/widgets",
        label="create bad json",
        content=b"{nope",
        headers={**ann, "content-type": "application/json"},
    )
    await call(
        "POST",
        "/widgets",
        label="create wrong media type",
        content=b"name=a",
        headers={**ann, "content-type": "text/plain"},
    )
    await call("POST", "/widgets", label="create empty body", headers=ann)

    first, second, third = (f"{n:024x}" for n in (1, 2, 3))

    # -- read and row scope -----------------------------------------------------
    await call("GET", f"/widgets/{first}", label="get own", headers=ann)
    await call("GET", f"/widgets/{first}", label="get someone else's is 404", headers=bob)
    await call("GET", f"/widgets/{first}", label="get anonymous is 404")
    await call("GET", f"/widgets/{third}", label="get public anonymously")
    await call("GET", f"/widgets/{first}", label="get as admin", headers=root)
    await call("GET", "/widgets/not-an-id", label="get malformed id")
    await call("GET", f"/widgets/{'f' * 24}", label="get missing")
    await call("GET", f"/widgets/{first}", label="get not modified", headers={**ann, "if-none-match": '"0"'})

    # -- list, filters, sort ------------------------------------------------------
    await call("GET", "/widgets", label="list anonymous sees public only")
    await call("GET", "/widgets", label="list as ann", headers=ann)
    await call("GET", "/widgets?sort=-price", label="list sorted desc", headers=root)
    await call("GET", "/widgets?sort=name&limit=2", label="list first page", headers=root)
    await call("GET", "/widgets?sort=name&limit=2&after=garbage", label="list bad cursor", headers=root)
    await call("GET", "/widgets?name=bolt", label="filter eq", headers=root)
    await call("GET", "/widgets?name[prefix]=an", label="filter prefix", headers=root)
    await call("GET", "/widgets?name[in]=anvil,cog&sort=name", label="filter in", headers=root)
    await call("GET", "/widgets?price[gte]=2&price[lt]=11&sort=price", label="filter range", headers=root)
    await call("GET", "/widgets?stock[gte]=3&sort=name", label="filter int cast", headers=root)
    await call("GET", "/widgets?sku[exists]=true&sort=name", label="filter exists", headers=root)
    await call(
        "GET", "/widgets?price[ne]=0.5&sort=name&total=true", label="filter ne with total", headers=root
    )
    await call("GET", "/widgets?tags=heavy", label="filter on unfilterable field", headers=root)
    await call("GET", "/widgets?internal_note=n", label="filter on private field", headers=root)
    await call("GET", "/widgets?name[gt]=a", label="filter operator not allowed", headers=root)
    await call("GET", "/widgets?name[$ne]=a", label="operator injection in brackets", headers=root)
    await call("GET", "/widgets?$where=1", label="operator injection as key", headers=root)
    await call("GET", "/widgets?price[gte]=cheap", label="filter uncastable value", headers=root)
    await call("GET", "/widgets?sort=stock", label="sort on unsortable field", headers=root)
    await call("GET", "/widgets?limit=0", label="limit too small", headers=root)
    await call("GET", "/widgets?limit=abc", label="limit not a number", headers=root)
    await call("GET", "/widgets?limit=100000&sort=name", label="limit is capped", headers=root)
    await call("GET", "/widgets?page=2", label="page number on cursor resource", headers=root)
    await call("GET", "/widgets?include=owner", label="include not allowed", headers=root)

    # -- offset pagination --------------------------------------------------------
    await call("GET", "/widget-pages?sort=name", label="offset page 1")
    await call("GET", "/widget-pages?sort=name&page=2&total=true", label="offset page 2 with total")
    await call("GET", "/widget-pages?after=x", label="cursor on offset resource")
    await call("POST", "/widget-pages", label="method not allowed", json={})

    # -- patch --------------------------------------------------------------------
    await call("PATCH", f"/widgets/{first}", label="patch needs login", json={"price": 1})
    await call(
        "PATCH",
        f"/widgets/{first}",
        label="patch",
        json={"price": 12, "tags": ["heavy", "iron"]},
        headers=ann,
    )
    await call(
        "PATCH",
        f"/widgets/{first}",
        label="patch stale revision",
        json={"price": 13},
        headers={**ann, "if-match": '"0"'},
    )
    await call(
        "PATCH",
        f"/widgets/{first}",
        label="patch current revision",
        json={"price": 13},
        headers={**ann, "if-match": '"1"'},
    )
    await call(
        "PATCH",
        f"/widgets/{first}",
        label="patch bad if-match",
        json={"price": 13},
        headers={**ann, "if-match": "abc"},
    )
    await call(
        "PATCH", f"/widgets/{first}", label="patch null clears optional", json={"sku": None}, headers=ann
    )
    await call("PATCH", f"/widgets/{first}", label="patch null on required", json={"name": None}, headers=ann)
    await call("PATCH", f"/widgets/{first}", label="patch wrong type", json={"stock": "many"}, headers=ann)
    await call(
        "PATCH", f"/widgets/{first}", label="patch insert-only field", json={"owner": "bob"}, headers=ann
    )
    await call("PATCH", f"/widgets/{first}", label="patch unknown field", json={"colour": "red"}, headers=ann)
    await call("PATCH", f"/widgets/{first}", label="patch empty object", json={}, headers=ann)
    await call(
        "PATCH", f"/widgets/{first}", label="patch someone else's is 404", json={"price": 1}, headers=bob
    )
    await call("PATCH", f"/widgets/{'f' * 24}", label="patch missing", json={"price": 1}, headers=ann)

    # -- custom operations ----------------------------------------------------------
    await call("POST", f"/widgets/{first}/restock", label="custom op", json={"amount": 4}, headers=ann)
    await call(
        "POST", f"/widgets/{first}/restock", label="custom op out of scope", json={"amount": 4}, headers=bob
    )
    await call("GET", "/widgets/stats", label="custom collection op", headers=root)

    # -- delete -----------------------------------------------------------------------
    await call("DELETE", f"/widgets/{second}", label="delete needs login")
    await call("DELETE", f"/widgets/{second}", label="delete forbidden for non-admin", headers=bob)
    await call("DELETE", f"/widgets/{second}", label="delete", headers=root)
    await call("DELETE", f"/widgets/{second}", label="delete again is 404", headers=root)
    await call("GET", "/widgets?sort=name", label="list after delete", headers=root)

    # -- routing --------------------------------------------------------------------------
    await call("GET", "/nothing-here", label="unknown route")
    await call("PUT", f"/widgets/{first}", label="unsupported method", json={}, headers=ann)
    spec = await call("GET", "/openapi.json", label="openapi")
    transcript[-1]["body"] = {
        "paths": sorted(spec["paths"]),
        "schemas": sorted(spec["components"]["schemas"]),
    }
    return transcript


async def reference_transcript(backend: str = "msgspec") -> Transcript:
    """The transcript produced by the built-in pure-ASGI plug: the reference for every other plug."""
    import httpx

    api = contract_api(backend)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.asgi()), base_url="http://contract"
    ) as client:
        return await run_contract(client, api)
