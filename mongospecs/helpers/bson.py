import typing as t
from datetime import date

import bson
import msgspec

from mongospecs.base import SpecBase
from mongospecs.helpers.empty import Empty, EmptyObject


def bson_enc_hook(obj: t.Any) -> t.Any:
    if obj is msgspec.UNSET or obj is Empty:
        return None
    if type(obj) == date:
        return str(obj)
    if isinstance(obj, bson.ObjectId):
        return str(obj)

    raise NotImplementedError(f"Objects of type {type(obj)} are not supported")


def bson_dec_hook(typ: t.Any, val: t.Any) -> t.Any:
    if typ == bson.ObjectId:
        return bson.ObjectId(val)
    if typ == EmptyObject:
        return Empty


def encode(obj: t.Any, enc_hook: t.Optional[t.Callable[[t.Any], t.Any]] = bson_enc_hook) -> bytes:
    return bson.encode(msgspec.to_builtins(obj, enc_hook=enc_hook, builtin_types=(bson.ObjectId,)))


def encode_spec(obj: SpecBase) -> bytes:
    return bson.encode(obj.to_json_type())


def decode(
    msg: bytes, typ: t.Any = t.Any, dec_hook: t.Optional[t.Callable[[type, t.Any], t.Any]] = bson_dec_hook
) -> t.Any:
    return msgspec.convert(bson.decode(msg), type=typ, dec_hook=dec_hook)
