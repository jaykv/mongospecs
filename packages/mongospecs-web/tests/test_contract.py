"""Every plug x model backend must produce the reference transcript."""

import httpx
import pytest

from mongospecs_web.testing import contract_api, reference_transcript, run_contract

BACKENDS = ("msgspec", "pydantic", "attrs")
PLUGS = ("asgi", "starlette", "fastapi", "litestar", "quart")


def build_app(plug, api):
    if plug == "asgi":
        return api.asgi()
    if plug == "starlette":
        from starlette.applications import Starlette

        from mongospecs_web.plugs.starlette import routes

        return Starlette(routes=routes(api))
    if plug == "fastapi":
        from fastapi import FastAPI

        from mongospecs_web.plugs.fastapi import mount

        app = FastAPI(openapi_url="/api/openapi.json")
        mount(app, api)
        return app
    if plug == "litestar":
        from litestar import Litestar

        from mongospecs_web.plugs.litestar import MongospecsPlugin

        return Litestar(plugins=[MongospecsPlugin(api)], openapi_config=None)
    if plug == "quart":
        from quart import Quart

        from mongospecs_web.plugs.quart import init_app

        app = Quart("contract")
        init_app(app, api)
        return app
    raise AssertionError(plug)


async def transcript_for(plug, backend):
    api = contract_api(backend)
    app = build_app(plug, api)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://contract"
    ) as client:
        return await run_contract(client, api)


@pytest.fixture(scope="module")
def references():
    return {}


