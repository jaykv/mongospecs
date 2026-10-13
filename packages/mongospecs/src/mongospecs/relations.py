"""Relationship loading.

``include()`` can be served two ways and both give the same result:

* **batch** - one extra ``find`` per relation with ``_id: {$in: [...]}`` (what
  Mongoose's ``populate`` does);
* **lookup** - a ``$lookup`` stage in a single aggregation pipeline.

``DEFAULT_STRATEGY`` is what ``strategy="auto"`` uses. It is chosen from
benchmark data, not assumed; see ``bench/`` workload B3.

Also here: cascade rules, ``$graphLookup`` traversal and ``$unionWith`` reads.
"""

from __future__ import annotations

import dataclasses
import typing as t
from dataclasses import dataclass

from mongospecs import tracking
from mongospecs.codec import FieldInfo
from mongospecs.errors import ConfigurationError, QueryError
from mongospecs.expr import SortInput, Trusted, compile_filter, compile_sort
from mongospecs.markers import OnDelete
from mongospecs.meta import meta_of
from mongospecs.refs import Ref, id_of

if t.TYPE_CHECKING:
    from mongospecs.manager import Manager
    from mongospecs.query import Query

__all__ = ["DEFAULT_STRATEGY", "GraphQuery", "GraphResult", "Include", "UnionQuery"]

T = t.TypeVar("T")

DEFAULT_STRATEGY: t.Literal["lookup", "batch"] = "batch"

_PREFIX = "__inc_"


@dataclass(frozen=True, slots=True)
class Include:
    name: str
    match: t.Any = None
    select: tuple[str, ...] | None = None
    sort: tuple[SortInput, ...] = ()
    limit: int | None = None
    per_parent_limit: int | None = None
    strategy: str = "auto"
    nested: tuple[Include, ...] = ()


def add_include(includes: tuple[Include, ...], path: str, **options: t.Any) -> tuple[Include, ...]:
    """Add ``path`` (possibly dotted) to a tuple of includes, merging shared prefixes."""
    head, _, rest = path.partition(".")
    existing = next((i for i in includes if i.name == head), None)
    if rest:
        base = existing or Include(head, strategy=options.get("strategy", "auto"))
        updated = dataclasses.replace(base, nested=add_include(base.nested, rest, **options))
    else:
        updated = Include(head, nested=existing.nested if existing else (), **options)
    return (*(i for i in includes if i.name != head), updated)


def check_include(meta: t.Any, path: str) -> None:
    """Raise :class:`QueryError` unless every segment of ``path`` is a relation.

    Called when the query is built, so a mistyped path fails at once and not only
    when a matching document happens to exist.
    """
    metas: tuple[t.Any, ...] = (meta,)
    for name in path.split("."):
        holders = [
            m
            for m in metas
            if (f := m.fields.get(name)) is not None and (f.ref is not None or f.backref is not None)
        ]
        if not holders:
            owner = " | ".join(m.model.__name__ for m in metas)
            raise QueryError(f"{owner}.{name} is not a relation and cannot be included")
        try:
            metas = tuple(target for m in holders for target in m.ref_targets(name))
        except ConfigurationError:
            return  # a target that is not defined yet; it is reported when the query runs


def _relation(manager: Manager[t.Any], include: Include) -> FieldInfo:
    info = manager.meta.fields.get(include.name)
    if info is None or (info.ref is None and info.backref is None):
        raise QueryError(f"{manager.model.__name__}.{include.name} is not a relation and cannot be included")
    return info


def _target(manager: Manager[t.Any], model: type) -> Manager[t.Any]:
    return manager.registry.manager(model, db=manager.db_name)


def _lookup_capable(manager: Manager[t.Any], include: Include) -> bool:
    info = _relation(manager, include)
    targets = manager.meta.ref_targets(include.name)
    if len(targets) != 1 or include.limit is not None:
        return False
    if info.ref is not None and info.ref.shape == "dict":
        return False
    child = _target(manager, targets[0].model)
    return all(_lookup_capable(child, nested) for nested in include.nested)


def plan_includes(
    manager: Manager[t.Any], includes: tuple[Include, ...], mode: str
) -> tuple[list[Include], list[Include]]:
    """Split includes into those served by ``$lookup`` and those served by batched queries."""
    lookups: list[Include] = []
    batched: list[Include] = []
    for include in includes:
        _relation(manager, include)
        strategy = include.strategy if include.strategy != "auto" else DEFAULT_STRATEGY
        if strategy == "lookup" and mode != "raw":
            if _lookup_capable(manager, include):
                lookups.append(include)
                continue
            if include.strategy == "lookup":
                raise QueryError(
                    f"include({include.name!r}) cannot use $lookup: polymorphic targets, dict-of-references "
                    "and an overall limit need the batch strategy"
                )
        batched.append(include)
    return lookups, batched


