"""Resources: a model exposed over HTTP, described as pure data.

A :class:`Resource` owns a list of :class:`Operation` descriptors. Nothing here
imports a web framework: a plug translates its framework's request into a
:class:`Request`, calls :meth:`Resource.dispatch`, and translates the
:class:`Response` back. That makes every operation testable without a server.
"""

from __future__ import annotations

import inspect
import json
import logging
import typing as t
from dataclasses import dataclass, field

from mongospecs.expr import Expr
from mongospecs.jsonable import jsonable
from mongospecs_web.backend import MongoBackend, ResourceBackend
from mongospecs_web.errors import (
    PROBLEM_JSON,
    BadRequest,
    Forbidden,
    NotFound,
    PreconditionFailed,
    Unprocessable,
    UnsupportedMediaType,
    problem,
)
from mongospecs_web.filters import Cursor, Pagination, parse_includes, parse_list_query
from mongospecs_web.policy import View, view_of

__all__ = [
    "Create",
    "Ctx",
    "Delete",
    "Get",
    "List",
    "Operation",
    "Patch",
    "Put",
    "Request",
    "Resource",
    "Response",
]

T = t.TypeVar("T")
F = t.TypeVar("F", bound=t.Callable[..., t.Any])

log = logging.getLogger("mongospecs_web")

# Built-in operation kinds. Used as keys for ``ops=``, ``guards=`` and hooks.
List = "list"
Get = "get"
Create = "create"
Patch = "patch"
Put = "put"
Delete = "delete"

JSON = "application/json"


def _stdlib_dumps(data: t.Any) -> bytes:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()


def _pick_dumps() -> t.Callable[[t.Any], bytes]:
    """Use msgspec's JSON encoder when it is installed (it usually is); the standard library otherwise."""
    try:
        import msgspec
    except ImportError:
        return _stdlib_dumps
    return msgspec.json.Encoder().encode


dumps: t.Callable[[t.Any], bytes] = _pick_dumps()


def _not_json(literal: str) -> t.NoReturn:
    # Python's parser reads NaN and Infinity; JSON has neither, and a NaN that got stored
    # could not be sent back as JSON.
    raise ValueError(f"{literal} is not a JSON value")


