"""Serialise the Read view straight from a stored document, without building a model.

The ordinary read path decodes BSON into a model, dumps the whole model to JSON
types and then picks the readable fields. For most models that work can be
skipped: the readable fields are simple values whose JSON form is known from the
declared type. :func:`compile_plan` builds such a plan when, and only when, the
result is certain to equal what the model path produces:

* every readable field has a type this module knows how to convert,
* the model has nothing that could change values on the way in or out
  (validators, converters, serialisers, post-init hooks, ``upcast``, computed
  fields, a discriminator),
* at run time each stored value has exactly the declared type. Anything else
  (an integer stored as a double, a missing required field) raises
  :class:`Slow`, and that one document goes through the model path instead.

Private fields are never read: not at the top level, where they are left out of
the projection, and not inside embedded models.
"""

from __future__ import annotations

import datetime as dt
import enum
import typing as t

from bson import ObjectId

from mongospecs import Document, Ref, meta_of
from mongospecs.codec import _UNION_ORIGINS, Codec, _codec_or_none, to_bson
from mongospecs.markers import Deferred, Marker
from mongospecs.refs import _OIDAnnotation

__all__ = ["Plan", "Slow", "compile_plan"]

Conv = t.Callable[[t.Any], t.Any]


class Slow(Exception):
    """This document needs the model path."""


_SLOW = Slow()
_MISSING: t.Any = object()
_HARMLESS_METADATA = ("msgspec", "annotated_types", "pydantic.fields", "pydantic.types")
_VALUE_CHANGING_CONFIG = ("str_strip_whitespace", "str_to_lower", "str_to_upper", "coerce_numbers_to_str")


def _exact(kind: type) -> Conv:
    def conv(value: t.Any) -> t.Any:
        if type(value) is not kind:
            raise _SLOW
        return value

    return conv


def _object_id(value: t.Any) -> t.Any:
    if type(value) is not ObjectId:
        raise _SLOW
    return str(value)


def _anything(value: t.Any) -> t.Any:
    """An untyped value: only plain JSON types are certain to come out unchanged."""
    kind = type(value)
    if kind is str or kind is int or kind is float or kind is bool or value is None:
        return value
    if kind is list:
        return [_anything(v) for v in value]
    if kind is dict:
        for key in value:
            if type(key) is not str:
                raise _SLOW
        return {k: _anything(v) for k, v in value.items()}
    raise _SLOW


def _plain_class(cls: type, codec: Codec) -> bool:
    """True when the class has nothing that could change a value between storage and output."""
    for hook in ("__post_init__", "__attrs_post_init__", "model_post_init"):
        fn = getattr(cls, hook, None)
        module = getattr(fn, "__module__", "") or ""
        if fn is not None and not module.startswith(("mongospecs.", "pydantic.")):
            return False
    if codec.name == "pydantic":
        decorators = getattr(cls, "__pydantic_decorators__", None)
        for group in (
            "validators",
            "field_validators",
            "root_validators",
            "model_validators",
            "field_serializers",
            "model_serializers",
            "computed_fields",
        ):
            if getattr(decorators, group, None):
                return False
        config = getattr(cls, "model_config", {})
        if any(config.get(key) for key in _VALUE_CHANGING_CONFIG):
            return False
    elif codec.name == "attrs":
        import attrs

        if any(a.converter is not None for a in attrs.fields(cls)):
            return False
    return True


