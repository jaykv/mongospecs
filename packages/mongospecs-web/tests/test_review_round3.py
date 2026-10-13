"""Regression tests for the third review of v2: one test per reproduced defect."""

import json
import typing as t

import httpx
import pytest

from mongospecs import Policy, Revision, SpecConfig
from mongospecs_web import Api, Create, Cursor, Delete, Get, List, Put, Resource
from mongospecs_web.testing import PLUGS, FakeBackend, mount_on

JSON = {"content-type": "application/json"}


def api_for(*, revision=False, **options):
    from mongospecs.msgspec import Spec

    if revision:

        class Item(Spec, kw_only=True):
            __spec__ = SpecConfig(collection="r3_items")
            name: t.Annotated[str, Policy(filter=("eq", "in"), sort=True)] = ""
            price: float = 0.0
            rev: t.Annotated[int, Revision] = 0

    else:

        class Item(Spec, kw_only=True):
            __spec__ = SpecConfig(collection="r3_items")
            name: t.Annotated[str, Policy(filter=("eq", "in"), sort=True)] = ""
            price: float = 0.0

    store = FakeBackend(Item)
    resource = Resource(Item, name="items", backend=store, page=Cursor(total=True))
    return Api(resource, prefix="/api", **options), store


async def create(api, **fields):
    response = await api.handle("POST", "/api/items", body=json.dumps(fields).encode(), headers=JSON)
    assert response.status == 201, response.body
    return response.headers["location"]


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
async def test_2_numbers_json_does_not_have_are_rejected(literal):
    api, store = api_for()
    url = await create(api, name="a", price=1.0)
    for method, path in (("POST", "/api/items"), ("PATCH", url)):
        response = await api.handle(method, path, body=b'{"price":' + literal + b"}", headers=JSON)
        assert response.status == 400, (method, response.status, response.body)
    assert [d.price for d in store.docs.values()] == [1.0]


async def test_3_json_nested_too_deeply_is_a_bad_request(caplog):
    api, _ = api_for()
    response = await api.handle("POST", "/api/items", body=b"[" * 200_000, headers=JSON)
    assert response.status == 400, response.body
    assert not caplog.records  # the client's mistake is not a server error


async def test_4_if_match_cannot_be_satisfied_without_revisions():
    api, store = api_for()
    url = await create(api, name="a")
    for method, body in (("PATCH", b'{"name":"b"}'), ("DELETE", b"")):
        response = await api.handle(method, url, body=body, headers={**JSON, "if-match": '"7"'})
        assert response.status == 412, (method, response.status)
    assert [d.name for d in store.docs.values()] == ["a"]
    # "*" only asks that the resource exists, which needs no revision
    assert (
        await api.handle("PATCH", url, body=b'{"name":"b"}', headers={**JSON, "if-match": "*"})
    ).status == 200


async def test_if_none_match_reads_etags_the_way_if_match_does():
    api, _ = api_for(revision=True)
    url = await create(api, name="a")
    for header, status in (('"0"', 304), ('W/"0"', 304), ('"9", "0"', 304), ("*", 304), ('"9"', 200)):
        response = await api.handle("GET", url, headers={"if-none-match": header})
        assert response.status == status, header


async def test_5_a_request_the_client_abandoned_is_not_run():
    api, store = api_for()
    url = await create(api, name="victim")
    messages = iter([{"type": "http.request", "body": b"", "more_body": True}, {"type": "http.disconnect"}])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "DELETE", "path": url, "headers": [], "query_string": b""}
    await api.asgi()(scope, receive, send)
    assert len(store.docs) == 1 and not sent


async def test_6_a_private_openapi_stays_private_on_fastapi():
    from fastapi import FastAPI

    from mongospecs_web.plugs.fastapi import mount

    api, _ = api_for(openapi=False)
    app = FastAPI()
    mount(app, api)
    spec = app.openapi()
    assert not [path for path in spec["paths"] if path.startswith("/api")]
    assert "ItemRead" not in spec.get("components", {}).get("schemas", {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get("/api/items")).status_code == 200  # still served
    # and it can be asked for explicitly, as on the other plugs
    shown = FastAPI()
    mount(shown, api, openapi=True)
    assert "/api/items" in shown.openapi()["paths"]


def test_9_guards_and_hooks_must_name_an_operation():
    from mongospecs.msgspec import Spec

    class Thing(Spec, kw_only=True):
        __spec__ = SpecConfig(collection="r3_things")
        name: str = ""

    def deny(ctx):
        return False

    for guards in ({"patchh": deny}, {Put: deny}):  # a typo; an operation this resource does not have
        resource = Resource(Thing, backend=FakeBackend(Thing), guards=guards)
        with pytest.raises(ValueError, match="guards"):
            Api(resource).match("GET", "/r3_things")
    hooked = Resource(Thing, backend=FakeBackend(Thing), ops=[List, Get])
    hooked.before(Create)(lambda ctx: None)
    with pytest.raises(ValueError, match="before"):
        Api(hooked).match("GET", "/r3_things")
    # a custom operation may be guarded before it is declared
    late = Resource(Thing, backend=FakeBackend(Thing), guards={"publish": deny, Delete: deny})

    @late.op("publish")
    async def publish(ctx):
        return None

    assert Api(late).match("GET", "/r3_things")[0].operation.name == "list"


async def test_overload_is_refused_before_authentication_runs():
    import asyncio

    authenticated = []
    release = asyncio.Event()

    async def authenticate(request):
        authenticated.append(1)
        await release.wait()
        return "ann"

    api, _ = api_for(authenticate=authenticate, max_concurrency=1)
    first = asyncio.ensure_future(api.handle("GET", "/api/items"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        second = await asyncio.wait_for(api.handle("GET", "/api/items"), 2)
    finally:
        release.set()
    assert second.status == 503 and len(authenticated) == 1
    assert (await first).status == 200
    assert (await api.handle("GET", "/api/items")).status == 200  # the slot was given back


async def test_a_filter_or_sort_key_given_twice_is_an_error():
    api, _ = api_for()
    await create(api, name="a")
    for query in (
        [("name", "a"), ("name", "b")],
        [("sort", "name,-name")],
        [("sort", "name"), ("sort", "name")],
    ):
        assert (await api.handle("GET", "/api/items", query=query)).status == 400, query
    listed = await api.handle("GET", "/api/items", query=[("name[in]", "a"), ("name[in]", "b")])
    assert listed.status == 200 and len(json.loads(listed.body)["data"]) == 1


@pytest.mark.parametrize("plug", PLUGS)
async def test_every_plug_refuses_what_the_core_refuses(plug):
    api, store = api_for()
    url = await create(api, name="a", price=1.0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mount_on(plug, api)), base_url="http://t"
    ) as client:
        assert (await client.patch(url, content=b'{"price":NaN}', headers=JSON)).status_code == 400
        assert (
            await client.patch(url, content=b'{"name":"b"}', headers={**JSON, "if-match": '"3"'})
        ).status_code == 412
    assert [(d.name, d.price) for d in store.docs.values()] == [("a", 1.0)]
