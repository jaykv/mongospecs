"""StoreNone / OmitNone / SpecConfig(none=...): whether a None value is stored as null or left out."""

import typing as t

import pytest

from mongospecs import (
    ConfigurationError,
    Exists,
    Index,
    InsertOne,
    OmitNone,
    Q,
    SpecConfig,
    StoreNone,
    Unset,
    UpdateOne,
    meta_of,
)


def embedded_base(m):
    if m.backend == "msgspec":
        import msgspec

        class Base(msgspec.Struct, kw_only=True):
            pass

        return Base
    if m.backend == "pydantic":
        import pydantic

        return pydantic.BaseModel
    return object


@pytest.fixture
async def models(m, make_registry):
    Base = embedded_base(m)

    @m.define
    class Part(Base):
        label: str = ""
        note: str | None = None  # omitted while None
        code: t.Annotated[str | None, StoreNone] = None  # stored as null

    @m.define
    class Profile(m.Spec):
        __spec__ = SpecConfig(collection="profiles")
        name: str = ""
        nick: str | None = None  # the default: omitted
        email: t.Annotated[str | None, StoreNone] = None
        part: Part | None = None
        parts: list[Part] = []  # noqa: RUF012

    @m.define
    class Legacy(m.Spec):
        __spec__ = SpecConfig(collection="legacy", none="store")
        name: str = ""
        phone: str | None = None  # stored as null: the model says so
        fax: t.Annotated[str | None, OmitNone] = None  # the field opts out

    await make_registry(Profile, Legacy)
    return Profile, Legacy, Part


async def raw(model, doc):
    return await model.collection().find_one({"_id": doc.id})


async def test_round_trip_per_mode(models):
    Profile, Legacy, Part = models
    doc = await Profile(name="a").insert()
    stored = await raw(Profile, doc)
    assert stored["email"] is None and "email" in stored  # StoreNone: an explicit null
    assert "nick" not in stored  # omit mode: no key
    loaded = await Profile.get(doc.id)
    assert loaded.email is None and loaded.nick is None and loaded.changes() == {}


async def test_delta_save_per_mode(models):
    Profile, Legacy, Part = models
    doc = await Profile(name="a").insert()
    doc.email, doc.nick = "a@x.io", "ann"
    assert doc.changes() == {"$set": {"email": "a@x.io", "nick": "ann"}}
    await doc.save()
    doc.email, doc.nick = None, None
    assert doc.changes() == {"$set": {"email": None}, "$unset": {"nick": ""}}
    await doc.save()
    stored = await raw(Profile, doc)
    assert stored["email"] is None and "email" in stored and "nick" not in stored
    assert (await Profile.get(doc.id)).changes() == {}


async def test_update_and_upsert_per_mode(models):
    Profile, Legacy, Part = models
    doc = await Profile(name="a", email="a@x.io", nick="ann").insert()
    await Profile.update(doc.id, email=None, nick=None)
    stored = await raw(Profile, doc)
    assert "email" in stored and stored["email"] is None and "nick" not in stored
    created = await Profile.upsert(Q.name == "fresh", name="fresh")
    stored = await raw(Profile, created)
    assert "email" in stored and stored["email"] is None and "nick" not in stored
    assert created.changes() == {}
    # Unset always removes the key, whatever the mode
    await Profile.update(doc.id, Unset("email"))
    assert "email" not in await raw(Profile, doc)


async def test_bulk_paths_per_mode(models):
    Profile, Legacy, Part = models
    many = (await Profile.insert_many([Profile(name="m1"), Profile(name="m2")])).inserted
    fresh, existing = Profile(name="b1"), many[0]
    existing.name = "m1!"
    await Profile.bulk_save([fresh, existing])
    await Profile.bulk_write(
        [InsertOne(Profile(name="w1")), UpdateOne(Q.name == "m2", email=None, nick=None)]
    )
    docs = [d async for d in Profile.collection().find({})]
    assert len(docs) == 4
    for stored in docs:
        assert "email" in stored and stored["email"] is None, stored
        assert "nick" not in stored, stored


