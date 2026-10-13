"""Codec contract: every backend must behave the same through the Codec interface."""

import datetime as dt

import bson
import pytest
from bson import ObjectId

from mongospecs import ConfigurationError, Policy, Ref, ValidationError, meta_of
from mongospecs.codec import codec_for


def test_fields_expose_names_markers_and_policy(m):
    fields = meta_of(m.User).fields
    assert list(fields)[:2] == ["id", "name"] or "id" in fields
    assert fields["id"].stored == "_id"
    assert fields["name"].policy == Policy(filter=("eq", "prefix"), sort=True)
    assert fields["secret"].policy.private and not fields["secret"].required
    assert fields["age"].policy == Policy()
    art = meta_of(m.Article).fields
    assert art["author"].ref.shape == "one" and art["author"].ref.targets == (m.User,)
    assert art["editors"].ref.shape == "list" and art["reviewers"].ref.shape == "dict"
    assert meta_of(m.Bookmark).fields["target"].ref.targets == (m.Article, m.Video)
    post = meta_of(m.Post).fields
    assert post["summary"].stored == "s"
    assert post["comments"].transient and post["comment_count"].backref.count


def test_round_trip_through_bson_ready_dict(m):
    oid, uid = ObjectId(), ObjectId()
    when = dt.datetime(2024, 5, 6, 7, 8, 9, tzinfo=dt.UTC)
    meta = meta_of(m.Article)
    doc = meta.decode(
        {
            "_id": oid,
            "title": "t",
            "tags": ["a"],
            "author": uid,
            "editors": [uid],
            "reviewers": {"lead": uid},
            "created_at": when,
            "unknown_field": 1,
        }
    )
    assert type(doc) is m.Article and doc.id == oid and doc.created_at == when
    assert isinstance(doc.author, Ref) and doc.author.id == uid and not doc.author.loaded
    assert doc.editors[0].id == uid and doc.reviewers["lead"].id == uid
    encoded = meta.encode(doc)
    assert encoded["_id"] == oid and "id" not in encoded
    assert encoded["author"] == uid and encoded["editors"] == [uid] and encoded["reviewers"] == {"lead": uid}
    assert "unknown_field" not in encoded
    bson.encode(encoded)  # BSON-ready
    assert encoded["tags"] is not doc.tags  # no shared containers


def test_decode_accepts_raw_bson_and_string_ids(m):
    oid = ObjectId()
    meta = meta_of(m.User)
    assert meta.codec.decode(m.User, bson.encode({"_id": oid, "name": "x"})).id == oid
    assert meta.decode({"_id": str(oid), "name": "x"}).id == oid


def test_documents_and_ids_assigned_to_refs_encode_as_ids(m):
    user = m.User(name="u")
    user.id = ObjectId()
    art = m.Article(title="t", author=user, editors=[user, user.id], reviewers={"a": user})
    encoded = meta_of(m.Article).encode(art)
    assert encoded["author"] == user.id
    assert encoded["editors"] == [user.id, user.id]
    assert encoded["reviewers"] == {"a": user.id}
    assert art.author.doc is user  # normalised to a loaded Ref
    art.author = user.id  # plain assignment later is still encoded as an id
    assert meta_of(m.Article).encode(art)["author"] == user.id


def test_alias_and_transient_fields(m):
    meta = meta_of(m.Post)
    post = m.Post(title="t", summary="short")
    encoded = meta.encode(post)
    assert encoded["s"] == "short" and "summary" not in encoded
    assert "comments" not in encoded and "comment_count" not in encoded
    assert meta.decode({"_id": ObjectId(), "title": "t", "s": "stored"}).summary == "stored"


def test_nested_models_and_enums(m):
    meta = meta_of(m.User)
    user = m.User(name="a", address=m.Address(city="Oslo"), color=m.Color.RED)
    encoded = meta.encode(user)
    assert encoded["address"] == {"city": "Oslo", "zip": ""}
    back = meta.decode({**encoded, "_id": ObjectId(), "color": "red"})
    assert type(back.address) is m.Address and back.color is m.Color.RED


def test_validation_errors_name_the_field(m):
    meta = meta_of(m.User)
    with pytest.raises(ValidationError) as info:
        meta.decode({"_id": ObjectId(), "name": "x", "age": "old"})
    assert info.value.errors[0]["field"] == "age"
    with pytest.raises(ValidationError) as info:
        meta.codec.validate_field(m.User, "age", "old")
    assert info.value.errors[0]["field"] == "age"


