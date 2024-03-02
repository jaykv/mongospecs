from typing import Any, ClassVar, Union

import msgspec
from bson import ObjectId

from .base import SpecBase, SubSpecBase
from .empty import Empty, EmptyObject
from .se import MongoEncoder, mongo_dec_hook, mongo_enc_hook

__all__ = ["Spec", "SubSpec"]


class Spec(msgspec.Struct, SpecBase, kw_only=True, dict=True):
    _id: Union[EmptyObject, ObjectId] = msgspec.field(default=Empty)
    _empty_type: ClassVar[Any] = msgspec.UNSET

    def encode(self, **encode_kwargs: Any) -> bytes:
        return msgspec.json.encode(self, **encode_kwargs) if encode_kwargs else MongoEncoder.encode(self)

    def decode(self, data: Any, **decode_kwargs: Any) -> Any:
        return msgspec.json.decode(data, type=self.__class__, dec_hook=mongo_dec_hook, **decode_kwargs)

    def to_json_type(self) -> Any:
        return msgspec.to_builtins(self, enc_hook=mongo_enc_hook)

    def to_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)

    def to_tuple(self) -> tuple[Any, ...]:
        return msgspec.structs.astuple(self)

    @classmethod
    def get_fields(cls) -> set[str]:
        return set(cls.__struct_fields__)

    # msgspec Struct includes these by default- so we need to override them
    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, self.__class__):
            return False

        return self._id == other._id

    def __lt__(self, other: Any) -> Any:
        return self._id < other._id


class SubSpec(msgspec.Struct, SubSpecBase, kw_only=True, dict=True):
    _parent: ClassVar[Any] = Spec

    def get(self, name, default=None):  # -> Any:
        return self.to_dict().get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)
