"""Testing the API without a server: api.handle() runs a request through the whole stack."""

import json

import pytest_asyncio

from app import URI, Task, api
from mongospecs import Registry

ANN = {"authorization": "Bearer ann-token", "content-type": "application/json"}
BOB = {"authorization": "Bearer bob-token", "content-type": "application/json"}


@pytest_asyncio.fixture(autouse=True)
async def database():
    # A registry cannot be reopened once closed, so each test opens its own.
    async with Registry(URI, models=[Task]) as registry:
        yield
        await registry.client.drop_database(registry.database().name)


async def call(method, path, body=None, headers=ANN):
    response = await api.handle(
        method, path, body=json.dumps(body).encode() if body is not None else b"", headers=headers
    )
    return response.status, json.loads(response.body) if response.body else None, response.headers


async def test_create_list_and_complete():
    status, created, headers = await call("POST", "/api/tasks", {"title": "Write docs"})
    assert status == 201 and created["data"]["owner"] == "ann" and headers["etag"] == '"0"'
    assert "note_to_self" not in created["data"]  # private fields are never returned

    status, listed, _ = await call("GET", "/api/tasks")
    assert [task["title"] for task in listed["data"]] == ["Write docs"]

    status, done, _ = await call("POST", f"{headers['location']}/complete")
    assert status == 200 and done["data"]["done"] is True


async def test_each_user_sees_only_their_own_tasks():
    _, _, headers = await call("POST", "/api/tasks", {"title": "Ann's"})
    assert (await call("GET", headers["location"], headers=BOB))[0] == 404
    assert (await call("GET", "/api/tasks", headers=BOB))[1]["data"] == []
    assert (await call("GET", "/api/tasks", headers={}))[0] == 401


async def test_the_field_policy_is_enforced():
    _, _, headers = await call("POST", "/api/tasks", {"title": "Mine"})
    status, problem, _ = await call("PATCH", headers["location"], {"owner": "bob"})
    assert status == 422 and problem["errors"] == [{"field": "owner", "message": "cannot be set on update"}]
    assert (await call("PATCH", headers["location"], {"note_to_self": "x"}))[0] == 422


async def test_a_stale_etag_is_refused():
    _, _, headers = await call("POST", "/api/tasks", {"title": "Mine"})
    fresh = {**ANN, "if-match": headers["etag"]}
    assert (await call("PATCH", headers["location"], {"done": True}, fresh))[0] == 200
    assert (await call("PATCH", headers["location"], {"done": False}, fresh))[0] == 412
