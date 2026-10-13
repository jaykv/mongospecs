"""Typed atomic update operators.

::

    await Article.update(article_id, Inc(views=1), Push(tags="new"), title="Renamed")

Keyword arguments are shorthand for ``Set``. Every supplied field is checked
against the model: unknown fields raise, ``Immutable`` fields are rejected and
values are validated with the model's own rules. There is no opt-in step
(Mongoose's ``runValidators``): updates are always validated.
"""

from __future__ import annotations

import decimal
import typing as t

from mongospecs.codec import element_type, to_bson
from mongospecs.errors import QueryError, ValidationError
from mongospecs.expr import Expr, compile_filter
from mongospecs.refs import id_of

if t.TYPE_CHECKING:
    from mongospecs.meta import ModelMeta

__all__ = [
    "AddToSet",
    "CurrentDate",
    "Each",
    "Inc",
    "Max",
    "Min",
    "Mul",
    "Pop",
    "Pull",
    "Push",
    "Set",
    "SetOnInsert",
    "Unset",
    "UpdateOp",
    "compile_update",
]


class UpdateOp:
    """Base class of update operators."""

    op: t.ClassVar[str]
    __slots__ = ("fields",)

    def __init__(self, _fields: t.Mapping[str, t.Any] | None = None, /, **fields: t.Any) -> None:
        self.fields: dict[str, t.Any] = {**(_fields or {}), **fields}

    def compile(self, meta: ModelMeta) -> dict[str, t.Any]:
        return {self._path(meta, k): self._value(meta, k, v) for k, v in self.fields.items()}

    def _path(self, meta: ModelMeta, name: str) -> str:
        _check_writable(meta, name, self.op)
        return meta.stored_path(name)

    def _value(self, meta: ModelMeta, name: str, value: t.Any) -> t.Any:
        return to_bson(id_of(value))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.fields!r})"


def _check_writable(meta: ModelMeta, name: str, op: str) -> None:
    head = name.partition(".")[0]
    info = meta.fields.get(head)
    if info is None:
        raise QueryError(f"{meta.model.__name__} has no field {head!r}")
    if head == "id":
        raise QueryError("the id of a document cannot be updated")
    if op != "$setOnInsert" and head in meta.immutable:
        raise QueryError(f"{meta.model.__name__}.{head} is immutable")


def _validated(meta: ModelMeta, name: str, value: t.Any) -> t.Any:
    info = meta.fields.get(name)
    if info is None:  # dotted path into a nested document
        return to_bson(id_of(value))
    if info.ref is not None:
        if value is None:
            return None
        if info.ref.shape == "list":
            return [meta._cast_ref(info, v) for v in value]
        if info.ref.shape == "dict":
            return {k: meta._cast_ref(info, v) for k, v in value.items()}
        return meta._cast_ref(info, value)
    return to_bson(meta.codec.validate_field(meta.model, name, value))


class Set(UpdateOp):
    op = "$set"
    __slots__ = ()

    def _value(self, meta: ModelMeta, name: str, value: t.Any) -> t.Any:
        return _validated(meta, name, value)


class SetOnInsert(Set):
    op = "$setOnInsert"
    __slots__ = ()


class Min(Set):
    op = "$min"
    __slots__ = ()


class Max(Set):
    op = "$max"
    __slots__ = ()


class Unset(UpdateOp):
    op = "$unset"
    __slots__ = ()

    def __init__(self, *names: str) -> None:
        super().__init__({name: "" for name in names})

    def _path(self, meta: ModelMeta, name: str) -> str:
        path = super()._path(meta, name)
        info = meta.fields.get(name)
        if info is not None and info.required:
            raise QueryError(f"{meta.model.__name__}.{name} is required and cannot be unset")
        return path

    def _value(self, meta: ModelMeta, name: str, value: t.Any) -> t.Any:
        return ""


