"""PATCH on embedded objects merges; values only the server sets can never be cleared or changed."""

import json
import typing as t

import pytest

from mongospecs import Immutable, Policy, Revision, SpecConfig, StoreNone
from mongospecs_web import Api, Create, Get, Patch, Resource

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
        __spec__ = SpecConfig(collection="patch_persons")
        name: str = ""
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
    api = Api(Resource(Person, name="persons", ops=[Get, Create, Patch]), prefix="/v1")

    async def call(method, path, body=None, headers=None):
        response = await api.handle(
            method,
            path,
            body=json.dumps(body).encode() if body is not None else b"",
            headers={**JSON, **(headers or {})},
        )
        return response.status, json.loads(response.body) if response.body else None

    async def person(**extra):
        doc = await Person(
            name="ann",
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
            **extra,
        ).insert()
        return doc, f"/v1/persons/{doc.id}"

    async def stored(doc):
        return await Person.collection().find_one({"_id": doc.id})

    return Person, api, call, person, stored, events


async def test_patching_an_embedded_object_merges_and_keeps_server_values(world):
    Person, api, call, person, stored, events = world
    doc, url = await person()
    events.clear()
    status, body = await call("PATCH", url, {"address": {"zip": "9999"}})
    assert status == 200, body
    writes = [e for e in events if e.phase == "started" and e.command_name != "endSessions"]
    raw = (await stored(doc))["address"]
    assert raw["zip"] == "9999" and raw["city"] == "Oslo"  # the rest of the object is untouched
    assert raw["vault_code"] == "vault" and raw["verified"] is True and raw["code"] == "C1"
    assert raw["geo"] == {"lat": 1.0, "surveyor": "sv"}
    assert body["data"]["address"]["zip"] == "9999" and body["data"]["address"]["city"] == "Oslo"
    assert "vault_code" not in body["data"]["address"]
    # one atomic write with dotted paths, and one revision
    assert [e.command_name for e in writes] == ["findAndModify"]
    assert writes[0].command["update"]["$set"]["address.zip"] == "9999"
    assert not any(key == "address" for key in writes[0].command["update"]["$set"])  # never the whole object
    assert body["data"]["rev"] == 1
    # deeper: a nested object inside the embedded one merges too
    status, body = await call("PATCH", url, {"name": "anna", "address": {"geo": {"lat": 5}}})
    assert status == 200 and body["data"]["name"] == "anna"
    assert (await stored(doc))["address"]["geo"] == {"lat": 5.0, "surveyor": "sv"}


async def test_null_inside_a_merge_follows_the_fields_mode(world):
    Person, api, call, person, stored, events = world
    doc, url = await person()
    status, body = await call("PATCH", url, {"address": {"zip": None, "note": None}})
    assert status == 200, body
    raw = (await stored(doc))["address"]
    assert "zip" not in raw  # left out while None
    assert "note" in raw and raw["note"] is None  # StoreNone: an explicit null
    assert body["data"]["address"]["zip"] is None and body["data"]["address"]["note"] is None
    status, body = await call("PATCH", url, {"address": {"city": None}})
    assert status == 422 and body["errors"][0]["field"] == "address.city"
    assert (await stored(doc))["address"]["city"] == "Oslo"


@pytest.mark.parametrize(
    ("patch", "field", "message"),
    [
        ({"address": {"nope": 1}}, "address.nope", "unknown field"),
        ({"address": {"vault_code": "x"}}, "address.vault_code", "unknown field"),
        ({"address": {"geo": {"surveyor": "me"}}}, "address.geo.surveyor", "unknown field"),
        ({"address": {"verified": False}}, "address.verified", "cannot be set on update"),
        ({"address": {"code": "C2"}}, "address.code", "cannot be set on update"),
        ({"address": {"geo": {"lat": "north"}}}, "address.geo.lat", None),
        ({"address": None}, "address", "cannot be removed"),
        ({"address": {"geo": None}}, "address.geo", "cannot be removed"),
        ({"history": [{"city": "New"}]}, "history", "cannot be replaced"),
        ({"history": []}, "history", "cannot be replaced"),
    ],
)
async def test_a_client_can_never_clear_or_change_a_server_value(world, patch, field, message):
    Person, api, call, person, stored, events = world
    doc, url = await person()
    before = await stored(doc)
    status, body = await call("PATCH", url, {"name": "changed", **patch})
    assert status == 422, body
    errors = {e["field"]: e["message"] for e in body["errors"]}
    assert field in errors, errors
    if message:
        assert message in errors[field]
    assert await stored(doc) == before  # nothing was written, not even the valid part


