"""Filter expressions.

``Q`` builds typed conditions: ``Q.title == "x"``, ``Q.views > 10``,
``Q.tags.in_(["a", "b"])``, combined with ``&``, ``|`` and ``~`` or with
``And``/``Or``/``Nor``. Conditions are immutable and are compiled to a MongoDB
filter against a model, which is where field names are checked and values are
cast to the field's type.

Plain mappings are accepted as filters too, but they are treated as untrusted:
operator keys (``$gt``, ``$where`` ...) are rejected unless the mapping is
wrapped in :func:`trusted`.
"""

from __future__ import annotations

import re
import typing as t
from dataclasses import dataclass

from mongospecs.errors import QueryError

if t.TYPE_CHECKING:
    from mongospecs.meta import ModelMeta

__all__ = [
    "All",
    "And",
    "Condition",
    "ElemMatch",
    "Exists",
    "Expr",
    "Field",
    "Group",
    "In",
    "Nor",
    "Not",
    "NotIn",
    "Or",
    "Q",
    "Size",
    "SortBy",
    "SortKey",
    "Trusted",
    "Type",
    "compile_filter",
    "compile_sort",
    "trusted",
]

FilterInput = t.Union["Expr", t.Mapping[str, t.Any], None]


class Expr:
    """Base class of filter expressions."""

    __slots__ = ()

    def __and__(self, other: Expr | t.Mapping[str, t.Any]) -> Group:
        return Group("$and", (self, other))

    def __or__(self, other: Expr | t.Mapping[str, t.Any]) -> Group:
        return Group("$or", (self, other))

    def __invert__(self) -> Expr:
        return Group("$nor", (self,))


@dataclass(frozen=True, slots=True, eq=False)
class Condition(Expr):
    """``{path: {op: value}}``. ``path`` uses attribute names; ``None`` targets the element itself."""

    path: str | None
    op: str
    value: t.Any

    def __invert__(self) -> Expr:
        if self.op == "$eq":
            return Condition(self.path, "$ne", self.value)
        if self.op == "$ne":
            return Condition(self.path, "$eq", self.value)
        if self.op == "$in":
            return Condition(self.path, "$nin", self.value)
        if self.op == "$nin":
            return Condition(self.path, "$in", self.value)
        return Condition(self.path, "$not", self)


@dataclass(frozen=True, slots=True, eq=False)
class Group(Expr):
    op: str
    items: tuple[t.Any, ...]


@dataclass(frozen=True, slots=True, eq=False)
class Trusted(Expr):
    """A raw MongoDB filter the application vouches for. Passed through verbatim."""

    filter: t.Mapping[str, t.Any]


def trusted(filter: t.Mapping[str, t.Any]) -> Trusted:
    """Mark a raw filter as written by the application, not built from user input."""
    return Trusted(filter)


@dataclass(frozen=True, slots=True)
class SortKey:
    path: str
    direction: int


def _text(value: t.Any, name: str) -> str:
    if not isinstance(value, str):
        raise QueryError(f"{name}() matches literal text and needs a str, got {type(value).__name__}")
    return value


def _flags(ignore_case: bool) -> str:
    return "i" if ignore_case else ""


def _values(values: t.Any, name: str) -> list[t.Any]:
    if isinstance(values, str | bytes | t.Mapping) or not isinstance(values, t.Iterable):
        raise QueryError(f"{name}() needs a list of values, got {type(values).__name__}")
    return list(values)


