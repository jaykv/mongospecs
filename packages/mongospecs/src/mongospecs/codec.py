"""The backend seam.

A :class:`Codec` knows how one model library (msgspec, pydantic, attrs) lists
fields, decodes a MongoDB document into a model, encodes a model back, validates
a single field and produces JSON Schema. Everything else in mongospecs is
backend-agnostic and goes through this interface.
"""

from __future__ import annotations

import abc
import sys
import types
import typing as t
from dataclasses import dataclass

import bson
from bson.raw_bson import RawBSONDocument

from mongospecs.errors import ConfigurationError, ValidationError
from mongospecs.markers import DEFAULT_POLICY, Alias, Backref, Marker, Policy, To
from mongospecs.refs import Ref, id_of

__all__ = ["Codec", "FieldInfo", "RefInfo", "codec_for", "to_bson"]


@dataclass(frozen=True, slots=True)
class RefInfo:
    """Shape and targets of a reference field."""

    shape: t.Literal["one", "list", "dict"]
    targets: tuple[t.Any, ...]
    """Target model classes, or class names (``str``) still to be resolved."""


@dataclass(frozen=True, slots=True)
class FieldInfo:
    name: str
    """Attribute name on the model."""
    stored: str
    """Key in the MongoDB document."""
    type: t.Any
    """The annotation, including ``Annotated`` metadata."""
    markers: tuple[t.Any, ...]
    policy: Policy
    required: bool
    ref: RefInfo | None = None
    backref: Backref | None = None

    def has(self, marker: type[Marker]) -> bool:
        return any(m is marker or isinstance(m, marker) for m in self.markers)

    def get(self, marker: type[Marker]) -> t.Any:
        for m in self.markers:
            if isinstance(m, marker):
                return m
            if m is marker:
                return marker()
        return None

    @property
    def transient(self) -> bool:
        """Never written to the database."""
        return self.backref is not None


# -- annotation analysis ----------------------------------------------------

_UNION_ORIGINS: tuple[t.Any, ...] = (t.Union, types.UnionType)


def _is_marker(obj: t.Any) -> bool:
    return isinstance(obj, Marker) or (isinstance(obj, type) and issubclass(obj, Marker))


def split_annotation(tp: t.Any) -> tuple[t.Any, tuple[t.Any, ...]]:
    """Return ``(bare type, markers)``, looking through ``Annotated`` and ``Optional``."""
    markers: list[t.Any] = []
    while t.get_origin(tp) is t.Annotated:
        args = t.get_args(tp)
        markers.extend(m for m in args[1:] if _is_marker(m))
        tp = args[0]
    if t.get_origin(tp) in _UNION_ORIGINS:
        for arg in t.get_args(tp):
            if t.get_origin(arg) is t.Annotated:
                markers.extend(m for m in t.get_args(arg)[1:] if _is_marker(m))
    return tp, tuple(markers)


