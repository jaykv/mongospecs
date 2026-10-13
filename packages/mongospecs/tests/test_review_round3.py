"""Regression tests for the third review of v2: one test per reproduced defect."""

import gc
import warnings

import pytest

from mongospecs import SpecConfig, ValidationError


def span_model():
    import pydantic

    from mongospecs.pydantic import Spec

    class Span(Spec):
        __spec__ = SpecConfig(collection="r3_spans")
        low: int = 0
        high: int = 10
        label: str = ""

        @pydantic.field_validator("label")
        @classmethod
        def tidy(cls, value):
            return value.strip()

        @pydantic.model_validator(mode="after")
        def ordered(self):
            if self.low > self.high:
                raise ValueError("low must not exceed high")
            return self

    return Span


async def test_1_updating_one_field_is_not_judged_against_the_other_fields_defaults(make_registry):
    Span = span_model()
    await make_registry(Span)
    doc = await Span(low=50, high=60).insert()
    # low=55 is fine next to the stored high=60; the model validator only knows the default high=10
    updated = await Span.update(doc.id, low=55)
    assert (updated.low, updated.high) == (55, 60)
    # the field's own rules still apply: its type, and its field validator
    with pytest.raises(ValidationError):
        await Span.update(doc.id, low="many")
    assert (await Span.update(doc.id, label="  x  ")).label == "x"
    # a whole document is still validated whole
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Span(low=5, high=1)


def hooked(backend):
    if backend == "msgspec":
        from mongospecs.msgspec import Spec

        class Note(Spec, kw_only=True):
            __spec__ = SpecConfig(collection="r3_notes")
            text: str = ""
            slug: str = ""

    elif backend == "pydantic":
        from mongospecs.pydantic import Spec

        class Note(Spec):
            __spec__ = SpecConfig(collection="r3_notes")
            text: str = ""
            slug: str = ""

    else:
        import attrs

        from mongospecs.attrs import Spec

        @attrs.define(kw_only=True)
        class Note(Spec):
            __spec__ = SpecConfig(collection="r3_notes")
            text: str = ""
            slug: str = ""

    seen = []

    @Note.before("insert")
    async def slugify(ctx, note):
        note.slug = note.text.lower().replace(" ", "-")

    @Note.before("save")
    async def before_save(ctx, note):
        seen.append(("before", note.text))

    @Note.after("save")
    async def after_save(ctx, note):
        seen.append(("after", note.text, note.id is not None))

    return Note, seen


async def test_7_document_hooks_run_for_insert_many_and_bulk_save(backend, make_registry):
    Note, seen = hooked(backend)
    await make_registry(Note)
    result = await Note.insert_many([Note(text="Hello World"), Note(text="Two")])
    assert [n.slug for n in result] == ["hello-world", "two"]
    assert [n["slug"] async for n in Note.find().raw().sort("slug")] == ["hello-world", "two"]
    assert seen == [
        ("before", "Hello World"),
        ("before", "Two"),
        ("after", "Hello World", True),
        ("after", "Two", True),
    ]
    seen.clear()
    first = result.inserted[0]
    first.text = "Changed"
    assert await Note.bulk_save([first, Note(text="New One")]) == 2
    assert await Note.count({"slug": "new-one"}) == 1
    assert seen == [
        ("before", "Changed"),
        ("before", "New One"),
        ("after", "Changed", True),
        ("after", "New One", True),
    ]
    assert (await Note.get(first.id)).slug == "hello-world"  # "insert" hooks are for new documents only


async def test_8_a_bad_cursor_leaves_no_coroutine_behind(make_registry):
    from mongospecs import QueryError

    Note, _ = hooked("msgspec")
    await make_registry(Note)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(QueryError):
            await Note.find().page(5, after="not-a-cursor", total=True)
        with pytest.raises(QueryError):
            await Note.find().page(5, number=0, total=True)
        gc.collect()
    assert not [w for w in caught if "never awaited" in str(w.message)]


# -- writes are validated ---------------------------------------------------------------------


