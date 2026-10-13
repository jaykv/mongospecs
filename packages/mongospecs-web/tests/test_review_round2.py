"""Regression tests for the second QA review of v2 (issue numbers refer to that review)."""

import json

import httpx
import pytest

from mongospecs_web import Api, Delete, Get, List, Resource
from mongospecs_web.testing import PLUGS, contract_api, mount_on

BACKENDS = ("msgspec", "pydantic", "attrs")
ANN = {"authorization": "Bearer ann", "content-type": "application/json"}


# -- 2: integers the database cannot hold ----------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("number", [2**63, -(2**63) - 1, 10**400])
async def test_2_out_of_range_integers_are_a_validation_error(backend, number):
    api = contract_api(backend)
    created = await api.handle("POST", "/api/widgets", body=b'{"name":"w","price":1}', headers=ANN)
    assert created.status == 201
    for method, path, field in (
        ("POST", "/api/widgets", "stock"),
        ("PATCH", created.headers["location"], "stock"),
    ):
        body = json.dumps({"name": "big", field: number}).encode()
        response = await api.handle(method, path, body=body, headers=ANN)
        assert response.status == 422, (method, response.status, response.body)
        problem = json.loads(response.body)
        assert any(e["field"] == field for e in problem["errors"]), problem
    # inside a container too, and the largest values that do fit are accepted
    nested = await api.handle(
        "POST", "/api/widgets", body=json.dumps({"name": "n", "tags": [number]}).encode(), headers=ANN
    )
    assert nested.status == 422
    for fits in (2**63 - 1, -(2**63)):
        ok = await api.handle(
            "POST", "/api/widgets", body=json.dumps({"name": "ok", "stock": fits}).encode(), headers=ANN
        )
        assert ok.status == 201, ok.body
    # a float field takes a large integer as a float
    as_float = await api.handle(
        "POST", "/api/widgets", body=json.dumps({"name": "f", "price": 2**63}).encode(), headers=ANN
    )
    assert as_float.status == 201


def test_2_an_encoding_error_is_not_a_server_error():
    from mongospecs import EncodingError
    from mongospecs_web.errors import problem

    assert problem(EncodingError("cannot encode object")).status == 422


# -- 3: the body limit holds on every plug, at the ASGI level -----------------------------------------


@pytest.mark.parametrize("plug", PLUGS)
@pytest.mark.parametrize("declared", [None, "10"])  # no Content-Length, and one that lies
async def test_3_no_plug_reads_a_body_past_the_limit(plug, declared):
    api = contract_api("msgspec")
    api.max_body = 1000
    app = mount_on(plug, api)
    offered = 0

    async def receive():
        nonlocal offered
        if offered >= 300:
            return {"type": "http.disconnect"}
        offered += 1
        return {"type": "http.request", "body": b"x" * 65536, "more_body": True}

    sent = []

    async def send(message):
        sent.append(message)

    headers = [(b"host", b"t"), (b"authorization", b"Bearer ann"), (b"content-type", b"application/json")]
    if declared:
        headers.append((b"content-length", declared.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/widgets",
        "raw_path": b"/api/widgets",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("t", 80),
        "state": {},
        "extensions": {},
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 413
    assert offered <= 3, f"{plug} read {offered} chunks of 64 KiB with a 1000 byte limit"


# -- 5: guards run once ------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["delete", "get", "list"])
async def test_5_a_guard_runs_once_per_request(key):
    model = contract_api("msgspec").resources[0].model
    calls = []

    def guard(ctx):
        calls.append(ctx.op.name)
        return True

    backend = contract_api("msgspec").resources[0].backend
    api = Api(Resource(model, ops=[List, Get, Delete], guards={key: guard}, backend=backend), prefix="/api")
    path = "/api/widgets" if key == "list" else "/api/widgets/000000000000000000000000"
    await api.handle("DELETE" if key == "delete" else "GET", path)
    assert calls == [key]

    # the same guard listed for the resource and for the operation is still one check
    calls.clear()
    both = Api(Resource(model, ops=[List], guards=[guard, guard], backend=backend), prefix="/api")
    await both.handle("GET", "/api/widgets")
    assert calls == ["list"]


# -- 8: private fields stay out of the API description ---------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_8_openapi_names_no_private_field(backend):
    api = contract_api(backend)
    text = json.dumps(api.openapi())
    assert "internal_note" not in text
    schemas = api.openapi()["components"]["schemas"]
    # every schema that is published is referenced by something: no stray full model definition
    for name in schemas:
        if name in ("Problem",) or name.endswith(("Read", "Create", "Patch")):
            continue
        assert f"#/components/schemas/{name}" in text, f"{name} is published but never referenced"


# -- HEAD mirrors GET on the ASGI plug -----------------------------------------------------------------


async def test_head_reports_the_length_of_the_get_body():
    api = contract_api("msgspec")
    created = await api.handle("POST", "/api/widgets", body=b'{"name":"w"}', headers=ANN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.asgi()), base_url="http://t"
    ) as client:
        get = await client.get(created.headers["location"], headers=ANN)
        head = await client.head(created.headers["location"], headers=ANN)
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(len(get.content)) != "0"
    assert head.headers["content-type"] == get.headers["content-type"]
