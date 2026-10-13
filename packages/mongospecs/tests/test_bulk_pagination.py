import pytest

from mongospecs import (
    BulkWriteFailed,
    DeleteMany,
    DeleteOne,
    DuplicateKeyError,
    Inc,
    InsertOne,
    Q,
    QueryError,
    StaleDocumentError,
    UpdateMany,
    UpdateOne,
)


async def test_insert_many_ordered_and_unordered(m, registry):
    await registry.apply_indexes()
    result = await m.User.insert_many([m.User(name="a", email="1"), {"name": "b", "email": "2"}])
    assert len(result) == 2 and not result.errors
    assert all(not d.is_new and d.id for d in result) and type(result.inserted[1]) is m.User
    with pytest.raises((DuplicateKeyError, BulkWriteFailed)):
        await m.User.insert_many(
            [m.User(name="c", email="3"), m.User(name="dup", email="1"), m.User(name="d", email="4")]
        )
    assert await m.User.count() == 3  # ordered: stopped at the failure
    docs = [
        m.User(name="e", email="5"),
        m.User(name="dup", email="1"),
        m.User(name="dup2", email="2"),
        m.User(name="f", email="6"),
    ]
    partial = await m.User.insert_many(docs, ordered=False)
    assert [d.name for d in partial.inserted] == ["e", "f"]
    assert [i for i, _ in partial.errors] == [1, 2]
    assert all(isinstance(e, DuplicateKeyError) for _, e in partial.errors)
    assert partial.errors[0][1].fields == {"email": "1"}
    assert docs[1].is_new and not docs[0].is_new
    assert (await m.User.insert_many([])).inserted == []


async def test_bulk_write_typed_operations(m, registry):
    a = await m.Article(title="a").insert()
    b = await m.Article(title="b").insert()
    c = await m.Article(title="c").insert()
    result = await m.Article.bulk_write(
        [
            InsertOne(m.Article(title="new")),
            InsertOne({"title": "from dict"}),
            UpdateOne(a.id, Inc(views=5), title="a2"),
            UpdateMany(Q.title.in_(["b", "c"]), Inc(views=1)),
            UpdateOne(Q.title == "upserted", Inc(views=1), upsert=True),
            DeleteOne(c.id),
            DeleteMany(Q.title == "b", hard=True),
        ]
    )
    assert result.inserted_count == 2 and result.upserted_count == 1
    raw = {d["title"]: d async for d in m.Article.collection().find({})}
    assert (
        raw["a2"]["views"] == 5
        and raw["a2"]["rev"] == 1
        and raw["a2"]["updated_at"] > raw["a2"]["created_at"]
    )
    assert raw["new"]["created_at"] is not None and raw["from dict"]["rev"] == 0
    assert raw["upserted"]["created_at"] is not None
    assert raw["c"]["deleted_at"] is not None and raw["c"]["views"] == 1  # soft-deleted by DeleteOne
    assert "b" not in raw  # hard-deleted
    assert await m.Article.count() == 4
    assert b.id not in [d["_id"] for d in raw.values()]
    with pytest.raises(QueryError):
        await m.Article.bulk_write([{"insertOne": {}}])
    with pytest.raises(QueryError):
        await m.Article.bulk_write([UpdateOne(a.id, nope=1)])
    assert await m.Article.bulk_write([]) is None


async def test_bulk_save_mixes_inserts_and_deltas(m, make_registry):
    events = []
    await make_registry(m.Article, m.User, on_command=events.append)
    a = await m.Article(title="a").insert()
    b = await m.Article(title="b").insert()
    untouched = await m.Article(title="same").insert()
    fresh = m.Article(title="fresh")
    a.title = "a2"
    b.views = 7
    events.clear()
    assert await m.Article.bulk_save([a, b, untouched, fresh]) == 3
    assert [e.command_name for e in events if e.phase == "started"] == ["bulkWrite"] or len(
        [e for e in events if e.phase == "started"]
    ) <= 2
    assert a.rev == 1 and b.rev == 1 and untouched.rev == 0 and not fresh.is_new
    assert a.changes() == b.changes() == fresh.changes() == {}
    assert sorted(d.title for d in await m.Article.find()) == ["a2", "b", "fresh", "same"]
    stale = await m.Article.get(a.id)
    a.title = "a3"
    await a.save()
    stale.title = "lost"
    b.views = 8
    with pytest.raises(StaleDocumentError):
        await m.Article.bulk_save([stale, b])
    assert await m.Article.bulk_save([]) == 0


async def test_cursor_pagination(m, registry):
    await m.User.insert_many([m.User(name=f"u{i:02d}", age=i % 3) for i in range(11)])
    query = m.User.find().sort("age", "-name")
    seen, cursor, pages = [], None, 0
    while True:
        page = await query.page(4, after=cursor)
        seen.extend((u.age, u.name) for u in page)
        pages += 1
        assert page.number is None and page.total is None and len(page) <= 4
        if not page.has_next:
            assert page.next_cursor is None
            break
        cursor = page.next_cursor
    expected = [(u.age, u.name) for u in await m.User.find().sort("age", "-name")]
    assert seen == expected and pages == 3
    default = await m.User.find().page(5)
    assert [u.id for u in default] == (await m.User.find().sort("id").ids())[:5]
    with_total = await m.User.find(Q.age == 0).page(2, total=True)
    assert with_total.total == 4 and with_total.has_next and with_total[0].age == 0
    for bad in ("garbage", "", "e30"):
        with pytest.raises(QueryError):
            await query.page(4, after=bad)


async def test_cursor_is_stable_under_concurrent_inserts(m, registry):
    await m.User.insert_many([m.User(name=f"u{i:02d}") for i in range(6)])
    first = await m.User.find().sort("name").page(3)
    await m.User(name="u00a").insert()  # lands before the cursor: must not shift the next page
    second = await m.User.find().sort("name").page(3, after=first.next_cursor)
    assert [u.name for u in first] == ["u00", "u01", "u02"]
    assert [u.name for u in second] == ["u03", "u04", "u05"]


async def test_offset_pagination(m, registry):
    await m.User.insert_many([m.User(name=f"u{i}", age=i) for i in range(7)])
    page = await m.User.find().sort("age").page(3, number=2, total=True)
    assert [u.age for u in page] == [3, 4, 5] and page.number == 2 and page.total == 7 and page.has_next
    last = await m.User.find().sort("age").page(3, number=3)
    assert [u.age for u in last] == [6] and not last.has_next and last.next_cursor is None
    assert (await m.User.find().page(3, number=9)).items == []
    with pytest.raises(QueryError):
        await m.User.find().page(3, number=0)
    with pytest.raises(QueryError):
        await m.User.find().page(0)
    with pytest.raises(QueryError):
        await m.User.find().page(3, number=1, after="x")
    raw = await m.User.find().raw().sort("age").page(2)
    assert isinstance(raw[0], dict) and raw.next_cursor
