"""Reads served straight from stored documents must equal reads served through the model."""

import datetime as dt
import enum
import json
import typing as t

import pytest

from mongospecs import (
    Alias,
    CreatedAt,
    Deferred,
    DeletedAt,
    Policy,
    Q,
    Ref,
    Revision,
    SpecConfig,
    computed,
)
from mongospecs_web import Api, Delete, Get, List, Resource
from mongospecs_web.policy import view_of

BACKENDS = ("msgspec", "pydantic", "attrs")


class Colour(enum.Enum):
    RED = "red"
    BLUE = "blue"


def models(backend):
    if backend == "msgspec":
        import msgspec

        from mongospecs.msgspec import Spec

        class Base(msgspec.Struct, kw_only=True):
            pass

        define = lambda c: c  # noqa: E731
    elif backend == "pydantic":
        import pydantic

        from mongospecs.pydantic import Spec

        Base = pydantic.BaseModel
        define = lambda c: c  # noqa: E731
    else:
        import attrs

        from mongospecs.attrs import Spec

        Base = object
        define = attrs.define(kw_only=True)

    @define
    class Geo(Base):
        lat: float = 0.0
        surveyor: t.Annotated[str | None, Policy(private=True)] = None

    @define
    class Addr(Base):
        city: str = ""
        vault_code: t.Annotated[str, Policy(private=True)] = ""
        geo: Geo | None = None

    @define
    class Maker(Spec):
        __spec__ = SpecConfig(collection="makers")
        name: str = ""

    @define
    class Item(Spec):
        __spec__ = SpecConfig(collection="items")
        name: t.Annotated[str, Policy(filter=("eq", "prefix"), sort=True)] = ""
        label: t.Annotated[str, Alias("l")] = ""
        price: t.Annotated[float, Policy(sort=True)] = 0.0
        stock: int = 0
        active: bool = True
        colour: Colour | None = None
        tags: list[str] = []  # noqa: RUF012
        scores: dict[str, int] = {}  # noqa: RUF012
        extra: dict[str, t.Any] = {}  # noqa: RUF012
        address: Addr | None = None
        history: list[Addr] = []  # noqa: RUF012
        maker: Ref[Maker] | None = None
        makers: list[Ref[Maker]] = []  # noqa: RUF012
        secret: t.Annotated[str | None, Policy(private=True)] = None
        blob: t.Annotated[str | None, Deferred] = None
        created_at: t.Annotated[dt.datetime | None, CreatedAt] = None
        seen_at: dt.datetime | None = None
        deleted_at: t.Annotated[dt.datetime | None, DeletedAt, Policy(private=True)] = None
        rev: t.Annotated[int, Revision] = 0

    @define
    class Shape(Spec):
        __spec__ = SpecConfig(collection="shapes", discriminator="kind")
        name: str = ""

    @define
    class Circle(Shape):
        radius: float = 1.0

    @define
    class Scored(Spec):
        __spec__ = SpecConfig(collection="scored")
        points: int = 0

        @computed
        def double(self) -> int:
            return self.points * 2

    return Geo, Addr, Maker, Item, Shape, Circle, Scored


@pytest.fixture(params=BACKENDS)
async def world(request, make_registry):
    classes = models(request.param)
    Geo, Addr, Maker, Item, Shape, Circle, Scored = classes
    events = []
    await make_registry(Maker, Item, Shape, Circle, Scored, on_command=events.append)

    def api(raw):
        resources = [
            Resource(Item, name="items", ops=[List, Get, Delete], include=["maker"]),
            Resource(Shape, name="shapes", ops=[List, Get]),
            Resource(Scored, name="scored", ops=[List, Get]),
        ]
        for resource in resources:
            resource.raw_reads = raw
        return Api(*resources, prefix="/v1")

    return classes, api(True), api(False), events


async def seed(Geo, Addr, Maker, Item, *_):
    acme = await Maker(name="acme").insert()
    other = await Maker(name="other").insert()
    await Item(
        name="full",
        label="L",
        price=9.5,
        stock=3,
        active=False,
        colour=Colour.BLUE,
        tags=["a", "b"],
        scores={"x": 1},
        extra={"k": [1, "two", {"three": 3.5, "none": None}], "flag": True},
        address=Addr(city="here", vault_code="v", geo=Geo(lat=1.25, surveyor="s")),
        history=[Addr(city="old"), Addr(city="older", geo=Geo(lat=2.0))],
        maker=acme,
        makers=[acme, other],
        secret="hidden",
        blob="big",
        seen_at=dt.datetime(2024, 5, 6, 7, 8, 9, 123000, tzinfo=dt.UTC),
    ).insert()
    await Item(name="defaults").insert()
    coll = Item.collection()
    # shapes other writers produce: missing keys, an int stored as a double, a double as an int,
    # extra keys, a non-JSON value inside an untyped field, a soft-deleted document
    await coll.insert_one({"name": "sparse"})
    await coll.insert_one({"name": "odd numbers", "stock": 4.0, "price": 7, "rev": 2.0})
    await coll.insert_one(
        {
            "name": "extras",
            "unknown": 1,
            "address": {"city": "c", "zz": 1},
            "extra": {"when": dt.datetime(2021, 1, 1)},
        }
    )
    await coll.insert_one({"name": "gone", "deleted_at": dt.datetime(2020, 1, 1)})
    return acme