class _Builder:
    def __init__(self, codec_name: str) -> None:
        self.models: dict[type, Conv | None] = {}
        if codec_name == "pydantic":
            from pydantic_core import to_jsonable_python

            self.temporal: Conv = to_jsonable_python
        else:
            import msgspec

            self.temporal = msgspec.to_builtins

    def conv(self, tp: t.Any) -> Conv | None:
        """A converter for values of type ``tp``, or ``None`` when the type is not supported."""
        while t.get_origin(tp) is t.Annotated:
            tp, *metadata = t.get_args(tp)
            for item in metadata:
                if item is _OIDAnnotation or isinstance(item, Marker):
                    continue
                if isinstance(item, type) and issubclass(item, Marker):
                    continue
                if not type(item).__module__.startswith(_HARMLESS_METADATA):
                    return None  # a validator or serialiser we cannot reason about
        origin = t.get_origin(tp)
        if origin in _UNION_ORIGINS:
            members = [a for a in t.get_args(tp) if a is not type(None)]
            if len(members) != 1 or len(members) == len(t.get_args(tp)):
                return None
            inner = self.conv(members[0])
            if inner is None:
                return None
            return lambda value: None if value is None else inner(value)
        if tp is t.Any:
            return _anything
        if tp is Ref or origin is Ref:
            return _object_id
        if origin is list:
            (arg,) = t.get_args(tp) or (t.Any,)
            item = self.conv(arg)
            if item is None:
                return None

            def conv_list(value: t.Any) -> t.Any:
                if type(value) is not list:
                    raise _SLOW
                return [item(v) for v in value]

            return conv_list
        if origin is dict:
            key, arg = t.get_args(tp) or (str, t.Any)
            each = self.conv(arg)
            if key is not str or each is None:
                return None

            def conv_dict(value: t.Any) -> t.Any:
                if type(value) is not dict:
                    raise _SLOW
                return {k: each(v) for k, v in value.items()}

            return conv_dict
        if origin is not None or not isinstance(tp, type):
            return None
        if tp in (str, int, float, bool):
            return _exact(tp)
        if tp is ObjectId:
            return _object_id
        if tp is dt.datetime:
            temporal = self.temporal

            def conv_datetime(value: t.Any) -> t.Any:
                if type(value) is not dt.datetime:
                    raise _SLOW
                return temporal(value)

            return conv_datetime
        if issubclass(tp, enum.Enum):
            values = {member.value for member in tp}
            if not all(type(v) in (str, int) for v in values):
                return None

            def conv_enum(value: t.Any) -> t.Any:
                if type(value) not in (str, int) or value not in values:
                    raise _SLOW
                return value

            return conv_enum
        if issubclass(tp, Document):
            return None
        codec = _codec_or_none(tp)
        if codec is None:
            return None
        if tp not in self.models:
            self.models[tp] = None  # a model that contains itself is not supported
            self.models[tp] = self.embedded(tp, codec)
        return self.models[tp]

    def fields(self, cls: type, codec: Codec, *, top: bool) -> list[tuple[str, str, Conv, t.Any]] | None:
        """``(output key, stored key, converter, default)`` per readable field, or ``None``."""
        if not _plain_class(cls, codec):
            return None
        natives = {name: native for name, native, _, _ in codec.raw_fields(cls)}
        defaults = codec.defaults(cls)
        out: list[tuple[str, str, Conv, t.Any]] = []
        for name, info in codec.fields(cls).items():
            if info.transient:
                continue
            if not top and info.stored != natives[name]:
                return None  # an alias inside an embedded model: leave it to the model path
            if info.policy.private or (top and info.has(Deferred)):
                if name not in defaults:
                    return None  # the model path needs the field, and we will not fetch it
                continue
            conv = self.conv(info.type)
            if conv is None:
                return None
            default = _MISSING
            if name in defaults:
                default = defaults[name]
                if default is not None:
                    try:
                        conv(to_bson(default))
                    except Slow:
                        return None
            out.append((name if top else natives[name], info.stored, conv, default))
        return out

    def embedded(self, cls: type, codec: Codec) -> Conv | None:
        fields = self.fields(cls, codec, top=False)
        if fields is None:
            return None

        def conv_model(value: t.Any) -> t.Any:
            if type(value) is not dict:
                raise _SLOW
            return _apply(fields, value)

        return conv_model


def _apply(fields: list[tuple[str, str, Conv, t.Any]], doc: dict[str, t.Any]) -> dict[str, t.Any]:
    out: dict[str, t.Any] = {}
    get = doc.get
    for key, stored, conv, default in fields:
        value = get(stored, _MISSING)
        if value is _MISSING:
            if default is _MISSING:
                raise _SLOW  # a required field is missing: let the model report it
            out[key] = None if default is None else conv(to_bson(default))
        else:
            out[key] = conv(value)
    return out


class Plan:
    """How to produce the Read view of one model from a stored document."""

    __slots__ = ("_fields", "select")

    def __init__(self, fields: list[tuple[str, str, Conv, t.Any]]) -> None:
        self._fields = fields
        self.select: tuple[str, ...] = tuple(key for key, _, _, _ in fields)
        """Attribute names to project: the readable fields and nothing else."""

    def serialize(self, doc: dict[str, t.Any]) -> dict[str, t.Any]:
        """The Read view as JSON types. Raises :class:`Slow` when this document needs the model path."""
        return _apply(self._fields, doc)


def compile_plan(model: type, computed: t.Sequence[str]) -> Plan | None:
    """A :class:`Plan` for ``model``, or ``None`` when only the model path is certain to be right."""
    meta = meta_of(model)
    if computed or meta.disc_key or meta.config.upcast is not None:
        return None
    fields = _Builder(meta.codec.name).fields(model, meta.codec, top=True)
    return Plan(fields) if fields is not None else None
