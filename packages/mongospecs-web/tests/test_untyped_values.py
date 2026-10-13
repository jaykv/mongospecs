"""Values only BSON has (ObjectId, dates, decimals, binary, UUID ...) inside untyped fields."""

import datetime as dt
import json
import typing as t
import uuid

import bson
import pytest

from mongospecs import SpecConfig
from mongospecs_web import Api, Create, Get, List, Patch, Resource
from mongospecs_web.policy import view_of

BACKENDS = ("msgspec", "pydantic", "attrs")
OID = bson.ObjectId("65a1b2c3d4e5f60718293a4b")
VALUES = {
    "oid": OID,
    "when": dt.datetime(2021, 1, 2, 3, 4, 5, 123000, tzinfo=dt.UTC),
    "amount": bson.Decimal128("12.50"),
    "blob": b"\xff\x00hi",
    "text_bytes": b"plain",
    "uid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    "big": bson.Int64(2**40),
    "binary": bson.Binary(b"\x01\x02", 128),
    "stamp": bson.Timestamp(1700000000, 1),
    "pattern": bson.Regex("^a", "i"),
    "deep": {"list": [OID, {"again": [dt.datetime(2020, 5, 6, tzinfo=dt.UTC), None, 1.5, "s", True]}]},
}
EXPECTED = {
    "oid": "65a1b2c3d4e5f60718293a4b",
    "when": "2021-01-02T03:04:05.123000Z",
    "amount": "12.50",
    "blob": "/wBoaQ==",
    "text_bytes": "cGxhaW4=",
    "uid": "12345678-1234-5678-1234-567812345678",
    "big": 2**40,
    "binary": "AQI=",
    "stamp": "2023-11-14T22:13:20Z",
    "pattern": "^a",
    "deep": {"list": ["65a1b2c3d4e5f60718293a4b", {"again": ["2020-05-06T00:00:00Z", None, 1.5, "s", True]}]},
}


def models(backend):
    if backend == "msgspec":
        import msgspec

        from mongospecs.msgspec import Spec

        class Base(msgspec.Struct, kw_only=True):
            pass

        define = lambda c: c  # noqa: E731
    elif backend == "pydantic":
        import pydantic

        from mongospecs.pydantic import Spec

        Base = pydantic.BaseModel
        define = lambda c: c  # noqa: E731
    else:
        import attrs

        from mongospecs.attrs import Spec

        Base = object
        define = attrs.define(kw_only=True)

    @define
    class Part(Base):
        label: str = ""
        meta: dict[str, t.Any] = {}  # noqa: RUF012

    @define
    class Loose(Spec):
        __spec__ = SpecConfig(collection="loose")
        name: str = ""
        extra: dict[str, t.Any] = {}  # noqa: RUF012
        things: list[t.Any] = []  # noqa: RUF012
        anything: t.Any = None
        part: Part | None = None
        parts: list[Part] = []  # noqa: RUF012

    return Part, Loose


@pytest.fixture(params=BACKENDS)
async def world(request, make_registry):
    Part, Loose = models(request.param)
    await make_registry(Loose)

    def api(raw):
        resource = Resource(Loose, name="loose", ops=[List, Get, Create, Patch])
        resource.raw_reads = raw
        return Api(resource, prefix="/v1")

    return Loose, api(True), api(False)


async def test_bson_only_values_in_untyped_fields_are_served(world):
    Loose, fast, slow = world
    stored = await Loose.collection().insert_one(
        {
            "name": "x",
            "extra": dict(VALUES),
            "things": list(VALUES.values()),
            "anything": VALUES["oid"],
            "part": {"label": "p", "meta": dict(VALUES)},
            "parts": [{"label": "q", "meta": {"k": VALUES["when"], "o": [VALUES["oid"]]}}],
        }
    )
    for path in (f"/v1/loose/{stored.inserted_id}", "/v1/loose"):
        bodies = []
        for api in (fast, slow):
            response = await api.handle("GET", path)
            assert response.status == 200, (path, response.body)
            bodies.append(response.body)
        assert bodies[0] == bodies[1]  # served from the stored document or through the model: the same
        data = json.loads(bodies[0])["data"]
        row = data[0] if isinstance(data, list) else data
        assert row["extra"] == EXPECTED
        assert row["things"] == list(EXPECTED.values())
        assert row["anything"] == EXPECTED["oid"]
        assert row["part"] == {"label": "p", "meta": EXPECTED}
        assert row["parts"] == [{"label": "q", "meta": {"k": EXPECTED["when"], "o": [EXPECTED["oid"]]}}]
    # and the same through a tracked model outside the web layer
    doc = await Loose.get(stored.inserted_id)
    assert view_of(Loose).serialize(doc)["extra"] == EXPECTED


async def test_json_written_into_untyped_fields_round_trips(world):
    """A client can only send JSON types, and they come back as sent."""
    Loose, fast, slow = world
    payload = {
        "name": "j",
        "extra": {"a": [1, 2.5, "s", None, True, {"b": {}}]},
        "things": [[], {}, "x"],
        "anything": {"k": 1},
    }
    headers = {"content-type": "application/json"}
    created = await fast.handle("POST", "/v1/loose", body=json.dumps(payload).encode(), headers=headers)
    assert created.status == 201, created.body
    row = json.loads(created.body)["data"]
    assert {k: row[k] for k in payload} == payload
    for api in (fast, slow):
        got = json.loads((await api.handle("GET", created.headers["location"])).body)["data"]
        assert {k: got[k] for k in payload} == payload
    patched = await fast.handle(
        "PATCH",
        created.headers["location"],
        body=json.dumps({"extra": {"z": [None]}}).encode(),
        headers=headers,
    )
    assert patched.status == 200 and json.loads(patched.body)["data"]["extra"] == {"z": [None]}
