# Resources

`mongospecs-web` exposes a model as REST operations. The description is pure data; a plug mounts it
on a framework.

```python
from typing import Annotated

from mongospecs import Policy, Q, Registry, Revision, SpecConfig
from mongospecs.msgspec import Spec
from mongospecs_web import Api, Create, Delete, Get, List, Patch, Resource, Unauthorized


class Task(Spec):
    __spec__ = SpecConfig(collection="tasks")
    title: Annotated[str, Policy(filter=("eq", "prefix"), sort=True)]
    done: Annotated[bool, Policy(filter=("eq",))] = False
    owner: Annotated[str, Policy(insert=False, update=False, filter=("eq",))] = ""
    secret_note: Annotated[str | None, Policy(private=True)] = None
    rev: Annotated[int, Revision] = 0


def authenticate(request):
    return request.headers.get("x-user")  # your real authentication goes here


def logged_in(ctx):
    if ctx.principal is None:
        raise Unauthorized("Log in first.")
    return True


tasks = Resource(
    Task,
    ops=[List, Get, Create, Patch, Delete],
    guards=[logged_in],  # who may call the operations
    scope=lambda ctx: Q.owner == ctx.principal,  # which rows they may touch
    server_fields=lambda ctx: {"owner": ctx.principal},  # set by the server on create
)
api = Api(tasks, prefix="/api", authenticate=authenticate)

registry = await Registry("mongodb://localhost/app", models=[Task]).open()
```

`Policy` on each field decides what a client may do with it:

| Policy | Meaning |
|---|---|
| `insert=False` / `update=False` | not settable on create / on patch |
| `private=True` | never returned, filtered or sorted; not writable unless `insert`/`update` say so |
| `filter=("eq", "gte", ...)` | operators allowed in the query string (`True` for all) |
| `sort=True` | usable in `?sort=` |

## Calling it

`api.handle(...)` runs a request without any server, which is also how you test resources.

```python
ann = {"x-user": "ann"}
created = await api.handle("POST", "/api/tasks", body=b'{"title": "Write docs"}', headers=ann)
assert created.status == 201 and created.headers["etag"] == '"0"'

import json

task = json.loads(created.body)["data"]
assert task["owner"] == "ann" and "secret_note" not in task

listed = await api.handle("GET", "/api/tasks", query=[("title[prefix]", "Write"), ("sort", "-title")], headers=ann)
assert [t["title"] for t in json.loads(listed.body)["data"]] == ["Write docs"]

# someone else cannot see or change it: the scope is part of the query
assert (await api.handle("GET", f"/api/tasks/{task['id']}", headers={"x-user": "bob"})).status == 404

# conditional update with the ETag
patched = await api.handle(
    "PATCH", f"/api/tasks/{task['id']}", body=b'{"done": true}', headers={**ann, "if-match": '"0"'}
)
assert patched.status == 200
stale = await api.handle(
    "PATCH", f"/api/tasks/{task['id']}", body=b'{"done": false}', headers={**ann, "if-match": '"0"'}
)
assert stale.status == 412

# input the policy does not allow never reaches the database
for body in (b'{"owner": "mallory"}', b'{"title": {"$ne": null}}', b'{"nope": 1}'):
    assert (await api.handle("PATCH", f"/api/tasks/{task['id']}", body=body, headers=ann)).status == 422
assert (await api.handle("GET", "/api/tasks", query=[("done[$ne]", "1")], headers=ann)).status == 400
```

Errors are RFC 9457 `application/problem+json` documents. Unexpected exceptions become a bare 500
with no detail.

## Custom operations and hooks

```python
@tasks.op("complete", method="POST", summary="Mark a task as done")
async def complete(ctx):
    task = await ctx.load()  # 404 unless it is within the caller's scope
    task.done = True
    return await task.save()


@tasks.after(Create)
async def audit(ctx, task):
    ctx.state["audited"] = task.id


done = await api.handle("POST", f"/api/tasks/{task['id']}/complete", headers=ann)
assert json.loads(done.body)["data"]["done"] is True

spec = api.openapi()
assert "/api/tasks/{id}/complete" in spec["paths"] and "TaskPatch" in spec["components"]["schemas"]

await registry.client.drop_database("app")
await registry.close()
```

## Query string

```
?title=hello                 equality
?views[gte]=10&views[lt]=50  operators in brackets
?tag[in]=a,b                 comma-separated lists
?sort=-views,title
?limit=20&after=<cursor>     cursor pagination (default)
?limit=20&page=3             offset pagination, with page=Offset(...)
?total=true                  also count all matches, with page=Cursor(total=True)
?include=author              embed allowlisted relations
```

Only fields and operators the policy allows are accepted; everything else is a 400.

## Limits and defaults

These are set so that a client cannot make the server do unbounded work.

| Setting | Default | Meaning |
|---|---|---|
| `Api(max_body=...)` | 1 MiB | Larger request bodies get a 413. The body is never buffered past the limit, on any plug |
| `Cursor(max=...)`, `Offset(max=...)` | 100 | Largest page a client can ask for |
| `Offset(max_offset=...)` | 10,000 | How many documents a page may skip; deeper pages get a 400 |
| `Cursor(total=...)`, `Offset(total=...)` | `False` | Whether `?total=true` is allowed (a count is a second query over every match) |
| `MongoBackend(max_time_ms=...)` | 10 s | Time limit for reads made for a request, when the registry and the model set none |
| `Api(openapi=...)` | `True` | Serve `<prefix>/openapi.json`; turn off to keep the description private |
| `Resource(explicit_writes=...)` | `False` | When `True`, a field is writable only if its `Policy` says `insert=True` / `update=True` |

An integer outside the 64-bit range MongoDB can store is a 422 with a field error, on create and
on patch (a float or Decimal field takes it as that type). A guard listed in more than one place
runs once per request. Private fields appear nowhere in `openapi.json`, and it only publishes
the nested definitions that the Read, Create and Patch schemas refer to.

`Policy(private=True)` also applies to the fields of embedded models, at any depth: they are never
in a response, a request that supplies one gets a 422, and they are in no schema. A `PATCH` that
sends an embedded object replaces it whole, so private values inside it are cleared; patch the
other fields of the parent, or set such values in a server-side hook.

`Api(max_concurrency=N)` answers the request beyond N in flight with 503 and `Retry-After`. See
[Tuning for production](../guide/production.md).

`DELETE` honours `If-Match` like `PATCH`, and the row scope is part of the delete's own filter.
`HEAD` and `OPTIONS` are answered by `api.handle` and the pure ASGI plug; the framework plugs leave
them to the framework.
