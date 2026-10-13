"""pydantic backend: ``from mongospecs.pydantic import Spec``."""

from __future__ import annotations

import inspect
import typing as t
from typing import ClassVar

import pydantic
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from mongospecs.codec import _UNION_ORIGINS, Codec, RawField, field_error
from mongospecs.errors import ValidationError
from mongospecs.jsonable import jsonable
from mongospecs.markers import SpecConfig
from mongospecs.model import Document
from mongospecs.refs import OID, Ref

__all__ = ["CODEC", "PydanticCodec", "Spec", "SpecAdapter"]

_HAS_EXCLUDE_COMPUTED = "exclude_computed_fields" in inspect.signature(BaseModel.model_dump).parameters


def _error(cls: type, exc: pydantic.ValidationError, name: str | None = None) -> ValidationError:
    errors = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = err.get("loc") or ()
        errors.append({"field": name or (str(loc[0]) if loc else None), "message": err.get("msg", "")})
    first = errors[0] if errors else {"field": name, "message": str(exc)}
    out = field_error(cls, first["field"], str(first["message"]))
    out.errors = errors or out.errors
    return out


def _annotation(info: t.Any) -> t.Any:
    """Field annotation with the non-pydantic ``Annotated`` metadata put back."""
    if info.metadata:
        return t.Annotated[(info.annotation, *info.metadata)]
    return info.annotation


def _has_untyped(tp: t.Any, seen: set[type]) -> bool:
    while t.get_origin(tp) is t.Annotated:
        tp = t.get_args(tp)[0]
    if tp is t.Any or tp is object or tp in (dict, list, set, tuple, frozenset):
        return True
    if isinstance(tp, type) and issubclass(tp, BaseModel) and not issubclass(tp, Document):
        if tp in seen:
            return False
        seen.add(tp)
        return any(_has_untyped(f.annotation, seen) for f in tp.model_fields.values())
    if t.get_origin(tp) is Ref:
        return False
    return any(_has_untyped(arg, seen) for arg in t.get_args(tp))


class _Omit:
    """Which keys of an embedded model are left out while None, and where its own embedded models are."""

    __slots__ = ("absent", "children", "elem")

    def __init__(self) -> None:
        self.absent: frozenset[str] = frozenset()
        self.children: dict[str, _Omit] = {}
        self.elem: _Omit | None = None

    def active(self, seen: set[int] | None = None) -> bool:
        seen = set() if seen is None else seen
        if id(self) in seen:
            return False
        seen.add(id(self))
        return (
            bool(self.absent)
            or any(c.active(seen) for c in self.children.values())
            or (self.elem is not None and self.elem.active(seen))
        )


def _drop_nones(value: t.Any, plan: _Omit) -> None:
    if isinstance(value, dict):
        if plan.elem is not None:
            for item in value.values():
                _drop_nones(item, plan.elem)
            return
        for key in plan.absent:
            if key in value and value[key] is None:
                del value[key]
        for key, child in plan.children.items():
            if key in value:
                _drop_nones(value[key], child)
    elif isinstance(value, list) and plan.elem is not None:
        for item in value:
            _drop_nones(item, plan.elem)


