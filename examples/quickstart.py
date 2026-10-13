"""mongospecs in one file. Start MongoDB (see compose.yaml), then: python examples/quickstart.py"""

import asyncio
import os
from datetime import datetime
from typing import Annotated

from mongospecs import CreatedAt, Inc, Index, Q, Ref, Registry, Revision, SpecConfig, UpdatedAt
from mongospecs.msgspec import Spec  # or mongospecs.pydantic / mongospecs.attrs

URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/quickstart?replicaSet=rs0")


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


async def main(uri: str = URI) -> None:
    async with Registry(uri, models=[User, Article]) as registry:
        await registry.apply_indexes()

        ann = await User(name="ann").insert()
        article = await Article(title="Hello", author=ann).insert()

        # change tracking: save() sends only what changed
        article.title = "Hello, world"
        assert article.changes() == {"$set": {"title": "Hello, world"}}
        await article.save()

        # typed queries: names are checked, values are cast
        popular = await Article.find(Q.views >= 0, Q.title.prefix("Hello")).sort("-views").limit(10)
        assert [a.title for a in popular] == ["Hello, world"]

        # relations: one extra query, or a $lookup
        with_author = await Article.find(Q.author == ann).include("author").one()
        print(with_author.title, "by", with_author.author.doc.name)

        # atomic updates, always validated; the updated document comes back
        article = await Article.update(article.id, Inc(views=1), tags=["intro"])
        assert article.views == 1

        # transactions (these need a replica set): the callback is retried on transient
        # errors, and in-memory documents are restored if it aborts
        async def publish(tx):
            article.tags.append("published")
            await article.save(session=tx)

        await registry.transaction(publish)
        print("tags:", (await Article.get(article.id)).tags)


if __name__ == "__main__":
    asyncio.run(main())
