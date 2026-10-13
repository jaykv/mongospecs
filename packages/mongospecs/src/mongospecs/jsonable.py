"""Conversion of BSON-ready values to plain JSON types."""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import enum
import typing as t
import uuid

from bson import ObjectId
from bson.code import Code
from bson.dbref import DBRef
from bson.decimal128 import Decimal128
from bson.max_key import MaxKey
from bson.min_key import MinKey
from bson.regex import Regex
from bson.timestamp import Timestamp

from mongospecs.refs import Ref

_BSON_ONLY = (bytes, bytearray, int, Timestamp, Regex, Code, DBRef, MaxKey, MinKey)
"""Types a serialiser may hand to :func:`json_default` that only BSON has, or that are
subclasses of standard types the serialiser does not recognise (``Binary``, ``Int64``)."""


__all__ = ["json_default", "jsonable"]


def jsonable(value: t.Any) -> t.Any:
    """Convert a BSON-ready value to plain JSON types."""
    kind = type(value)
    if value is None or kind is str or kind is bool or kind is int or kind is float:
        return value
    if isinstance(value, bool | str | float):
        return value
    if isinstance(value, int):
        return int(value)  # bson.Int64 and other int subclasses
    if isinstance(value, Timestamp):
        return jsonable(value.as_datetime())
    if isinstance(value, Regex):
        return value.pattern
    if isinstance(value, ObjectId | uuid.UUID | decimal.Decimal | Decimal128):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return jsonable(value.value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(v) for v in value]
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Ref):
        return jsonable(value.id)
    return str(value)


def json_default(value: t.Any) -> t.Any:
    """``enc_hook`` for serialisers that handle the standard types themselves:
    only the types they do not know (ObjectId, Ref, Decimal128, nested documents) arrive here."""
    if isinstance(value, ObjectId | Decimal128):
        return str(value)
    if isinstance(value, Ref):
        return jsonable(value.id)
    if isinstance(value, _BSON_ONLY):
        # Found in untyped fields: whatever another writer stored there has to be readable.
        return jsonable(value)
    identifier = getattr(value, "id", None)
    if identifier is not None:  # a document assigned where a reference is expected
        return jsonable(identifier)
    raise NotImplementedError(f"cannot serialise {type(value).__name__}")
