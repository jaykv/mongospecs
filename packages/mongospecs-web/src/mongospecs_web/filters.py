"""Query-string parsing for list operations.

Only what the model's ``Policy`` allows gets through: a filter must name a
filterable field and one of the operators that field declares; a sort key must
be sortable. Everything else is a 400, never a pass-through to MongoDB.

Syntax::

    ?title=hello                 equality
    ?views[gte]=10&views[lt]=50  operators in brackets
    ?tag[in]=a,b                 lists are comma-separated (or repeat the parameter)
    ?email[exists]=false
    ?sort=-views,title
    ?limit=20&after=<cursor>     cursor pagination
    ?limit=20&page=3             offset pagination
    ?total=true                  also count all matches
    ?include=author,comments     load allowlisted relations
"""

from __future__ import annotations

import re
import typing as t
from dataclasses import dataclass

from mongospecs import QueryError
from mongospecs_web.backend import Filter, ListQuery
from mongospecs_web.errors import BadRequest
from mongospecs_web.policy import View

__all__ = ["Cursor", "Offset", "Pagination", "parse_list_query"]

RESERVED = frozenset({"sort", "limit", "after", "page", "total", "include"})
_PARAM = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[([a-z]+)\])?$")
_TRUE = frozenset({"true", "1", "yes"})
_FALSE = frozenset({"false", "0", "no"})


@dataclass(frozen=True, slots=True)
class Pagination:
    default: int = 20
    max: int = 100
    total: bool = True
    """Whether clients may ask for a total count with ``?total=true``."""
    kind: t.ClassVar[str] = "cursor"


class Cursor(Pagination):
    """Keyset pagination: ``?limit=&after=``. Stable under concurrent writes."""

    kind = "cursor"


class Offset(Pagination):
    """Page-number pagination: ``?limit=&page=``."""

    kind = "offset"


def _bool(value: str, name: str) -> bool:
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise BadRequest(f"{name} must be true or false")


def _int(value: str, name: str, minimum: int = 1) -> int:
    try:
        number = int(value)
    except ValueError:
        raise BadRequest(f"{name} must be an integer") from None
    if number < minimum:
        raise BadRequest(f"{name} must be at least {minimum}")
    return number


def parse_list_query(
    params: t.Iterable[tuple[str, str]],
    view: View,
    pagination: Pagination,
    includes: t.Collection[str] = (),
) -> ListQuery:
    """Turn raw query parameters into a validated :class:`ListQuery`."""
    grouped: dict[str, list[str]] = {}
    for key, value in params:
        grouped.setdefault(key, []).append(value)

    filters: list[Filter] = []
    for key, values in grouped.items():
        if key in RESERVED:
            continue
        match = _PARAM.match(key)
        if match is None:
            raise BadRequest(f"unrecognised query parameter {key!r}")
        name, op = match.group(1), match.group(2) or "eq"
        allowed = view.filterable.get(name)
        if allowed is None:
            raise BadRequest(f"{name!r} cannot be used as a filter")
        if op not in allowed:
            raise BadRequest(f"operator {op!r} is not allowed on {name!r}; allowed: {', '.join(allowed)}")
        filters.append(Filter(name, op, _value(view, name, op, values)))

    sort: list[tuple[str, int]] = []
    for item in ",".join(grouped.get("sort", [])).split(","):
        item = item.strip()
        if not item:
            continue
        direction = -1 if item.startswith("-") else 1
        name = item.lstrip("+-")
        if name not in view.sortable:
            raise BadRequest(f"{name!r} cannot be used for sorting")
        sort.append((name, direction))

    size = pagination.default
    if "limit" in grouped:
        size = min(_int(grouped["limit"][-1], "limit"), pagination.max)
    after = grouped.get("after", [None])[-1]
    number: int | None = None
    if pagination.kind == "offset":
        if after is not None:
            raise BadRequest("this resource uses page numbers; use page= instead of after=")
        number = _int(grouped["page"][-1], "page") if "page" in grouped else 1
    elif "page" in grouped:
        raise BadRequest("this resource uses cursors; use after= instead of page=")
    total = _bool(grouped["total"][-1], "total") if "total" in grouped else False
    if total and not pagination.total:
        raise BadRequest("this resource does not report totals")

    include: list[str] = []
    for item in ",".join(grouped.get("include", [])).split(","):
        item = item.strip()
        if not item:
            continue
        if item not in includes:
            raise BadRequest(f"{item!r} cannot be included")
        include.append(item)

    return ListQuery(tuple(filters), tuple(sort), size, after, number, total, tuple(include))


def _value(view: View, name: str, op: str, values: list[str]) -> t.Any:
    meta = view.meta
    if op == "exists":
        return _bool(values[-1], f"{name}[exists]")
    if op == "prefix":
        return values[-1]
    try:
        if op in ("in", "nin"):
            items = [v for raw in values for v in raw.split(",") if v != ""]
            return [meta.cast(name, v, lax=True) for v in items]
        return meta.cast(name, values[-1], lax=True)
    except QueryError as exc:
        raise BadRequest(f"invalid value for {name!r}: {exc}") from None


def parse_includes(params: t.Iterable[tuple[str, str]], includes: t.Collection[str]) -> tuple[str, ...]:
    """The ``include`` parameter of a single-resource read."""
    out: list[str] = []
    for key, value in params:
        if key != "include":
            continue
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            if item not in includes:
                raise BadRequest(f"{item!r} cannot be included")
            out.append(item)
    return tuple(out)
