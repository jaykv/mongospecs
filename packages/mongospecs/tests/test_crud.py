import datetime as dt

import pytest
from bson import ObjectId

from mongospecs import (
    DocumentNotFound,
    DuplicateKeyError,
    NotBoundError,
    Q,
    QueryError,
    ValidationError,
    trusted,
)


async def test_insert_assigns_id_and_tracks(m, registry):
    user = m.User(name="ann", age=30)
    assert user.is_new and user.id is None
    await user.insert()
    assert isinstance(user.id, ObjectId)
    assert not user.is_new
    assert user.changes() == {}
    raw = await m.User.collection().find_one({"_id": user.id})
    assert raw["name"] == "ann" and raw["age"] == 30


async def test_get_returns_typed_document_or_none(m, registry):
    user = await m.User(name="ann").insert()
    found = await m.User.get(user.id)
    assert type(found) is m.User and found.name == "ann" and found.id == user.id
    assert await m.User.get(str(user.id)) is not None  # ids are cast
    assert await m.User.get(ObjectId()) is None


async def test_find_all_first_one_and_or_fail(m, registry):
    await m.User.insert_many([m.User(name="a", age=1), m.User(name="b", age=2), m.User(name="c", age=3)])
    assert [u.name for u in await m.User.find().sort("name")] == ["a", "b", "c"]
    assert [u.name for u in await m.User.find(Q.age >= 2).sort("-age").all()] == ["c", "b"]
    assert (await m.User.find(Q.name == "b").first()).age == 2
    assert await m.User.find(Q.name == "zzz").first() is None
    with pytest.raises(DocumentNotFound):
        await m.User.find(Q.name == "zzz").one()
    with pytest.raises(DocumentNotFound):
        await m.User.find(Q.name == "zzz").or_fail().all()
    assert [u.name async for u in m.User.find().sort("name").skip(1).limit(1)] == ["b"]


async def test_count_exists_ids_estimated(m, registry):
    docs = await m.User.insert_many([m.User(name=n, age=i) for i, n in enumerate("abcd")])
    assert await m.User.count() == 4
    assert await m.User.count(Q.age > 1) == 2
    assert await m.User.estimated_count() == 4
    assert await m.User.exists(Q.name == "a") and not await m.User.exists(Q.name == "x")
    assert await m.User.find().sort("age").ids() == [d.id for d in docs]


async def test_read_modes(m, registry):
    user = await m.User(name="ann").insert()
    tracked = await m.User.find().one()
    lean = await m.User.find().lean().one()
    raw = await m.User.find().raw().one()
    assert not tracked.is_new and lean.is_new
    assert isinstance(raw, dict) and raw["_id"] == user.id and raw["name"] == "ann"
    assert type(lean) is m.User and lean.name == "ann"


async def test_save_new_then_delta(m, registry):
    user = m.User(name="ann", age=1)
    await user.save()
    assert not user.is_new
    user.age = 2
    assert user.changes() == {"$set": {"age": 2}}
    await user.save()
    assert user.changes() == {}
    assert (await m.User.get(user.id)).age == 2


async def test_save_unchanged_sends_nothing(m, make_registry):
    events = []
    await make_registry(m.User, on_command=events.append)
    user = await m.User(name="ann").insert()
    events.clear()
    await user.save()
    assert [e.command_name for e in events if e.phase == "started"] == []


async def test_save_preserves_unknown_fields(m, registry):
    user = await m.User(name="ann").insert()
    await m.User.collection().update_one({"_id": user.id}, {"$set": {"from_newer_app": 7}})
    loaded = await m.User.get(user.id)
    loaded.age = 5
    await loaded.save()
    raw = await m.User.collection().find_one({"_id": user.id})
    assert raw["from_newer_app"] == 7 and raw["age"] == 5


async def test_nested_document_diff_uses_dotted_paths(m, registry):
    user = await m.User(name="ann", address=m.Address(city="Oslo", zip="1")).insert()
    loaded = await m.User.get(user.id)
    assert type(loaded.address) is m.Address and loaded.address.city == "Oslo"
    loaded.address.city = "Bergen"
    assert loaded.changes() == {"$set": {"address.city": "Bergen"}}
    await loaded.save()
    assert (await m.User.find().raw().one())["address"] == {"city": "Bergen", "zip": "1"}


async def test_delete_and_reload(m, registry):
    user = await m.User(name="ann").insert()
    await m.User.collection().update_one({"_id": user.id}, {"$set": {"age": 9}})
    await user.reload()
    assert user.age == 9 and user.changes() == {}
    await user.delete()
    assert user.is_new and await m.User.count() == 0
    with pytest.raises(DocumentNotFound):
        await user.reload()


async def test_delete_missing_raises(m, registry):
    user = await m.User(name="ann").insert()
    await m.User.collection().delete_one({"_id": user.id})
    with pytest.raises(DocumentNotFound):
        await user.delete()
    user2 = await m.User(name="bob").insert()
    await m.User.collection().delete_one({"_id": user2.id})
    user2.age = 3
    with pytest.raises(DocumentNotFound):
        await user2.save()