async def test_model_wide_setting_and_field_opt_out(models):
    Profile, Legacy, Part = models
    doc = await Legacy(name="l").insert()
    stored = await raw(Legacy, doc)
    assert "phone" in stored and stored["phone"] is None  # none="store"
    assert "fax" not in stored  # OmitNone wins over the model
    doc.phone, doc.fax = "1", "2"
    await doc.save()
    doc.phone, doc.fax = None, None
    assert doc.changes() == {"$set": {"phone": None}, "$unset": {"fax": ""}}
    await Legacy.update(doc.id, phone=None, fax=None)
    stored = await raw(Legacy, doc)
    assert "phone" in stored and "fax" not in stored


async def test_embedded_models_follow_their_own_field_markers(models):
    Profile, Legacy, Part = models
    doc = await Profile(
        name="e", part=Part(label="one"), parts=[Part(label="a"), Part(label="b", code="c")]
    ).insert()
    stored = await raw(Profile, doc)
    assert stored["part"] == {"label": "one", "code": None}  # note omitted, code stored as null
    assert stored["parts"] == [{"label": "a", "code": None}, {"label": "b", "code": "c"}]
    loaded = await Profile.get(doc.id)
    assert loaded.changes() == {} and loaded.part.note is None and loaded.parts[0].code is None


async def test_exists_means_different_things_per_mode(models):
    Profile, Legacy, Part = models
    await Profile.insert_many([Profile(name="none"), Profile(name="both", email="e", nick="n")])
    assert await Profile.count(Exists(Q.nick)) == 1  # omit: "has a value"
    assert await Profile.count(Exists(Q.email)) == 2  # StoreNone: "the key is present", null included
    # comparing with None matches "no value" in both modes
    assert await Profile.count(Q.nick == None) == 1 and await Profile.count(Q.email == None) == 1  # noqa: E711


async def test_existing_data_loads_clean_in_both_modes(models):
    Profile, Legacy, Part = models
    # a null where the model omits, and a missing key where the model stores null
    result = await Profile.collection().insert_one({"name": "old", "nick": None})
    loaded = await Profile.get(result.inserted_id)
    assert loaded.nick is None and loaded.email is None and loaded.changes() == {}
    await loaded.save()  # nothing to send
    stored = await Profile.collection().find_one({"_id": result.inserted_id})
    assert stored == {"_id": result.inserted_id, "name": "old", "nick": None}  # untouched
    loaded.name = "renamed"
    assert loaded.changes() == {"$set": {"name": "renamed"}}  # and only real changes are sent


def test_sparse_unique_index_on_a_stored_null_is_refused(m):
    def build(config, annotation):
        if m.backend == "attrs":
            import attrs

            cls = attrs.make_class(
                "Clash", {"email": attrs.field(default=None, type=annotation)}, bases=(m.Spec,), kw_only=True
            )
        elif m.backend == "pydantic":
            import pydantic

            cls = pydantic.create_model("Clash", __base__=m.Spec, email=(annotation, None))
        else:
            import msgspec

            cls = msgspec.defstruct("Clash", [("email", annotation, None)], bases=(m.Spec,), kw_only=True)
        cls.__spec__ = config
        return cls

    sparse = [Index("email", unique=True, sparse=True)]
    with pytest.raises(ConfigurationError, match="sparse.*partial"):
        meta_of(build(SpecConfig(indexes=sparse), t.Annotated[str | None, StoreNone]))
    with pytest.raises(ConfigurationError, match="sparse.*partial"):
        meta_of(build(SpecConfig(indexes=sparse, none="store"), str | None))
    # fine: omitted while None, or a partial index instead of a sparse one
    meta_of(build(SpecConfig(indexes=sparse), str | None))
    meta_of(build(SpecConfig(indexes=sparse, none="store"), t.Annotated[str | None, OmitNone]))
    partial = [Index("email", unique=True, partial={"email": {"$type": "string"}})]
    meta_of(build(SpecConfig(indexes=partial), t.Annotated[str | None, StoreNone]))
