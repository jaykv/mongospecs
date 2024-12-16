import typing as t
from datetime import datetime

import attrs
import msgspec
from attr import AttrsInstance
from bson import ObjectId
from pymongo import MongoClient

from mongospecs.base import SpecBase, SubSpecBase
from mongospecs.helpers.empty import Empty
from mongospecs.helpers.se import MongoEncoder, mongo_dec_hook

__all__ = ["Spec", "SubSpec"]

T = t.TypeVar("T")


def attrs_serializer(inst: type, field: attrs.Attribute, value: t.Any) -> t.Any:  # type: ignore[type-arg]
    if isinstance(value, ObjectId):
        return str(value)
    elif isinstance(value, datetime):
        return datetime.isoformat(value)
    return value


@attrs.define(kw_only=True)
class Spec(SpecBase):
    _id: t.Optional[ObjectId] = attrs.field(default=None, alias="_id", repr=True)  # type: ignore[assignment]

    @property
    def id(self) -> t.Optional[ObjectId]:
        return self._id

    @id.setter
    def id(self, value: ObjectId) -> None:
        self._id = value

    @classmethod
    def get_fields(cls) -> set[str]:
        return {f.name for f in attrs.fields(cls)}

    def encode(self, **encode_kwargs: t.Any) -> bytes:
        return msgspec.json.encode(self, **encode_kwargs) if encode_kwargs else MongoEncoder.encode(self)

    def decode(self, data: t.Any, **decode_kwargs: t.Any) -> t.Any:
        return msgspec.json.decode(data, type=self.__class__, dec_hook=mongo_dec_hook, **decode_kwargs)

    def to_json_type(self) -> t.Any:
        return attrs.asdict(
            self,
            filter=lambda attr, value: value is not Empty or (attr.name == "_id" and value is not None),
            value_serializer=attrs_serializer,
        )

    def to_dict(self) -> dict[str, t.Any]:
        return attrs.asdict(self, recurse=False).copy()

    def to_tuple(self) -> tuple[t.Any, ...]:
        return attrs.astuple(self)


@attrs.define(kw_only=True)
class SubSpec(SubSpecBase):
    _parent: t.ClassVar[t.Any] = Spec

    def get(self, name: str, default: t.Any = None) -> t.Any:
        return self.to_dict().get(name, default)

    def to_dict(self) -> dict[str, t.Any]:
        return attrs.asdict(self)


class AdapterBuilder:
    def __call__(
        self, obj: type[AttrsInstance], *, collection: str, client: t.Optional[MongoClient[t.Any]] = None, **kwds: t.Any
    ) -> t.Any:
        @attrs.define(kw_only=True)
        class BuiltSpecAdapter(Spec, obj):  # type: ignore
            ...

        BuiltSpecAdapter.__name__ = f"{obj.__name__}SpecAdapter"
        BuiltSpecAdapter._collection = collection
        BuiltSpecAdapter.__doc__ = obj.__doc__
        if client:
            BuiltSpecAdapter._client = client
        return BuiltSpecAdapter


SpecAdapter = AdapterBuilder()
