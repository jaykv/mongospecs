import httpx
import pytest
from openapi_spec_validator import validate

from mongospecs_web import Api, Resource, view_of
from mongospecs_web.policy import jsonable
from mongospecs_web.testing import contract_api, mount_on

BACKENDS = ("msgspec", "pydantic", "attrs")


@pytest.mark.parametrize("backend", BACKENDS)
def test_openapi_document_is_valid(backend):
    spec = contract_api(backend).openapi()
    validate(spec)
    assert spec["openapi"] == "3.1.0" and spec["info"] == {"title": "Contract", "version": "1.0.0"}
    listing = spec["paths"]["/api/widgets"]["get"]
    names = [p["name"] for p in listing["parameters"]]
    assert {
        "name",
        "name[prefix]",
        "name[in]",
        "price[gte]",
        "stock[lte]",
        "sort",
        "limit",
        "after",
        "total",
    } <= set(names)
    assert "internal_note" not in str(names) and "tags" not in names
    assert "page" not in names
    assert "page" in [p["name"] for p in spec["paths"]["/api/widget-pages"]["get"]["parameters"]]
    patch = spec["paths"]["/api/widgets/{id}"]["patch"]
    assert patch["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WidgetPatch"
    }
    assert {"404", "409", "412", "422", "401", "403"} <= set(patch["responses"])
    assert "If-Match" in [p["name"] for p in patch["parameters"]]
    assert "204" in spec["paths"]["/api/widgets/{id}"]["delete"]["responses"]
    assert spec["paths"]["/api/widgets/{id}/restock"]["post"]["summary"] == "Add stock to a widget"


@pytest.mark.parametrize("backend", BACKENDS)
def test_view_schemas_follow_the_policy(backend):
    api = contract_api(backend)
    view = api.resources[0].view
    read, create, patch = view.schema("read"), view.schema("create"), view.schema("patch")
    assert "internal_note" not in read["properties"] and "id" in read["properties"]
    assert set(create["properties"]) == {"name", "price", "stock", "tags", "owner", "sku"}
    assert create["required"] == ["name"] and create["additionalProperties"] is False
    assert set(patch["properties"]) == {"name", "price", "stock", "tags", "sku"}  # owner is insert-only
    assert "required" not in patch
    assert view.filterable["name"] == ("eq", "prefix", "in") and view.sortable == {"name", "price"}
    assert view.revision == "rev" and view_of(view.model) is view
    assert read["properties"]["id"].get("anyOf") or read["properties"]["id"].get("type")


def test_jsonable_covers_bson_types():
    import datetime as dt
    import decimal
    import enum
    import uuid

    from bson import ObjectId
    from bson.decimal128 import Decimal128

    class Shade(enum.Enum):
        DARK = "dark"

    oid = ObjectId()
    uid = uuid.uuid4()
    out = jsonable(
        {
            "o": oid,
            "d": dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
            "day": dt.date(2024, 1, 2),
            "n": decimal.Decimal("1.50"),
            "n128": Decimal128("2.5"),
            "u": uid,
            "e": Shade.DARK,
            "b": b"\x00\x01",
            "s": {3, 3},
            "nested": [{"o": oid}],
        }
    )
    assert out == {
        "o": str(oid),
        "d": "2024-01-02T03:04:05Z",
        "day": "2024-01-02",
        "n": "1.50",
        "n128": "2.5",
        "u": str(uid),
        "e": "dark",
        "b": "AAE=",
        "s": [3],
        "nested": [{"o": str(oid)}],
    }


@pytest.mark.parametrize("backend", BACKENDS)
async def test_fastapi_documents_any_backend(backend):
    """Spike S5: externally supplied schema fully replaces FastAPI's pydantic-only generation."""
    from fastapi import FastAPI

    from mongospecs_web.plugs.fastapi import mount

    api = contract_api(backend)
    app = FastAPI(title="Host app")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    mount(app, api)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        spec = (await client.get("/openapi.json")).json()
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/health")).json() == {"status": "ok"}
    validate(spec)
    ours = api.openapi()
    assert "/health" in spec["paths"]  # the host application's own routes are untouched
    for path, methods in ours["paths"].items():
        for method, operation in methods.items():
            theirs = spec["paths"][path][method]
            assert theirs["responses"] == operation["responses"], (path, method)
            assert theirs.get("parameters") == operation.get("parameters"), (path, method)
            assert theirs.get("requestBody") == operation.get("requestBody"), (path, method)
            assert theirs["operationId"] == operation["operationId"]
    assert spec["components"]["schemas"]["WidgetRead"] == ours["components"]["schemas"]["WidgetRead"]
    assert "HTTPValidationError" not in str(spec["paths"]["/api/widgets"])


def test_resource_declaration_errors():
    api = contract_api("msgspec")
    model = api.resources[0].model
    with pytest.raises(ValueError, match="unknown operation"):
        Resource(model, ops=["explode"])
    with pytest.raises(ValueError, match="not a readable relation"):
        Resource(model, include=["name"])
    with pytest.raises(ValueError, match="unique"):
        Api(Resource(model), Resource(model))
    assert Api(Resource(model), prefix="api/").prefix == "/api" and Api(Resource(model)).prefix == ""
    assert api.resources[0].operation("restock").path == "/{id}/restock"
    with pytest.raises(KeyError):
        api.resources[0].operation("nope")


