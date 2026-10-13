"""Resources over a real database: the parts the in-memory contract cannot show."""

import datetime as dt
import typing as t

import httpx
import pytest

from mongospecs import (
    Backref,
    CreatedAt,
    DeletedAt,
    Index,
    Policy,
    Q,
    Ref,
    Revision,
    SpecConfig,
    UpdatedAt,
    computed,
)
from mongospecs_web import Api, Create, Cursor, Delete, Get, List, Offset, Patch, Resource, Unauthorized
from mongospecs_web.plugs.asgi import AsgiApp

BACKENDS = ("msgspec", "pydantic", "attrs")


def models(backend):
    if backend == "msgspec":
        from mongospecs.msgspec import Spec

        define = lambda c: c  # noqa: E731
    elif backend == "pydantic":
        from mongospecs.pydantic import Spec

        define = lambda c: c  # noqa: E731
    else:
        import attrs

        from mongospecs.attrs import Spec

        define = attrs.define(kw_only=True)

    @define
    class Author(Spec):
        __spec__ = SpecConfig(collection="authors", indexes=[Index("email", unique=True)])
        name: t.Annotated[str, Policy(filter=("eq",), sort=True)] = ""
        email: t.Annotated[str, Policy(filter=("eq",))] = ""
        password: t.Annotated[str | None, Policy(private=True, insert=True)] = None  # write-only

    @define
    class Story(Spec):
        __spec__ = SpecConfig(collection="stories")
        title: t.Annotated[str, Policy(filter=("eq", "prefix"), sort=True)] = ""
        words: t.Annotated[int, Policy(filter=True, sort=True)] = 0
        published: t.Annotated[dt.datetime | None, Policy(filter=("gte", "lt", "exists"), sort=True)] = None
        author: t.Annotated[Ref[Author] | None, Policy(filter=("eq",))] = None
        reactions: t.Annotated[list[t.Any], Backref("story", model="Reaction")] = []
        reaction_count: t.Annotated[int, Backref("story", model="Reaction", count=True)] = 0
        created_at: t.Annotated[dt.datetime | None, CreatedAt] = None
        updated_at: t.Annotated[dt.datetime | None, UpdatedAt] = None
        deleted_at: t.Annotated[dt.datetime | None, DeletedAt, Policy(private=True)] = None
        rev: t.Annotated[int, Revision] = 0

        @computed
        def reading_minutes(self) -> int:
            return max(1, self.words // 200)

    @define
    class Reaction(Spec):
        __spec__ = SpecConfig(collection="reactions", indexes=[Index("story")])
        emoji: str = ""
        story: Ref[Story] | None = None

    return Author, Story, Reaction


class User(t.NamedTuple):
    id: str


def authenticate(request):
    token = request.headers.get("x-user")
    return User(token) if token else None


@pytest.fixture(params=BACKENDS)
async def app(request, make_registry):
    Author, Story, Reaction = models(request.param)
    registry = await make_registry(Author, Story, Reaction)
    await registry.apply_indexes()

    def mine(ctx):
        if ctx.principal is None:
            raise Unauthorized("who are you?")
        return Q.author == ctx.principal.id

    api = Api(
        Resource(Author, ops=[List, Get, Create, Patch]),
        Resource(
            Story,
            ops=[List, Get, Create, Patch, Delete],
            include=["author", "reactions", "reaction_count"],
            page=Cursor(total=True),
        ),
        Resource(
            Story,
            name="my-stories",
            ops=[List, Get, Patch, Delete],
            scope=mine,
            page=Offset(default=10, total=True, max_offset=50),
        ),
        prefix="/v1",
        authenticate=authenticate,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.asgi()), base_url="http://t")
    try:
        yield client, (Author, Story, Reaction), api, registry
    finally:
        await client.aclose()


