from typing import Any
from datetime import date
from bson import ObjectId
import msgspec

__all__ = ["MongoEncoder", "MongoDecoder"]


def mongo_enc_hook(obj: Any) -> Any:
    if obj is msgspec.UNSET:
        return None
    elif type(obj) == date:
        return str(obj)
    # Object Id
    elif isinstance(obj, ObjectId):
        return str(obj)

    raise NotImplementedError(f"Objects of type {type(obj)} are not supported")


def mongo_dec_hook(typ: Any, obj: Any) -> Any:
    if typ is ObjectId:
        return ObjectId(obj)
    raise NotImplementedError(f"Objects of type {type(obj)} are not supported")


MongoEncoder = msgspec.json.Encoder(enc_hook=mongo_enc_hook)
MongoDecoder = msgspec.json.Decoder(dec_hook=mongo_dec_hook)
