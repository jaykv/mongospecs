import typing as t

import msgspec
from bson import ObjectId
from pymongo import MongoClient

from mongospecs.base import SpecBase, SubSpecBase
from mongospecs.helpers.empty import Empty, EmptyObject
from mongospecs.helpers.se import MongoEncoder, mongo_dec_hook, mongo_enc_hook

__all__ = ["Spec", "SubSpec"]


class Spec(msgspec.Struct, SpecBase, kw_only=True):
    _id: t.Union[ObjectId, msgspec.UnsetType] = msgspec.field(name="_id", default=msgspec.UNSET)  # type: ignore[assignment]
    _empty_type: t.ClassVar[t.Any] = msgspec.UNSET

    def encode(self, **encode_kwargs: t.Any) -> bytes:
        return msgspec.json.encode(self, **encode_kwargs) if encode_kwargs else MongoEncoder.encode(self)

    def decode(self, data: t.Any, **decode_kwargs: t.Any) -> t.Any:
        return msgspec.json.decode(data, type=self.__class__, dec_hook=mongo_dec_hook, **decode_kwargs)

    def to_json_type(self) -> t.Any:
        return msgspec.to_builtins(self, enc_hook=mongo_enc_hook)

    def to_dict(self) -> dict[str, t.Any]:
        return msgspec.structs.asdict(self)

    def to_tuple(self) -> tuple[t.Any, ...]:
        return msgspec.structs.astuple(self)

    @classmethod
    def get_fields(cls) -> set[str]:
        return set(cls.__struct_fields__)

    # msgspec Struct includes these by default- so we need to override them
    def __eq__(self, other: t.Any) -> bool:
        if not isinstance(other, self.__class__):
            return False

        return self._id == other._id

    def __lt__(self, other: t.Any) -> t.Any:
        return self._id < other._id


class SubSpec(msgspec.Struct, SubSpecBase, kw_only=True, dict=True):
    _parent: t.ClassVar[t.Any] = Spec

    def get(self, name: str, default: t.Any = None) -> t.Any:
        return self.to_dict().get(name, default)

    def to_dict(self) -> dict[str, t.Any]:
        return msgspec.structs.asdict(self)


class MsgspecAdapter(SpecBase):
    _id: t.Union[EmptyObject, ObjectId] = msgspec.field(default=Empty)
    _empty_type: t.ClassVar[t.Any] = msgspec.UNSET

    def __init__(self, **data: t.Any) -> None: ...


class AdapterBuilder:
    def __call__(
        self,
        obj: type[msgspec.Struct],
        *,
        collection: str,
        client: t.Optional[MongoClient[t.Any]] = None,
        **kwds: t.Any,
    ) -> t.Any:
        class BuiltSpecAdapter(SpecBase, obj):  # type: ignore
            _id: t.Union[EmptyObject, ObjectId] = msgspec.field(default=Empty)
            _empty_type: t.ClassVar[t.Any] = msgspec.UNSET

            def encode(self, **encode_kwargs: t.Any) -> bytes:
                return msgspec.json.encode(self, **encode_kwargs) if encode_kwargs else MongoEncoder.encode(self)

            def decode(self, data: t.Any, **decode_kwargs: t.Any) -> t.Any:
                return msgspec.json.decode(data, type=self.__class__, dec_hook=mongo_dec_hook, **decode_kwargs)

            def to_json_type(self) -> t.Any:
                return msgspec.to_builtins(self, enc_hook=mongo_enc_hook)

            def to_dict(self) -> dict[str, t.Any]:
                return msgspec.structs.asdict(self)

            def to_tuple(self) -> tuple[t.Any, ...]:
                return msgspec.structs.astuple(self)

            @classmethod
            def get_fields(cls) -> set[str]:
                return set(cls.__struct_fields__)

            # msgspec Struct includes these by default- so we need to override them
            def __eq__(self, other: t.Any) -> bool:
                if not isinstance(other, self.__class__):
                    return False

                return self._id == other._id

            def __lt__(self, other: t.Any) -> t.Any:
                return self._id < other._id

        BuiltSpecAdapter.__name__ = f"{obj.__name__}SpecAdapter"
        BuiltSpecAdapter._collection = collection
        BuiltSpecAdapter.__doc__ = obj.__doc__
        if client:
            BuiltSpecAdapter._client = client
        return BuiltSpecAdapter


SpecAdapter = AdapterBuilder()