# -- materialising related documents ------------------------------------------------


def _child_projection(
    target: Manager[t.Any], include: Include, extra: t.Iterable[str] = ()
) -> dict[str, int] | None:
    query = target.find()
    if include.select is not None:
        query = query.select(*include.select)
    return query._projection(extra)[0]


def _materialize(
    target: Manager[t.Any], raw: dict[str, t.Any], mode: str, partial: frozenset[str] | None
) -> t.Any:
    if mode == "raw":
        return raw
    doc = target.meta.decode(raw)
    if mode == "tracked":
        tracking.track(doc, meta_of(type(doc)).encode(doc), target, partial)
    return doc


def _key(value: t.Any) -> t.Any:
    value = id_of(value)
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _attach_forward(
    item: t.Any, info: FieldInfo, by_id: t.Mapping[t.Any, t.Any], model: type | None, limit: int | None
) -> None:
    assert info.ref is not None
    shape = info.ref.shape
    if isinstance(item, dict):  # raw mode: replace ids with the related raw documents
        value = item.get(info.stored)
        if value is None:
            return
        if shape == "one":
            item[info.stored] = by_id.get(_key(value))
        elif shape == "list":
            found = [by_id[_key(v)] for v in value if _key(v) in by_id]
            item[info.stored] = found[:limit] if limit else found
        else:
            item[info.stored] = {k: by_id.get(_key(v)) for k, v in value.items()}
        return
    value = getattr(item, info.name)
    if value is None:
        return

    def ref(v: t.Any) -> Ref[t.Any]:
        r = v if type(v) is Ref else Ref(v, model)
        doc = by_id.get(_key(r.id))
        if doc is not None:
            r._doc = doc
            r._model = type(doc)
        return r

    if shape == "one":
        new: t.Any = ref(value)
    elif shape == "list":
        new = [ref(v) for v in value]
        if limit:
            loaded = 0
            for r in new:
                if r._doc is not None:
                    loaded += 1
                    if loaded > limit:
                        r._doc = None
    else:
        new = {k: ref(v) for k, v in value.items()}
    setattr(item, info.name, new)


def _attach_backref(item: t.Any, info: FieldInfo, value: t.Any) -> None:
    if isinstance(item, dict):
        item[info.name] = value
    else:
        setattr(item, info.name, value)


def _item_id(item: t.Any) -> t.Any:
    return item["_id"] if isinstance(item, dict) else item.id


# -- lookup strategy ---------------------------------------------------------------------


def lookup_stages(manager: Manager[t.Any], include: Include, session: t.Any) -> list[dict[str, t.Any]]:
    """The ``$lookup`` stage (with nested lookups) for one include."""
    info = _relation(manager, include)
    (target_meta,) = manager.meta.ref_targets(include.name)
    target = _target(manager, target_meta.model)
    ctx = target.ctx("find", session, many=True)
    match = compile_filter(include.match, target_meta) if include.match is not None else None
    scope = target.scoped(ctx, match)
    counting = info.backref is not None and info.backref.count

    sub: list[dict[str, t.Any]] = []
    if scope:
        sub.append({"$match": scope})
    if counting:
        sub.append({"$count": "n"})
    else:
        if include.sort:
            sub.append({"$sort": dict(compile_sort(include.sort, target_meta))})
        if include.per_parent_limit:
            sub.append({"$limit": include.per_parent_limit})
        needed = [target_meta.fields[n.name].stored for n in include.nested if n.name in target_meta.refs]
        projection = _child_projection(target, include, needed)
        if projection:
            sub.append({"$project": projection})
        for nested in include.nested:
            sub.extend(lookup_stages(target, nested, session))

    if info.backref is not None:
        local, foreign = "_id", target_meta.stored_path(info.backref.field)
    else:
        local, foreign = info.stored, "_id"
    stage: dict[str, t.Any] = {
        "from": target_meta.collection,
        "localField": local,
        "foreignField": foreign,
        "as": _PREFIX + include.name,
    }
    if sub:
        stage["pipeline"] = sub
    return [{"$lookup": stage}]


