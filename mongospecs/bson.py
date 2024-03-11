from typing import Any

import bson
import msgspec


def encode(obj: Any) -> bytes:
    return bson.encode(msgspec.to_builtins(obj))


def decode(msg: bytes, type=Any):
    return msgspec.convert(bson.decode(msg), type=type)
