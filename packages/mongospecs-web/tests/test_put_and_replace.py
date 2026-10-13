"""PUT replaces what the client may write; ``merge_patch=False`` makes PATCH replace embedded objects."""

import json
import typing as t

import pytest

from mongospecs import Immutable, Policy, Revision, SpecConfig, StoreNone
from mongospecs_web import Api, Create, Get, Patch, Put, Resource

BACKENDS = ("msgspec", "pydantic", "attrs")
JSON = {"content-type": "application/json"}


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
    class Geo(Base):
        lat: float = 0.0
        surveyor: t.Annotated[str | None, Policy(private=True)] = None

    @define
    class Addr(Base):
        city: str  # required
        zip: str | None = None
        note: t.Annotated[str | None, StoreNone] = None
        vault_code: t.Annotated[str, Policy(private=True)] = ""
        verified: t.Annotated[bool, Policy(insert=False, update=False)] = False
        code: t.Annotated[str, Immutable] = ""
        geo: Geo | None = None

    @define
    class Tag(Base):
        label: str = ""
        weight: int = 0

    @define
    class Person(Spec):
        __spec__ = SpecConfig(collection="put_persons")
        name: str  # required
        nick: str | None = None
        score: int = 0
        owner: t.Annotated[str, Policy(update=False)] = ""
        secret: t.Annotated[str | None, Policy(private=True)] = None
        address: Addr | None = None
        history: list[Addr] = []  # noqa: RUF012
        tag: Tag | None = None
        tags: list[Tag] = []  # noqa: RUF012
        rev: t.Annotated[int, Revision] = 0

    return Geo, Addr, Tag, Person


@pytest.fixture(params=BACKENDS)
async def world(request, make_registry):
    Geo, Addr, Tag, Person = models(request.param)
    events = []
    await make_registry(Person, on_command=events.append)
    api = Api(
        Resource(Person, name="persons", ops=[Get, Create, Patch, Put]),
        Resource(Person, name="replaced", ops=[Get, Patch], merge_patch=False),
        prefix="/v1",
    )

    async def call(method, path, body=None, headers=None):
        response = await api.handle(
            method,
            path,
            body=json.dumps(body).encode() if body is not None else b"",
            headers={**JSON, **(headers or {})},
        )
        return response.status, json.loads(response.body) if response.body else None

    async def person():
        doc = await Person(
            name="ann",
            nick="annie",
            score=7,
            owner="ann-id",
            secret="s3",
            address=Addr(
                city="Oslo",
                zip="0150",
                note="n",
                vault_code="vault",
                verified=True,
                code="C1",
                geo=Geo(lat=1.0, surveyor="sv"),
            ),
            history=[Addr(city="Old", vault_code="old-vault")],
            tag=Tag(label="t", weight=1),
            tags=[Tag(label="a"), Tag(label="b")],
        ).insert()
        return doc, str(doc.id)

    async def stored(doc):
        return await Person.collection().find_one({"_id": doc.id})

    return Person, api, call, person, stored, events


