# mongospecs

An async MongoDB ODM for the model library you already use: **msgspec**, **pydantic** or **attrs**.

The aim is Mongoose's production feature set (change tracking, middleware, populate,
discriminators, transactions that restore document state) with typed Python models, plus
[`mongospecs-web`](https://github.com/jaykv/mongospecs/tree/main/packages/mongospecs-web), a framework-neutral REST layer.

> **Status: 0.2.0, alpha.** This is a rewrite of 0.1.x: async-only, Python 3.11+, MongoDB 7.0+,
> and not backwards compatible. The synchronous 0.1.x line lives on the
> [`0.1.x` branch](https://github.com/jaykv/mongospecs/tree/0.1.x). The design and the work graph
> are in [`claudedocs/v2-plan.md`](https://github.com/jaykv/mongospecs/blob/main/claudedocs/v2-plan.md).

## Install

```
pip install "mongospecs[msgspec]"     # or [pydantic], [attrs], [all]
```

## A taste

```python
from datetime import datetime
from typing import Annotated

from mongospecs import CreatedAt, Inc, Index, Q, Ref, Registry, Revision, SpecConfig, UpdatedAt
from mongospecs.msgspec import Spec  # or mongospecs.pydantic / mongospecs.attrs


class User(Spec):
    name: str = ""


class Article(Spec):
    __spec__ = SpecConfig(collection="articles", indexes=[Index("title", unique=True)])

    title: str
    views: int = 0
    tags: list[str] = []
    author: Ref[User] | None = None
    created_at: Annotated[datetime | None, CreatedAt] = None
    updated_at: Annotated[datetime | None, UpdatedAt] = None
    rev: Annotated[int, Revision] = 0  # optimistic locking


async def main():
    async with Registry("mongodb://localhost/app", models=[User, Article]) as registry:
        await registry.apply_indexes()

        ann = await User(name="ann").insert()
        article = await Article(title="Hello", author=ann).insert()

        # change tracking: save() sends only what changed
        article.title = "Hello, world"
        assert article.changes() == {"$set": {"title": "Hello, world"}}
        await article.save()

        # typed queries: names are checked, values are cast
        popular = await Article.find(Q.views >= 100, Q.title.prefix("Hello")).sort("-views").limit(10)

        # relations: one extra query, or a $lookup
        with_author = await Article.find(Q.author == ann).include("author").one()
        print(with_author.author.doc.name)

        # atomic updates, always validated; the updated document comes back
        article = await Article.update(article.id, Inc(views=1), tags=["intro"])

        # transactions: the callback is retried on transient errors, and in-memory
        # documents are restored if it aborts
        async def publish(tx):
            article.tags.append("published")
            await article.save(session=tx)

        await registry.transaction(publish)
```

## What is in the box

| | |
|---|---|
| Models | msgspec `Struct`, pydantic `BaseModel` or attrs classes, through one `Codec` seam; `SpecAdapter` for classes you already have |
| Reads | immutable chainable queries, `Q` expressions, server-side projection, `tracked` / `lean()` / `raw()` modes, cursor and offset pagination |
| Writes | delta `save()`, atomic operators (`Inc`, `Push`, `Pull`, ...), real upsert, typed `bulk_write`, `bulk_save`, partial-failure `insert_many` |
| Relations | `Ref[T]`, lists and dicts of refs, polymorphic refs, reverse relations (`Backref`), nested `include()`, `$lookup` or batched loading, cascade rules, `$graphLookup`, `$unionWith` |
| Plugins | timestamps, soft delete, revisions - activated by field markers; registry-wide plugins; document hooks |
| Modelling | discriminators (single-collection inheritance), deferred and immutable fields, aliases, lazy upcasting of old documents |
| Operations | explicit `Registry` lifecycle, transactions, index planner with gated destructive actions, `$jsonSchema` validators, change streams, multi-database use, shard-key aware writes |
| Observability | per-command events with model and operation names; logging, OpenTelemetry and Sentry listeners |

## How it is tested

- Unit and contract suites run against real MongoDB (a replica set and a sharded cluster), on
  every backend, on Python 3.11 to 3.15.
- A reference application, the storefront in `bench/`, is booted as a real server process and
  driven over HTTP: a full customer journey on every plug, concurrent buyers that must never
  oversell, multi-item orders that are all-or-nothing, a change-stream projector that must count
  each sale exactly once across restarts, and orders that stay consistent across a primary
  step-down.
- Every example in the guide pages is executed.
- Benchmarks and stress scenarios in `bench/` run against real MongoDB; results and the
  conclusions drawn from them are in `docs/benchmarks.md`.

## Development

```
make install     # uv sync for the whole workspace
make up          # MongoDB replica set + sharded cluster in Docker
make check       # ruff, mypy --strict, tests against real MongoDB (unit, contract, end-to-end)
uv run mongospecs-bench run          # benchmarks; `run LOAD` for the HTTP load test; `stress`; `trace`
```

The repository is a uv workspace: `packages/mongospecs`, `packages/mongospecs-web` and `bench/`
(benchmarks and stress scenarios against real MongoDB, never published).