async def test_api_handle_without_a_server():
    api = contract_api("msgspec")
    created = await api.handle(
        "POST", "/api/widgets", body=b'{"name":"direct"}', headers={"authorization": "Bearer ann"}
    )
    assert created.status == 201 and b'"direct"' in created.body
    # Location is a URL that resolves to the new resource, wherever the API is mounted
    assert created.headers["location"].startswith("/api/widgets/")
    assert (
        await api.handle("GET", created.headers["location"], headers={"authorization": "Bearer ann"})
    ).status == 200
    assert (await api.handle("GET", "/api/widgets/")).status == 200  # trailing slash tolerated
    assert (await api.handle("GET", "/api/widgets/stats")).status == 200  # literal segment beats {id}
    assert (await api.handle("GET", "/api/nope")).status == 404
    assert (await api.handle("PUT", "/api/widgets")).headers["allow"] == "GET, POST"


BODY_PLUGS = ("asgi", "starlette", "fastapi", "litestar", "quart")


@pytest.mark.parametrize("plug", BODY_PLUGS)
async def test_oversized_bodies_are_refused_without_being_buffered(plug):
    """Every plug stops reading at the limit, whether or not the client declares the size."""
    api = contract_api("msgspec")
    api.max_body = 256
    app = mount_on(plug, api)
    headers = {"authorization": "Bearer ann", "content-type": "application/json"}
    received = 0

    async def endless():
        nonlocal received
        yield b'{"name": "'
        for _ in range(10_000):
            received += 1
            yield b"x" * 64

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        declared = await client.post("/api/widgets", content=b"x" * 1000, headers=headers)
        assert declared.status_code == 413 and declared.json()["status"] == 413
        streamed = await client.post("/api/widgets", content=endless(), headers=headers)
        assert streamed.status_code == 413
        ok = await client.post("/api/widgets", content=b'{"name": "fits"}', headers=headers)
        assert ok.status_code == 201


async def test_body_is_not_read_past_the_limit():
    """Driving the ASGI app directly shows that reading stops once the limit is passed."""
    api = contract_api("msgspec")
    api.max_body = 256
    pulls = 0

    async def receive():
        nonlocal pulls
        pulls += 1
        return {"type": "http.request", "body": b"x" * 100, "more_body": True}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/widgets",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer ann"), (b"content-type", b"application/json")],
    }
    await api.asgi()(scope, receive, send)
    assert sent[0]["status"] == 413 and pulls == 3  # 300 bytes read, then stopped


async def test_head_options_and_private_openapi():
    api = contract_api("msgspec")
    created = await api.handle(
        "POST", "/api/widgets", body=b'{"name":"w"}', headers={"authorization": "Bearer ann"}
    )
    path = created.headers["location"]
    get = await api.handle("GET", path, headers={"authorization": "Bearer ann"})
    head = await api.handle("HEAD", path, headers={"authorization": "Bearer ann"})
    assert head.status == 200 and head.body == b"" and head.headers["etag"] == get.headers["etag"]
    options = await api.handle("OPTIONS", path)
    assert options.status == 204 and options.headers["allow"] == "DELETE, GET, HEAD, OPTIONS, PATCH"
    assert (await api.handle("OPTIONS", "/api/nope")).status == 404
    assert (await api.handle("HEAD", "/api/widgets/stats")).status == 200
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.asgi()), base_url="http://t"
    ) as client:
        assert (await client.get("/api/openapi.json")).status_code == 200
        assert (await client.head(path, headers={"authorization": "Bearer ann"})).status_code == 200
    model = api.resources[0].model
    private = Api(Resource(model), prefix="/api", openapi=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=private.asgi()), base_url="http://t"
    ) as client:
        assert (await client.get("/api/openapi.json")).status_code == 404
    assert "paths" in private.openapi()  # still available to serve behind your own authentication


def test_explicit_writes_mode_is_default_deny():
    import typing as t

    from mongospecs import Policy
    from mongospecs.msgspec import Spec

    model = contract_api("msgspec").resources[0].model
    open_view = Resource(model).view
    strict = Resource(model, explicit_writes=True).view
    assert {"name", "price", "tags", "sku"} <= open_view.insertable
    # nothing on the contract model opts in explicitly, so nothing is writable
    assert strict.insertable == frozenset() and strict.updatable == frozenset()
    assert strict.schema("create")["properties"] == {}
    assert view_of(model, explicit_writes=True) is strict and view_of(model) is open_view

    class Opt(Spec):
        title: t.Annotated[str, Policy(insert=True, update=True)] = ""
        internal: str = ""
        created_only: t.Annotated[str, Policy(insert=True)] = ""

    view = view_of(Opt, explicit_writes=True)
    assert view.insertable == {"title", "created_only"} and view.updatable == {"title"}