class _Numeric(UpdateOp):
    __slots__ = ()

    def _value(self, meta: ModelMeta, name: str, value: t.Any) -> t.Any:
        if isinstance(value, bool) or not isinstance(value, int | float | decimal.Decimal):
            raise QueryError(f"{self.op} on {name!r} needs a number, got {type(value).__name__}")
        return value


class Inc(_Numeric):
    op = "$inc"
    __slots__ = ()


class Mul(_Numeric):
    op = "$mul"
    __slots__ = ()


class Each:
    """Several values for ``Push``/``AddToSet``, with optional ``$push`` modifiers."""

    __slots__ = ("modifiers", "values")

    def __init__(
        self,
        values: t.Iterable[t.Any],
        *,
        position: int | None = None,
        slice: int | None = None,
        sort: t.Any = None,
    ) -> None:
        self.values = list(values)
        self.modifiers: dict[str, t.Any] = {}
        if position is not None:
            self.modifiers["$position"] = position
        if slice is not None:
            self.modifiers["$slice"] = slice
        if sort is not None:
            self.modifiers["$sort"] = sort


def _element(meta: ModelMeta, name: str, value: t.Any) -> t.Any:
    info = meta.fields.get(name)
    if info is None:
        return to_bson(id_of(value))
    if info.ref is not None:
        return meta._cast_ref(info, value)
    elem = element_type(info.type)
    if elem is None:
        raise QueryError(f"{meta.model.__name__}.{name} is not a list field")
    try:
        return to_bson(meta.validate_type(elem, value))
    except ValidationError as exc:
        raise ValidationError(
            f"{meta.model.__name__}.{name}: {exc}", [{"field": name, "message": str(exc)}]
        ) from exc


class Push(UpdateOp):
    op = "$push"
    __slots__ = ()

    def _value(self, meta: ModelMeta, name: str, value: t.Any) -> t.Any:
        if isinstance(value, Each):
            return {"$each": [_element(meta, name, v) for v in value.values], **value.modifiers}
        return _element(meta, name, value)


class AddToSet(Push):
    op = "$addToSet"
    __slots__ = ()

    def _value(self, meta: ModelMeta, name: str, value: t.Any) -> t.Any:
        if isinstance(value, Each):
            return {"$each": [_element(meta, name, v) for v in value.values]}
        return _element(meta, name, value)


class Pull(UpdateOp):
    """Remove matching elements. The value is an element, or a ``Q`` condition on elements."""

    op = "$pull"
    __slots__ = ()

    def _value(self, meta: ModelMeta, name: str, value: t.Any) -> t.Any:
        if isinstance(value, Expr):
            return compile_filter(value, None)
        return _element(meta, name, value)


class Pop(UpdateOp):
    """Remove the last (``1``) or first (``-1``) element."""

    op = "$pop"
    __slots__ = ()

    def _value(self, meta: ModelMeta, name: str, value: t.Any) -> t.Any:
        if value not in (1, -1):
            raise QueryError("Pop takes 1 (last element) or -1 (first element)")
        return value


class CurrentDate(UpdateOp):
    op = "$currentDate"
    __slots__ = ()

    def __init__(self, *names: str) -> None:
        super().__init__({name: True for name in names})

    def _value(self, meta: ModelMeta, name: str, value: t.Any) -> t.Any:
        return True


def compile_update(
    meta: ModelMeta, ops: t.Iterable[UpdateOp], fields: t.Mapping[str, t.Any]
) -> dict[str, t.Any]:
    """Compile operators (plus ``Set`` shorthand keywords) to a MongoDB update document."""
    update: dict[str, dict[str, t.Any]] = {}
    seen: dict[str, str] = {}
    all_ops = [*ops, *([Set(fields)] if fields else [])]
    for op in all_ops:
        if not isinstance(op, UpdateOp):
            raise QueryError(f"expected an update operator, got {type(op).__name__}")
        for path, value in op.compile(meta).items():
            if path in seen:
                raise QueryError(f"field {path!r} is updated by both {seen[path]} and {op.op}")
            seen[path] = op.op
            update.setdefault(op.op, {})[path] = value
    return update