def pop_lookups(raw: dict[str, t.Any], lookups: t.Sequence[Include]) -> dict[str, list[dict[str, t.Any]]]:
    return {i.name: raw.pop(_PREFIX + i.name, []) for i in lookups}


def attach_lookups(
    manager: Manager[t.Any],
    items: list[t.Any],
    joined: list[dict[str, list[dict[str, t.Any]]]],
    lookups: t.Sequence[Include],
    mode: str,
) -> None:
    for include in lookups:
        info = _relation(manager, include)
        (target_meta,) = manager.meta.ref_targets(include.name)
        target = _target(manager, target_meta.model)
        counting = info.backref is not None and info.backref.count
        partial = None if counting else target.find().select(*(include.select or ()))._projection()[1]
        for item, joins in zip(items, joined, strict=True):
            raws = joins[include.name]
            if counting:
                _attach_backref(item, info, raws[0]["n"] if raws else 0)
                continue
            nested_joined = [pop_lookups(raw, include.nested) for raw in raws] if include.nested else None
            children = [_materialize(target, raw, mode, partial) for raw in raws]
            if nested_joined is not None:
                attach_lookups(target, children, nested_joined, include.nested, mode)
            if info.backref is not None:
                _attach_backref(item, info, children)
            else:
                by_id = {_key(_item_id(c)): c for c in children}
                # A per-parent limit was applied server-side already.
                _attach_forward(item, info, by_id, target_meta.model, None)


# -- batch strategy ----------------------------------------------------------------------


def _child_query(
    target: Manager[t.Any],
    include: Include,
    flt: dict[str, t.Any],
    mode: str,
    session: t.Any,
    extra_select: tuple[str, ...] = (),
) -> Query[t.Any]:
    query = target.find(Trusted(flt), session=session)
    if include.match is not None:
        query = query.filter(include.match)
    if include.select is not None:
        query = query.select(*include.select, *extra_select)
    if include.sort:
        query = query.sort(*include.sort)
    if include.limit is not None:
        query = query.limit(include.limit)
    query = query._clone(mode=mode, includes=include.nested)
    return query


async def load_batched(
    manager: Manager[t.Any], items: list[t.Any], includes: t.Sequence[Include], mode: str, session: t.Any
) -> None:
    """Resolve includes with one query per relation (per target model) for the whole batch."""
    if not items:
        return
    for include in includes:
        info = _relation(manager, include)
        targets = manager.meta.ref_targets(include.name)
        if info.backref is not None:
            await _load_backref(manager, items, include, info, targets[0].model, mode, session)
            continue
        assert info.ref is not None
        ids: dict[t.Any, t.Any] = {}
        for item in items:
            value = item.get(info.stored) if isinstance(item, dict) else getattr(item, info.name)
            if value is None:
                continue
            values = (
                value
                if info.ref.shape == "list"
                else value.values()
                if info.ref.shape == "dict"
                else (value,)
            )
            for v in values:
                if v is not None:
                    ids[_key(v)] = id_of(v)
        by_id: dict[t.Any, t.Any] = {}
        if ids:
            for target_meta in targets:
                target = _target(manager, target_meta.model)
                children = await _child_query(
                    target, include, {"_id": {"$in": list(ids.values())}}, mode, session
                ).all()
                for child in children:
                    by_id[_key(_item_id(child))] = child
        model = targets[0].model if len(targets) == 1 else None
        for item in items:
            _attach_forward(item, info, by_id, model, include.per_parent_limit)


async def _load_backref(
    manager: Manager[t.Any],
    items: list[t.Any],
    include: Include,
    info: FieldInfo,
    model: type,
    mode: str,
    session: t.Any,
) -> None:
    assert info.backref is not None
    target = _target(manager, model)
    fk_attr = info.backref.field
    fk = target.meta.stored_path(fk_attr)
    parent_ids = [_item_id(item) for item in items]
    if info.backref.count:
        pipeline: list[dict[str, t.Any]] = [
            {"$match": {fk: {"$in": parent_ids}}},
            {"$unwind": f"${fk}"},
            {"$match": {fk: {"$in": parent_ids}}},
            {"$group": {"_id": f"${fk}", "n": {"$sum": 1}}},
        ]
        if include.match is not None:
            pipeline.insert(1, {"$match": compile_filter(include.match, target.meta)})
        counts = {_key(row["_id"]): row["n"] async for row in target.aggregate(pipeline, session=session)}
        for item in items:
            _attach_backref(item, info, counts.get(_key(_item_id(item)), 0))
        return
    children = await _child_query(
        target, include, {fk: {"$in": parent_ids}}, mode, session, extra_select=(fk_attr,)
    ).all()
    grouped: dict[t.Any, list[t.Any]] = {}
    for child in children:
        value = child.get(fk) if isinstance(child, dict) else getattr(child, fk_attr)
        for v in value if isinstance(value, list | tuple) else (value,):
            grouped.setdefault(_key(v), []).append(child)
    for item in items:
        found = grouped.get(_key(_item_id(item)), [])
        _attach_backref(
            item, info, found[: include.per_parent_limit] if include.per_parent_limit else list(found)
        )


