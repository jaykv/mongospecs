from __future__ import annotations

from datetime import date, datetime
from typing import Any, ClassVar, Union

import attrs
import msgspec
from bson import ObjectId

from .base import SpecBase, SubSpecBase
from .empty import Empty, EmptyObject
from .se import MongoEncoder, mongo_dec_hook

__all__ = ["Spec", "SubSpec"]


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
    _id: Union[EmptyObject, ObjectId] = attrs.field(default=Empty, alias="_id", repr=True)
    _empty_type: ClassVar[Any] = attrs.NOTHING

    @classmethod
    def get_fields(cls) -> set[str]:
        return {f.name for f in attrs.fields(cls)}  # type: ignore[misc]

    def encode(self, **encode_kwargs: Any) -> bytes:
        return msgspec.json.encode(self, **encode_kwargs) if encode_kwargs else MongoEncoder.encode(self)

    def decode(self, data: Any, **decode_kwargs: Any) -> Any:
        return msgspec.json.decode(data, type=self.__class__, dec_hook=mongo_dec_hook, **decode_kwargs)

    def to_json_type(self) -> Any:
        return attrs.asdict(
            self, filter=lambda attr, value: value is not attrs.NOTHING, value_serializer=attrs_serializer
        )

    def to_dict(self) -> dict[str, Any]:
        return attrs.asdict(self, recurse=False)

    def to_tuple(self) -> tuple[Any, ...]:
        return attrs.astuple(self)


@attrs.define(kw_only=True)
class SubSpec(SubSpecBase):
    _parent: ClassVar[Any] = Spec

    def get(self, name, default=None):  # -> Any:
        return self.to_dict().get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return attrs.asdict(self)
