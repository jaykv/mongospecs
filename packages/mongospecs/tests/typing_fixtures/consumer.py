"""What a user of the library should and should not be able to write, as seen by a type checker.

Lines marked ``# expect-error`` must be rejected by mypy; every other line must be accepted.
This file is checked, never executed.
"""

from mongospecs import Inc, Page, Q, Query, Ref, Registry, SpecConfig
from mongospecs.msgspec import Spec


class User(Spec):
    name: str = ""


class Article(Spec):
    __spec__ = SpecConfig(collection="articles")
    title: str = ""
    views: int = 0
    author: Ref[User] | None = None


async def reads(registry: Registry) -> None:
    query: Query[Article] = Article.find(Q.views > 1).sort("-views").limit(5)
    articles: list[Article] = await query.all()
    also: list[Article] = await query
    maybe: Article | None = await query.first()
    one: Article = await query.one()
    by_id: Article | None = await Article.get("abc")
    raw: list[dict[str, object]] = [dict(d) for d in await query.raw().all()]
    page: Page[Article] = await query.page(10)
    count: int = await Article.count(Q.title == "x")
    async for article in query:
        title: str = article.title
    print(articles, also, maybe, one, by_id, raw, page.items[0].views, count, title)

    wrong_model: list[User] = await query.all()  # expect-error
    not_optional: Article = await query.first()  # expect-error
    not_a_list: Article = await query.all()  # expect-error
    print(wrong_model, not_optional, not_a_list)
    if maybe is not None:
        print(maybe.title.upper())
    print(maybe.title)  # expect-error


async def writes(registry: Registry) -> None:
    article: Article = await Article(title="t").insert()
    saved: Article = await article.save()
    updated: Article = await Article.update(article.id, Inc(views=1), title="x")
    upserted: Article = await Article.upsert(Q.title == "x", views=2)
    modified: int = await Article.find(Q.views > 1).update(Inc(views=1))
    deleted: int = await Article.find(Q.views > 1).delete()
    await article.delete()
    print(saved, updated, upserted, modified, deleted)

    as_user: User = await Article.update(article.id, title="x")  # expect-error
    count_as_doc: Article = await Article.find().update(views=1)  # expect-error
    print(as_user, count_as_doc)


async def relations(article: Article) -> None:
    if article.author is not None:
        user: User = await article.author.fetch()
        loaded: User = article.author.doc
        print(user.name, loaded.name)
        wrong: Article = article.author.doc  # expect-error
        print(wrong)


async def transactions(registry: Registry) -> None:
    async def work(tx: object) -> int:
        return 1

    result: int = await registry.transaction(work)
    text: str = await registry.transaction(work)  # expect-error
    print(result, text)