async def test_lean_documents_are_not_saved_as_updates(m, registry):
    await m.User(name="ann").insert()
    lean = await m.User.find().lean().one()
    lean.age = 5
    with pytest.raises(DuplicateKeyError):
        await lean.save()  # untracked: treated as a new document


async def test_duplicate_key_reports_fields(m, registry):
    await registry.apply_indexes()
    await m.User(name="a", email="x@y.z").insert()
    with pytest.raises(DuplicateKeyError) as info:
        await m.User(name="b", email="x@y.z").insert()
    assert info.value.fields == {"email": "x@y.z"}
    assert info.value.__cause__ is not None


async def test_deferred_field_is_not_loaded_or_clobbered(m, registry):
    user = await m.User(name="ann", secret="s3cret").insert()
    loaded = await m.User.get(user.id)
    assert loaded.secret is None
    loaded.age = 4
    await loaded.save()
    assert "secret" not in await m.User.find().raw().one()  # deferred applies to raw reads too
    assert (await m.User.collection().find_one({}))["secret"] == "s3cret"
    with_secret = await m.User.find().select("+secret").one()
    assert with_secret.secret == "s3cret"
    updated = await m.User.update(user.id, age=5)
    assert updated.secret is None and updated.changes() == {}
    updated.name = "anna"
    await updated.save()
    assert (await m.User.collection().find_one({}))["secret"] == "s3cret"


async def test_select_projection_and_partial_save(m, registry):
    await m.User(name="ann", age=3, email="a@b.c").insert()
    part = await m.User.find().select("name").one()
    assert part.name == "ann" and part.age == 0 and part.email is None  # defaults, not loaded
    part.name = "anna"
    part.age = 99  # not loaded -> not saved
    assert part.changes() == {"$set": {"name": "anna"}}
    await part.save()
    raw = await m.User.find().raw().one()
    assert raw["name"] == "anna" and raw["age"] == 3 and raw["email"] == "a@b.c"
    excl = await m.User.find().select("-email").raw().one()
    assert "email" not in excl and excl["age"] == 3
    with pytest.raises(QueryError):
        m.User.find().select("name", "-age").compile()


async def test_enum_date_and_datetime_round_trip(m, registry):
    user = await m.User(name="ann", color=m.Color.BLUE).insert()
    raw = await m.User.find().raw().one()
    assert raw["color"] == "blue"
    assert (await m.User.get(user.id)).color is m.Color.BLUE
    assert (await m.User.find(Q.color == m.Color.BLUE).one()).id == user.id
    article = await m.Article(title="t").insert()
    assert article.created_at.tzinfo is not None
    loaded = await m.Article.get(article.id)
    assert loaded.created_at == article.created_at
    assert dt.datetime.now(dt.UTC) - loaded.created_at < dt.timedelta(minutes=1)


async def test_validation_on_read(m, registry):
    await m.User.collection().insert_one({"name": "bad", "age": "not a number"})
    with pytest.raises(ValidationError) as info:
        await m.User.find().all()
    assert info.value.errors[0]["field"] == "age"
    assert (await m.User.find().raw().one())["age"] == "not a number"


async def test_unbound_model_raises(m):
    class_ = m.User
    with pytest.raises(NotBoundError):
        class_.find()


async def test_raw_filters_are_untrusted(m, registry):
    await m.User(name="ann", age=5).insert()
    assert await m.User.count({"name": "ann"}) == 1
    with pytest.raises(QueryError):
        await m.User.count({"age": {"$gt": 1}})
    with pytest.raises(QueryError):
        await m.User.count({"$where": "true"})
    assert await m.User.count(trusted({"age": {"$gt": 1}})) == 1


async def test_each_with_concurrency(m, registry):
    await m.User.insert_many([m.User(name=str(i), age=i) for i in range(25)])
    seen = []

    async def visit(user):
        seen.append(user.age)

    assert await m.User.find().each(visit, concurrency=5) == 25
    assert sorted(seen) == list(range(25))
    assert await m.User.find(Q.age < 3).each(visit) == 3


async def test_hydrate_and_collection_escape_hatch(m, registry):
    await m.User.collection().insert_one({"name": "raw"})
    raw = await m.User.collection().find_one({"name": "raw"})
    doc = m.User.hydrate(raw)
    assert not doc.is_new
    doc.age = 1
    await doc.save()
    assert (await m.User.get(doc.id)).age == 1


async def test_aggregate_decodes_and_explain(m, registry):
    await m.User.insert_many([m.User(name="a", age=1), m.User(name="b", age=5)])
    rows = await m.User.aggregate([{"$group": {"_id": None, "total": {"$sum": "$age"}}}])
    assert rows[0]["total"] == 6
    typed = await m.User.aggregate([{"$match": {"age": {"$gt": 2}}}], as_=m.User)
    assert [type(u) for u in typed] == [m.User] and typed[0].name == "b"
    plan = await m.User.find(Q.age > 1).explain()
    assert "queryPlanner" in plan or "stages" in plan
