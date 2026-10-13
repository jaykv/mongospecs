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
