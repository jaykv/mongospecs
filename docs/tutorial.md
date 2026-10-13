# Getting started

This page takes you from nothing to a working model, and then to a REST API over it. Every file
shown here is in [`examples/`](https://github.com/jaykv/mongospecs/tree/main/examples) and is run
by the test suite.

## 1. What you need

- Python 3.11 or newer.
- MongoDB 7.0 or newer, running as a **replica set**. A single node is enough. Transactions,
  cascading deletes and change streams do not work on a standalone `mongod`; everything else does.

If you have Docker, this starts a one-node replica set on `localhost:27017`:

```yaml title="compose.yaml"
--8<-- "examples/compose.yaml"
```

```
docker compose -f compose.yaml up -d --wait
```

MongoDB Atlas works too (every Atlas cluster is a replica set): use its connection string
wherever you see `mongodb://localhost:27017/...` below.

## 2. Install

Pick the model library you want to write your models in. If you have no preference, start with
msgspec; [Choosing a backend](guide/backends.md) compares the three.

```
pip install "mongospecs[msgspec]"        # or [pydantic], or [attrs]
```

## 3. A model, and what you can do with it

```python title="quickstart.py"
--8<-- "examples/quickstart.py"
```

```
$ python quickstart.py
Hello, world by ann
tags: ['intro', 'published']
```

What happened there:

- **A model is a class.** `Spec` is the base for the backend you chose; everything else comes from
  `mongospecs` and is the same on all three. Every document has an `id`, stored as `_id`.
- **A `Registry` owns the connection.** Models do nothing until they are in an open registry.
  `async with` opens it and closes it; in a web application you open it once at startup.
- **Nothing is built implicitly.** `apply_indexes()` creates the indexes you declared.
- **`save()` sends only what changed**, and `changes()` shows you what that is.
- **Queries are typed.** `Q.views >= 0` checks that `views` is a field and casts the value; a
  misspelt name raises `QueryError` instead of matching nothing.
- **A `Ref` is not loaded until you ask**: `.include("author")` loads it with the query.
- **`Model.update(id, ...)` is one atomic command** and returns the updated document.
- **A transaction is a function.** It may run more than once, and your in-memory documents are
  put back if it aborts.

Each of these has a page: [Models](guide/models.md), [Queries](guide/queries.md),
[Writes and transactions](guide/writes.md), [Relations](guide/relations.md).

!!! note "Top-level await"
    The guide pages use `await` at the top level to stay short. In your own code that goes inside
    an `async def`, as in the file above, or straight into `python -m asyncio`.

## 4. A REST API over the model

`mongospecs-web` describes an API as data and mounts it on plain ASGI, Starlette, FastAPI,
Litestar or Quart. This example uses plain ASGI, so the only other thing to install is a server.

```
pip install mongospecs-web uvicorn
```

```python title="app.py"
--8<-- "examples/todo_api/app.py"
```

```
$ uvicorn app:app
$ curl -s -X POST localhost:8000/api/tasks -H 'authorization: Bearer ann-token' \
       -H 'content-type: application/json' -d '{"title": "Write docs"}'
{"data":{"id":"...","title":"Write docs","done":false,"owner":"ann","created_at":"...","rev":0}}
$ curl -s 'localhost:8000/api/tasks?done=false&sort=-title' -H 'authorization: Bearer ann-token'
$ curl -s localhost:8000/api/openapi.json
```

The parts:

- **`Policy` on each field is the contract.** It says whether a client may set the field, change
  it, see it, filter by it and sort by it. `owner` cannot be written by clients; `note_to_self`
  never leaves the server.
- **`authenticate`** turns a request into a principal (anything you like), or `None`.
- **Guards** decide who may call an operation; **`scope`** decides which rows they may touch, and
  is part of every query, so Bob gets a 404 for Ann's task rather than a 403.
- **`@tasks.op`** adds an operation of your own next to the built-in ones.
- **`rev`** is a `Revision` field, so responses carry an `ETag` and writes honour `If-Match`.

More in [Resources](web/resources.md) and
[Authentication and authorization](web/auth.md).

## 5. Test it

`api.handle()` runs a request through routing, authentication, guards, validation and the
database, without a server:

```python title="test_app.py"
--8<-- "examples/todo_api/test_app.py"
```

[Testing your API](web/testing.md) covers the other ways, including an in-memory backend.

## Where next

- Coming from Mongoose? [The parity table](parity.md) maps what you know.
- Coming from mongospecs 0.1? [Migrating from 0.1.x](migration.md).
- Going to production? [Running in production](guide/operations.md) and
  [Tuning for production](guide/production.md).
- Something raised an error you do not recognise? [Errors](guide/errors.md).
