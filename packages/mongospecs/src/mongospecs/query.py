"""Immutable, chainable queries.

Every builder method returns a new ``Query``; nothing touches the database until
a terminal method is awaited (``all``, ``first``, ``count`` ...) or the query is
iterated with ``async for``. Awaiting a query directly is the same as ``all()``.
"""

from __future__ import annotations

import asyncio
import base64
import typing as t
from dataclasses import dataclass

import bson

from mongospecs import tracking
from mongospecs.errors import DocumentNotFound, QueryError
from mongospecs.expr import SortInput, and_filters, compile_filter, compile_sort
from mongospecs.meta import meta_of
from mongospecs.updates import UpdateOp

if t.TYPE_CHECKING:
    from mongospecs.manager import Manager
    from mongospecs.relations import Include

__all__ = ["Page", "Query"]

T = t.TypeVar("T")

Mode = t.Literal["tracked", "lean", "raw"]

# find() option name -> aggregate() option name
_AGG_OPTIONS = {"max_time_ms": "maxTimeMS", "batch_size": "batchSize", "allow_disk_use": "allowDiskUse"}
_FIND_OPTIONS = frozenset(
    {"max_time_ms", "batch_size", "allow_disk_use", "hint", "comment", "collation", "no_cursor_timeout"}
)
_CHUNK = 500


@dataclass(slots=True)
class Page(t.Generic[T]):
    """One page of results."""

    items: list[T]
    size: int
    has_next: bool
    next_cursor: str | None = None
    """Opaque token for the following page (cursor pagination only)."""
    number: int | None = None
    """1-based page number (offset pagination only)."""
    total: int | None = None
    """Total matching documents, when it was asked for."""

    def __iter__(self) -> t.Iterator[T]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> T:
        return self.items[index]