# -- cascades ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CascadeRule:
    manager: t.Any
    field: FieldInfo
    action: str


def cascade_rules(manager: Manager[t.Any]) -> list[CascadeRule]:
    """Rules declared (with ``OnDelete``) on references that point at ``manager``'s model."""
    rules: list[CascadeRule] = []
    for model in manager.registry.models:
        meta = meta_of(model)
        for name, info in meta.refs.items():
            marker = info.get(OnDelete)
            if marker is None:
                continue
            targets = [m.model for m in meta.ref_targets(name)]
            if not any(
                issubclass(manager.model, target) or issubclass(target, manager.model) for target in targets
            ):
                continue
            assert info.ref is not None
            if marker.action == "pull" and info.ref.shape != "list":
                raise ConfigurationError(
                    f"{model.__name__}.{name}: OnDelete('pull') needs a list of references"
                )
            if marker.action == "nullify" and info.ref.shape != "one":
                raise ConfigurationError(
                    f"{model.__name__}.{name}: OnDelete('nullify') needs a single reference"
                )
            rules.append(CascadeRule(_target(manager, model), info, marker.action))
    return rules


async def apply_cascades(
    manager: Manager[t.Any], rules: list[CascadeRule], ids: list[t.Any], session: t.Any, seen: set[t.Any]
) -> None:
    """Apply ``OnDelete`` rules for documents of ``manager``'s model that are about to be deleted.

    ``seen`` holds every (collection, id) already being deleted in this cascade, so a cycle of
    cascading references ends instead of recursing forever.
    """
    from mongospecs.manager import _chunks

    for rule in rules:
        other: Manager[t.Any] = rule.manager
        stored = rule.field.stored
        for chunk in _chunks(ids):
            flt = {stored: {"$in": chunk}}
            if rule.action == "cascade":
                await other.delete_many(flt, session=session, _seen=seen)
                continue
            ctx = other.ctx("update", session, many=True)
            scoped = other.scoped(ctx, flt, True)
            update: dict[str, t.Any] = (
                {"$set": {stored: None}} if rule.action == "nullify" else {"$pull": {stored: {"$in": chunk}}}
            )
            for plugin in other.plugins:
                plugin.on_update(ctx, scoped, update)
            with other._op("cascade"):
                await other.collection.update_many(scoped, update, **other._kw(session))


# -- $graphLookup ----------------------------------------------------------------------------


@dataclass(slots=True)
class GraphResult(t.Generic[T]):
    """A starting document and everything reachable from it."""

    doc: t.Any
    nodes: list[t.Any]
    """Connected documents, nearest first."""
    depths: list[int]
    """Depth of each node (0 = directly connected)."""


