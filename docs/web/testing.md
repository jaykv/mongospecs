# Testing your API

There are three ways to test a resource, from fastest to most complete. All of them run your real
guards, scope, field policy and validation; they differ in what stands behind them.

| | Runs | Needs |
|---|---|---|
| `api.handle(...)` with `FakeBackend` | routing, auth, policy, validation, your operations | nothing |
| `api.handle(...)` with MongoDB | all of that, plus real queries, indexes and transactions | MongoDB |
| An HTTP client on the mounted app | all of that, plus the framework plug | MongoDB |

```python
import json
from typing import Annotated

from mongospecs import Index, Policy, Q, Registry, Revision, SpecConfig
from mongospecs.msgspec import Spec
from mongospecs_web import Api, Resource
from mongospecs_web.testing import FakeBackend


class Task(Spec):
    __spec__ = SpecConfig(collection="tasks", indexes=[Index("owner", "title", unique=True)])
    title: Annotated[str, Policy(filter=("eq",), sort=True)]
    done: bool = False
    owner: Annotated[str, Policy(insert=False, update=False)] = ""
    rev: Annotated[int, Revision] = 0


def build_api(backend=None):
    tasks = Resource(
        Task,
        backend=backend,  # None: MongoDB
        scope=lambda ctx: Q.owner == ctx.principal,
        server_fields=lambda ctx: {"owner": ctx.principal},
    )
    return Api(tasks, prefix="/api", authenticate=lambda request: request.headers.get("x-user"))


async def call(api, method, path, body=None, user="ann", **headers):
    response = await api.handle(
        method,
        path,
        headers={"x-user": user, "content-type": "application/json", **headers},
        body=json.dumps(body).encode() if body is not None else b"",
    )
    return response.status, json.loads(response.body) if response.body else None, response.headers
```

Build the `Api` in a function, as above, so tests can make one of their own.

## Without a database: `FakeBackend`

`FakeBackend(Model)` keeps documents in memory. No registry is opened and nothing is connected, so
these tests run anywhere and in microseconds. Ids are sequential, which makes them predictable.

```python
store = FakeBackend(Task, unique=["title"])
api = build_api(store)

status, created, headers = await call(api, "POST", "/api/tasks", {"title": "Write docs"})
assert status == 201 and created["data"]["owner"] == "ann"
assert (await call(api, "GET", headers["location"], user="bob"))[0] == 404  # the scope applies
assert (await call(api, "PATCH", headers["location"], {"owner": "bob"}))[0] == 422  # so does the policy
assert (await call(api, "POST", "/api/tasks", {"title": "Write docs"}))[0] == 409  # unique=["title"]
assert [task.title for task in store.docs.values()] == ["Write docs"]  # look inside directly
```

What it does not do: it evaluates the filters a resource can produce, not arbitrary queries; it
has no indexes beyond `unique=`, no transactions and no relations to `include`. A custom operation
that calls `Task.find(...)` itself talks to MongoDB and needs the next kind of test.

## With MongoDB: `api.handle`

The same calls against a real database. Open a registry for the test and drop its database
afterwards; a registry cannot be reopened once closed, so each test (or session) makes its own.

```python
registry = await Registry("mongodb://localhost/app", models=[Task]).open()
await registry.apply_indexes()
api = build_api()

status, created, headers = await call(api, "POST", "/api/tasks", {"title": "Write docs"})
assert status == 201 and headers["etag"] == '"0"'

# things only the database decides: the unique index, the revision check
assert (await call(api, "POST", "/api/tasks", {"title": "Write docs"}))[0] == 409
url = headers["location"]
assert (await call(api, "PATCH", url, {"done": True}, **{"if-match": '"0"'}))[0] == 200
assert (await call(api, "PATCH", url, {"done": False}, **{"if-match": '"0"'}))[0] == 412

# and the documents are there to be checked with the model
assert (await Task.find(Q.owner == "ann").one()).done is True
```

With pytest and `pytest-asyncio` that becomes a fixture:

```py
@pytest_asyncio.fixture
async def api():
    async with Registry(TEST_URI, database=f"test_{uuid.uuid4().hex[:8]}", models=[Task]) as registry:
        await registry.apply_indexes()
        yield build_api()
        await registry.client.drop_database(registry.database().name)
```

## Through the framework: an HTTP client

To test what a real client sees, including your framework's own routing and middleware, mount the
API and drive it with `httpx`. No server process is needed.

```python
import httpx

async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.asgi()), base_url="http://test") as client:
    listed = await client.get("/api/tasks", params={"sort": "-title"}, headers={"x-user": "ann"})
    assert listed.status_code == 200 and [t["title"] for t in listed.json()["data"]] == ["Write docs"]
    assert (await client.get("/api/openapi.json")).json()["info"]["title"] == "API"

await registry.client.drop_database("app")
await registry.close()
```

Replace `api.asgi()` with your Starlette, FastAPI, Litestar or Quart application.

## What to assert

- **Authorization, from the outside in**: anonymous, the wrong user, the right user, for each
  operation. A document outside the caller's scope is a 404.
- **The field policy**: a write that names a closed field is a 422 and changes nothing.
- **Errors**: responses are `application/problem+json`; field errors are in `errors`.
- **Concurrency**, where it matters: two writers with the same `If-Match`, one of which gets 412.

If you write a plug for another framework, `mongospecs_web.testing.run_contract` checks it against
the reference behaviour; see [Plugs](plugs.md).
