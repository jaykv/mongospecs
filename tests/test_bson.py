from dataclasses import dataclass

import msgspec
import pytest
from attr import define
from bson import ObjectId

from mongospecs.attrs import Spec as AttrsSpec
from mongospecs.bson import decode, encode, encode_spec
from mongospecs.msgspec import Spec as MsgspecSpec


class PointMsgspec(msgspec.Struct):
    x: int
    y: int


class PointMsgspecSpec(MsgspecSpec):
    x: int
    y: int


@define
class PointAttrs:
    x: int
    y: int


@define(kw_only=True)
class PointAttrsSpec(AttrsSpec):
    x: int
    y: int


@dataclass
class PointDataclass:
    x: int
    y: int


@pytest.mark.parametrize("klass", [PointMsgspec, PointAttrs, PointDataclass])
def test_bson(klass):
    x = klass(1, 2)

    msg = encode(x)  # Encoding a high-level type works

    assert x == decode(msg, typ=klass)

    with pytest.raises(msgspec.ValidationError):
        decode(encode(klass("oops", 2)), typ=klass)


@pytest.mark.parametrize(
    "spec_klass",
    [PointMsgspecSpec, PointAttrsSpec],
)
def test_bson_with_spec(spec_klass):
    x = spec_klass(x=1, y=2, _id=ObjectId())

    msg = encode_spec(x)  # Encoding a high-level type works

    assert x == decode(msg, typ=spec_klass)

    with pytest.raises(msgspec.ValidationError):
        decode(encode_spec(spec_klass(x="oops", y=2)), typ=spec_klass)