class GraphQuery:
    """``Model.find(...).graph("parent")``: follow a self-reference recursively in one round trip."""

    def __init__(
        self, query: Query[t.Any], field: str, connect_to: str, max_depth: int | None, match: t.Any
    ) -> None:
        self._query = query
        self._field = field
        self._connect_to = connect_to
        self._max_depth = max_depth
        self._match = match

    def compile(self) -> list[dict[str, t.Any]]:
        query = self._query
        manager = query._manager
        meta = manager.meta
        ctx = manager.ctx("find", query._session, many=True)
        connect_from = meta.stored_path(self._field)
        stage: dict[str, t.Any] = {
            "from": meta.collection,
            "startWith": f"${connect_from}",
            "connectFromField": connect_from,
            "connectToField": meta.stored_path(self._connect_to),
            "as": "__graph",
            "depthField": "__depth",
        }
        if self._max_depth is not None:
            stage["maxDepth"] = self._max_depth
        restrict = manager.scoped(
            ctx, compile_filter(self._match, meta) if self._match is not None else None, query._unscoped
        )
        if restrict:
            stage["restrictSearchWithMatch"] = restrict
        pipeline: list[dict[str, t.Any]] = []
        flt = query._filter()
        if flt:
            pipeline.append({"$match": flt})
        sort = query._sort_spec()
        if sort:
            pipeline.append({"$sort": dict(sort)})
        if query._skip:
            pipeline.append({"$skip": query._skip})
        if query._limit:
            pipeline.append({"$limit": query._limit})
        pipeline.append({"$graphLookup": stage})
        return pipeline

    async def all(self) -> list[GraphResult[t.Any]]:
        query = self._query
        manager = query._manager
        mode = query._mode
        results: list[GraphResult[t.Any]] = []
        with manager._op("graph"):
            cursor = await manager.collection.aggregate(self.compile(), **manager._agg_kw(query._session))
            raws = [raw async for raw in cursor]
        for raw in raws:
            nodes_raw = sorted(raw.pop("__graph", []), key=lambda n: n.get("__depth", 0))
            depths = [int(n.pop("__depth", 0)) for n in nodes_raw]
            results.append(
                GraphResult(
                    _materialize(manager, raw, mode, None),
                    [_materialize(manager, n, mode, None) for n in nodes_raw],
                    depths,
                )
            )
        return results

    def __await__(self) -> t.Generator[t.Any, None, list[GraphResult[t.Any]]]:
        return self.all().__await__()


# -- $unionWith --------------------------------------------------------------------------------


class UnionQuery:
    """Several queries read as one result set. Each document is decoded by its own model."""

    def __init__(
        self,
        queries: tuple[Query[t.Any], ...],
        sort: tuple[tuple[str, int], ...] = (),
        skip: int = 0,
        limit: int = 0,
    ) -> None:
        if (
            len({q._manager.db_name for q in queries}) > 1
            or len({id(q._manager.registry) for q in queries}) > 1
        ):
            raise QueryError("union() needs every query to be on the same database")
        self._queries = queries
        self._sort = sort
        self._skip = skip
        self._limit = limit

    def sort(self, *keys: str | tuple[str, int]) -> UnionQuery:
        """Sort the combined results by stored field names (``"-created_at"``)."""
        parsed = tuple(
            (k[1:], -1) if isinstance(k, str) and k.startswith("-") else (k, 1) if isinstance(k, str) else k
            for k in keys
        )
        return UnionQuery(self._queries, (*self._sort, *parsed), self._skip, self._limit)

    def skip(self, n: int) -> UnionQuery:
        return UnionQuery(self._queries, self._sort, n, self._limit)

    def limit(self, n: int) -> UnionQuery:
        return UnionQuery(self._queries, self._sort, self._skip, n)

    @staticmethod
    def _branch(query: Query[t.Any], index: int) -> list[dict[str, t.Any]]:
        if query._includes or query._skip or query._limit:
            raise QueryError(
                "queries inside union() cannot use include, skip or limit; apply them to the union"
            )
        stages: list[dict[str, t.Any]] = []
        flt = query._filter()
        if flt:
            stages.append({"$match": flt})
        projection = query._projection()[0]
        if projection:
            stages.append({"$project": projection})
        stages.append({"$addFields": {"__src": index}})
        return stages

    def compile(self) -> list[dict[str, t.Any]]:
        first, *rest = self._queries
        pipeline = self._branch(first, 0)
        for index, query in enumerate(rest, start=1):
            pipeline.append(
                {
                    "$unionWith": {
                        "coll": query._manager.meta.collection,
                        "pipeline": self._branch(query, index),
                    }
                }
            )
        if self._sort:
            pipeline.append({"$sort": dict(self._sort)})
        if self._skip:
            pipeline.append({"$skip": self._skip})
        if self._limit:
            pipeline.append({"$limit": self._limit})
        return pipeline

    async def __aiter__(self) -> t.AsyncIterator[t.Any]:
        first = self._queries[0]
        manager = first._manager
        with manager._op("union"):
            cursor = await manager.collection.aggregate(self.compile(), **manager._agg_kw(first._session))
        async with cursor:
            async for raw in cursor:
                query = self._queries[raw.pop("__src")]
                yield _materialize(query._manager, raw, query._mode, query._projection()[1])

    async def all(self) -> list[t.Any]:
        return [item async for item in self]

    def __await__(self) -> t.Generator[t.Any, None, list[t.Any]]:
        return self.all().__await__()
