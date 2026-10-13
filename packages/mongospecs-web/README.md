# mongospecs-web

REST resources for [mongospecs](https://github.com/jaykv/mongospecs) models, described once and
mounted on the framework you use: plain ASGI, Starlette, FastAPI, Litestar or Quart.

> **Status: 0.2.0, alpha.**

```
pip install "mongospecs-web[fastapi]" "mongospecs[msgspec]"
```

```python
from typing import Annotated

from mongospecs import Policy, Q, Registry, Revision, SpecConfig
from mongospecs.msgspec import Spec
from mongospecs_web import Api, Create, Delete, Get, List, Patch, Resource


class Task(Spec):
    __spec__ = SpecConfig(collection="tasks")
    title: Annotated[str, Policy(filter=("eq", "prefix"), sort=True)]
    done: Annotated[bool, Policy(filter=("eq",))] = False
    owner: Annotated[str, Policy(insert=False, update=False)] = ""
    rev: Annotated[int, Revision] = 0


tasks = Resource(
    Task,
    ops=[List, Get, Create, Patch, Delete],
    scope=lambda ctx: Q.owner == ctx.principal,  # row-level: part of every query
    server_fields=lambda ctx: {"owner": ctx.principal},  # set by the server on create
)
registry = Registry("mongodb://localhost/app", models=[Task])
app = Api(tasks, prefix="/api", authenticate=lambda request: request.headers.get("x-user")).asgi(
    registry=registry
)
```

That is a complete API: `GET /api/tasks?title[prefix]=Wri&sort=-title&limit=20`, `POST`, `GET /{id}`,
`PATCH /{id}` with `If-Match`, `DELETE /{id}`, and `GET /api/openapi.json`.

- **The field policy is the contract.** What can be created, patched, read, filtered and sorted
  comes from `Policy` on each field. Input is validated against the real model; nothing a client
  sends reaches MongoDB as a raw filter or update.
- **Authorization in two parts.** Guards decide who may call an operation; `scope` returns a
  condition that is ANDed into list, get, patch and delete.
- **HTTP done properly.** ETag and `If-Match` from the revision field, RFC 9457 problem documents,
  cursor or offset pagination, OpenAPI 3.1.
- **Framework-neutral.** The same resource mounts on five frameworks through thin plugs, and a
  contract suite (`mongospecs_web.testing`) checks that they all behave identically.

Start with [Getting started](https://jaykv.github.io/mongospecs/tutorial/), which builds an API
like this one step by step. Guides: [resources](https://jaykv.github.io/mongospecs/web/resources/),
[authentication and authorization](https://jaykv.github.io/mongospecs/web/auth/),
[testing](https://jaykv.github.io/mongospecs/web/testing/),
[plugs](https://jaykv.github.io/mongospecs/web/plugs/).

Responses are encoded with msgspec when it is installed (`mongospecs-web[msgspec]`), and with the
standard library otherwise.