def test_validate_field_coerces_one_value(m):
    codec = meta_of(m.User).codec
    assert codec.validate_field(m.User, "age", 5) == 5
    assert codec.validate_field(m.User, "color", "blue") is m.Color.BLUE
    oid = ObjectId()
    assert codec.validate_field(m.User, "id", str(oid)) == oid
    assert type(codec.validate_field(m.User, "address", {"city": "X"})) is m.Address


def test_json_schema_is_normalised(m):
    schema = meta_of(m.User).codec.json_schema(m.User)
    assert schema["type"] == "object"
    assert {"id", "name", "age", "email", "secret", "address", "color"} <= set(schema["properties"])
    assert "_id" not in schema["properties"]
    assert schema["required"] == []
    art = meta_of(m.Article).codec.json_schema(m.Article)
    assert "title" in art["properties"] and "author" in art["properties"]


def test_codec_for_plain_models_and_rejects_others(m):
    assert codec_for(m.Plain).name == m.backend
    assert codec_for(m.User) is meta_of(m.User).codec
    with pytest.raises(ConfigurationError):
        codec_for(dict)
    with pytest.raises(ConfigurationError):
        meta_of(m.Plain)


def test_spec_adapter_turns_existing_class_into_document(m):
    Dragon = m.SpecAdapter(m.Plain, collection="dragons")
    meta = meta_of(Dragon)
    assert meta.collection == "dragons" and "id" in meta.fields and "legs" in meta.fields
    d = Dragon(name="Burt")
    assert d.legs == 4 and d.id is None and d.is_new
    assert meta.encode(d)["name"] == "Burt"
    assert Dragon.__name__ == "Plain"


def test_misdeclared_models_fail_at_metadata_time(m):
    import typing as t

    from mongospecs import Deferred, DeletedAt, Revision

    @m.define
    class RequiredDeferred(m.Spec):
        secret: t.Annotated[str, Deferred]

    with pytest.raises(ConfigurationError, match="Deferred"):
        meta_of(RequiredDeferred)

    @m.define
    class TwoRevisions(m.Spec):
        a: t.Annotated[int, Revision] = 0
        b: t.Annotated[int, Revision] = 0

    with pytest.raises(ConfigurationError, match="Revision"):
        meta_of(TwoRevisions)

    @m.define
    class RequiredDeleted(m.Spec):
        gone: t.Annotated[dt.datetime, DeletedAt]

    with pytest.raises(ConfigurationError, match="DeletedAt"):
        meta_of(RequiredDeleted)

    @m.define
    class BareRef(m.Spec):
        other: Ref | None = None

    with pytest.raises(ConfigurationError, match="target"):
        meta_of(BareRef)


def test_validate_field_runs_the_models_own_validators(m):
    """Spike S4: a single field can be validated with each backend's native rules."""
    import typing as t

    if m.backend == "msgspec":
        import msgspec

        class Strict(m.Spec):
            title: t.Annotated[str, msgspec.Meta(min_length=3)] = "abc"

    elif m.backend == "pydantic":
        import pydantic

        class Strict(m.Spec):
            title: str = "abc"

            @pydantic.field_validator("title")
            @classmethod
            def long_enough(cls, v):
                if len(v.strip()) < 3:
                    raise ValueError("too short")
                return v.strip()

    else:
        import attrs

        def long_enough(instance, attribute, value):
            if len(value) < 3:
                raise ValueError("too short")

        @attrs.define(kw_only=True)
        class Strict(m.Spec):
            title: str = attrs.field(default="abc", validator=long_enough)

    codec = meta_of(Strict).codec
    assert codec.validate_field(Strict, "title", "long enough") == "long enough"
    with pytest.raises(ValidationError) as info:
        codec.validate_field(Strict, "title", "no")
    assert info.value.errors[0]["field"] == "title"
    # the same rule guards atomic updates and query casting
    from mongospecs import Q, QueryError
    from mongospecs.expr import compile_filter
    from mongospecs.updates import compile_update

    with pytest.raises(ValidationError):
        compile_update(meta_of(Strict), [], {"title": "no"})
    # ...but a query may look for a value that would not be valid to store: casting is by type only
    assert compile_filter(Q.title == "no", meta_of(Strict)) == {"title": "no"}
    with pytest.raises(QueryError):
        compile_filter(Q.title == 5, meta_of(Strict))