def counter(backend, **config):
    spec = SpecConfig(collection="r3_counters", **config)
    if backend == "msgspec":
        import msgspec

        from mongospecs.msgspec import Spec

        class Part(msgspec.Struct, kw_only=True):
            size: int = 0

        class Counter(Spec, kw_only=True):
            __spec__ = spec
            name: str = ""
            n: int = 0
            part: Part | None = None
            parts: list[Part] = []

    elif backend == "pydantic":
        import pydantic

        from mongospecs.pydantic import Spec

        class Part(pydantic.BaseModel):
            size: int = 0

        class Counter(Spec):
            __spec__ = spec
            name: str = ""
            n: int = 0
            part: Part | None = None
            parts: list[Part] = []

    else:
        import attrs

        from mongospecs.attrs import Spec

        @attrs.define(kw_only=True)
        class Part:
            size: int = 0

        @attrs.define(kw_only=True)
        class Counter(Spec):
            __spec__ = spec
            name: str = ""
            n: int = 0
            part: Part | None = None
            parts: list[Part] = attrs.field(factory=list)

    return Counter, Part


def wrong(Counter, Part, **fields):
    """A document holding a wrongly typed value, however the backend lets one come about."""
    doc = Counter(name="c")
    for name, value in fields.items():
        setattr(doc, name, value)
    return doc


async def test_10_a_wrongly_typed_value_is_refused_before_it_is_written(backend, make_registry):
    Counter, Part = counter(backend)
    await make_registry(Counter)
    bad_part = Part()
    bad_part.size = "big"
    for fields in ({"n": "many"}, {"part": bad_part}, {"parts": [bad_part]}, {"name": None}):
        with pytest.raises(ValidationError):
            await wrong(Counter, Part, **fields).insert()
        with pytest.raises(ValidationError):
            await wrong(Counter, Part, **fields).save()
        with pytest.raises(ValidationError):
            await Counter.insert_many([wrong(Counter, Part, **fields)])
        with pytest.raises(ValidationError):
            await Counter.bulk_save([wrong(Counter, Part, **fields)])
    assert await Counter.count() == 0

    doc = await Counter(name="ok", n=1, part=Part(size=2), parts=[Part(size=3)]).insert()
    doc.n = "many"
    with pytest.raises(ValidationError) as caught:
        await doc.save()
    assert caught.value.errors[0]["field"] == "n"
    with pytest.raises(ValidationError):
        await Counter.bulk_save([doc])
    assert (await Counter.get(doc.id)).n == 1  # nothing was sent
    doc.n = 2
    assert (await doc.save()).n == 2  # and the document is still usable once it is put right

    # a document loaded with a projection is judged by what was changed, not as a whole
    partial = await Counter.find().select("n").one()
    partial.n = "many"
    with pytest.raises(ValidationError):
        await partial.save()
    partial.n = 5
    await partial.save()
    assert [(c.name, c.n) for c in await Counter.find()] == [("ok", 5)]


async def test_10_validation_of_writes_can_be_turned_off(backend, make_registry):
    Counter, Part = counter(backend)
    await make_registry(Counter)
    doc = await wrong(Counter, Part, n="many").insert(validate=False)
    assert (await Counter.collection().find_one({"_id": doc.id}))["n"] == "many"
    doc.name = 7
    await doc.save(validate=False)
    await Counter.insert_many([wrong(Counter, Part, n="x")], validate=False)
    await Counter.bulk_save([wrong(Counter, Part, n="y")], validate=False)
    assert await Counter.find().raw().count() == 3


async def test_10_or_for_a_whole_model(backend, make_registry, db_name):
    Counter, Part = counter(backend, validate_writes=False)
    await make_registry(Counter)
    doc = await wrong(Counter, Part, n="many").insert()
    assert (await Counter.collection().find_one({"_id": doc.id}))["n"] == "many"
    with pytest.raises(ValidationError):
        await wrong(Counter, Part, n="many").insert(validate=True)  # a call can still ask for it
