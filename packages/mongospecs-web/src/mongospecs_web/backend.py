"""The narrow contract between a Resource and whatever stores its documents.

``MongoBackend`` implements it with mongospecs. ``mongospecs_web.testing.FakeBackend``
is an in-memory double used by the plug contract suite.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

from mongospecs import DocumentNotFound, Page, Q, QueryError, meta_of
from mongospecs.expr import Condition, Expr, Field

__all__ = ["OPERATORS", "Filter", "ListQuery", "MongoBackend", "ResourceBackend"]

T = t.TypeVar("T")

OPERATORS: dict[str, str] = {
    "eq": "$eq",
    "ne": "$ne",
    "in": "$in",
    "nin": "$nin",
    "gt": "$gt",
    "gte": "$gte",
    "lt": "$lt",
    "lte": "$lte",
    "exists": "$exists",
    "prefix": "$regex",
}


@dataclass(frozen=True, slots=True)
class Filter:
    """One allowlisted condition from the client: field, bounded operator, already-cast value."""

    field: str
    op: str
    value: t.Any

    def to_expr(self) -> Expr:
        f = Field(self.field)
        if self.op == "prefix":
            return f.prefix(self.value)
        return Condition(self.field, OPERATORS[self.op], self.value)


@dataclass(frozen=True, slots=True)
class ListQuery:
    filters: tuple[Filter, ...] = ()
    sort: tuple[tuple[str, int], ...] = ()
    size: int = 20
    after: str | None = None
    number: int | None = None
    total: bool = False
    include: tuple[str, ...] = field(default=())


class ResourceBackend(t.Protocol[T]):
    """What a Resource needs from storage. ``scope`` is a filter ANDed into the operation, or ``None``."""

    model: type[T]

    async def get(self, id: t.Any, *, scope: t.Any, include: t.Sequence[str] = ()) -> T | None: ...

    async def list(self, query: ListQuery, *, scope: t.Any) -> Page[T]: ...

    async def create(self, data: dict[str, t.Any]) -> T: ...

    async def patch(
        self, id: t.Any, data: dict[str, t.Any], *, scope: t.Any, if_rev: int | None = None
    ) -> T: ...

    async def delete(self, id: t.Any, *, scope: t.Any) -> None: ...


class MongoBackend(t.Generic[T]):
    """``ResourceBackend`` over a mongospecs model (or a manager from ``Model.using(...)``)."""

    def __init__(self, model: type[T], manager: t.Any = None) -> None:
        self.model = model
        self._manager = manager
        self.meta = meta_of(model)

    @property
    def manager(self) -> t.Any:
        return self._manager if self._manager is not None else t.cast(t.Any, self.model)._manager()

    def _by_id(self, id: t.Any, scope: t.Any) -> t.Any:
        try:
            query = self.manager.find(Q.id == id)
            query._user_filter()  # cast the id now: a malformed id is "not found", not a server error
        except QueryError:
            return None
        return query.filter(scope) if scope is not None else query

    async def get(self, id: t.Any, *, scope: t.Any, include: t.Sequence[str] = ()) -> T | None:
        query = self._by_id(id, scope)
        if query is None:
            return None
        for path in include:
            query = query.include(path)
        return t.cast("T | None", await query.first())

    async def list(self, query: ListQuery, *, scope: t.Any) -> Page[T]:
        q = self.manager.find(*(f.to_expr() for f in query.filters))
        if scope is not None:
            q = q.filter(scope)
        if query.sort:
            q = q.sort(*query.sort)
        for path in query.include:
            q = q.include(path)
        return t.cast(
            "Page[T]", await q.page(query.size, after=query.after, number=query.number, total=query.total)
        )

    async def create(self, data: dict[str, t.Any]) -> T:
        stored = {self.meta.fields[k].stored: v for k, v in data.items()}
        doc = self.meta.decode(stored, stored=False)
        return t.cast(T, await self.manager.insert(doc))

    async def patch(self, id: t.Any, data: dict[str, t.Any], *, scope: t.Any, if_rev: int | None = None) -> T:
        query = self._by_id(id, scope)
        if query is None:
            raise DocumentNotFound(f"{self.model.__name__} {id!r} does not exist")
        if not data:
            doc = await query.first()
            if doc is None:
                raise DocumentNotFound(f"{self.model.__name__} {id!r} does not exist")
            return t.cast(T, doc)
        from mongospecs import trusted

        target = trusted(query._user_filter())
        return t.cast(T, await self.manager.update(target, if_rev=if_rev, **data))

    async def delete(self, id: t.Any, *, scope: t.Any) -> None:
        query = self._by_id(id, scope)
        doc = await query.first() if query is not None else None
        if doc is None:
            raise DocumentNotFound(f"{self.model.__name__} {id!r} does not exist")
        await self.manager.delete_document(doc)
