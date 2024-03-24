from datetime import date
from typing import Any, Callable, Optional

import bson
import msgspec

from .empty import Empty, EmptyObject

try:
    import attrs

    ATTRS_NOTHING_TYPE = attrs.NOTHING
except ImportError:
    ATTRS_NOTHING_TYPE = type("BaseNothing")  # type: ignore[assignment]


def bson_enc_hook(obj: Any) -> Any:
    if obj is msgspec.UNSET or obj is ATTRS_NOTHING_TYPE:
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
    if typ == ATTRS_NOTHING_TYPE:
        return attrs.NOTHING


def encode(obj: Any, enc_hook: Optional[Callable[[Any], Any]] = bson_enc_hook) -> bytes:
    return bson.encode(msgspec.to_builtins(obj, enc_hook=enc_hook))


def decode(msg: bytes, typ=Any, dec_hook: Optional[Callable[[type, Any], Any]] = bson_dec_hook):
    return msgspec.convert(bson.decode(msg), type=typ, dec_hook=dec_hook)