def strip_optional(tp: t.Any) -> t.Any:
    """``X | None`` -> ``X`` (also unwraps ``Annotated``)."""
    while t.get_origin(tp) is t.Annotated:
        tp = t.get_args(tp)[0]
    if t.get_origin(tp) in _UNION_ORIGINS:
        args = [a for a in t.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return strip_optional(args[0])
    return tp


def loosen(tp: t.Any) -> t.Any:
    """The type without validation constraints: ``Annotated`` metadata is dropped,
    except the marker that lets pydantic handle ``ObjectId``.

    Used to cast query values, which should match the field's type but need not
    satisfy rules meant for stored values (a search for a short title is legitimate
    even if titles must be long).
    """
    from mongospecs.refs import _OIDAnnotation

    origin = t.get_origin(tp)
    if origin is t.Annotated:
        inner, *meta = t.get_args(tp)
        inner = loosen(inner)
        return t.Annotated[inner, _OIDAnnotation] if _OIDAnnotation in meta else inner
    if origin in _UNION_ORIGINS:
        return t.Union[tuple(loosen(a) for a in t.get_args(tp))]  # noqa: UP007
    args = t.get_args(tp)
    if origin is not None and args and origin is not t.Literal:
        try:
            return origin[tuple(loosen(a) for a in args)]
        except TypeError:
            return tp
    return tp


def element_type(tp: t.Any) -> t.Any:
    """Element type of a list/set/tuple annotation, or ``None``."""
    tp = strip_optional(tp)
    origin = t.get_origin(tp)
    if origin in (list, set, frozenset, tuple, t.Sequence, t.MutableSequence) or (
        isinstance(origin, type) and issubclass(origin, list | set | frozenset | tuple)
    ):
        args = t.get_args(tp)
        if args:
            return args[0]
    return None


def _ref_targets(tp: t.Any) -> tuple[t.Any, ...] | None:
    tp = strip_optional(tp)
    if tp is Ref:
        return ()
    if t.get_origin(tp) is not Ref:
        return None
    (arg,) = t.get_args(tp) or (t.Any,)
    members = t.get_args(arg) if t.get_origin(arg) in _UNION_ORIGINS else (arg,)
    out: list[t.Any] = []
    for member in members:
        if isinstance(member, t.ForwardRef):
            out.append(member.__forward_arg__)
        elif isinstance(member, str | type):
            out.append(member)
    return tuple(out)


def analyze_ref(tp: t.Any, markers: tuple[t.Any, ...]) -> RefInfo | None:
    named = tuple(m.model for m in markers if isinstance(m, To))
    inner = strip_optional(tp)
    targets = _ref_targets(inner)
    shape: t.Literal["one", "list", "dict"] = "one"
    if targets is None:
        elem = element_type(inner)
        if elem is not None:
            targets, shape = _ref_targets(elem), "list"
        elif t.get_origin(inner) in (dict, t.Mapping, t.MutableMapping):
            args = t.get_args(inner)
            if len(args) == 2:
                targets, shape = _ref_targets(args[1]), "dict"
    if targets is None:
        return None
    targets = named or targets
    if not targets:
        raise ConfigurationError("a bare Ref needs a target: use Ref[Model] or the To('Model') marker")
    return RefInfo(shape, targets)


# -- generic BSON-ready walker ----------------------------------------------


def to_bson(value: t.Any) -> t.Any:
    """Make ``value`` BSON-ready: models become dicts, references become ids.

    Leaf values the driver cannot encode natively (enums, dates, decimals, sets)
    are left alone; the registry's codec options handle them at encode time.
    """
    tp = type(value)
    if tp in _LEAVES:
        return value
    if tp is list or tp is tuple:
        return [to_bson(v) for v in value]
    if tp is dict:
        return {k: to_bson(v) for k, v in value.items()}
    if tp is Ref:
        return value.id
    if _DOCUMENT is not None and isinstance(value, _DOCUMENT):
        return t.cast(t.Any, value).id  # a document inside another document is a reference, never embedded
    codec = _codec_or_none(tp)
    if codec is not None:
        return codec.encode(value)
    if isinstance(value, list | tuple | set | frozenset):
        return [to_bson(v) for v in value]
    if isinstance(value, dict):
        return {k: to_bson(v) for k, v in value.items()}
    return value


_DOCUMENT: type | None = None
"""Set to ``Document`` by ``mongospecs.model`` when it is imported."""

_LEAVES: frozenset[type] = frozenset({str, int, float, bool, type(None), bson.ObjectId, bytes})


def ref_ids(value: t.Any, shape: str) -> t.Any:
    """Reduce the value of a reference field to ids."""
    if value is None:
        return None
    if shape == "list":
        return [id_of(v) for v in value]
    if shape == "dict":
        return {k: id_of(v) for k, v in value.items()}
    return id_of(value)


# -- the codec interface ----------------------------------------------------

RawField = tuple[str, str, t.Any, bool]
"""``(attribute name, natively stored name, annotation, required)``"""


class Codec(abc.ABC):
    """Adapter for one model library."""

    name: t.ClassVar[str]

    def __init__(self) -> None:
        self._fields: dict[type, dict[str, FieldInfo]] = {}

    # -- to implement ------------------------------------------------------

    @abc.abstractmethod
    def raw_fields(self, cls: type) -> list[RawField]: ...

    @abc.abstractmethod
    def _decode(self, cls: type, doc: dict[str, t.Any], trusted: bool) -> t.Any: ...

    @abc.abstractmethod
    def _encode(self, obj: t.Any) -> dict[str, t.Any]:
        """Encode to a dict keyed by natively stored names. Must not share containers with ``obj``."""

    @abc.abstractmethod
    def validate_field(self, cls: type, name: str, value: t.Any) -> t.Any:
        """Validate and coerce one field value with the model's own rules."""

    @abc.abstractmethod
    def _json_schema(self, cls: type) -> dict[str, t.Any]: ...

    # -- shared ------------------------------------------------------------

    def fields(self, cls: type) -> dict[str, FieldInfo]:
        cached = self._fields.get(cls)
        if cached is not None:
            return cached
        from mongospecs.model import Document

        is_document = issubclass(cls, Document)
        out: dict[str, FieldInfo] = {}
        for name, native, annotation, required in self.raw_fields(cls):
            _, markers = split_annotation(annotation)
            policy = next((m for m in markers if isinstance(m, Policy)), DEFAULT_POLICY)
            alias = next((m for m in markers if isinstance(m, Alias)), None)
            backref = next((m for m in markers if isinstance(m, Backref)), None)
            stored = alias.name if alias is not None else native
            if is_document and name == "id" and stored == "id":
                stored = "_id"
            ref = None if backref is not None else analyze_ref(annotation, markers)
            if backref is not None and required:
                raise ConfigurationError(f"{cls.__name__}.{name}: a Backref field needs a default")
            out[name] = FieldInfo(name, stored, annotation, markers, policy, required, ref, backref)
        self._fields[cls] = out
        return out

    def _renames(self, cls: type) -> tuple[dict[str, str], dict[str, str]]:
        """Key translations the backend does not do itself: ``(stored->native, native->stored)``."""
        cache: dict[type, tuple[dict[str, str], dict[str, str]]] = self.__dict__.setdefault("_rn", {})
        hit = cache.get(cls)
        if hit is None:
            natives = {name: native for name, native, _, _ in self.raw_fields(cls)}
            to_stored = {
                natives[f.name]: f.stored for f in self.fields(cls).values() if natives[f.name] != f.stored
            }
            hit = ({v: k for k, v in to_stored.items()}, to_stored)
            cache[cls] = hit
        return hit

    def decode(self, cls: type, doc: t.Any, *, trusted: bool = False) -> t.Any:
        """Build a model from a MongoDB document (a mapping or raw BSON bytes)."""
        if isinstance(doc, bytes | bytearray | memoryview):
            doc = bson.decode(bytes(doc))
        elif isinstance(doc, RawBSONDocument):
            doc = bson.decode(doc.raw)
        to_native, _ = self._renames(cls)
        if to_native:
            doc = {to_native.get(k, k): v for k, v in doc.items()}
        return self._decode(cls, doc, trusted)

    def encode(self, obj: t.Any) -> dict[str, t.Any]:
        """Encode a model to a BSON-ready dict keyed by stored names."""
        cls = type(obj)
        doc = self._encode(obj)
        _, to_stored = self._renames(cls)
        if to_stored:
            doc = {to_stored.get(k, k): v for k, v in doc.items()}
        for f in self._special(cls):
            if f.transient:
                doc.pop(f.stored, None)
            elif f.ref is not None and f.stored in doc:
                doc[f.stored] = ref_ids(doc[f.stored], f.ref.shape)
        return doc

    def _special(self, cls: type) -> tuple[FieldInfo, ...]:
        cache: dict[type, tuple[FieldInfo, ...]] = self.__dict__.setdefault("_sp", {})
        hit = cache.get(cls)
        if hit is None:
            hit = tuple(f for f in self.fields(cls).values() if f.transient or f.ref is not None)
            cache[cls] = hit
        return hit

    def encode_value(self, value: t.Any) -> t.Any:
        return to_bson(value)

    def json_schema(self, cls: type) -> dict[str, t.Any]:
        """JSON Schema for the model, keyed by attribute names, root inlined, ``$defs`` kept."""
        schema = dict(self._json_schema(cls))
        defs = dict(schema.pop("$defs", {}))
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            schema = dict(defs[ref.rsplit("/", 1)[1]])
        props = dict(schema.get("properties", {}))
        by_stored = {f.stored: f.name for f in self.fields(cls).values()}
        natives = {native: name for name, native, _, _ in self.raw_fields(cls)}
        rename = {**by_stored, **natives}
        schema["properties"] = {rename.get(k, k): v for k, v in props.items()}
        schema["required"] = [rename.get(k, k) for k in schema.get("required", [])]
        schema.setdefault("type", "object")
        if defs:
            schema["$defs"] = defs
        return schema

    def clear_cache(self) -> None:
        self._fields.clear()
        self.__dict__.pop("_rn", None)
        self.__dict__.pop("_sp", None)


def field_error(cls: type, name: str | None, message: str) -> ValidationError:
    where = f"{cls.__name__}.{name}" if name else cls.__name__
    return ValidationError(f"{where}: {message}", [{"field": name, "message": message}])


# -- codec lookup -----------------------------------------------------------

_CODECS: dict[type, Codec | None] = {}


def _codec_or_none(cls: type) -> Codec | None:
    try:
        return _CODECS[cls]
    except KeyError:
        pass
    codec: Codec | None = getattr(cls, "__codec__", None)
    if codec is None:
        msgspec = sys.modules.get("msgspec")
        pydantic = sys.modules.get("pydantic")
        attrs = sys.modules.get("attrs")
        if msgspec is not None and issubclass(cls, msgspec.Struct):
            from mongospecs.msgspec import CODEC as codec
        elif pydantic is not None and issubclass(cls, pydantic.BaseModel):
            from mongospecs.pydantic import CODEC as codec
        elif attrs is not None and attrs.has(cls):
            from mongospecs.attrs import CODEC as codec
    _CODECS[cls] = codec
    return codec


def codec_for(cls: type) -> Codec:
    """Return the codec for a model class."""
    codec = _codec_or_none(cls)
    if codec is None:
        raise ConfigurationError(
            f"{cls.__name__} is not a msgspec Struct, a pydantic BaseModel or an attrs class"
        )
    return codec
