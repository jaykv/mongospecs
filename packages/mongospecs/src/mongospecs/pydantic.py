"""pydantic backend: ``from mongospecs.pydantic import Spec``."""

from __future__ import annotations

import inspect
import typing as t
from typing import ClassVar

import pydantic
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from mongospecs.codec import Codec, RawField, field_error
from mongospecs.errors import ValidationError
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
            return t.cast("dict[str, t.Any]", obj.model_dump(by_alias=True, exclude_computed_fields=True))
        doc: dict[str, t.Any] = obj.model_dump(by_alias=True)
        for name in type(obj).model_computed_fields:
            doc.pop(name, None)
        return doc

    def validate_field(self, cls: type, name: str, value: t.Any) -> t.Any:
        model = t.cast("type[BaseModel]", cls)
        try:
            # Runs the model's own field validators for this one field.
            blank = model.model_construct()
            return getattr(model.__pydantic_validator__.validate_assignment(blank, name, value), name)
        except pydantic.ValidationError as exc:
            raise _error(cls, exc, name) from exc
        except Exception:
            # A model-level validator could not run on a partial instance; fall
            # back to the field's type and constraints alone.
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
