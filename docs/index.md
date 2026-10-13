# mongospecs

An async MongoDB ODM for the model library you already use (msgspec, pydantic or attrs) and a
framework-neutral REST layer on top of it.

```
pip install "mongospecs[msgspec]"        # or [pydantic], [attrs]
pip install "mongospecs-web[fastapi]"    # or [starlette], [litestar], [quart]; none needed for plain ASGI
```

Requires Python 3.11+ and MongoDB 7.0+ running as a replica set (one node is enough; transactions,
cascading deletes and change streams need it). Status: alpha.

## Where to start

- **[Getting started](tutorial.md)**: MongoDB, a first model, a REST API and its tests, in one page
- [Models](guide/models.md): declaring documents, binding them, field markers;
  [Choosing a backend](guide/backends.md)
- [Queries](guide/queries.md) and [Writes and transactions](guide/writes.md)
- [Relations](guide/relations.md), [Hooks and plugins](guide/plugins.md)
- [Errors](guide/errors.md): what each one means and what a client sees
- [Running in production](guide/operations.md): indexes, observability
- [Resources](web/resources.md), [Authentication and authorization](web/auth.md),
  [Testing your API](web/testing.md) and [Plugs](web/plugs.md): the REST layer
- [Coming from Mongoose](parity.md), [Migrating from 0.1.x](migration.md)
- [Benchmarks](benchmarks.md): measured against real MongoDB

Every example in the guide is executed by the test suite, and so is everything in
[`examples/`](https://github.com/jaykv/mongospecs/tree/main/examples).

## What it looks like

```py
class Article(Spec):
    __spec__ = SpecConfig(collection="articles", indexes=[Index("title", unique=True)])

    title: Annotated[str, Policy(filter=("eq", "prefix"), sort=True)]
    views: int = 0
    author: Ref[User] | None = None
    updated_at: Annotated[datetime | None, UpdatedAt] = None
    rev: Annotated[int, Revision] = 0


async with Registry("mongodb://localhost/app", models=[User, Article]) as registry:
    article = await Article.find(Q.title.prefix("Hello")).include("author").one()
    article.views += 1
    await article.save()                      # sends {"$set": {"views": ...}} and checks the revision

app = Api(Resource(Article), prefix="/api").asgi(registry=registry)   # a REST API, on any framework
```
