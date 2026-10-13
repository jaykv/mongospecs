# Plugs

A plug mounts an `Api` on a framework. It only translates requests and responses; routing targets,
validation, authorization and serialisation are the same code everywhere, and a contract suite
checks that every plug behaves identically.

```py
api = Api(articles, users, prefix="/api", authenticate=current_user)
```

## Pure ASGI

```py
app = api.asgi(registry=registry)  # opens and closes the registry on lifespan events
```

## Starlette

```py
from starlette.applications import Starlette
from mongospecs_web.plugs import starlette as plug

app = Starlette(routes=plug.routes(api), lifespan=plug.lifespan(registry))
```

## FastAPI

```py
from fastapi import FastAPI
from mongospecs_web.plugs import fastapi as plug

app = FastAPI(lifespan=plug.lifespan(registry))
plug.mount(app, api)
```

The operations and their schemas are merged into the application's own `/openapi.json` and
`/docs`, for msgspec and attrs models as well as pydantic ones. With `Api(openapi=False)` (or
`plug.mount(app, api, openapi=False)`) the routes are served but stay out of both.

## Litestar

```py
from litestar import Litestar
from mongospecs_web.plugs.litestar import MongospecsPlugin

app = Litestar(plugins=[MongospecsPlugin(api, registry=registry)])
```

Handlers are left out of Litestar's own schema generation; the API's OpenAPI document is served at
`<prefix>/openapi.json`.

## Quart

```py
from quart import Quart
from mongospecs_web.plugs import quart as plug

app = Quart(__name__)
plug.init_app(app, api, registry=registry)
```

Quart queues every body chunk as it arrives, before a view asks for it, so `init_app` also wraps
the app in `plug.BodyLimit`, which reads API request bodies under `api.max_body` before Quart
sees them. If you register `plug.blueprint(api)` yourself, add
`app.asgi_app = plug.BodyLimit(app.asgi_app, api)`.

## What the frameworks decide themselves

The plugs register one route per operation and leave the rest of routing to the framework, so
three things differ between them. The contract suite does not cover them.

| | Pure ASGI | Starlette | FastAPI | Litestar | Quart |
|---|---|---|---|---|---|
| Trailing slash (`/widgets/`) | 200 | 307 redirect | 307 redirect | 200 | 404 |
| `HEAD` on a `GET` route | 200, with GET's headers and length | 200 | 405 | 405 | 200 |
| `OPTIONS` | 204 with `Allow` | 405 | 405 | 204 | 200 |

Use the framework's own settings (or its CORS middleware for `OPTIONS`) where you need one
behaviour everywhere.

## Writing another plug

Convert the framework's request to `mongospecs_web.Request`, call `await api.run(route, request)`
for the matching entry of `api.routes`, and convert the `Response` back. Then prove it:

```py
from mongospecs_web.testing import contract_api, reference_transcript, run_contract

api = contract_api("msgspec")
app = my_framework_app(api)
async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
    assert await run_contract(client, api) == await reference_transcript("msgspec")
```

Routes the framework rejects by itself (unknown path, unsupported method) keep that framework's
native 404/405 body; the contract compares only their status.