class Query(t.Generic[T]):
    __slots__ = (
        "_conditions",
        "_fail",
        "_includes",
        "_limit",
        "_manager",
        "_mode",
        "_options",
        "_select",
        "_session",
        "_skip",
        "_sort",
        "_unscoped",
    )

    def __init__(
        self,
        manager: Manager[T],
        *,
        conditions: tuple[t.Any, ...] = (),
        sort: tuple[SortInput, ...] = (),
        skip: int = 0,
        limit: int = 0,
        select: tuple[str, ...] = (),
        includes: tuple[Include, ...] = (),
        mode: Mode = "tracked",
        unscoped: frozenset[str] | bool = False,
        session: t.Any = None,
        options: t.Mapping[str, t.Any] | None = None,
        fail: bool = False,
    ) -> None:
        self._manager = manager
        self._conditions = conditions
        self._sort = sort
        self._skip = skip
        self._limit = limit
        self._select = select
        self._includes = includes
        self._mode: Mode = mode
        self._unscoped = unscoped
        self._session = session
        self._options: dict[str, t.Any] = dict(options or {})
        self._fail = fail

    def _clone(self, **changes: t.Any) -> Query[T]:
        state = {name.lstrip("_"): getattr(self, name) for name in self.__slots__ if name != "_manager"}
        state.update(changes)
        return type(self)(self._manager, **state)

    # -- builders --------------------------------------------------------------

    def filter(self, *conditions: t.Any) -> Query[T]:
        """AND more conditions into the query."""
        return self._clone(conditions=(*self._conditions, *(c for c in conditions if c is not None)))

    where = filter

    def sort(self, *keys: SortInput) -> Query[T]:
        """Sort by ``Q.field.desc()``, ``"-field"``, ``"field"`` or ``(field, direction)``."""
        return self._clone(sort=(*self._sort, *keys))

    def skip(self, n: int) -> Query[T]:
        if n < 0:
            raise QueryError("skip must be >= 0")
        return self._clone(skip=n)

    def limit(self, n: int) -> Query[T]:
        if n < 0:
            raise QueryError("limit must be >= 0")
        return self._clone(limit=n)

    def select(self, *names: str) -> Query[T]:
        """Project fields server-side.

        ``"name"`` includes, ``"-name"`` excludes, ``"+name"`` adds a ``Deferred``
        field to the default projection. A document loaded with a projection is
        only saved for the fields that were loaded.
        """
        return self._clone(select=(*self._select, *names))

    def include(
        self,
        path: str,
        *,
        match: t.Any = None,
        select: t.Sequence[str] | None = None,
        sort: t.Sequence[SortInput] | SortInput | None = None,
        limit: int | None = None,
        per_parent_limit: int | None = None,
        strategy: t.Literal["auto", "lookup", "batch"] = "auto",
    ) -> Query[T]:
        """Load a relation (``Ref``, list or dict of ``Ref``, or ``Backref``) with the results.

        ``path`` may be nested (``"author.company"``). ``limit`` caps the related
        documents overall; ``per_parent_limit`` caps them per parent document.
        """
        from mongospecs.relations import add_include

        return self._clone(
            includes=add_include(
                self._includes,
                path,
                match=match,
                select=tuple(select) if select is not None else None,
                sort=()
                if sort is None
                else tuple(sort)
                if isinstance(sort, list | tuple) and not _is_pair(sort)
                else (sort,),
                limit=limit,
                per_parent_limit=per_parent_limit,
                strategy=strategy,
            )
        )

    def tracked(self) -> Query[T]:
        """Return model instances with change tracking (the default)."""
        return self._clone(mode="tracked")

    def lean(self) -> Query[T]:
        """Return model instances without a snapshot. They are read-only as far as ``save()`` goes."""
        return self._clone(mode="lean")

    def raw(self) -> Query[dict[str, t.Any]]:
        """Return plain dicts exactly as stored, with no decoding or validation."""
        return t.cast("Query[dict[str, t.Any]]", self._clone(mode="raw"))

    def unscoped(self, *names: str) -> Query[T]:
        """Switch plugin scopes off: all of them, or only the named ones (``"soft_delete"``)."""
        if not names:
            return self._clone(unscoped=True)
        current = frozenset() if isinstance(self._unscoped, bool) else self._unscoped
        return self._clone(unscoped=True if self._unscoped is True else current | frozenset(names))

    def session(self, session: t.Any) -> Query[T]:
        return self._clone(session=session)

    def options(self, **options: t.Any) -> Query[T]:
        """Driver options: ``max_time_ms``, ``hint``, ``comment``, ``collation``, ``batch_size``, ``allow_disk_use``."""
        unknown = set(options) - _FIND_OPTIONS
        if unknown:
            raise QueryError(f"unknown query options {sorted(unknown)}; allowed: {sorted(_FIND_OPTIONS)}")
        return self._clone(options={**self._options, **options})

    def or_fail(self) -> Query[T]:
        """Make ``first()`` and ``all()`` raise :class:`DocumentNotFound` when nothing matches."""
        return self._clone(fail=True)

    def __getattr__(self, name: str) -> t.Callable[..., Query[T]]:
        if name.startswith("_"):
            raise AttributeError(name)
        scope = self._manager.meta.scope(name)
        if scope is None:
            raise AttributeError(f"{type(self).__name__!r} has no attribute or named scope {name!r}")

        def apply(*args: t.Any, **kwargs: t.Any) -> Query[T]:
            result = scope(self, *args, **kwargs)
            return result if isinstance(result, Query) else self.filter(result)

        return apply

    # -- compilation -----------------------------------------------------------

    def _user_filter(self) -> dict[str, t.Any]:
        meta = self._manager.meta
        return and_filters(*(compile_filter(c, meta) for c in self._conditions))

    def _filter(self, op: str = "find") -> dict[str, t.Any]:
        ctx = self._manager.ctx(op, self._session, many=True)
        return self._manager.scoped(ctx, self._user_filter(), self._unscoped)

    def _projection(
        self, needed: t.Iterable[str] = ()
    ) -> tuple[dict[str, int] | None, frozenset[str] | None]:
        meta = self._manager.meta
        deferred = set(meta.deferred)
        include: list[str] = []
        exclude: list[str] = []
        for name in self._select:
            if name.startswith("+"):
                deferred.discard(meta.stored_path(name[1:]))
            elif name.startswith("-"):
                exclude.append(meta.stored_path(name[1:]))
            else:
                include.append(meta.stored_path(name))
        if include and exclude:
            raise QueryError("select() cannot mix included and excluded fields")
        if include:
            keys = dict.fromkeys([*include, *needed])
            if meta.disc_key:
                keys[meta.disc_key] = None
            projection = {k: 1 for k in keys}
            return projection, frozenset(k.partition(".")[0] for k in keys) | {"_id"}
        excluded = [*deferred, *exclude]
        if not excluded:
            return None, None
        return {k: 0 for k in excluded}, meta.stored_keys - {k for k in excluded if "." not in k}

    def _sort_spec(self) -> list[tuple[str, int]]:
        return compile_sort(self._sort, self._manager.meta)

    def _plan(self) -> dict[str, t.Any]:
        from mongospecs.relations import plan_includes

        manager = self._manager
        lookups, batched = plan_includes(manager, self._includes, self._mode)
        needed = [manager.meta.fields[i.name].stored for i in self._includes if i.name in manager.meta.refs]
        projection, partial = self._projection(needed)
        plan: dict[str, t.Any] = {
            "filter": self._filter(),
            "projection": projection,
            "partial": partial,
            "sort": self._sort_spec(),
            "skip": self._skip,
            "limit": self._limit,
            "lookups": lookups,
            "batched": batched,
        }
        if lookups:
            from mongospecs.relations import lookup_stages

            pipeline: list[dict[str, t.Any]] = []
            if plan["filter"]:
                pipeline.append({"$match": plan["filter"]})
            if plan["sort"]:
                pipeline.append({"$sort": dict(plan["sort"])})
            if self._skip:
                pipeline.append({"$skip": self._skip})
            if self._limit:
                pipeline.append({"$limit": self._limit})
            if projection:
                pipeline.append({"$project": projection})
            for include in lookups:
                pipeline.extend(lookup_stages(manager, include, self._session))
            plan["pipeline"] = pipeline
        return plan

    def compile(self) -> dict[str, t.Any]:
        """What will be sent to MongoDB: ``find`` arguments, or a ``pipeline`` when joins are used."""
        plan = self._plan()
        if "pipeline" in plan:
            return {"pipeline": plan["pipeline"]}
        return {k: plan[k] for k in ("filter", "projection", "sort", "skip", "limit")}

    # -- execution -------------------------------------------------------------

    async def _cursor(self, plan: dict[str, t.Any]) -> t.Any:
        manager = self._manager
        kw = manager._read_kw(self._session, self._options)
        if "pipeline" in plan:
            agg_kw = {_AGG_OPTIONS.get(k, k): v for k, v in kw.items() if k != "no_cursor_timeout"}
            return await manager.collection.aggregate(plan["pipeline"], **agg_kw)
        return manager.collection.find(
            plan["filter"],
            plan["projection"],
            sort=plan["sort"] or None,
            skip=plan["skip"],
            limit=plan["limit"],
            **kw,
        )

    async def _chunks(self, chunk: int | None = None) -> t.AsyncIterator[list[t.Any]]:
        """Yield materialised results in chunks (relations are resolved per chunk)."""
        from mongospecs.relations import attach_lookups, load_batched, pop_lookups

        manager = self._manager
        meta = manager.meta
        plan = self._plan()
        lookups, batched, partial = plan["lookups"], plan["batched"], plan["partial"]
        size = chunk or self._options.get("batch_size") or _CHUNK
        mode = self._mode

        async def flush(raws: list[dict[str, t.Any]]) -> list[t.Any]:
            joined = [pop_lookups(raw, lookups) for raw in raws] if lookups else None
            if mode == "raw":
                items: list[t.Any] = raws
            else:
                items = []
                for raw in raws:
                    doc = meta.decode(raw)
                    if mode == "tracked":
                        tracking.track(doc, meta_of(type(doc)).encode(doc), manager, partial)
                    items.append(doc)
            if joined is not None:
                attach_lookups(manager, items, joined, lookups, mode)
            if batched:
                await load_batched(manager, items, batched, mode, self._session)
            return items

        with manager._op("find"):
            cursor = await self._cursor(plan)
        async with cursor:
            buffer: list[dict[str, t.Any]] = []
            while True:
                with manager._op("find"):
                    try:
                        raw = await cursor.__anext__()
                    except StopAsyncIteration:
                        break
                buffer.append(raw)
                if len(buffer) >= size:
                    yield await flush(buffer)
                    buffer = []
            if buffer:
                yield await flush(buffer)

    async def __aiter__(self) -> t.AsyncIterator[T]:
        async for chunk in self._chunks():
            for item in chunk:
                yield item

    async def all(self) -> list[T]:
        out: list[T] = []
        async for chunk in self._chunks():
            out.extend(chunk)
        if self._fail and not out:
            raise DocumentNotFound(f"{self._manager.model.__name__}: no document matched")
        return out

    def __await__(self) -> t.Generator[t.Any, None, list[T]]:
        return self.all().__await__()

    async def first(self) -> T | None:
        """The first match, or ``None`` (or :class:`DocumentNotFound` after ``or_fail()``)."""
        items = await self._clone(limit=1, fail=False).all()
        if items:
            return items[0]
        if self._fail:
            raise DocumentNotFound(f"{self._manager.model.__name__}: no document matched")
        return None

    async def one(self) -> T:
        """The first match; raises :class:`DocumentNotFound` when there is none."""
        doc = await self.or_fail().first()
        assert doc is not None
        return doc

    async def count(self) -> int:
        manager = self._manager
        kw = manager._read_kw(self._session, self._options)
        agg_kw = {
            _AGG_OPTIONS.get(k, k): v
            for k, v in kw.items()
            if k in ("session", "max_time_ms", "hint", "collation", "comment")
        }
        if self._skip:
            agg_kw["skip"] = self._skip
        if self._limit:
            agg_kw["limit"] = self._limit
        with manager._op("count"):
            return int(await manager.collection.count_documents(self._filter("count"), **agg_kw))

    async def exists(self) -> bool:
        manager = self._manager
        with manager._op("exists"):
            found = await manager.collection.find_one(
                self._filter(), {"_id": 1}, **manager._read_kw(self._session, self._options)
            )
        return found is not None

    async def ids(self) -> list[t.Any]:
        manager = self._manager
        with manager._op("ids"):
            cursor = manager.collection.find(
                self._filter(),
                {"_id": 1},
                sort=self._sort_spec() or None,
                skip=self._skip,
                limit=self._limit,
                **manager._read_kw(self._session, self._options),
            )
            return [raw["_id"] async for raw in cursor]

    async def explain(self) -> dict[str, t.Any]:
        """The server's query plan."""
        manager = self._manager
        plan = self._plan()
        with manager._op("explain"):
            if "pipeline" in plan:
                return dict(
                    await manager.registry.database(manager.db_name).command(
                        "explain",
                        {"aggregate": manager.meta.collection, "pipeline": plan["pipeline"], "cursor": {}},
                    )
                )
            cursor = await self._cursor(plan)
            return dict(await cursor.explain())

    async def each(self, fn: t.Callable[[T], t.Awaitable[t.Any]], *, concurrency: int = 1) -> int:
        """Call ``fn`` for every result with bounded concurrency. Returns the number processed."""
        if concurrency < 1:
            raise QueryError("concurrency must be >= 1")
        count = 0
        if concurrency == 1:
            async for item in self:
                await fn(item)
                count += 1
            return count
        semaphore = asyncio.Semaphore(concurrency)

        async def run(item: T) -> None:
            try:
                await fn(item)
            finally:
                semaphore.release()

        async with asyncio.TaskGroup() as group:
            async for item in self:
                await semaphore.acquire()
                group.create_task(run(item))
                count += 1
        return count

    # -- filter-based writes -----------------------------------------------------

    def _check_plain(self, what: str) -> None:
        if self._skip or self._limit or self._sort:
            raise QueryError(f"{what} applies to every match; sort, skip and limit are not supported")

    async def update(self, *ops: UpdateOp, **fields: t.Any) -> int:
        """Atomically update every match. Returns the number of documents modified.

        Documents are not loaded, so document hooks do not run; plugins still
        rewrite the command (timestamps, revisions, scopes).
        """
        self._check_plain("update()")
        return await self._manager.update_many(
            self._user_filter(), ops, fields, session=self._session, unscoped=self._unscoped
        )

    async def delete(self, *, hard: bool = False) -> int:
        """Delete every match (a soft delete when the model has a ``DeletedAt`` field, unless ``hard``)."""
        self._check_plain("delete()")
        return await self._manager.delete_many(
            self._user_filter(), session=self._session, hard=hard, unscoped=self._unscoped
        )

    # -- pagination ----------------------------------------------------------------

    async def page(
        self,
        size: int = 20,
        *,
        after: str | None = None,
        number: int | None = None,
        total: bool = False,
    ) -> Page[T]:
        """Fetch one page.

        Without ``number`` this is cursor (keyset) pagination: pass the previous
        page's ``next_cursor`` as ``after``. The sort is made total by appending
        ``_id``; sort fields should not be null. With ``number`` (1-based) it is
        offset pagination. ``total=True`` adds a count of all matches.
        """
        if size < 1:
            raise QueryError("page size must be >= 1")
        if number is not None and after is not None:
            raise QueryError("use either number (offset pagination) or after (cursor pagination)")
        count_task: t.Awaitable[int] | None = self._clone(skip=0, limit=0, sort=()).count() if total else None

        if number is not None:
            if number < 1:
                raise QueryError("page numbers start at 1")
            query = self._clone(skip=(number - 1) * size, limit=size + 1, fail=False)
            if not self._sort:
                query = query.sort(("_id", 1))
        else:
            sort = self._sort_spec()
            if not any(key == "_id" for key, _ in sort):
                sort.append(("_id", 1))
            query = self._clone(sort=tuple(sort), skip=0, limit=size + 1, fail=False)
            if after is not None:
                from mongospecs.expr import Trusted

                query = query.filter(Trusted(_keyset_filter(sort, _decode_cursor(after, len(sort)))))

        total_count: int | None
        if count_task is not None and self._session is None:
            items, total_count = await asyncio.gather(query.all(), count_task)
        else:
            items = await query.all()
            total_count = await count_task if count_task is not None else None
        has_next = len(items) > size
        items = items[:size]
        next_cursor = None
        if number is None and has_next and items:
            next_cursor = _encode_cursor(self._sort_values(items[-1], query._sort_spec()))
        return Page(items, size, has_next, next_cursor, number, total_count)

    def _sort_values(self, item: t.Any, sort: list[tuple[str, int]]) -> list[t.Any]:
        doc = item if isinstance(item, dict) else meta_of(type(item)).encode(item)
        return [_dig(doc, key) for key, _ in sort]

    # -- other read shapes -----------------------------------------------------------

    def graph(
        self,
        field: str,
        *,
        connect_to: str = "id",
        max_depth: int | None = None,
        match: t.Any = None,
    ) -> t.Any:
        """Follow ``field`` recursively with ``$graphLookup`` (see :class:`mongospecs.relations.GraphQuery`)."""
        from mongospecs.relations import GraphQuery

        return GraphQuery(self, field, connect_to, max_depth, match)

    def union(self, *others: Query[t.Any]) -> t.Any:
        """Read several collections as one result set with ``$unionWith``."""
        from mongospecs.relations import UnionQuery

        return UnionQuery((self, *others))

    def __repr__(self) -> str:
        return f"<Query {self._manager.model.__name__} {self._user_filter()!r} mode={self._mode}>"