async def test_crud_round_trip_with_plugins(app):
    client, (Author, Story, Reaction), *_ = app
    author = (
        await client.post("/v1/authors", json={"name": "Ann", "email": "a@x.io", "password": "pw"})
    ).json()["data"]
    assert "password" not in author  # write-only: stored, never returned
    assert (await Author.collection().find_one({}))["password"] == "pw"
    res = await client.post("/v1/stories", json={"title": "First", "words": 450, "author": author["id"]})
    assert res.status_code == 201 and res.headers["etag"] == '"0"'
    story = res.json()["data"]
    assert story["reading_minutes"] == 2  # computed property
    assert story["created_at"].endswith("Z") and story["author"] == author["id"]
    assert "deleted_at" not in story and "reactions" not in story
    # conditional update through the real revision plugin
    ok = await client.patch(f"/v1/stories/{story['id']}", json={"words": 900}, headers={"if-match": '"0"'})
    assert ok.status_code == 200 and ok.headers["etag"] == '"1"' and ok.json()["data"]["reading_minutes"] == 4

    # compare as instants: serialisers may or may not print a zero fractional second
    def instant(text):
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))

    assert instant(ok.json()["data"]["updated_at"]) >= instant(story["updated_at"])
    stale = await client.patch(f"/v1/stories/{story['id']}", json={"words": 1}, headers={"if-match": '"0"'})
    assert stale.status_code == 412
    # unique index -> 409 with the field named
    dup = await client.post("/v1/authors", json={"name": "Other", "email": "a@x.io"})
    assert dup.status_code == 409 and dup.json()["errors"] == [
        {"field": "email", "message": "must be unique"}
    ]
    # soft delete: gone from the API, still in the collection
    assert (await client.delete(f"/v1/stories/{story['id']}")).status_code == 204
    assert (await client.get(f"/v1/stories/{story['id']}")).status_code == 404
    assert (await client.get("/v1/stories")).json()["data"] == []
    assert await Story.collection().count_documents({}) == 1


async def test_filters_sorting_and_cursor_pagination(app):
    client, (Author, Story, Reaction), *_ = app
    ann = await Author(name="Ann", email="a").insert()
    bob = await Author(name="Bob", email="b").insert()
    when = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    for i in range(7):
        await Story(
            title=f"story {i}",
            words=i * 100,
            author=ann if i % 2 else bob,
            published=when + dt.timedelta(days=i) if i < 5 else None,
        ).insert()

    def titles(response):
        assert response.status_code == 200, response.text
        return [s["title"] for s in response.json()["data"]]

    assert titles(await client.get("/v1/stories?words[gte]=400&sort=-words")) == [
        "story 6",
        "story 5",
        "story 4",
    ]
    assert titles(await client.get(f"/v1/stories?author={ann.id}&sort=title")) == [
        "story 1",
        "story 3",
        "story 5",
    ]
    assert titles(await client.get("/v1/stories?published[gte]=2024-01-04T00:00:00Z&sort=published")) == [
        "story 3",
        "story 4",
    ]
    assert titles(await client.get("/v1/stories?title[prefix]=story 6")) == ["story 6"]
    assert (
        titles(await client.get("/v1/stories?title[prefix]=.*")) == []
    )  # the prefix is a literal, not a regex
    assert (await client.get("/v1/stories?author=not-an-id")).status_code in (200, 400)
    seen, cursor = [], None
    while True:
        url = "/v1/stories?sort=-words&limit=3" + (f"&after={cursor}" if cursor else "")
        body = (await client.get(url)).json()
        seen += [s["words"] for s in body["data"]]
        cursor = body["page"]["next_cursor"]
        if not body["page"]["has_next"]:
            break
    assert seen == [600, 500, 400, 300, 200, 100, 0]
    assert (await client.get("/v1/stories?limit=3&after=tampered")).status_code == 400
    total = (await client.get("/v1/stories?words[lt]=300&total=true")).json()["page"]["total"]
    assert total == 3