@dataclass(slots=True)
class Request:
    """A framework-neutral HTTP request."""

    method: str
    path_params: dict[str, str] = field(default_factory=dict)
    query: list[tuple[str, str]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    """Header names in lower case."""
    body: bytes = b""
    principal: t.Any = None
    """Whoever is calling, as returned by the plug's ``authenticate`` callable."""
    raw: t.Any = None
    """The framework's own request object, for custom operations that need it."""

    def json(self) -> t.Any:
        if not self.body:
            return None
        content_type = self.headers.get("content-type", JSON).split(";")[0].strip().lower()
        if content_type != JSON and not content_type.endswith("+json"):
            raise UnsupportedMediaType("The request body must be application/json.")
        try:
            return json.loads(self.body, parse_constant=_not_json)
        except ValueError:
            raise BadRequest("The request body is not valid JSON.") from None
        except RecursionError:
            raise BadRequest("The request body is nested too deeply.") from None


@dataclass(slots=True)
class Response:
    """A framework-neutral HTTP response."""

    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    media_type: str = JSON

    @classmethod
    def json(cls, data: t.Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
        return cls(status, dumps(data), dict(headers or {}), JSON)

    @classmethod
    def error(cls, exc: BaseException) -> Response:
        err = problem(exc)
        body = json.dumps(err.body(), separators=(",", ":"), ensure_ascii=False).encode()
        return cls(err.status, body, dict(err.headers), PROBLEM_JSON)


@dataclass(slots=True)
class Ctx(t.Generic[T]):
    """What an operation handler, guard, scope or hook receives."""

    resource: Resource[T]
    op: Operation
    request: Request
    state: dict[str, t.Any] = field(default_factory=dict)

    @property
    def principal(self) -> t.Any:
        return self.request.principal

    @property
    def path_params(self) -> dict[str, str]:
        return self.request.path_params

    @property
    def id(self) -> str:
        return self.request.path_params["id"]

    @property
    def backend(self) -> ResourceBackend[T]:
        return self.resource.backend

    def body(self) -> t.Any:
        return self.request.json()

    async def scope(self) -> t.Any:
        """The row-level filter for this request (``None`` when the resource has no scope)."""
        if "scope" not in self.state:
            self.state["scope"] = await self.resource._scope(self)
        return self.state["scope"]

    async def load(self) -> T:
        """The document named by ``{id}``, within the request's scope; 404 if it is not visible."""
        doc = await self.backend.get(self.id, scope=await self.scope())
        if doc is None:
            raise NotFound("The resource does not exist.")
        return doc


Handler = t.Callable[[Ctx[t.Any]], t.Awaitable[t.Any]]
Guard = t.Callable[[Ctx[t.Any]], t.Any]


@dataclass(frozen=True, slots=True)
class Operation:
    """One HTTP operation of a resource: pure data plus an async handler."""

    name: str
    method: str
    path: str
    """Path under the resource, with ``{name}`` parameters: ``""``, ``"/{id}"``, ``"/{id}/publish"``."""
    handler: Handler
    kind: str = "custom"
    summary: str = ""
    status: int = 200
    request_schema: dict[str, t.Any] | None = None
    response_schema: dict[str, t.Any] | None = None
    query_parameters: tuple[dict[str, t.Any], ...] = ()
    guards: tuple[Guard, ...] = ()

    @property
    def path_parameters(self) -> tuple[str, ...]:
        return tuple(
            part[1:-1] for part in self.path.split("/") if part.startswith("{") and part.endswith("}")
        )


async def _maybe(value: t.Any) -> t.Any:
    return await value if inspect.isawaitable(value) else value


class Resource(t.Generic[T]):
    """A model exposed as REST operations.

    ::

        articles = Resource(
            Article,
            ops=[List, Get, Create, Patch, Delete],
            scope=lambda ctx: Q.author == ctx.principal.id,   # row-level, ANDed into every filter
            guards={Delete: is_admin},                        # operation-level
            include=["author"],
            page=Cursor(default=20, max=100),
        )
    """

    def __init__(
        self,
        model: type[T],
        *,
        name: str | None = None,
        ops: t.Sequence[str] = (List, Get, Create, Patch, Delete),
        backend: ResourceBackend[T] | None = None,
        scope: t.Callable[[Ctx[T]], t.Any] | None = None,
        guards: t.Mapping[str, Guard | t.Sequence[Guard]] | t.Sequence[Guard] | None = None,
        include: t.Sequence[str] = (),
        page: Pagination | None = None,
        tags: t.Sequence[str] | None = None,
        server_fields: t.Callable[[Ctx[T]], t.Any] | None = None,
        explicit_writes: bool = False,
        merge_patch: bool = True,
    ) -> None:
        self.model = model
        self.view: View = view_of(model, explicit_writes=explicit_writes)
        """With ``explicit_writes=True`` a field is writable only if its ``Policy`` says so
        (``Policy(insert=True)``, ``Policy(update=True)``); by default ordinary fields are
        writable unless marked otherwise."""
        self.merge_patch = merge_patch
        """``PATCH`` merges an object given for an embedded model into the stored one. With
        ``merge_patch=False`` it replaces the stored object: the embedded model is validated
        whole, and every value in it that the client cannot send goes back to its default."""
        self.name = name or self.view.meta.collection
        self.backend: ResourceBackend[T] = backend if backend is not None else MongoBackend(model)
        self.scope_fn = scope
        self.server_fields = server_fields
        """``server_fields(ctx) -> dict`` of values the server sets on create (the author of a
        review, a tenant id). They are not subject to the field policy, so the fields can stay
        closed to clients with ``Policy(insert=False)``."""
        self.includes: tuple[str, ...] = tuple(include)
        for path in self.includes:
            head = path.partition(".")[0]
            if head not in self.view.relations:
                raise ValueError(f"{model.__name__}.{head} is not a readable relation and cannot be included")
        self.page: Pagination = page or Cursor()
        self.tags: tuple[str, ...] = tuple(tags) if tags is not None else (self.name,)
        self.raw_reads = True
        """Serve ``GET`` straight from stored documents where that is certain to give the same
        response as going through the model. Set to ``False`` to always build models."""
        self._all_guards: tuple[Guard, ...] = ()
        self._guards: dict[str, tuple[Guard, ...]] = {}
        if isinstance(guards, t.Mapping):
            for key, value in guards.items():
                self._guards[key] = (
                    tuple(value) if isinstance(value, list | tuple) else (t.cast(Guard, value),)
                )
        elif guards:
            self._all_guards = tuple(guards)
        self._before: dict[str, list[t.Callable[..., t.Any]]] = {}
        self._after: dict[str, list[t.Callable[..., t.Any]]] = {}
        self.operations: list[Operation] = []
        builders = {
            List: self._list_op,
            Get: self._get_op,
            Create: self._create_op,
            Patch: self._patch_op,
            Put: self._put_op,
            Delete: self._delete_op,
        }
        for kind in ops:
            if kind not in builders:
                raise ValueError(
                    f"unknown operation {kind!r}; use List, Get, Create, Patch, Put, Delete or @resource.op"
                )
            self.operations.append(builders[kind]())

    # -- declaration -------------------------------------------------------------

    def op(
        self,
        name: str,
        *,
        method: str = "POST",
        path: str | None = None,
        summary: str = "",
        status: int = 200,
        request_schema: dict[str, t.Any] | None = None,
        response_schema: dict[str, t.Any] | None = None,
        guards: t.Sequence[Guard] = (),
    ) -> t.Callable[[Handler], Handler]:
        """Add a custom operation. The handler receives a :class:`Ctx` and returns a document,
        JSON-able data, a :class:`Response`, or ``None`` (204)."""

        def register(handler: Handler) -> Handler:
            self.operations.append(
                Operation(
                    name,
                    method.upper(),
                    path if path is not None else f"/{{id}}/{name}",
                    handler,
                    "custom",
                    summary or (handler.__doc__ or "").strip().split("\n")[0],
                    status,
                    request_schema,
                    response_schema,
                    (),
                    tuple(guards),
                )
            )
            return handler

        return register

    def before(self, op: str) -> t.Callable[[F], F]:
        """Register ``async def hook(ctx)`` to run before an operation (after guards)."""

        def register(fn: F) -> F:
            self._before.setdefault(op, []).append(fn)
            return fn

        return register

    def after(self, op: str) -> t.Callable[[F], F]:
        """Register ``async def hook(ctx, result)`` to run after an operation succeeded."""

        def register(fn: F) -> F:
            self._after.setdefault(op, []).append(fn)
            return fn

        return register

    def check(self) -> None:
        """Raise ``ValueError`` if a guard or hook is registered for an operation this resource
        does not have: a misspelt key, or ``Put`` on a resource whose ``ops`` leave it out, would
        otherwise guard nothing without a word. Called when an ``Api`` builds its routes, so
        custom operations may be declared after the guards that name them."""
        known = {op.name for op in self.operations} | {op.kind for op in self.operations}
        for what, keys in (("guards", self._guards), ("before", self._before), ("after", self._after)):
            unknown = sorted(set(keys) - known)
            if unknown:
                raise ValueError(
                    f"{self.name}: {what} registered for {unknown}, which this resource does not have; "
                    f"its operations are {sorted(known - {'custom'})}"
                )

    def operation(self, name: str) -> Operation:
        for op in self.operations:
            if op.name == name:
                return op
        raise KeyError(name)

    # -- dispatch ----------------------------------------------------------------

    async def _scope(self, ctx: Ctx[T]) -> t.Any:
        if self.scope_fn is None:
            return None
        scope = await _maybe(self.scope_fn(ctx))
        if scope is not None and not isinstance(scope, Expr | t.Mapping):
            raise TypeError("scope must return a Q condition, a mapping, or None")
        return scope

    async def dispatch(self, op: Operation, request: Request) -> Response:
        """Run one operation. Never raises: every error becomes a problem+json response."""
        ctx: Ctx[T] = Ctx(self, op, request)
        try:
            ran: set[int] = set()
            for guard in (
                *self._all_guards,
                *self._guards.get(op.kind, ()),
                *self._guards.get(op.name, ()),  # the same key as the kind for built-in operations
                *op.guards,
            ):
                if id(guard) in ran:
                    continue  # one guard is one check, however many places list it
                ran.add(id(guard))
                if not await _maybe(guard(ctx)):
                    raise Forbidden("You are not allowed to do this.")
            for hook in self._before.get(op.name, ()):
                await _maybe(hook(ctx))
            result = await op.handler(ctx)
            for hook in self._after.get(op.name, ()):
                await _maybe(hook(ctx, ctx.state.get("result", result)))
            return self._respond(op, result, ctx)
        except Exception as exc:
            response = Response.error(exc)
            if response.status >= 500:
                # The client gets a bare 500; the cause belongs in the server log.
                log.error("%s %s failed", self.name, op.name, exc_info=exc)
            return response

    def _respond(self, op: Operation, result: t.Any, ctx: Ctx[T]) -> Response:
        if isinstance(result, Response):
            return result
        if result is None:
            return Response(204, b"", {})
        if isinstance(result, self.model):
            headers: dict[str, str] = {}
            etag = self.view.etag(result)
            if etag:
                headers["etag"] = etag
            return Response.json({"data": self.view.serialize(result)}, op.status, headers)
        return Response.json({"data": jsonable(result)}, op.status)

    # -- built-in operations -------------------------------------------------------

    def _ref(self, kind: str) -> dict[str, t.Any]:
        return {"$ref": f"#/components/schemas/{self.schema_name(kind)}"}

    def schema_name(self, kind: str) -> str:
        return f"{self.model.__name__}{kind.capitalize()}"

    def _raw_fields(self, kind: str, include: t.Collection[str]) -> tuple[str, ...] | None:
        """The fields to read as a plain stored document, or ``None`` to read a model.

        Reading without a model is possible when the response is all the document is used
        for: the view can serialise it directly, no relation is embedded, and no ``after``
        hook is waiting for a model.
        """
        plan = self.view.fast
        if plan is None or include or self._after.get(kind) or not self.raw_reads:
            return None
        if not getattr(self.backend, "supports_raw", False):
            return None
        return plan.select

    def _list_op(self) -> Operation:
        async def handler(ctx: Ctx[T]) -> Response:
            query = parse_list_query(ctx.request.query, self.view, self.page, self.includes)
            fields = self._raw_fields(List, query.include)
            if fields is not None:
                page = await t.cast(t.Any, self.backend).list(query, scope=await ctx.scope(), fields=fields)
            else:
                page = await self.backend.list(query, scope=await ctx.scope())
            ctx.state["result"] = page
            meta: dict[str, t.Any] = {"size": page.size, "has_next": page.has_next}
            if page.number is not None:
                meta["number"] = page.number
            else:
                meta["next_cursor"] = page.next_cursor
            if page.total is not None:
                meta["total"] = page.total
            include = set(query.include)
            return Response.json(
                {"data": [self.view.serialize(d, include) for d in page.items], "page": meta}
            )

        return Operation(
            List,
            "GET",
            "",
            handler,
            List,
            f"List {self.name}",
            200,
            None,
            {
                "type": "object",
                "properties": {
                    "data": {"type": "array", "items": self._ref("read")},
                    "page": {"type": "object"},
                },
                "required": ["data", "page"],
            },
            tuple(self._list_parameters()),
        )

    def _list_parameters(self) -> list[dict[str, t.Any]]:
        params: list[dict[str, t.Any]] = []
        for name, ops in self.view.filterable.items():
            for op in ops:
                params.append(
                    {
                        "name": name if op == "eq" else f"{name}[{op}]",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": f"Filter: {name} {op}",
                    }
                )
        if self.view.sortable:
            params.append(
                {
                    "name": "sort",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Comma-separated sort keys; prefix with - for descending. Allowed: "
                    + ", ".join(sorted(self.view.sortable)),
                }
            )
        params.append(
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "schema": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": self.page.max,
                    "default": self.page.default,
                },
            }
        )
        if self.page.kind == "cursor":
            params.append({"name": "after", "in": "query", "required": False, "schema": {"type": "string"}})
        else:
            params.append(
                {
                    "name": "page",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "minimum": 1},
                }
            )
        if self.page.total:
            params.append({"name": "total", "in": "query", "required": False, "schema": {"type": "boolean"}})
        if self.includes:
            params.append(self._include_parameter())
        return params

    def _include_parameter(self) -> dict[str, t.Any]:
        return {
            "name": "include",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "Comma-separated relations to embed. Allowed: " + ", ".join(self.includes),
        }

    def _one(self) -> dict[str, t.Any]:
        return {"type": "object", "properties": {"data": self._ref("read")}, "required": ["data"]}

    def _get_op(self) -> Operation:
        async def handler(ctx: Ctx[T]) -> Response:
            include = parse_includes(ctx.request.query, self.includes)
            fields = self._raw_fields(Get, include)
            if fields is not None:
                doc = await t.cast(t.Any, self.backend).get(ctx.id, scope=await ctx.scope(), fields=fields)
            else:
                doc = await self.backend.get(ctx.id, scope=await ctx.scope(), include=include, read_only=True)
            if doc is None:
                raise NotFound("The resource does not exist.")
            ctx.state["result"] = doc
            headers: dict[str, str] = {}
            etag = self.view.etag(doc)
            if etag:
                headers["etag"] = etag
                if _etag_listed(ctx.request.headers.get("if-none-match"), etag):
                    return Response(304, b"", headers)
            return Response.json({"data": self.view.serialize(doc, set(include))}, 200, headers)

        params = (self._include_parameter(),) if self.includes else ()
        return Operation(
            Get, "GET", "/{id}", handler, Get, f"Get one of {self.name}", 200, None, self._one(), params
        )

    def _create_op(self) -> Operation:
        async def handler(ctx: Ctx[T]) -> Response:
            body = ctx.body()
            if body is None:
                raise Unprocessable("A JSON request body is required.")
            data = dict(self.view.check_input(body, self.view.insertable, "create"))
            if self.server_fields is not None:
                data.update(await _maybe(self.server_fields(ctx)))
            doc = await self.backend.create(data)
            ctx.state["result"] = doc
            headers = {"location": str(jsonable(getattr(doc, "id", "")))}
            etag = self.view.etag(doc)
            if etag:
                headers["etag"] = etag
            return Response.json({"data": self.view.serialize(doc)}, 201, headers)

        return Operation(
            Create,
            "POST",
            "",
            handler,
            Create,
            f"Create one of {self.name}",
            201,
            self._ref("create"),
            self._one(),
        )

    def _patch_op(self) -> Operation:
        async def handler(ctx: Ctx[T]) -> Response:
            body = ctx.body()
            if body is None:
                raise Unprocessable("A JSON request body is required.")
            data = self.view.check_input(body, self.view.updatable, "update")
            return await self._write(ctx, self.view.plan_patch(data, merge=self.merge_patch))

        return Operation(
            Patch,
            "PATCH",
            "/{id}",
            handler,
            Patch,
            f"Update one of {self.name}",
            200,
            self._ref("patch"),
            self._one(),
        )

    def _put_op(self) -> Operation:
        async def handler(ctx: Ctx[T]) -> Response:
            body = ctx.body()
            if body is None:
                raise Unprocessable("A JSON request body is required.")
            data = self.view.check_input(body, self.view.updatable, "update")
            return await self._write(ctx, self.view.plan_put(data))

        return Operation(
            Put,
            "PUT",
            "/{id}",
            handler,
            Put,
            f"Replace one of {self.name}",
            200,
            self._ref("put"),
            self._one(),
        )

    async def _write(self, ctx: Ctx[T], plan: t.Any) -> Response:
        """Apply a planned PATCH or PUT in one atomic update and return the document."""
        if_rev = _if_match(ctx.request.headers.get("if-match"), self.view.revision is not None)
        if plan.nested:
            # Writes inside an embedded object, leaving the rest of it as stored.
            if not getattr(self.backend, "supports_merge", False):
                raise Unprocessable("This resource cannot write part of an embedded object.")
            doc = await t.cast(t.Any, self.backend).patch(
                ctx.id, plan.fields, scope=await ctx.scope(), if_rev=if_rev, merge=plan
            )
        else:
            doc = await self.backend.patch(ctx.id, plan.fields, scope=await ctx.scope(), if_rev=if_rev)
        ctx.state["result"] = doc
        headers: dict[str, str] = {}
        etag = self.view.etag(doc)
        if etag:
            headers["etag"] = etag
        return Response.json({"data": self.view.serialize(doc)}, 200, headers)

    def _delete_op(self) -> Operation:
        async def handler(ctx: Ctx[T]) -> None:
            if_rev = _if_match(ctx.request.headers.get("if-match"), self.view.revision is not None)
            await self.backend.delete(ctx.id, scope=await ctx.scope(), if_rev=if_rev)

        return Operation(Delete, "DELETE", "/{id}", handler, Delete, f"Delete one of {self.name}", 204)

    def __repr__(self) -> str:
        return f"<Resource {self.name} ops={[op.name for op in self.operations]}>"


def _etag_listed(header: str | None, etag: str) -> bool:
    """Whether ``If-None-Match`` names ``etag``: one of a list, weak or strong, or ``*``."""
    if header is None:
        return False
    for item in header.split(","):
        item = item.strip()
        if item == "*" or item.removeprefix("W/") == etag:
            return True
    return False


def _if_match(header: str | None, has_revision: bool) -> int | None:
    """Parse ``If-Match`` into a revision number. ``*`` and absence mean "no check"."""
    if header is None:
        return None
    value = header.strip()
    if value == "*":
        return None
    if not has_revision:
        # There is no ETag to compare with, so the condition cannot hold. Going ahead anyway
        # would be an unconditional write the client explicitly did not ask for.
        raise PreconditionFailed("This resource has no revisions, so If-Match cannot be satisfied.")
    if value.startswith("W/"):
        value = value[2:]
    try:
        return int(value.strip('"'))
    except ValueError:
        raise BadRequest("If-Match must carry the ETag of the resource.") from None
