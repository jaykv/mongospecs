"""Policy(private=True) on the fields of embedded (non-document) models, at any depth."""

import json
import typing as t

import httpx
import pytest

from mongospecs import Policy, Ref, SpecConfig
from mongospecs_web import Api, Create, Get, List, Patch, Resource
from mongospecs_web.policy import view_of

BACKENDS = ("msgspec", "pydantic", "attrs")
SECRETS = ("vault_code", "surveyor")


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
        city: str = ""
        vault_code: t.Annotated[str, Policy(private=True)] = ""
        geo: Geo | None = None

    @define
    class Company(Spec):
        __spec__ = SpecConfig(collection="companies")
        name: str = ""
        hq: Addr | None = None

    @define
    class Person(Spec):
        __spec__ = SpecConfig(collection="persons")
        name: t.Annotated[str, Policy(filter=("eq",), sort=True)] = ""
        address: Addr | None = None
        history: list[Addr] = []  # noqa: RUF012
        by_label: dict[str, Addr] = {}  # noqa: RUF012
        employer: Ref[Company] | None = None

    return Geo, Addr, Company, Person


@pytest.fixture(params=BACKENDS)
async def world(request, make_registry):
    Geo, Addr, Company, Person = models(request.param)
    await make_registry(Company, Person)
    api = Api(
        Resource(Person, name="persons", ops=[List, Get, Create, Patch], include=["employer"]),
        Resource(Company, name="companies", ops=[Get]),
        prefix="/v1",
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.asgi()), base_url="http://t")
    try:
        yield client, api, (Geo, Addr, Company, Person)
    finally:
        await client.aclose()


async def seeded(Geo, Addr, Company, Person):
    def addr(city):
        return Addr(city=city, vault_code=f"vault-{city}", geo=Geo(lat=1.5, surveyor=f"surveyor-{city}"))

    acme = await Company(name="acme", hq=addr("hq")).insert()
    return await Person(
        name="ann",
        address=addr("home"),
        history=[addr("old1"), addr("old2")],
        by_label={"work": addr("work")},
        employer=acme,
    ).insert()


async def test_nested_private_fields_are_never_read(world):
    client, api, classes = world
    ann = await seeded(*classes)
    stored = await classes[3].collection().find_one({"_id": ann.id})
    assert stored["address"]["vault_code"] == "vault-home"  # it is stored; it is only never shown
    for url in (
        f"/v1/persons/{ann.id}",
        "/v1/persons",
        f"/v1/persons/{ann.id}?include=employer",
        "/v1/persons?include=employer",
        f"/v1/companies/{ann.employer.id}",
    ):
        response = await client.get(url)
        assert response.status_code == 200, response.text
        for secret in (*SECRETS, "vault-", "surveyor-"):
            assert secret not in response.text, (url, secret)
    body = (await client.get(f"/v1/persons/{ann.id}?include=employer")).json()["data"]
    assert body["address"] == {"city": "home", "geo": {"lat": 1.5}}
    assert [a["city"] for a in body["history"]] == ["old1", "old2"] and body["by_label"]["work"][
        "city"
    ] == "work"
    assert body["employer"]["hq"] == {"city": "hq", "geo": {"lat": 1.5}}


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"address": {"city": "x", "vault_code": "client-set"}}, "address.vault_code"),
        ({"address": {"city": "x", "geo": {"lat": 1, "surveyor": "me"}}}, "address.geo.surveyor"),
        ({"history": [{"city": "a"}, {"city": "b", "vault_code": "v"}]}, "history.1.vault_code"),
        ({"by_label": {"work": {"geo": {"surveyor": "me"}}}}, "by_label.work.geo.surveyor"),
    ],
)
async def test_nested_private_fields_are_never_written(world, payload, field):
    client, api, (Geo, Addr, Company, Person) = world
    existing = await Person(name="bo", address=Addr(city="here", vault_code="server-set")).insert()
    for method, url in (("POST", "/v1/persons"), ("PATCH", f"/v1/persons/{existing.id}")):
        response = await client.request(method, url, json={"name": "eve", **payload})
        assert response.status_code == 422, (method, response.text)
        assert {"field": field, "message": "unknown field"} in response.json()["errors"]
    assert await Person.count() == 1
    assert (await Person.get(existing.id)).address.vault_code == "server-set"
    # without the private keys the same shapes are accepted
    ok = await client.post("/v1/persons", json={"name": "ok", "address": {"city": "x", "geo": {"lat": 2}}})
    assert ok.status_code == 201 and ok.json()["data"]["address"] == {"city": "x", "geo": {"lat": 2.0}}
    patched = await client.patch(f"/v1/persons/{existing.id}", json={"address": {"city": "moved"}})
    assert patched.status_code == 200 and patched.json()["data"]["address"]["city"] == "moved"


async def test_nested_private_fields_are_not_described_or_addressable(world):
    client, api, classes = world
    text = json.dumps(api.openapi())
    for secret in SECRETS:
        assert secret not in text
    assert "city" in text and "lat" in text  # the public nested fields are described
    await seeded(*classes)
    for query in (
        "address.vault_code=x",
        "address.vault_code[eq]=x",
        "sort=address.vault_code",
        "include=address.vault_code",
    ):
        response = await client.get(f"/v1/persons?{query}")
        assert response.status_code == 400, (query, response.text)
        assert "vault-" not in response.text


def test_views_without_embedded_private_fields_do_no_extra_work():
    from mongospecs_web.testing import contract_api

    assert view_of(contract_api("msgspec").resources[0].model)._nested == {}
