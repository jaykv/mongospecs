"""Benchmark models, declared once per backend at module level.

No ``from __future__ import annotations``: the classes are built by a factory.
"""

import datetime as dt
import types
import typing as t

import attrs
import msgspec
import pydantic

from mongospecs import CreatedAt, Index, Ref, Revision, SpecConfig, UpdatedAt
from mongospecs import attrs as at
from mongospecs import msgspec as ms
from mongospecs import pydantic as pd

BACKENDS = ("msgspec", "pydantic", "attrs")
SHAPES = ("small", "wide", "nested")
WIDE_FIELDS = 40


def _build(backend: str) -> types.SimpleNamespace:
    if backend == "msgspec":
        Spec: t.Any = ms.Spec
        define: t.Any = lambda cls: cls  # noqa: E731

        class Address(msgspec.Struct, kw_only=True):
            street: str = ""
            city: str = ""
            zip: str = ""

        class Line(msgspec.Struct, kw_only=True):
            sku: str = ""
            qty: int = 0
            price: float = 0.0

    elif backend == "pydantic":
        Spec = pd.Spec
        define = lambda cls: cls  # noqa: E731

        class Address(pydantic.BaseModel):  # type: ignore[no-redef]
            street: str = ""
            city: str = ""
            zip: str = ""

        class Line(pydantic.BaseModel):  # type: ignore[no-redef]
            sku: str = ""
            qty: int = 0
            price: float = 0.0

    else:
        Spec = at.Spec
        define = attrs.define(kw_only=True)

        @attrs.define(kw_only=True)
        class Address:  # type: ignore[no-redef]
            street: str = ""
            city: str = ""
            zip: str = ""

        @attrs.define(kw_only=True)
        class Line:  # type: ignore[no-redef]
            sku: str = ""
            qty: int = 0
            price: float = 0.0

    @define
    class Small(Spec):  # type: ignore[misc]
        __spec__ = SpecConfig(collection="bench_small", indexes=[Index("n")])
        n: int = 0
        name: str = ""
        score: float = 0.0
        active: bool = True
        created: dt.datetime | None = None

    # 40 scalar fields, built so the three backends get identical definitions.
    wide_annotations: dict[str, t.Any] = {}
    wide_defaults: dict[str, t.Any] = {}
    for i in range(WIDE_FIELDS):
        kind = i % 4
        wide_annotations[f"f{i:02d}"] = (int, str, float, bool)[kind]
        wide_defaults[f"f{i:02d}"] = (0, "", 0.0, False)[kind]
    wide_ns = {
        "__annotations__": {"n": int, **wide_annotations},
        "__spec__": SpecConfig(collection="bench_wide", indexes=[Index("n")]),
        "n": 0,
        **wide_defaults,
    }
    Wide = define(types.new_class("Wide", (Spec,), {}, lambda ns: ns.update(wide_ns)))

    @define
    class Nested(Spec):  # type: ignore[misc]
        __spec__ = SpecConfig(collection="bench_nested", indexes=[Index("n")])
        n: int = 0
        customer: str = ""
        address: Address | None = None
        lines: list[Line] = []
        tags: list[str] = []
        meta: dict[str, str] = {}
        placed: dt.datetime | None = None

    @define
    class Child(Spec):  # type: ignore[misc]
        __spec__ = SpecConfig(collection="bench_children")
        n: int = 0
        label: str = ""
        payload: str = ""

    @define
    class Parent(Spec):  # type: ignore[misc]
        __spec__ = SpecConfig(collection="bench_parents", indexes=[Index("n")])
        n: int = 0
        name: str = ""
        owner: Ref[Child] | None = None
        children: list[Ref[Child]] = []

    @define
    class Managed(Spec):  # type: ignore[misc]
        """A model with the three built-in plugins active."""

        __spec__ = SpecConfig(collection="bench_managed")
        n: int = 0
        name: str = ""
        created_at: t.Annotated[dt.datetime | None, CreatedAt] = None
        updated_at: t.Annotated[dt.datetime | None, UpdatedAt] = None
        rev: t.Annotated[int, Revision] = 0

    @define
    class Plain(Spec):  # type: ignore[misc]
        __spec__ = SpecConfig(collection="bench_plain")
        n: int = 0
        name: str = ""

    return types.SimpleNamespace(
        backend=backend,
        Small=Small,
        Wide=Wide,
        Nested=Nested,
        Child=Child,
        Parent=Parent,
        Managed=Managed,
        Plain=Plain,
        ALL=[Small, Wide, Nested, Child, Parent, Managed, Plain],
    )


MODELS: dict[str, types.SimpleNamespace] = {backend: _build(backend) for backend in BACKENDS}


def raw_document(shape: str, n: int) -> dict[str, t.Any]:
    """A raw MongoDB document of the given shape (without ``_id``)."""
    when = dt.datetime(2024, 1, 1, tzinfo=dt.UTC) + dt.timedelta(minutes=n)
    if shape == "small":
        return {"n": n, "name": f"name-{n}", "score": n * 1.5, "active": n % 2 == 0, "created": when}
    if shape == "wide":
        doc: dict[str, t.Any] = {"n": n}
        for i in range(WIDE_FIELDS):
            kind = i % 4
            doc[f"f{i:02d}"] = (n + i, f"value-{n}-{i}", (n + i) * 0.5, (n + i) % 2 == 0)[kind]
        return doc
    if shape == "nested":
        return {
            "n": n,
            "customer": f"customer-{n}",
            "address": {"street": f"{n} Main St", "city": "Springfield", "zip": f"{n % 100000:05d}"},
            "lines": [{"sku": f"sku-{n}-{i}", "qty": i + 1, "price": 9.99 + i} for i in range(8)],
            "tags": [f"tag-{(n + i) % 7}" for i in range(5)],
            "meta": {"source": "bench", "batch": str(n // 100)},
            "placed": when,
        }
    raise ValueError(shape)


def model_for(backend: str, shape: str) -> type:
    return t.cast(type, getattr(MODELS[backend], shape.capitalize()))
