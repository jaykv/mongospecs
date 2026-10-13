import typing as t

import pytest

from mongospecs import Action, Backref, Inc, Index, OperationFailed, Ref, Set, SpecConfig
from mongospecs.indexes import mongo_json_schema


def by_name(plan):
    return {a.name: a.action for a in plan.actions}


async def test_plan_create_then_retain(m, make_registry):
    reg = await make_registry(m.Article, m.User)
    plan = await reg.plan_indexes()
    assert by_name(plan) == {
        "title_1": Action.CREATE,
        "views_-1_title_1": Action.CREATE,
        "email_1": Action.CREATE,
    }
    assert not plan.in_sync and not any(a.destructive for a in plan)
    done = await reg.apply_indexes(plan)
    assert len(done) == 3
    again = await reg.plan_indexes()
    assert again.in_sync and set(by_name(again).values()) == {Action.RETAIN}
    info = await m.User.collection().index_information()
    assert info["email_1"]["unique"] is True and info["email_1"]["sparse"] is True
    assert await reg.apply_indexes() == []
    assert "CREATE" in str(plan.actions[0])


async def test_destructive_actions_are_gated(m, make_registry):
    reg = await make_registry(m.User)
    coll = m.User.collection()
    await coll.create_index([("email", 1)], name="email_1")  # same name, not unique -> RECREATE
    await coll.create_index([("age", 1)], name="stray")  # undeclared -> DROP
    plan = await reg.plan_indexes()
    assert by_name(plan) == {"email_1": Action.RECREATE, "stray": Action.DROP}
    assert await reg.apply_indexes(plan) == []  # nothing destructive without drop=True
    assert "stray" in await coll.index_information()
    done = await reg.apply_indexes(plan, drop=True)
    assert {a.action for a in done} == {Action.RECREATE, Action.DROP}
    info = await coll.index_information()
    assert "stray" not in info and info["email_1"]["unique"] is True
    assert (await reg.plan_indexes()).in_sync


async def test_conflict_same_keys_other_name(m, make_registry):
    reg = await make_registry(m.User)
    await m.User.collection().create_index([("email", 1)], name="legacy_email", unique=True, sparse=True)
    plan = await reg.plan_indexes()
    (action,) = plan.actions
    assert action.action is Action.CONFLICT and action.existing == "legacy_email" and action.destructive
    await reg.apply_indexes(plan, drop=True)
    assert set(await m.User.collection().index_information()) == {"_id_", "email_1"}


async def test_text_ttl_partial_and_alias_indexes(m, make_registry):
    @m.define
    class Doc(m.Spec):
        __spec__ = SpecConfig(
            collection="idx_docs",
            indexes=[
                Index(("body", "text"), ("title", "text"), name="search"),
                Index("expires", ttl=3600),
                Index("code", unique=True, partial={"active": True}),
                Index("short", collation={"locale": "en", "strength": 2}),
            ],
        )
        body: str = ""
        title: str = ""
        expires: t.Any = None
        code: str = ""
        active: bool = True
        short: str = ""

    reg = await make_registry(Doc)
    await reg.apply_indexes()
    info = await Doc.collection().index_information()
    assert info["search"]["weights"] == {"body": 1, "title": 1}
    assert info["expires_1"]["expireAfterSeconds"] == 3600
    assert info["code_1"]["partialFilterExpression"] == {"active": True}
    # materialised text indexes and server-filled collation defaults do not show as drift
    assert (await reg.plan_indexes()).in_sync


async def test_index_on_aliased_field_uses_stored_name(m, make_registry):
    from mongospecs import Alias

    @m.define
    class Short(m.Spec):
        __spec__ = SpecConfig(collection="idx_short", indexes=[Index("-summary")])
        summary: t.Annotated[str, Alias("s")] = ""

    reg = await make_registry(Short)
    await reg.apply_indexes()
    assert (await Short.collection().index_information())["s_-1"]["key"] == [("s", -1)]


async def test_discriminated_models_share_index_ownership(m, make_registry):
    @m.define
    class Shape(m.Spec):
        __spec__ = SpecConfig(collection="idx_shapes", discriminator="kind", indexes=[Index("name")])
        name: str = ""

    @m.define
    class Circle(Shape):
        __spec__ = SpecConfig(indexes=[Index("radius")])
        radius: int = 0

    reg = await make_registry(Shape, Circle)
    await reg.apply_indexes()
    assert {"name_1", "radius_1"} <= set(await Shape.collection().index_information())
    plan = await reg.plan_indexes()
    assert not plan.of(Action.DROP) and plan.in_sync


async def test_missing_foreign_key_index_is_flagged(m, make_registry):
    @m.define
    class Parent(m.Spec):
        __spec__ = SpecConfig(collection="idx_parents")
        kids: t.Annotated[list[t.Any], Backref("parent", model="Kid")] = []

    @m.define
    class Kid(m.Spec):
        __spec__ = SpecConfig(collection="idx_kids")
        parent: Ref[Parent] | None = None

    reg = await make_registry(Parent, Kid)
    plan = await reg.plan_indexes()
    assert len(plan.advisories) == 1 and "Kid.parent" in plan.advisories[0]
    reg2_plan = await (await _rebind(reg, m.Post, m.Note)).plan_indexes(m.Post, m.Note)
    assert reg2_plan.advisories == []  # Note declares Index("post")


async def _rebind(reg, *models):
    reg.bind(*models)
    return reg


async def test_json_schema_validator_is_opt_in(m, make_registry):
    @m.define
    class Checked(m.Spec):
        __spec__ = SpecConfig(collection="checked", json_schema_validation="strict")
        name: str
        age: int = 0
        owner: Ref[m.User] | None = None

    schema = mongo_json_schema(Checked)
    assert schema["bsonType"] == "object" and schema["required"] == ["name"]
    assert schema["properties"]["name"]["bsonType"] == "string"
    assert "_id" not in schema["properties"]
    reg = await make_registry(Checked, m.User)
    await reg.apply_indexes()
    await Checked(name="ok", age=3).insert()
    user = await m.User(name="u").insert()
    await Checked(name="ok2", owner=user).insert()
    with pytest.raises(OperationFailed) as info:
        await Checked.find().update(Set({"name": "x"}), Inc(age=0.5))  # server-side: age must stay an integer
    assert info.value.code == 121
    from pymongo.errors import WriteError

    with pytest.raises(WriteError):  # the raw collection is the raw driver: errors are not translated
        await Checked.collection().insert_one({"name": 5})


async def test_collection_options_are_applied(m, make_registry):
    @m.define
    class Capped(m.Spec):
        __spec__ = SpecConfig(collection="capped_log", collection_options={"capped": True, "size": 4096})
        line: str = ""

    reg = await make_registry(Capped)
    await reg.apply_indexes()
    options = await Capped.collection().options()
    assert options["capped"] is True and options["size"] >= 4096
    await reg.apply_indexes()  # idempotent
