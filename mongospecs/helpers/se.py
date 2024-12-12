import typing as t
from datetime import date

try:
    import attrs

    ATTRS_NOTHING_TYPE = attrs.NOTHING
except ImportError:
    ATTRS_NOTHING_TYPE = type("BaseNothing")  # type: ignore[assignment]

import msgspec
from bson import ObjectId

__all__ = ["MongoEncoder", "MongoDecoder"]


def mongo_enc_hook(obj: t.Any) -> t.Any:
    if obj is msgspec.UNSET or obj is ATTRS_NOTHING_TYPE:
        return None
    elif type(obj) == date:
        return str(obj)
    # Object Id
    elif isinstance(obj, ObjectId):
        return str(obj)

    raise NotImplementedError(f"Objects of type {type(obj)} are not supported")


def mongo_dec_hook(typ: t.Any, obj: t.Any) -> t.Any:
    if typ is ObjectId:
        return ObjectId(obj)
    raise NotImplementedError(f"Objects of type {type(obj)} are not supported")


MongoEncoder = msgspec.json.Encoder(enc_hook=mongo_enc_hook)
MongoDecoder = msgspec.json.Decoder(dec_hook=mongo_dec_hook)
