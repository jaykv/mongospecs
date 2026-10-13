# mongospecs

An async MongoDB ODM for the model library you already use (msgspec, pydantic or attrs) and a
framework-neutral REST layer on top of it.

```
pip install "mongospecs[msgspec]"        # or [pydantic], [attrs]
pip install "mongospecs-web[fastapi]"    # or [starlette], [litestar], [quart]; none needed for plain ASGI
```

Requires Python 3.11+ and MongoDB 7.0+. Status: alpha.

## Where to start

- [Models](guide/models.md): declaring documents, binding them, field markers
- [Queries](guide/queries.md) and [Writes and transactions](guide/writes.md)
- [Relations](guide/relations.md), [Hooks and plugins](guide/plugins.md)
- [Running in production](guide/operations.md): indexes, observability, errors
- [Resources](web/resources.md) and [Plugs](web/plugs.md): the REST layer
- [Coming from Mongoose](parity.md), [Migrating from 0.1.x](migration.md)
- [Benchmarks](benchmarks.md): measured against real MongoDB

Every example in the guide is executed by the test suite.

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