async def test_objects_without_server_values_can_be_replaced_or_removed(world):
    Person, api, call, person, stored, events = world
    doc, url = await person()
    status, body = await call("PATCH", url, {"tags": [{"label": "only", "weight": 3}], "tag": {"weight": 9}})
    assert status == 200, body
    raw = await stored(doc)
    assert raw["tags"] == [{"label": "only", "weight": 3}]  # a list is replaced whole
    assert raw["tag"] == {"label": "t", "weight": 9}  # an object is merged
    status, body = await call("PATCH", url, {"tag": None, "tags": []})
    assert status == 200 and body["data"]["tag"] is None and body["data"]["tags"] == []
    assert "tag" not in await stored(doc)


async def test_merging_into_an_object_that_does_not_exist_yet(world):
    Person, api, call, person, stored, events = world
    bare = await Person(name="bare").insert()
    url = f"/v1/persons/{bare.id}"
    status, body = await call("PATCH", url, {"address": {"zip": "1"}})
    assert status == 422, body  # city is required and there is nothing to merge into
    assert body["errors"][0]["field"] == "address" and "city" in body["errors"][0]["message"]
    assert "address" not in await stored(bare)
    status, body = await call("PATCH", url, {"address": {"city": "Bergen", "zip": "1"}})
    assert status == 200 and body["data"]["address"]["city"] == "Bergen"
    assert (await Person.get(bare.id)).address.zip == "1"
    assert (await call("PATCH", "/v1/persons/000000000000000000000000", {"address": {"zip": "1"}}))[0] == 404


async def test_merge_respects_if_match(world):
    Person, api, call, person, stored, events = world
    doc, url = await person()
    assert (await call("PATCH", url, {"address": {"zip": "1"}}, {"if-match": '"7"'}))[0] == 412
    status, body = await call("PATCH", url, {"address": {"zip": "1"}}, {"if-match": '"0"'})
    assert status == 200 and body["data"]["rev"] == 1
    bare = await Person(name="bare").insert()
    assert (await call("PATCH", f"/v1/persons/{bare.id}", {"address": {"zip": "1"}}, {"if-match": '"3"'}))[
        0
    ] == 412


async def test_create_rejects_nested_fields_the_client_may_not_set(world):
    Person, api, call, person, stored, events = world
    status, body = await call(
        "POST", "/v1/persons", {"name": "n", "address": {"city": "x", "verified": True}}
    )
    assert (
        status == 422
        and {"field": "address.verified", "message": "cannot be set on create"} in body["errors"]
    )
    status, body = await call(
        "POST", "/v1/persons", {"name": "n", "history": [{"city": "x", "verified": True}]}
    )
    assert status == 422 and body["errors"][0]["field"] == "history.0.verified"
    status, body = await call("POST", "/v1/persons", {"name": "n", "address": {"city": "x", "code": "C9"}})
    assert status == 201 and body["data"]["address"]["code"] == "C9"  # immutable fields are set on create
    assert await Person.count() == 1


async def test_the_patch_schema_describes_a_partial_object(world):
    Person, api, call, person, stored, events = world
    schemas = api.openapi()["components"]["schemas"]
    patch = (
        schemas["PersonPatch"]
        if "PersonPatch" in schemas
        else next(
            v for k, v in schemas.items() if k.endswith("Patch") and "address" in v.get("properties", {})
        )
    )
    text = json.dumps(patch["properties"]["address"])
    assert "AddrPatch" in text
    partial = next(v for k, v in schemas.items() if k.endswith("AddrPatch"))
    assert "required" not in partial  # every key is optional in a merge
    assert set(partial["properties"]) == {
        "city",
        "zip",
        "note",
        "geo",
    }  # no private, read-only or immutable keys
    assert "GeoPatch" in json.dumps(partial["properties"]["geo"])
    # a list of embedded objects is replaced whole, so its items keep the full schema
    assert "AddrPatch" not in json.dumps(patch["properties"].get("history", {}))
    full = next(v for k, v in schemas.items() if k.endswith("_Addr"))
    assert "city" in full.get("required", [])
