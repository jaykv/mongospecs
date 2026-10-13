"""attrs backend: ``from mongospecs.attrs import Spec``.

attrs has no decoding or validation of its own, so documents are converted with
msgspec, which supports attrs classes and runs their validators.
"""

from __future__ import annotations

import typing as t
from typing import ClassVar

import attrs
import msgspec

from mongospecs.codec import Codec, RawField, field_error, to_bson
from mongospecs.errors import ValidationError
from mongospecs.markers import SpecConfig
from mongospecs.model import Document
from mongospecs.msgspec import _error, dec_hook, schema_hook
from mongospecs.refs import OID

__all__ = ["CODEC", "AttrsCodec", "Spec", "SpecAdapter"]


def encode_attrs(obj: t.Any) -> dict[str, t.Any]:
    return {a.name: to_bson(getattr(obj, a.name)) for a in attrs.fields(type(obj))}


class AttrsCodec(Codec):
    name = "attrs"

    def raw_fields(self, cls: type) -> list[RawField]:
        hints = t.get_type_hints(cls, include_extras=True)
        return [
            (a.name, a.name, hints.get(a.name, a.type or t.Any), a.default is attrs.NOTHING)
            for a in attrs.fields(cls)
        ]

    def _decode(self, cls: type, doc: dict[str, t.Any], trusted: bool) -> t.Any:
        try:
            return msgspec.convert(doc, cls, dec_hook=dec_hook)
        except (msgspec.ValidationError, ValueError) as exc:
            raise _error(cls, exc) from exc

    def _encode(self, obj: t.Any) -> dict[str, t.Any]:
        return encode_attrs(obj)

    def validate_field(self, cls: type, name: str, value: t.Any) -> t.Any:
        attribute = getattr(attrs.fields(cls), name)
        try:
            if attribute.converter is not None:
                value = attribute.converter(value)
            value = msgspec.convert(value, self.fields(cls)[name].type, dec_hook=dec_hook)
            if attribute.validator is not None:
                attribute.validator(object.__new__(cls), attribute, value)
        except ValidationError:
            raise
        except msgspec.ValidationError as exc:
            raise _error(cls, exc, name) from exc
        except (ValueError, TypeError) as exc:
            raise field_error(cls, name, str(exc)) from exc
        return value

    def _json_schema(self, cls: type) -> dict[str, t.Any]:
        return msgspec.json.schema(cls, schema_hook=schema_hook)


CODEC = AttrsCodec()


@attrs.define(kw_only=True)
class Spec(Document):
    """Base class for attrs documents. Subclasses must also be decorated with ``@attrs.define``."""

    __codec__: ClassVar[Codec] = CODEC

    id: OID | None = None

    def __attrs_post_init__(self) -> None:
        self._normalize_refs()


def SpecAdapter(cls: type, **config: t.Any) -> t.Any:
    """Turn an existing attrs class into a document class.

    Keyword arguments are :class:`~mongospecs.SpecConfig` fields, for example
    ``SpecAdapter(Dragon, collection="dragons")``.
    """

    if issubclass(cls, Document):
        return cls

    @attrs.define(kw_only=True)
    class Adapted(cls, Document):  # type: ignore[misc]
        __codec__: ClassVar[Codec] = CODEC
        __spec__ = SpecConfig(**{"collection": cls.__name__, **config})

        id: OID | None = None

        def __attrs_post_init__(self) -> None:
            parent = getattr(super(), "__attrs_post_init__", None)
            if parent is not None:
                parent()
            self._normalize_refs()

    Adapted.__name__ = Adapted.__qualname__ = cls.__name__
    Adapted.__doc__ = cls.__doc__
    return Adapted