async def call(api, method, url, headers=None):
    from urllib.parse import parse_qsl, urlsplit

    parts = urlsplit(url)
    return await api.handle(method, parts.path, query=parse_qsl(parts.query), headers=headers or {})


async def both(fast, slow, method, path):
    a, b = await call(fast, method, path), await call(slow, method, path)
    assert a.status == b.status, (path, a.body, b.body)
    if a.body != b.body:
        # The only permitted difference is the opaque cursor: it is built from the last sort key as
        # stored (an int 7) on one path and as the model holds it (a float 7.0) on the other.
        # Both resume at the same place, which the pagination walk below checks.
        one, two = json.loads(a.body), json.loads(b.body)
        assert one["page"].pop("next_cursor") and two["page"].pop("next_cursor")
        assert one == two, f"{path}\nraw:   {a.body!r}\nmodel: {b.body!r}"
    assert a.headers == b.headers
    return a


async def test_raw_reads_equal_model_reads(world):
    classes, fast, slow, events = world
    Item = classes[3]
    assert view_of(Item).fast is not None  # this model is served without building models
    await seed(*classes)
    listing = await both(fast, slow, "GET", "/v1/items?sort=name&limit=50")
    rows = json.loads(listing.body)["data"]
    assert [r["name"] for r in rows] == ["defaults", "extras", "full", "odd numbers", "sparse"]  # not "gone"
    for row in rows:
        await both(fast, slow, "GET", f"/v1/items/{row['id']}")
        assert "secret" not in row and "blob" not in row and "deleted_at" not in row
        assert "vault_code" not in json.dumps(row) and "surveyor" not in json.dumps(row)
    full = next(r for r in rows if r["name"] == "full")
    assert (
        full["label"] == "L"
        and full["colour"] == "blue"
        and full["address"] == {"city": "here", "geo": {"lat": 1.25}}
    )
    for path in (
        "/v1/items?sort=-price&limit=2",
        "/v1/items?name[prefix]=o",
        "/v1/items?include=maker&sort=name",
        f"/v1/items/{full['id']}?include=maker",
        "/v1/items/000000000000000000000000",
        "/v1/items/not-an-id",
    ):
        await both(fast, slow, "GET", path)
    # cursor pagination walks the same pages on both paths, also across a sort key stored as another number type
    for sort in ("name", "-price", "price"):
        walks = []
        for api in (fast, slow):
            cursor, seen = None, []
            while True:
                url = f"/v1/items?sort={sort}&limit=2" + (f"&after={cursor}" if cursor else "")
                page = json.loads((await call(api, "GET", url)).body)
                seen += [r["name"] for r in page["data"]]
                cursor = page["page"]["next_cursor"]
                if not page["page"]["has_next"]:
                    break
            walks.append(seen)
        # two documents have no stored price: they are paged as well, first ascending, last descending
        assert walks[0] == walks[1] and len(walks[0]) == len(set(walks[0])) == 5, (sort, walks)


async def test_raw_reads_fetch_only_what_is_shown(world):
    classes, fast, slow, events = world
    Item = classes[3]
    await seed(*classes)
    events.clear()
    assert (await call(fast, "GET", "/v1/items?sort=name")).status == 200
    (find,) = [e.command for e in events if e.phase == "started" and e.command_name == "find"]
    projection = find["projection"]
    assert set(projection.values()) == {1}
    for hidden in ("secret", "blob", "deleted_at"):
        assert hidden not in projection  # private and deferred fields never leave the database
    assert {"name", "l", "price", "address", "rev"} <= set(projection)
    assert find["filter"].get("deleted_at", "absent") is None  # the soft-delete scope still applies
    # an after-hook wants a model, so the resource reads models again
    items = fast.resources[0]
    seen = []
    items.after("list")(lambda ctx, result: seen.append(type(result.items[0])))
    events.clear()
    assert (await call(fast, "GET", "/v1/items")).status == 200
    assert seen == [Item]
    (find,) = [e.command for e in events if e.phase == "started" and e.command_name == "find"]
    assert "name" not in (find.get("projection") or {})