async def test_includes_embed_related_documents(app):
    client, (Author, Story, Reaction), *_ = app
    ann = await Author(name="Ann", email="a", password="hunter2").insert()
    story = await Story(title="s", author=ann).insert()
    await Reaction.insert_many([Reaction(emoji="+1", story=story), Reaction(emoji="!", story=story)])
    plain = (await client.get(f"/v1/stories/{story.id}")).json()["data"]
    assert plain["author"] == str(ann.id) and "reactions" not in plain
    rich = (await client.get(f"/v1/stories/{story.id}?include=author,reactions,reaction_count")).json()[
        "data"
    ]
    assert rich["author"] == {"id": str(ann.id), "name": "Ann", "email": "a"}  # private field stays private
    assert sorted(r["emoji"] for r in rich["reactions"]) == ["!", "+1"] and rich["reaction_count"] == 2
    listed = (await client.get("/v1/stories?include=author")).json()["data"]
    assert listed[0]["author"]["name"] == "Ann"
    assert (await client.get(f"/v1/stories/{story.id}?include=password")).status_code == 400


async def test_row_scope_is_enforced_in_the_query(app):
    client, (Author, Story, Reaction), *_ = app
    ann = await Author(name="Ann", email="a").insert()
    bob = await Author(name="Bob", email="b").insert()
    hers = await Story(title="hers", author=ann).insert()
    his = await Story(title="his", author=bob).insert()
    as_ann = {"x-user": str(ann.id)}
    assert (await client.get("/v1/my-stories")).status_code == 401
    body = (await client.get("/v1/my-stories?total=true", headers=as_ann)).json()
    assert [s["title"] for s in body["data"]] == ["hers"] and body["page"] == {
        "size": 10,
        "has_next": False,
        "number": 1,
        "total": 1,
    }
    assert (await client.get(f"/v1/my-stories/{hers.id}", headers=as_ann)).status_code == 200
    assert (await client.get(f"/v1/my-stories/{his.id}", headers=as_ann)).status_code == 404
    assert (
        await client.patch(f"/v1/my-stories/{his.id}", json={"title": "x"}, headers=as_ann)
    ).status_code == 404
    assert (await client.delete(f"/v1/my-stories/{his.id}", headers=as_ann)).status_code == 404
    assert (await Story.get(his.id)).title == "his"
    assert (
        await client.patch(f"/v1/my-stories/{hers.id}", json={"title": "mine"}, headers=as_ann)
    ).status_code == 200
    assert (await client.delete(f"/v1/my-stories/{hers.id}", headers=as_ann)).status_code == 204


async def test_validation_never_reaches_the_database(app):
    client, (Author, Story, Reaction), _, registry = app
    for body in ({"title": 5}, {"title": "t", "words": "many"}, {"title": "t", "author": {"$ne": None}}):
        res = await client.post("/v1/stories", json=body)
        assert res.status_code == 422, (body, res.text)
    story = await Story(title="t").insert()
    for body in (
        {"words": {"$inc": 1}},
        {"title": None},
        {"rev": 5},
        {"deleted_at": None},
        {"reactions": []},
    ):
        res = await client.patch(f"/v1/stories/{story.id}", json=body)
        assert res.status_code == 422, (body, res.text)
    for query in ("words[$gt]=1", "$where=sleep(1)", "words[regex]=.*", "deleted_at[exists]=true", "rev=0"):
        assert (await client.get(f"/v1/stories?{query}")).status_code == 400, query
    raw = await Story.collection().find_one({"_id": story.id})
    assert raw["title"] == "t" and raw["words"] == 0 and raw["rev"] == 0
    assert await Story.collection().count_documents({}) == 1


async def test_unexpected_errors_do_not_leak(app):
    client, (Author, Story, Reaction), api, _ = app
    stories = api.resources[1]

    @stories.op("explode", method="POST", path="/explode")
    async def explode(ctx):
        raise RuntimeError("secret connection string mongodb://user:pw@host")

    res = await client.post("/v1/stories/explode")
    assert res.status_code == 500
    assert res.json() == {"type": "about:blank", "title": "Internal Server Error", "status": 500}