async def test_put_replaces_what_the_client_may_write_and_keeps_the_rest(world):
    Person, api, call, person, stored, events = world
    doc, id = await person()
    before = await stored(doc)
    events.clear()
    status, body = await call(
        "PUT", f"/v1/persons/{id}", {"name": "bob", "address": {"city": "Bergen"}, "tag": {"label": "x"}}
    )
    assert status == 200, body
    writes = [e for e in events if e.phase == "started" and e.command_name != "endSessions"]
    assert [e.command_name for e in writes] == ["findAndModify"]  # one atomic write
    raw = await stored(doc)
    # sent: replaced. Left out: back to the default.
    assert raw["name"] == "bob" and raw["score"] == 0 and raw.get("nick") is None
    assert raw["tag"] == {"label": "x", "weight": 0}  # nothing to keep in it: replaced whole
    assert raw.get("tags", []) == []
    # what the client cannot write is untouched
    assert raw["owner"] == "ann-id" and raw["secret"] == "s3"
    assert raw["history"] == before["history"]
    address = raw["address"]
    assert address["city"] == "Bergen" and "zip" not in address and address["note"] is None
    assert address["vault_code"] == "vault" and address["verified"] is True and address["code"] == "C1"
    assert address["geo"] == {"lat": 1.0, "surveyor": "sv"}  # not sent, holds a server value: kept
    assert body["data"]["rev"] == 1 and body["data"]["name"] == "bob"
    assert "secret" not in body["data"] and "vault_code" not in body["data"]["address"]
    # deeper: an object sent inside the embedded one is replaced the same way
    status, body = await call(
        "PUT", f"/v1/persons/{id}", {"name": "bob", "address": {"city": "Bergen", "zip": "5", "geo": {}}}
    )
    assert status == 200, body
    address = (await stored(doc))["address"]
    assert address["zip"] == "5" and address["geo"] == {"lat": 0.0, "surveyor": "sv"}
    assert (await stored(doc)).get("tag") is None  # left out this time
    assert (await Person.get(doc.id)).address.verified is True


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({}, "name", "is required"),
        ({"name": "b", "address": {"zip": "1"}}, "address.city", "is required"),
        ({"name": "b", "owner": "mallory"}, "owner", "cannot be set on update"),
        ({"name": "b", "secret": "x"}, "secret", "unknown field"),
        ({"name": "b", "address": {"city": "c", "verified": False}}, "address.verified", "cannot be set"),
        ({"name": "b", "address": {"city": "c", "vault_code": "x"}}, "address.vault_code", "unknown field"),
        ({"name": "b", "address": None}, "address", "cannot be removed"),
        ({"name": "b", "address": {"city": "c", "geo": None}}, "address.geo", "cannot be removed"),
        ({"name": "b", "history": []}, "history", "cannot be replaced"),
        ({"name": "b", "score": "many"}, "score", None),
    ],
)
async def test_put_rejects_what_it_cannot_do_and_writes_nothing(world, body, field, message):
    Person, api, call, person, stored, events = world
    doc, id = await person()
    before = await stored(doc)
    status, problem = await call("PUT", f"/v1/persons/{id}", body)
    assert status == 422, problem
    errors = {e["field"]: e["message"] for e in problem["errors"]}
    assert field in errors, errors
    if message:
        assert message in errors[field]
    assert await stored(doc) == before


async def test_put_conditions_and_routing(world):
    Person, api, call, person, stored, events = world
    doc, id = await person()
    assert (await call("PUT", f"/v1/persons/{id}", {"name": "b"}, {"if-match": '"7"'}))[0] == 412
    status, body = await call("PUT", f"/v1/persons/{id}", {"name": "b"}, {"if-match": '"0"'})
    assert status == 200 and body["data"]["rev"] == 1
    assert (await stored(doc))["address"]["city"] == "Oslo"  # not sent, holds server values: kept
    assert (await call("PUT", "/v1/persons/000000000000000000000000", {"name": "b"}))[0] == 404
    assert await Person.count() == 1  # PUT never creates
    assert (await call("PUT", f"/v1/replaced/{id}", {"name": "b"}))[0] == 405  # only where ops= lists it


async def test_put_into_an_embedded_object_that_does_not_exist_yet(world):
    Person, api, call, person, stored, events = world
    bare = await Person(name="bare").insert()
    status, body = await call("PUT", f"/v1/persons/{bare.id}", {"name": "bare", "address": {"city": "Rome"}})
    assert status == 200, body
    assert body["data"]["address"]["city"] == "Rome" and body["data"]["address"]["verified"] is False