async def test_models_that_need_the_model_path_take_it(world):
    classes, fast, slow, events = world
    Geo, Addr, Maker, Item, Shape, Circle, Scored = classes
    assert view_of(Shape).fast is None and view_of(Scored).fast is None  # discriminator, computed field
    await Circle(name="c", radius=2.0).insert()
    await Shape(name="s").insert()
    await Scored(points=4).insert()
    shapes = json.loads((await both(fast, slow, "GET", "/v1/shapes")).body)["data"]
    assert {s["kind"] for s in shapes} == {"Circle", "Shape"} and any("radius" in s for s in shapes)
    scored = json.loads((await both(fast, slow, "GET", "/v1/scored")).body)["data"]
    assert scored[0]["double"] == 8


def test_value_changing_models_are_not_served_raw():
    import attrs
    import msgspec
    import pydantic

    from mongospecs.attrs import Spec as AttrsSpec
    from mongospecs.msgspec import Spec as MsgspecSpec
    from mongospecs.pydantic import Spec as PydanticSpec

    class Plain(PydanticSpec):
        name: str = ""

    class Validated(PydanticSpec):
        name: str = ""

        @pydantic.field_validator("name")
        @classmethod
        def tidy(cls, value):
            return value.strip()

    class Stripped(PydanticSpec):
        model_config = pydantic.ConfigDict(str_strip_whitespace=True)
        name: str = ""

    class Serialised(PydanticSpec):
        name: str = ""

        @pydantic.field_serializer("name")
        def shout(self, value):
            return value.upper()

    class Annotated(PydanticSpec):
        name: t.Annotated[str, pydantic.BeforeValidator(str.strip)] = ""

    @attrs.define(kw_only=True)
    class Converted(AttrsSpec):
        name: str = attrs.field(default="", converter=str.strip)

    class Hooked(MsgspecSpec):
        name: str = ""

        def __post_init__(self):
            super().__post_init__()
            self.name = self.name.strip()

    class Constrained(MsgspecSpec):
        name: t.Annotated[str, msgspec.Meta(max_length=50)] = ""
        when: dt.date | None = None  # a type the plan does not know

    class Known(MsgspecSpec):
        name: t.Annotated[str, msgspec.Meta(max_length=50)] = ""

    assert view_of(Plain).fast is not None and view_of(Known).fast is not None
    for model in (Validated, Stripped, Serialised, Annotated, Converted, Hooked, Constrained):
        assert view_of(model).fast is None, model.__name__


async def test_etag_and_conditional_get_on_raw_reads(world):
    classes, fast, slow, events = world
    Item = classes[3]
    item = await Item(name="e").insert()
    await Item.update(item.id, stock=1)
    got = await both(fast, slow, "GET", f"/v1/items/{item.id}")
    assert got.headers["etag"] == '"1"'
    for api in (fast, slow):
        cached = await call(api, "GET", f"/v1/items/{item.id}", headers={"if-none-match": '"1"'})
        assert cached.status == 304
    assert await Item.count(Q.name == "e") == 1


async def test_delete_is_one_write_unless_the_document_is_needed(world):
    classes, fast, slow, events = world
    Geo, Addr, Maker, Item, Shape, Circle, Scored = classes
    item = await Item(name="d").insert()
    events.clear()
    assert (await call(fast, "DELETE", f"/v1/items/{item.id}")).status == 204
    assert [e.command_name for e in events if e.phase == "started"] == [
        "update"
    ]  # soft delete, no read first
    assert await Item.count() == 0 and await Item.collection().count_documents({}) == 1
    assert (await call(fast, "DELETE", f"/v1/items/{item.id}")).status == 404  # already deleted
    assert (await call(fast, "DELETE", "/v1/items/nope")).status == 404
    # with If-Match: a stale revision is 412 and the current one deletes
    other = await Item(name="d2").insert()
    await Item.update(other.id, stock=2)
    assert (await call(fast, "DELETE", f"/v1/items/{other.id}", headers={"if-match": '"0"'})).status == 412
    assert (await call(fast, "DELETE", f"/v1/items/{other.id}", headers={"if-match": '"1"'})).status == 204
    # a model with a delete hook still gets its document
    third = await Item(name="d3").insert()
    seen = []

    @Item.before("delete")
    async def note(ctx, doc):
        seen.append(doc.name)

    try:
        events.clear()
        assert (await call(fast, "DELETE", f"/v1/items/{third.id}")).status == 204
        assert seen == ["d3"]
        assert [e.command_name for e in events if e.phase == "started"] == ["find", "update"]
    finally:
        from mongospecs.meta import _HOOKS

        _HOOKS.pop(Item, None)
