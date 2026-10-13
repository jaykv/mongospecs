"""msgspec backend: ``from mongospecs.msgspec import Spec``."""

from __future__ import annotations

import re
import typing as t
from typing import ClassVar

import msgspec
from bson import ObjectId

from mongospecs.codec import Codec, RawField, field_error, to_bson
from mongospecs.errors import ValidationError
from mongospecs.markers import SpecConfig
from mongospecs.model import Document
from mongospecs.refs import OID, OID_PATTERN, Ref, _to_object_id, coerce_ref

__all__ = ["CODEC", "MsgspecCodec", "Spec", "SpecAdapter"]

_PATH = re.compile(r" - at `\$\.([^`.\[]+)")


def dec_hook(tp: t.Any, obj: t.Any) -> t.Any:
    if tp is ObjectId:
        return _to_object_id(obj)
    origin = t.get_origin(tp)
    if tp is Ref or origin is Ref:
        args = t.get_args(tp)
        model = args[0] if args and isinstance(args[0], type) else None
        return coerce_ref(obj, model)
    if isinstance(tp, type) and isinstance(obj, tp):
        return obj
    raise NotImplementedError(f"objects of type {tp} are not supported")


def schema_hook(tp: t.Any) -> dict[str, t.Any]:
    if tp is ObjectId or tp is Ref or t.get_origin(tp) is Ref:
        return {"type": "string", "pattern": OID_PATTERN}
    raise NotImplementedError(f"no JSON schema for {tp}")


def _error(cls: type, exc: Exception, name: str | None = None) -> ValidationError:
    message = str(exc)
    match = _PATH.search(message)
    field = name or (match.group(1) if match else None)
    return field_error(cls, field, _PATH.sub("", message) if name else message)


def encode_struct(obj: msgspec.Struct) -> dict[str, t.Any]:
    out: dict[str, t.Any] = {}
    for name, key in zip(obj.__struct_fields__, obj.__struct_encode_fields__, strict=True):
        value = getattr(obj, name)
        if value is not msgspec.UNSET:
            out[key] = to_bson(value)
    return out


class MsgspecCodec(Codec):
    name = "msgspec"

    def raw_fields(self, cls: type) -> list[RawField]:
        return [(f.name, f.encode_name, f.type, f.required) for f in msgspec.structs.fields(cls)]

    def _decode(self, cls: type, doc: dict[str, t.Any], trusted: bool) -> t.Any:
        # msgspec has no unvalidated construction path; conversion is cheap, so
        # ``trusted`` changes nothing here.
        try:
            return msgspec.convert(doc, cls, dec_hook=dec_hook)
        except (msgspec.ValidationError, ValueError) as exc:
            raise _error(cls, exc) from exc

    def _encode(self, obj: t.Any) -> dict[str, t.Any]:
        return encode_struct(obj)

    def validate_field(self, cls: type, name: str, value: t.Any) -> t.Any:
        tp = self.fields(cls)[name].type
        try:
            return msgspec.convert(value, tp, dec_hook=dec_hook)
        except (msgspec.ValidationError, ValueError) as exc:
            raise _error(cls, exc, name) from exc

    def _json_schema(self, cls: type) -> dict[str, t.Any]:
        return msgspec.json.schema(cls, schema_hook=schema_hook)


CODEC = MsgspecCodec()


class Spec(msgspec.Struct, Document, kw_only=True, weakref=True):
    """Base class for msgspec documents."""

    __codec__: ClassVar[Codec] = CODEC

    id: OID | None = msgspec.field(default=None, name="_id")

    def __post_init__(self) -> None:
        self._normalize_refs()


def SpecAdapter(cls: type[msgspec.Struct], **config: t.Any) -> t.Any:
    """Turn an existing Struct into a document class.

    Keyword arguments are :class:`~mongospecs.SpecConfig` fields, for example
    ``SpecAdapter(Dragon, collection="dragons")``.
    """

    if issubclass(cls, Document):
        return cls

    class Adapted(cls, Document, kw_only=True, weakref=True):  # type: ignore[misc, valid-type, call-arg]
        __codec__: ClassVar[Codec] = CODEC
        __spec__ = SpecConfig(**{"collection": cls.__name__, **config})

        id: OID | None = msgspec.field(default=None, name="_id")

        def __post_init__(self) -> None:
            parent = getattr(super(), "__post_init__", None)
            if parent is not None:
                parent()
            self._normalize_refs()

    Adapted.__name__ = Adapted.__qualname__ = cls.__name__
    Adapted.__doc__ = cls.__doc__
    return Adapted
