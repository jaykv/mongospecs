from datetime import date, datetime
from typing import Any, ClassVar, Optional, TypeVar

import attrs
import msgspec
from attr import AttrsInstance
from bson import ObjectId
from pymongo import MongoClient

from .base import SpecBase, SpecProtocol, SubSpecBase
from .empty import Empty
from .se import MongoEncoder, mongo_dec_hook

__all__ = ["Spec", "SubSpec"]

T = TypeVar("T")


def attrs_serializer(inst: type, field: attrs.Attribute, value: Any) -> Any:
    if type(value) == date:
        return str(value)
    elif isinstance(value, ObjectId):
        return str(value)
    elif isinstance(value, datetime):
        return datetime.isoformat(value)
    return value


@attrs.define(kw_only=True)
class Spec(SpecBase):
    _id: Optional[ObjectId] = attrs.field(default=None, alias="_id", repr=True)  # type: ignore[assignment]

    @property
    def id(self) -> Optional[ObjectId]:
        return self._id

    @id.setter
    def id(self, value: ObjectId) -> None:
        self._id = value

    @classmethod
    def get_fields(cls) -> set[str]:
        return {f.name for f in attrs.fields(cls)}  # type: ignore[misc]

    def encode(self, **encode_kwargs: Any) -> bytes:
        return msgspec.json.encode(self, **encode_kwargs) if encode_kwargs else MongoEncoder.encode(self)

    def decode(self, data: Any, **decode_kwargs: Any) -> Any:
        return msgspec.json.decode(data, type=self.__class__, dec_hook=mongo_dec_hook, **decode_kwargs)

    def to_json_type(self) -> Any:
        return attrs.asdict(
            self,
            filter=lambda attr, value: value is not Empty or (attr.name == "_id" and value is not None),
            value_serializer=attrs_serializer,
        )

    def to_dict(self) -> dict[str, Any]:
        return attrs.asdict(self, recurse=False).copy()

    def to_tuple(self) -> tuple[Any, ...]:
        return attrs.astuple(self)


@attrs.define(kw_only=True)
class SubSpec(SubSpecBase):
    _parent: ClassVar[Any] = Spec

    def get(self, name, default=None):  # -> Any:
        return self.to_dict().get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return attrs.asdict(self)


class AttrsAdapter(AttrsInstance, SpecProtocol): ...


class AdapterBuilder:
    def __call__(
        self, obj: type[AttrsInstance], *, collection: str, client: Optional[MongoClient] = None, **kwds: Any
    ) -> Any:
        @attrs.define(kw_only=True)
        class BuiltSpecAdapter(Spec, obj):  # type: ignore
            ...

        BuiltSpecAdapter.__name__ = f"{obj.__name__}SpecAdapter"
        BuiltSpecAdapter._collection = collection
        BuiltSpecAdapter.__doc__ = obj.__doc__
        if client:
            BuiltSpecAdapter._client = client
        return BuiltSpecAdapter  # type: ignore


SpecAdapter = AdapterBuilder()