class Field:
    """A path into a document, produced by ``Q.name`` or ``Q["name"]``."""

    __slots__ = ("_path",)
    _path: str

    def __init__(self, path: str) -> None:
        object.__setattr__(self, "_path", path)

    def __setattr__(self, name: str, value: t.Any) -> None:
        raise AttributeError("Q fields are immutable")

    def __getattr__(self, name: str) -> Field:
        if name.startswith("__"):
            raise AttributeError(name)
        return Field(f"{self._path}.{name}")

    def __getitem__(self, name: str | int) -> Field:
        return Field(f"{self._path}.{name}")

    __hash__ = None  # type: ignore[assignment]

    def __eq__(self, other: t.Any) -> Condition:  # type: ignore[override]
        return Condition(self._path, "$eq", other)

    def __ne__(self, other: t.Any) -> Condition:  # type: ignore[override]
        return Condition(self._path, "$ne", other)

    def __gt__(self, other: t.Any) -> Condition:
        return Condition(self._path, "$gt", other)

    def __ge__(self, other: t.Any) -> Condition:
        return Condition(self._path, "$gte", other)

    def __lt__(self, other: t.Any) -> Condition:
        return Condition(self._path, "$lt", other)

    def __le__(self, other: t.Any) -> Condition:
        return Condition(self._path, "$lte", other)

    def in_(self, values: t.Iterable[t.Any]) -> Condition:
        return Condition(self._path, "$in", _values(values, "in_"))

    def nin(self, values: t.Iterable[t.Any]) -> Condition:
        return Condition(self._path, "$nin", _values(values, "nin"))

    def all(self, values: t.Iterable[t.Any]) -> Condition:
        return Condition(self._path, "$all", _values(values, "all"))

    def exists(self, value: bool = True) -> Condition:
        return Condition(self._path, "$exists", bool(value))

    def size(self, value: int) -> Condition:
        return Condition(self._path, "$size", int(value))

    def type_(self, value: int | str) -> Condition:
        return Condition(self._path, "$type", value)

    def prefix(self, value: str, *, ignore_case: bool = False) -> Condition:
        """Literal starts-with match (the value is escaped)."""
        return Condition(self._path, "$regex", ("^" + re.escape(_text(value, "prefix")), _flags(ignore_case)))

    def contains(self, value: str, *, ignore_case: bool = False) -> Condition:
        """Literal substring match (the value is escaped)."""
        return Condition(self._path, "$regex", (re.escape(_text(value, "contains")), _flags(ignore_case)))

    def regex(self, pattern: str, options: str = "") -> Condition:
        """Regular-expression match. The pattern is used as written; do not pass user input."""
        return Condition(self._path, "$regex", (pattern, options))

    def elem_match(self, *conditions: Expr | t.Mapping[str, t.Any]) -> Condition:
        return Condition(self._path, "$elemMatch", tuple(conditions))

    def asc(self) -> SortKey:
        return SortKey(self._path, 1)

    def desc(self) -> SortKey:
        return SortKey(self._path, -1)

    def __repr__(self) -> str:
        return f"Q.{self._path}"


class _QMeta(type):
    def __getattr__(cls, name: str) -> Field:
        if name.startswith("__"):
            raise AttributeError(name)
        return Field(name)

    def __getitem__(cls, name: str) -> Field:
        return Field(name)

    # Comparisons on bare ``Q`` target the array element itself, for use inside
    # ``elem_match`` on arrays of scalars: ``Q.scores.elem_match(Q > 80)``.
    def __eq__(cls, other: t.Any) -> Condition:  # type: ignore[override]
        return Condition(None, "$eq", other)

    def __ne__(cls, other: t.Any) -> Condition:  # type: ignore[override]
        return Condition(None, "$ne", other)

    def __gt__(cls, other: t.Any) -> Condition:
        return Condition(None, "$gt", other)

    def __ge__(cls, other: t.Any) -> Condition:
        return Condition(None, "$gte", other)

    def __lt__(cls, other: t.Any) -> Condition:
        return Condition(None, "$lt", other)

    def __le__(cls, other: t.Any) -> Condition:
        return Condition(None, "$lte", other)

    __hash__ = type.__hash__


class Q(metaclass=_QMeta):
    """Entry point for building conditions: ``Q.hit_points > 100``."""


# -- functional helpers (kept from v1) ---------------------------------------


def In(q: Field, values: t.Iterable[t.Any]) -> Condition:
    return q.in_(values)


def NotIn(q: Field, values: t.Iterable[t.Any]) -> Condition:
    return q.nin(values)


def All(q: Field, values: t.Iterable[t.Any]) -> Condition:
    return q.all(values)


def Exists(q: Field, value: bool = True) -> Condition:
    return q.exists(value)


def Size(q: Field, value: int) -> Condition:
    return q.size(value)


def Type(q: Field, value: int | str) -> Condition:
    return q.type_(value)


def ElemMatch(q: Field, *conditions: Expr | t.Mapping[str, t.Any]) -> Condition:
    return q.elem_match(*conditions)


def Not(condition: Expr) -> Expr:
    return ~condition


def And(*conditions: Expr | t.Mapping[str, t.Any]) -> Group:
    return Group("$and", conditions)


def Or(*conditions: Expr | t.Mapping[str, t.Any]) -> Group:
    if not conditions:
        raise QueryError("Or() needs at least one condition")
    return Group("$or", conditions)


def Nor(*conditions: Expr | t.Mapping[str, t.Any]) -> Group:
    if not conditions:
        raise QueryError("Nor() needs at least one condition")
    return Group("$nor", conditions)


SortInput = SortKey | Field | str | tuple[str, int]


def SortBy(*keys: SortInput) -> list[SortKey]:
    """Normalise sort keys: ``SortBy(Q.dob.desc(), Q.name)`` or ``SortBy("-dob", "name")``."""
    out: list[SortKey] = []
    for key in keys:
        if isinstance(key, SortKey):
            out.append(key)
        elif isinstance(key, Field):
            out.append(SortKey(key._path, 1))
        elif isinstance(key, str):
            out.append(SortKey(key[1:], -1) if key.startswith("-") else SortKey(key.lstrip("+"), 1))
        elif isinstance(key, tuple) and len(key) == 2:
            out.append(SortKey(key[0], -1 if key[1] in (-1, "desc") else 1))
        else:
            raise QueryError(f"cannot sort by {key!r}")
    return out


# -- compilation --------------------------------------------------------------

