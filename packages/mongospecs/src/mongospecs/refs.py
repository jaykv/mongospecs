"""Backend-neutral value types: ``OID`` and ``Ref``."""

from __future__ import annotations

import typing as t

from bson import ObjectId

from mongospecs.errors import NotLoadedError

__all__ = ["OID", "OID_PATTERN", "Ref", "id_of"]

T = t.TypeVar("T")

OID_PATTERN = "^[0-9a-fA-F]{24}$"


def _to_object_id(value: t.Any) -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str | bytes):
        try:
            return ObjectId(value)
        except Exception as exc:
            raise ValueError(f"{value!r} is not a valid ObjectId") from exc
    raise ValueError(f"{type(value).__name__} is not a valid ObjectId")


class _OIDAnnotation:
    """Teaches pydantic about ``bson.ObjectId``. msgspec and attrs ignore it."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source: t.Any, handler: t.Any) -> t.Any:
        from pydantic_core import core_schema as cs

        return cs.no_info_plain_validator_function(
            _to_object_id,
            serialization=cs.plain_serializer_function_ser_schema(str, when_used="json"),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: t.Any, handler: t.Any) -> dict[str, t.Any]:
        return {"type": "string", "pattern": OID_PATTERN}


OID = t.Annotated[ObjectId, _OIDAnnotation]
"""``bson.ObjectId`` usable as a field type on every backend."""


def id_of(value: t.Any) -> t.Any:
    """Return the id behind a document, a ``Ref`` or a bare id."""
    if isinstance(value, Ref):
        return value.id
    from mongospecs.model import Document

    if isinstance(value, Document):
        return value.id
    return value


class Ref(t.Generic[T]):
    """A reference to a document in another collection.

    Stored as the target's id. ``doc`` is available after ``include()`` or
    ``await ref.fetch()``; before that it raises :class:`NotLoadedError`.
    """

    __slots__ = ("__weakref__", "_doc", "_model", "id")

    id: t.Any
    _doc: T | None
    _model: type[T] | None

    def __init__(self, target: t.Any, model: type[T] | None = None) -> None:
        from mongospecs.model import Document

        if isinstance(target, Ref):
            self.id = target.id
            self._doc = target._doc
            self._model = model or target._model
        elif isinstance(target, Document):
            if target.id is None:
                raise ValueError("cannot reference a document that has no id yet")
            self.id = target.id
            self._doc = t.cast(T, target)
            self._model = model or t.cast("type[T]", type(target))
        else:
            self.id = target
            self._doc = None
            self._model = model

    @property
    def loaded(self) -> bool:
        return self._doc is not None

    @property
    def doc(self) -> T:
        if self._doc is None:
            raise NotLoadedError(
                f"reference to {self.id!r} is not loaded; use include() on the query or await ref.fetch()"
            )
        return self._doc

    async def fetch(self, *, session: t.Any = None) -> T:
        """Load the referenced document (once) and return it."""
        if self._doc is None:
            if self._model is None:
                raise NotLoadedError("reference has no target model; it cannot be fetched")
            self._doc = await t.cast(t.Any, self._model).get(self.id, session=session)
        return t.cast(T, self._doc)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Ref):
            return bool(self.id == other.id)
        return bool(self.id == other)

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        state = "loaded" if self._doc is not None else "unloaded"
        return f"Ref({self.id!r}, {state})"

    # -- pydantic -----------------------------------------------------------

    @classmethod
    def __get_pydantic_core_schema__(cls, source: t.Any, handler: t.Any) -> t.Any:
        from pydantic_core import core_schema as cs

        args = t.get_args(source)
        model = args[0] if args and isinstance(args[0], type) else None

        def validate(value: t.Any) -> Ref[t.Any]:
            return coerce_ref(value, model)

        def serialize(ref: t.Any, info: t.Any) -> t.Any:
            # Plain assignment is not validated, so this may be a bare id or a document.
            value = id_of(ref)
            return _json_id(value) if info.mode == "json" else value

        return cs.no_info_plain_validator_function(
            validate,
            serialization=cs.plain_serializer_function_ser_schema(serialize, info_arg=True),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: t.Any, handler: t.Any) -> dict[str, t.Any]:
        return {"type": "string", "pattern": OID_PATTERN}


def _json_id(value: t.Any) -> t.Any:
    return str(value) if isinstance(value, ObjectId) else value


def coerce_ref(value: t.Any, model: type[t.Any] | None) -> Ref[t.Any]:
    """Build a ``Ref`` from an id, an id string, a document or another ``Ref``."""
    from mongospecs.model import Document

    if isinstance(value, Ref):
        if value._model is None and model is not None:
            value._model = model
        return value
    if isinstance(value, Document):
        return Ref(value, model)
    if isinstance(value, str) and ObjectId.is_valid(value) and _target_id_is_object_id(model):
        return Ref(ObjectId(value), model)
    if value is None or isinstance(value, dict | list):
        raise ValueError(f"{type(value).__name__} is not a valid reference")
    return Ref(value, model)


def _target_id_is_object_id(model: type[t.Any] | None) -> bool:
    if model is None:
        return True
    from mongospecs.meta import id_python_type

    return id_python_type(model) is ObjectId