@pytest.mark.parametrize("backend", BACKENDS)
async def test_reference_transcript_is_what_the_api_should_do(backend, references):
    transcript = await reference_transcript(backend)
    references[backend] = transcript
    got = {step["label"]: step for step in transcript}
    status = {label: step["status"] for label, step in got.items()}
    expected = {
        "create needs login": 401,
        "malformed credentials": 401,
        "create": 201,
        "create second": 201,
        "create public": 201,
        "create duplicate sku": 409,
        "create missing required": 422,
        "create wrong type": 422,
        "create unknown field": 422,
        "create read-only field": 422,
        "create private field": 422,
        "create managed field": 422,
        "create with id": 422,
        "create not an object": 422,
        "create bad json": 400,
        "create wrong media type": 415,
        "create empty body": 422,
        "get own": 200,
        "get someone else's is 404": 404,
        "get anonymous is 404": 404,
        "get public anonymously": 200,
        "get as admin": 200,
        "get malformed id": 404,
        "get missing": 404,
        "get not modified": 304,
        "list anonymous sees public only": 200,
        "list as ann": 200,
        "list sorted desc": 200,
        "list first page": 200,
        "list bad cursor": 400,
        "filter eq": 200,
        "filter prefix": 200,
        "filter in": 200,
        "filter range": 200,
        "filter int cast": 200,
        "filter exists": 200,
        "filter ne with total": 200,
        "filter on unfilterable field": 400,
        "filter on private field": 400,
        "filter operator not allowed": 400,
        "operator injection in brackets": 400,
        "operator injection as key": 400,
        "filter uncastable value": 400,
        "sort on unsortable field": 400,
        "limit too small": 400,
        "limit not a number": 400,
        "limit is capped": 200,
        "page number on cursor resource": 400,
        "include not allowed": 400,
        "offset page 1": 200,
        "offset page 2 with total": 200,
        "cursor on offset resource": 400,
        "method not allowed": 405,
        "patch needs login": 401,
        "patch": 200,
        "patch stale revision": 412,
        "patch current revision": 200,
        "patch bad if-match": 400,
        "patch null clears optional": 200,
        "patch null on required": 422,
        "patch wrong type": 422,
        "patch insert-only field": 422,
        "patch unknown field": 422,
        "patch empty object": 200,
        "patch someone else's is 404": 404,
        "patch missing": 404,
        "custom op": 200,
        "custom op out of scope": 404,
        "custom collection op": 200,
        "delete needs login": 401,
        "delete forbidden for non-admin": 403,
        "delete": 204,
        "delete again is 404": 404,
        "list after delete": 200,
        "unknown route": 404,
        "unsupported method": 405,
        "openapi": 200,
    }
    assert status == expected

    def names(label):
        return [w["name"] for w in got[label]["body"]["data"]]

    created = got["create"]
    assert created["headers"]["location"] == f"/api/widgets/{1:024x}" and created["headers"]["etag"] == '"0"'
    assert created["body"]["data"] == {
        "id": f"{1:024x}",
        "name": "anvil",
        "price": 10.5,
        "stock": 3,
        "tags": ["heavy"],
        "owner": "ann",
        "sku": "A-1",
        "audit": None,
        "rev": 0,
    }
    assert "internal_note" not in created["body"]["data"]  # private fields never leave
    assert got["get own"]["body"]["data"]["audit"] == "created by ann"  # after-hook ran
    assert names("list anonymous sees public only") == ["cog"]
    assert names("list as ann") == ["anvil", "cog"]
    assert names("list sorted desc") == ["anvil", "cog", "bolt"]
    assert names("list first page") == ["anvil", "bolt"]
    assert got["list first page"]["body"]["page"] == {"size": 2, "has_next": True, "next_cursor": f"{2:024x}"}
    assert names("filter eq") == ["bolt"] and names("filter prefix") == ["anvil"]
    assert names("filter in") == ["anvil", "cog"] and names("filter range") == ["cog", "anvil"]
    assert names("filter int cast") == ["anvil", "cog"] and names("filter exists") == [
        "anvil",
        "bolt",
        "cog",
    ]  # null counts as existing, as in MongoDB
    assert got["filter ne with total"]["body"]["page"]["total"] == 2
    assert got["limit is capped"]["body"]["page"]["size"] == 100
    assert names("offset page 1") == ["anvil", "bolt"]
    assert got["offset page 2 with total"]["body"]["page"] == {
        "size": 2,
        "has_next": False,
        "number": 2,
        "total": 3,
    }
    assert got["patch"]["body"]["data"]["price"] == 12 and got["patch"]["headers"]["etag"] == '"1"'
    assert got["patch current revision"]["headers"]["etag"] == '"2"'
    assert got["patch null clears optional"]["body"]["data"]["sku"] is None
    assert got["custom op"]["body"]["data"]["stock"] == 7
    assert got["custom collection op"]["body"]["data"] == {"count": 3, "stock": 16}
    assert names("list after delete") == ["anvil", "cog"]
    assert got["delete"]["body"] is None and "content-type" not in got["delete"]["headers"]
    # errors are RFC 9457 problem documents and never leak internals
    for label, step in got.items():
        if step["status"] >= 400 and not step.get("framework"):
            assert step["headers"]["content-type"] == "application/problem+json", label
            assert step["body"]["status"] == step["status"] and step["body"]["type"] == "about:blank", label
    assert got["create needs login"]["headers"]["www-authenticate"] == "Bearer"
    assert got["create duplicate sku"]["body"]["errors"] == [{"field": "sku", "message": "must be unique"}]
    assert got["create unknown field"]["body"]["errors"] == [{"field": "colour", "message": "unknown field"}]
    assert got["create read-only field"]["body"]["errors"] == [
        {"field": "audit", "message": "cannot be set on create"}
    ]
    assert got["create private field"]["body"]["errors"] == [
        {"field": "internal_note", "message": "unknown field"}
    ]
    assert got["patch insert-only field"]["body"]["errors"] == [
        {"field": "owner", "message": "cannot be set on update"}
    ]
    assert got["create missing required"]["body"]["errors"][0]["field"] == "name"
    assert got["patch wrong type"]["body"]["errors"][0]["field"] == "stock"
    assert got["openapi"]["body"]["paths"] == [
        "/api/widget-pages",
        "/api/widgets",
        "/api/widgets/stats",
        "/api/widgets/{id}",
        "/api/widgets/{id}/restock",
    ]
    assert {"WidgetRead", "WidgetCreate", "WidgetPatch", "Problem"} <= set(got["openapi"]["body"]["schemas"])


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("plug", PLUGS)
async def test_every_plug_matches_the_reference(plug, backend, references):
    reference = references.get(backend) or await reference_transcript(backend)
    transcript = await transcript_for(plug, backend)
    for ours, theirs in zip(transcript, reference, strict=True):
        assert ours == theirs, ours["label"]