_VALUE_OPS = frozenset({"$eq", "$ne", "$gt", "$gte", "$lt", "$lte"})
_LIST_OPS = frozenset({"$in", "$nin", "$all"})


def _has_operator(value: t.Any) -> bool:
    if isinstance(value, t.Mapping):
        return any(isinstance(k, str) and k.startswith("$") for k in value) or any(
            _has_operator(v) for v in value.values()
        )
    if isinstance(value, list | tuple):
        return any(_has_operator(v) for v in value)
    return False


def _literal(value: t.Any) -> t.Any:
    """A compiled value can never be read as an operator document."""
    if _has_operator(value):
        raise QueryError(
            "filter values may not contain MongoDB operators; build the condition with Q "
            "or wrap a raw filter in trusted()"
        )
    return value


def _compile_condition(cond: Condition, meta: ModelMeta | None) -> dict[str, t.Any]:
    op, value = cond.op, cond.value
    path = cond.path
    stored = meta.stored_path(path) if (meta is not None and path is not None) else path

    if op == "$not":
        inner = _compile_condition(t.cast(Condition, value), meta)
        if stored is None:
            return {"$not": inner}
        body = inner[stored]
        if not isinstance(body, dict) or not any(k.startswith("$") for k in body):
            body = {"$eq": body}
        return {stored: {"$not": body}}

    if op in _VALUE_OPS:
        compiled = _literal(
            meta.cast(path, value) if (meta is not None and path is not None) else _plain(value)
        )
    elif op in _LIST_OPS:
        if isinstance(value, str | bytes) or not isinstance(value, t.Iterable):
            raise QueryError(f"{op} needs a list of values")
        compiled = [
            _literal(meta.cast(path, v) if (meta is not None and path is not None) else _plain(v))
            for v in value
        ]
    elif op == "$regex":
        pattern, options = value
        if not isinstance(pattern, str):
            raise QueryError("a regex pattern must be a string")
        body = {"$regex": pattern, **({"$options": options} if options else {})}
        return body if stored is None else {stored: body}
    elif op == "$elemMatch":
        inner_meta = None  # element shape is not modelled; conditions inside are not cast
        merged: dict[str, t.Any] = {}
        for item in value:
            for k, v in compile_filter(item, inner_meta).items():
                if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
        compiled = merged
    elif op in ("$exists", "$size", "$type"):
        compiled = value
    else:
        raise QueryError(f"unsupported operator {op}")

    if stored is None:
        return {op: compiled}
    if op == "$eq":
        if isinstance(compiled, dict):
            return {stored: {"$eq": compiled}}
        return {stored: compiled}
    return {stored: {op: compiled}}


def _plain(value: t.Any) -> t.Any:
    from mongospecs.codec import to_bson
    from mongospecs.refs import id_of

    return to_bson(id_of(value))


def compile_filter(expr: t.Any, meta: ModelMeta | None = None) -> dict[str, t.Any]:
    """Compile an expression (or an untrusted mapping) to a MongoDB filter."""
    if expr is None:
        return {}
    if isinstance(expr, Trusted):
        return dict(expr.filter)
    if isinstance(expr, Condition):
        return _compile_condition(expr, meta)
    if isinstance(expr, Group):
        parts = [compile_filter(item, meta) for item in expr.items]
        if expr.op == "$and":
            flat: list[dict[str, t.Any]] = []
            for part in parts:
                if set(part) == {"$and"}:
                    flat.extend(part["$and"])
                elif part:
                    flat.append(part)
            return and_filters(*flat)
        return {expr.op: parts}
    if isinstance(expr, t.Mapping):
        out: dict[str, t.Any] = {}
        for key, value in expr.items():
            if not isinstance(key, str) or key.startswith("$"):
                raise QueryError(
                    f"raw filter key {key!r} is a MongoDB operator; build the condition with Q "
                    "or wrap the filter in trusted()"
                )
            out.update(_compile_condition(Condition(key, "$eq", value), meta))
        return out
    if isinstance(expr, Field):
        raise QueryError(f"{expr!r} is a field, not a condition; compare it to a value")
    raise QueryError(f"cannot use {type(expr).__name__} as a filter")


def and_filters(*filters: t.Mapping[str, t.Any] | None) -> dict[str, t.Any]:
    """AND filters together, merging when keys do not collide."""
    parts = [dict(f) for f in filters if f]
    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]
    merged: dict[str, t.Any] = {}
    for part in parts:
        if any(k in merged for k in part):
            return {"$and": parts}
        merged.update(part)
    return merged


def compile_sort(keys: t.Iterable[SortInput], meta: ModelMeta | None = None) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for key in SortBy(*keys):
        if not isinstance(key.path, str) or any(
            not part or part.startswith("$") for part in key.path.split(".")
        ):
            raise QueryError(f"cannot sort by {key.path!r}: not a field path")
        out.append((meta.stored_path(key.path) if meta is not None else key.path, key.direction))
    return out
