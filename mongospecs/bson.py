from datetime import date
from typing import Any, Callable, Optional

import bson
import msgspec

from mongospecs.base import SpecBase

from .empty import Empty, EmptyObject


def bson_enc_hook(obj: Any) -> Any:
    if obj is msgspec.UNSET or obj is Empty:
        return None
    if type(obj) == date:
        return str(obj)
    if isinstance(obj, bson.ObjectId):
        return str(obj)

    raise NotImplementedError(f"Objects of type {type(obj)} are not supported")


def bson_dec_hook(typ, val) -> Any:
    if typ == bson.ObjectId:
        return bson.ObjectId(val)
    if typ == EmptyObject:
        return Empty


def encode(obj: Any, enc_hook: Optional[Callable[[Any], Any]] = bson_enc_hook) -> bytes:
    return bson.encode(msgspec.to_builtins(obj, enc_hook=enc_hook, builtin_types=(bson.ObjectId,)))


def encode_spec(obj: SpecBase) -> bytes:
    return bson.encode(obj.to_json_type())


def decode(msg: bytes, typ=Any, dec_hook: Optional[Callable[[type, Any], Any]] = bson_dec_hook):
    return msgspec.convert(bson.decode(msg), type=typ, dec_hook=dec_hook)
