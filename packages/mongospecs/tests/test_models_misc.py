"""Discriminators, schema evolution, multi-database use, adapters, forward references."""

import typing as t

import pytest
from bson import ObjectId

from mongospecs import ConfigurationError, Q, Ref, SpecConfig, To, meta_of
from mongospecs.msgspec import Spec as MsgspecSpec


async def test_discriminators_share_a_collection(m, registry):
    await m.Animal(name="generic").insert()
    tom = await m.Cat(name="tom", lives=7).insert()
    await m.Dog(name="rex").insert()
    await m.Puppy(name="bit", age_weeks=6).insert()
    kinds = {d["name"]: d["kind"] async for d in m.Animal.collection().find({})}
    assert kinds == {"generic": "Animal", "tom": "Cat", "rex": "doggo", "bit": "Puppy"}
    assert meta_of(m.Cat).collection == meta_of(m.Puppy).collection == "animals"
    everything = await m.Animal.find().sort("name")
    assert [type(a).__name__ for a in everything] == ["Puppy", "Animal", "Dog", "Cat"]
    assert everything[3].lives == 7
    assert [c.name for c in await m.Cat.find()] == ["tom"]
    assert sorted(d.name for d in await m.Dog.find()) == ["bit", "rex"]  # includes subclasses
    assert await m.Puppy.count() == 1 and await m.Animal.count() == 4
    assert await m.Dog.get(tom.id) is None and type(await m.Animal.get(tom.id)) is m.Cat
    loaded = await m.Animal.get(tom.id)
    loaded.lives = 6
    await loaded.save()
    raw = await m.Cat.collection().find_one({"_id": tom.id})
    assert raw["lives"] == 6 and raw["kind"] == "Cat"
    assert await m.Dog.find().update(name="renamed") == 2
    assert (await m.Animal.get(tom.id)).name == "tom"
    assert await m.Cat.find().delete() == 1 and await m.Animal.count() == 3


async def test_unknown_discriminator_value_falls_back_to_the_queried_class(m, registry):
    await m.Animal.collection().insert_one({"name": "alien", "kind": "Martian"})
    (doc,) = await m.Animal.find()
    assert type(doc) is m.Animal and doc.name == "alien"


def test_discriminator_key_cannot_collide_with_a_field(m):
    @m.define
    class Clash(m.Spec):
        __spec__ = SpecConfig(discriminator="kind")
        kind: str = ""

    with pytest.raises(ConfigurationError, match="collides"):
        meta_of(Clash)


def test_config_inheritance_rules(m):
    @m.define
    class Base(m.Spec):
        __spec__ = SpecConfig(collection="bases", max_time_ms=500)

    @m.define
    class Child(Base):
        pass

    @m.define
    class Renamed(Base):
        __spec__ = SpecConfig(collection="renamed")

    assert meta_of(Base).collection == "bases"
    assert meta_of(Child).collection == "Child" and meta_of(Child).config.max_time_ms == 500
    assert meta_of(Renamed).collection == "renamed"
    assert meta_of(m.Video).collection == "videos" and meta_of(m.Puppy).disc_value == "Puppy"


async def test_upcast_migrates_old_documents_lazily(m, make_registry):
    def upcast(doc):
        if "full_name" in doc:  # renamed field
            doc["name"] = doc.pop("full_name")
        doc.setdefault("age", 18)  # field added later
        return doc

    @m.define
    class Person(m.Spec):
        __spec__ = SpecConfig(collection="people", upcast=upcast)
        name: str
        age: int

    await make_registry(Person)
    await Person.collection().insert_one({"full_name": "Old Shape"})
    person = await Person.find().one()
    assert person.name == "Old Shape" and person.age == 18
    person.age = 19
    await person.save()
    raw = await Person.collection().find_one({})
    assert raw["age"] == 19 and raw["full_name"] == "Old Shape"  # untouched until written explicitly


async def test_using_another_database(m, registry, db_name):
    other = db_name + "_b"
    try:
        tenant_b = m.User.using(db=other)
        await m.User(name="main").insert()
        b_user = await tenant_b.insert(m.User(name="b"))
        assert [u.name for u in await m.User.find()] == ["main"]
        assert [u.name for u in await tenant_b.find()] == ["b"]
        assert await tenant_b.count() == 1 and await m.User.count() == 1
        b_user.age = 4
        await b_user.save()  # saved back through the manager it was written with
        assert (await tenant_b.get(b_user.id)).age == 4
        assert await m.User.get(b_user.id) is None
        loaded = await tenant_b.find().one()
        await loaded.delete()
        assert await tenant_b.count() == 0 and await m.User.count() == 1
    finally:
        await registry.client.drop_database(other)


async def test_two_registries_are_isolated(m, make_registry, db_name):
    first = await make_registry(m.User)
    with pytest.raises(ConfigurationError, match="already bound"):
        await make_registry(m.User, database=db_name + "_2")
    await m.User(name="first").insert()
    await first.close()
    second = await make_registry(m.User, database=db_name + "_2")
    assert await m.User.count() == 0  # rebinding after close goes to the new registry
    assert second.models == (m.User,)


async def test_spec_adapter_documents_work_end_to_end(m, make_registry):
    Dragon = m.SpecAdapter(m.Plain, collection="dragons")
    await make_registry(Dragon)
    burt = await Dragon(name="Burt").insert()
    burt.legs = 2
    await burt.save()
    loaded = await Dragon.get(burt.id)
    assert loaded.legs == 2 and isinstance(loaded, m.Plain)
    assert await Dragon.count(Q.name == "Burt") == 1


# -- module-level forward references (real annotations resolved lazily) ---------------


class Chapter(MsgspecSpec):
    __spec__ = SpecConfig(collection="fwd_chapters")
    title: str = ""
    book: t.Optional[Ref["Book"]] = None  # noqa: UP045
    previous: t.Annotated[t.Optional[Ref], To("Chapter")] = None  # noqa: UP045


class Book(MsgspecSpec):
    __spec__ = SpecConfig(collection="fwd_books")
    name: str = ""


async def test_forward_references_resolve(make_registry):
    await make_registry(Chapter, Book)
    assert meta_of(Chapter).ref_targets("book")[0].model is Book
    assert meta_of(Chapter).ref_targets("previous")[0].model is Chapter
    book = await Book(name="b").insert()
    one = await Chapter(title="1", book=book).insert()
    await Chapter(title="2", book=book, previous=one).insert()
    second = await Chapter.find(Q.title == "2").include("book").include("previous").one()
    assert second.book.doc.name == "b" and second.previous.doc.title == "1"
    assert (await second.book.fetch()).id == book.id


def test_unresolvable_names_are_reported():
    class Orphan(MsgspecSpec):
        other: t.Annotated[t.Optional[Ref], To("DoesNotExist")] = None  # noqa: UP045

    with pytest.raises(ConfigurationError, match="DoesNotExist"):
        meta_of(Orphan).ref_targets("other")
    assert ObjectId.is_valid(str(ObjectId()))