async def test_patch_replaces_embedded_objects_when_merging_is_turned_off(world):
    Person, api, call, person, stored, events = world
    doc, id = await person()
    url = f"/v1/replaced/{id}"
    # a partial object is not enough: the embedded model is validated whole
    status, body = await call("PATCH", url, {"address": {"zip": "1"}})
    assert status == 422, body
    assert (await stored(doc))["address"]["zip"] == "0150"
    status, body = await call("PATCH", url, {"address": {"city": "Bergen"}})
    assert status == 200, body
    raw = await stored(doc)
    # replaced whole: what the client did not (and could not) send is back at its default
    assert raw["address"]["city"] == "Bergen" and "zip" not in raw["address"]
    assert raw["address"].get("vault_code", "") == "" and raw["address"].get("verified", False) is False
    assert raw["name"] == "ann" and raw["score"] == 7  # other fields are still left alone
    # it still may not set what the policy closes
    for patch, field in (
        ({"address": {"city": "c", "verified": True}}, "address.verified"),
        ({"address": {"city": "c", "code": "C2"}}, "address.code"),
        ({"address": {"city": "c", "vault_code": "x"}}, "address.vault_code"),
        ({"history": [{"city": "c", "verified": True}]}, "history.0.verified"),
    ):
        status, body = await call("PATCH", url, patch)
        assert status == 422 and field in [e["field"] for e in body["errors"]], body
    # lists of such objects and null are accepted again
    status, body = await call("PATCH", url, {"history": [{"city": "New"}], "address": None})
    assert status == 200, body
    raw = await stored(doc)
    assert raw.get("address") is None and [h["city"] for h in raw["history"]] == ["New"]
    # a stored null is no obstacle to a whole object
    await Person.collection().update_one({"_id": doc.id}, {"$set": {"address": None}})
    status, body = await call("PATCH", url, {"address": {"city": "Rome"}})
    assert status == 200 and body["data"]["address"]["city"] == "Rome"
    # the same request on the merging resource merges
    status, body = await call("PATCH", f"/v1/persons/{id}", {"address": {"zip": "9"}})
    assert status == 200 and body["data"]["address"] == {
        **body["data"]["address"],
        "city": "Rome",
        "zip": "9",
    }


async def test_replacing_runs_the_embedded_models_own_validators(make_registry):
    import pydantic

    from mongospecs.pydantic import Spec

    class Span(pydantic.BaseModel):
        low: int = 0
        high: int = 10

        @pydantic.model_validator(mode="after")
        def ordered(self):
            if self.low > self.high:
                raise ValueError("low must not exceed high")
            return self

    class Gauge(Spec):
        __spec__ = SpecConfig(collection="put_gauges")
        span: Span = Span()

    await make_registry(Gauge)
    api = Api(Resource(Gauge, name="replaced", ops=[Patch, Put], merge_patch=False))
    doc = await Gauge(span=Span(low=0, high=100)).insert()

    async def send(method, name, body):
        return (await api.handle(method, f"/{name}/{doc.id}", body=json.dumps(body).encode())).status

    # the object as sent is what gets validated: high is left out, so it is the default 10
    assert await send("PATCH", "replaced", {"span": {"low": 50}}) == 422
    assert await send("PUT", "replaced", {"span": {"low": 50}}) == 422
    assert (await Gauge.get(doc.id)).span == Span(low=0, high=100)
    assert await send("PATCH", "replaced", {"span": {"low": 50, "high": 60}}) == 200
    assert (await Gauge.get(doc.id)).span == Span(low=50, high=60)


async def test_openapi_describes_put_and_the_replacing_patch(world):
    Person, api, call, person, stored, events = world
    spec = api.openapi()
    schemas = spec["components"]["schemas"]
    put = spec["paths"]["/v1/persons/{id}"]["put"]
    assert put["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PersonPut"
    }
    assert "If-Match" in [p["name"] for p in put["parameters"]]
    assert {"404", "412", "422"} <= set(put["responses"])
    body = schemas["PersonPut"]
    assert body["required"] == ["name"] and body["additionalProperties"] is False
    assert set(body["properties"]) == {"name", "nick", "score", "address", "tag", "tags"}
    assert "AddrPut" in json.dumps(body["properties"]["address"])
    addr = next(v for k, v in schemas.items() if k.endswith("AddrPut"))
    assert addr["required"] == ["city"] and set(addr["properties"]) == {"city", "zip", "note", "geo"}
    assert "GeoPut" in json.dumps(addr["properties"]["geo"])
    assert "TagPut" not in json.dumps(schemas)  # nothing to keep in a Tag: it is sent whole
    assert "put" not in spec["paths"]["/v1/replaced/{id}"]
    # one schema name per model, so two resources over one model share it; ask the view directly
    replacing = api.resources[1].view.schema("patch", merge=False)
    assert "AddrPatch" not in json.dumps(replacing) and "Addr" in json.dumps(
        replacing["properties"]["address"]
    )
