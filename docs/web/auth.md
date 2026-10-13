# Authentication and authorization

Four things decide what a request may do. They are separate on purpose: each answers one question.

| | Question | Where |
|---|---|---|
| `authenticate` | Who is calling? | `Api(authenticate=...)` |
| Guards | May they call this operation at all? | `Resource(guards=...)`, `@resource.op(guards=...)` |
| `scope` | Which documents may they see and change? | `Resource(scope=...)` |
| `Policy` | Which fields may they set, change and see? | on each field of the model |

```python
from typing import Annotated, NamedTuple

from mongospecs import Policy, Q, Registry, SpecConfig
from mongospecs.msgspec import Spec
from mongospecs_web import Api, Create, Delete, Forbidden, Get, List, Patch, Resource, Unauthorized


class Report(Spec):
    __spec__ = SpecConfig(collection="reports")
    title: str = ""
    team: Annotated[str, Policy(insert=False, update=False)] = ""  # set by the server
    approved: Annotated[bool, Policy(insert=False, update=False)] = False  # changed only by an operation
    review_notes: Annotated[str | None, Policy(private=True)] = None  # never sent to clients
```

## Who is calling: `authenticate`

`authenticate(request)` returns the principal, which can be anything: a user id, a dataclass, a
model. It may be `async`. Return `None` for an anonymous request and let guards decide whether that
is allowed; raise `Unauthorized` when credentials are present but wrong.

```python
class Principal(NamedTuple):
    name: str
    team: str
    admin: bool = False


SESSIONS = {"t-ann": Principal("ann", "red"), "t-bob": Principal("bob", "blue"), "t-root": Principal("root", "red", True)}


async def authenticate(request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not token:
        return None
    if token not in SESSIONS:  # look the token up in your session store, or verify a JWT here
        raise Unauthorized("Unknown token.", headers={"www-authenticate": "Bearer"})
    return SESSIONS[token]
```

`request` is the framework-neutral `mongospecs_web.Request`; `request.raw` is your framework's own
request object when you need it (a session cookie, `request.state`).

## May they call this: guards

A guard is `guard(ctx) -> bool`, sync or async. Returning false is a 403; raise `Unauthorized` for
a 401. `ctx.principal` is what `authenticate` returned.

```python
def logged_in(ctx):
    if ctx.principal is None:
        raise Unauthorized("Log in first.", headers={"www-authenticate": "Bearer"})
    return True


def is_admin(ctx):
    return ctx.principal is not None and ctx.principal.admin
```

`guards=[...]` applies to every operation; `guards={Delete: is_admin}` to the ones named. A key
must name an operation the resource has, so a misspelt one is an error and not an open door.

## Which documents: `scope`

`scope(ctx)` returns a condition that is ANDed into list, get, patch, put and delete, in the
database query itself. A document outside the scope does not exist for that caller: they get a
404, not a 403, and it never appears in a list or a count. Return `None` for "no restriction".

```python
def own_team(ctx):
    return None if ctx.principal.admin else Q.team == ctx.principal.team


reports = Resource(
    Report,
    guards={List: logged_in, Get: logged_in, Create: logged_in, Patch: logged_in, Delete: [logged_in, is_admin]},
    scope=own_team,
    server_fields=lambda ctx: {"team": ctx.principal.team},  # the server, not the client, says whose it is
)
```

`server_fields` is the other half: on create it sets the fields the scope filters on, so a client
cannot create a document in someone else's scope. Those fields stay closed to clients with
`Policy(insert=False, update=False)`.

## Which fields: `Policy`

| On a field | A client can |
|---|---|
| nothing | set it on create, change it, read it |
| `Policy(update=False)` | set it once, on create |
| `Policy(insert=False, update=False)` | only read it |
| `Policy(private=True)` | not see it, set it, filter or sort by it |
| `Policy(private=True, insert=True)` | send it on create and never read it back (a password) |
| `Policy(filter=("eq", "gte"), sort=True)` | use it in the query string |

The same applies to the fields of embedded models, at any depth. A request that names a field it
may not write is rejected as a whole with a 422. If you would rather list what is writable than
what is not, `Resource(Model, explicit_writes=True)` makes every field read-only unless its policy
says `insert=True` or `update=True`.

## A change only some people may make: a custom operation

A field clients cannot write is changed by an operation with its own guard. `ctx.load()` applies
the scope, so the guard and the scope still both hold.

```python
@reports.op("approve", method="POST", guards=[logged_in, is_admin])
async def approve(ctx):
    report = await ctx.load()
    report.approved = True
    return await report.save()


api = Api(reports, prefix="/api", authenticate=authenticate)
registry = await Registry("mongodb://localhost/app", models=[Report]).open()
```

## All of it at work

```python
import json


async def call(method, path, token=None, body=None):
    headers = {"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})}
    response = await api.handle(method, path, headers=headers, body=json.dumps(body).encode() if body else b"")
    return response.status, json.loads(response.body) if response.body else None


assert (await call("GET", "/api/reports"))[0] == 401  # anonymous: the guard says no
assert (await call("GET", "/api/reports", "t-nobody"))[0] == 401  # bad credentials: authenticate says no

status, created = await call("POST", "/api/reports", "t-ann", {"title": "Q3"})
url = f"/api/reports/{created['data']['id']}"
assert status == 201 and created["data"]["team"] == "red"  # server_fields
assert "review_notes" not in created["data"]  # private

assert (await call("GET", url, "t-bob"))[0] == 404  # another team: outside the scope
assert (await call("PATCH", url, "t-ann", {"approved": True}))[0] == 422  # the policy closes the field
assert (await call("POST", f"{url}/approve", "t-ann"))[0] == 403  # logged in, but not an admin
assert (await call("POST", f"{url}/approve", "t-root"))[1]["data"]["approved"] is True
assert (await call("DELETE", url, "t-ann"))[0] == 403 and (await call("DELETE", url, "t-root"))[0] == 204

await registry.client.drop_database("app")
await registry.close()
```

## Checklist

- Every write operation has a guard, including `Put` if you list it and every custom operation.
- Every field the scope filters on is set by `server_fields` and closed with
  `Policy(insert=False, update=False)`.
- Secrets are `Policy(private=True)`, and `Deferred` too if they should not even be loaded.
- `Api(openapi=False)` if the API description itself should not be public.
- `Forbidden` and `Unauthorized` can be raised from anywhere inside an operation.