class PydanticCodec(Codec):
    name = "pydantic"

    def __init__(self) -> None:
        super().__init__()
        self._adapters: dict[tuple[type, str], TypeAdapter[t.Any]] = {}

    def raw_fields(self, cls: type) -> list[RawField]:
        model = t.cast("type[BaseModel]", cls)
        return [
            (name, info.alias or name, _annotation(info), info.is_required())
            for name, info in model.model_fields.items()
        ]

    def _decode(self, cls: type, doc: dict[str, t.Any], lax: bool) -> t.Any:
        try:
            return t.cast("type[BaseModel]", cls).model_validate(doc)
        except pydantic.ValidationError as exc:
            raise _error(cls, exc) from exc

    def _encode(self, obj: t.Any) -> dict[str, t.Any]:
        if _HAS_EXCLUDE_COMPUTED:
            doc: dict[str, t.Any] = obj.model_dump(by_alias=True, exclude_computed_fields=True)
        else:
            doc = obj.model_dump(by_alias=True)
            for name in type(obj).model_computed_fields:
                doc.pop(name, None)
        # model_dump also dumps embedded models, so the "None is not stored" rule of each
        # embedded model has to be applied here; the other backends encode them one by one.
        for key, plan in self._embedded_omits(type(obj)):
            if key in doc:
                _drop_nones(doc[key], plan)
        return doc

    def _embedded_omits(self, cls: type) -> tuple[tuple[str, _Omit], ...]:
        cache: dict[type, tuple[tuple[str, _Omit], ...]] = self.__dict__.setdefault("_eo", {})
        hit = cache.get(cls)
        if hit is None:
            plans: dict[type, _Omit] = {}
            natives = {name: native for name, native, _, _ in self.raw_fields(cls)}
            hit = tuple(
                (natives[name], plan)
                for name, info in self.fields(cls).items()
                if (plan := self._omit_plan(info.type, plans)) is not None and plan.active()
            )
            cache[cls] = hit
        return hit

    def _omit_plan(self, tp: t.Any, plans: dict[type, _Omit]) -> _Omit | None:
        while t.get_origin(tp) is t.Annotated:
            tp = t.get_args(tp)[0]
        if t.get_origin(tp) is None:
            if not (isinstance(tp, type) and issubclass(tp, BaseModel)) or issubclass(tp, Document):
                return None
            plan = plans.get(tp)
            if plan is None:
                plan = plans[tp] = _Omit()
                plan.absent = self.absent_when_none(tp)
                for name, _native, annotation, _ in self.raw_fields(tp):
                    child = self._omit_plan(annotation, plans)
                    if child is not None:
                        plan.children[self.fields(tp)[name].stored] = child
            return plan
        parts = [p for arg in t.get_args(tp) if (p := self._omit_plan(arg, plans)) is not None]
        if not parts:
            return None
        if t.get_origin(tp) in _UNION_ORIGINS:
            return parts[0]
        container = _Omit()
        container.elem = parts[-1]
        return container

    def validate_field(self, cls: type, name: str, value: t.Any) -> t.Any:
        model = t.cast("type[BaseModel]", cls)
        # Runs the model's own field validators for this one field. The instance it is assigned
        # to is blank: every other field holds its default, not the stored value.
        blank = model.model_construct()
        blank.__dict__.pop(name, None)
        try:
            return getattr(model.__pydantic_validator__.validate_assignment(blank, name, value), name)
        except Exception as exc:
            if isinstance(exc, pydantic.ValidationError) and any(e.get("loc") for e in exc.errors()):
                raise _error(cls, exc, name) from exc  # the field itself is wrong
            # A model-level validator rejected, or could not run on, the blank instance. It
            # compares fields, and the other fields here are defaults, so its verdict says
            # nothing about the document being updated. The field was validated before it ran.
            if name in blank.__dict__:
                return blank.__dict__[name]
            adapter = self._adapters.get((cls, name))
            if adapter is None:
                adapter = TypeAdapter(self.fields(cls)[name].type, config=_adapter_config(model))
                self._adapters[(cls, name)] = adapter
            try:
                return adapter.validate_python(value)
            except pydantic.ValidationError as exc:
                raise _error(cls, exc, name) from exc

    def _json_schema(self, cls: type) -> dict[str, t.Any]:
        return t.cast("type[BaseModel]", cls).model_json_schema(by_alias=False)

    def to_json(self, obj: t.Any) -> dict[str, t.Any]:
        cls = type(obj)
        loose = self._untyped(cls)
        options: dict[str, t.Any] = {"exclude": loose} if loose else {}
        if _HAS_EXCLUDE_COMPUTED:
            options["exclude_computed_fields"] = True
        data: dict[str, t.Any] = obj.model_dump(mode="json", **options)
        if not _HAS_EXCLUDE_COMPUTED:
            for name in cls.model_computed_fields:
                data.pop(name, None)
        if loose:
            # pydantic cannot serialise the BSON-only values an untyped field may hold (an
            # ObjectId raises, bytes are read as UTF-8). Those fields are converted by the same
            # rules the other backends use, so all three give the same JSON.
            plain = obj.model_dump(mode="python", include=loose)
            data = {
                name: jsonable(plain[name]) if name in loose else data[name]
                for name in cls.model_fields
                if name in loose or name in data
            }
        return data

    def _untyped(self, cls: type) -> set[str]:
        """Fields whose type leaves some value untyped (``Any``, a bare ``dict`` or ``list``),
        directly or inside an embedded model."""
        cache: dict[type, set[str]] = self.__dict__.setdefault("_ut", {})
        hit = cache.get(cls)
        if hit is None:
            hit = cache[cls] = {
                name
                for name, _native, annotation, _ in self.raw_fields(cls)
                if _has_untyped(annotation, set())
            }
        return hit

    def defaults(self, cls: type) -> dict[str, t.Any]:
        fields = t.cast("type[BaseModel]", cls).model_fields
        return {
            name: info.get_default(call_default_factory=True)
            for name, info in fields.items()
            if not info.is_required()
        }

    def clear_cache(self) -> None:
        super().clear_cache()
        self._adapters.clear()


def _adapter_config(model: type[BaseModel]) -> ConfigDict | None:
    return (
        ConfigDict(arbitrary_types_allowed=True)
        if model.model_config.get("arbitrary_types_allowed")
        else None
    )


CODEC = PydanticCodec()


class Spec(BaseModel, Document):
    """Base class for pydantic documents."""

    model_config = ConfigDict(populate_by_name=True, validate_assignment=False)

    __codec__: ClassVar[Codec] = CODEC

    id: OID | None = Field(default=None, alias="_id")


def SpecAdapter(cls: type[BaseModel], **config: t.Any) -> t.Any:
    """Turn an existing BaseModel into a document class.

    Keyword arguments are :class:`~mongospecs.SpecConfig` fields, for example
    ``SpecAdapter(Dragon, collection="dragons")``.
    """

    if issubclass(cls, Document):
        return cls

    class Adapted(cls, Spec):  # type: ignore[misc, valid-type]
        __spec__ = SpecConfig(**{"collection": cls.__name__, **config})

    Adapted.__name__ = Adapted.__qualname__ = cls.__name__
    Adapted.__doc__ = cls.__doc__
    return Adapted


# Ref is generic over the target model; pydantic needs to see it resolved here.
_ = Ref