def _is_pair(value: t.Any) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and value[1] in (1, -1, "asc", "desc")
    )


def _dig(doc: t.Mapping[str, t.Any], path: str) -> t.Any:
    value: t.Any = doc
    for part in path.split("."):
        if not isinstance(value, t.Mapping):
            return None
        value = value.get(part)
    return value


def _encode_cursor(values: list[t.Any]) -> str:
    return base64.urlsafe_b64encode(bson.encode({"v": values})).decode("ascii").rstrip("=")


def _decode_cursor(token: str, expected: int) -> list[t.Any]:
    from mongospecs.expr import _has_operator

    try:
        padded = token + "=" * (-len(token) % 4)
        values = bson.decode(base64.urlsafe_b64decode(padded.encode("ascii")))["v"]
    except Exception as exc:
        raise QueryError("invalid pagination cursor") from exc
    if not isinstance(values, list) or len(values) != expected or _has_operator(values):
        raise QueryError("invalid pagination cursor")
    return values


def _keyset_filter(sort: list[tuple[str, int]], values: list[t.Any]) -> dict[str, t.Any]:
    """Documents strictly after ``values`` in ``sort`` order."""
    branches: list[dict[str, t.Any]] = []
    for i, (key, direction) in enumerate(sort):
        branch: dict[str, t.Any] = {sort[j][0]: values[j] for j in range(i)}
        branch[key] = {"$gt" if direction == 1 else "$lt": values[i]}
        branches.append(branch)
    return branches[0] if len(branches) == 1 else {"$or": branches}