async def test_body_size_limit_and_lifespan(make_registry):
    from mongospecs import Registry

    Author, Story, Reaction = models("msgspec")
    registry = Registry(
        "mongodb://localhost:27117/?replicaSet=rs0", database="ms_web_lifespan", models=[Author]
    )
    api = Api(Resource(Author), max_body=64)
    asgi = AsgiApp(api, registry=registry)
    sent = []
    messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def receive():
        message = next(messages)
        if message["type"] == "lifespan.shutdown":
            assert registry.is_open
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=asgi), base_url="http://t"
            ) as client:
                big = await client.post("/authors", json={"name": "x" * 200})
                assert big.status_code == 413 and big.json()["status"] == 413
                assert (await client.post("/authors", json={"name": "ok"})).status_code == 201
            await registry.client.drop_database("ms_web_lifespan")
        return message

    async def send(message):
        sent.append(message["type"])

    await asgi({"type": "lifespan"}, receive, send)
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert not registry.is_open


async def test_delete_is_conditional_and_scoped_at_the_write(app):
    client, (Author, Story, Reaction), api, registry = app
    ann = await Author(name="Ann", email="a").insert()
    story = await Story(title="s", author=ann).insert()
    as_ann = {"x-user": str(ann.id)}
    # If-Match on DELETE: a stale revision is refused, the current one is accepted
    await client.patch(f"/v1/stories/{story.id}", json={"words": 5})
    stale = await client.delete(f"/v1/stories/{story.id}", headers={"if-match": '"0"'})
    assert stale.status_code == 412
    assert (await client.get(f"/v1/stories/{story.id}")).status_code == 200
    # the row scope is part of the delete's own filter, not only of the lookup before it
    events = []
    observer = type(registry)("mongodb://localhost:27117/?replicaSet=rs0", on_command=events.append)
    await observer.open()
    try:
        from mongospecs_web.backend import MongoBackend

        backend = MongoBackend(Story, observer.manager(Story, db=registry.database().name))
        await backend.delete(str(story.id), scope={"author": ann.id}, if_rev=1)
    finally:
        await observer.close()
    (write,) = [e.command for e in events if e.phase == "started" and e.command_name == "update"]
    assert write["updates"][0]["q"] == {"_id": story.id, "rev": 1, "author": ann.id}
    assert (await client.get(f"/v1/my-stories/{story.id}", headers=as_ann)).status_code == 404  # soft-deleted


async def test_listing_limits(app):
    client, (Author, Story, Reaction), api, registry = app
    ann = await Author(name="Ann", email="a").insert()
    await Story.insert_many([Story(title=f"s{i}", author=ann) for i in range(3)])
    as_ann = {"x-user": str(ann.id)}
    assert (await client.get("/v1/my-stories?page=6&limit=10", headers=as_ann)).status_code == 200
    deep = await client.get("/v1/my-stories?page=7&limit=10", headers=as_ann)
    assert deep.status_code == 400 and "too deep" in deep.json()["detail"]
    assert (await client.get("/v1/authors?total=true")).status_code == 400  # totals are opt-in per resource
    # reads made for a request carry a time limit even when nothing else sets one
    events = []
    observer = type(registry)("mongodb://localhost:27117/?replicaSet=rs0", on_command=events.append)
    await observer.open()
    try:
        from mongospecs_web.backend import ListQuery, MongoBackend

        manager = observer.manager(Story, db=registry.database().name)
        await MongoBackend(Story, manager).list(ListQuery(), scope=None)
        await MongoBackend(Story, manager, max_time_ms=None).list(ListQuery(), scope=None)
    finally:
        await observer.close()
    finds = [e.command for e in events if e.phase == "started" and e.command_name == "find"]
    assert finds[0]["maxTimeMS"] == 10_000 and "maxTimeMS" not in finds[1]
