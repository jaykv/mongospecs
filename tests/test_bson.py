import msgspec
import pytest

from mongospecs.bson import decode, encode


class Point(msgspec.Struct):
    x: int
    y: int


def test_bson_encode_decode():
    x = Point(1, 2)

    msg = encode(x)  # Encoding a high-level type works

    print(msg)

    assert x == decode(msg, type=Point)

    with pytest.raises(msgspec.ValidationError):
        decode(encode(Point("oops", 2)), type=Point)
