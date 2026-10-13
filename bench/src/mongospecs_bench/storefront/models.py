"""Storefront models, available on each backend (no ``from __future__ import annotations``:
the classes are built by a factory, so annotations must be real objects)."""

import datetime as dt
import functools
import types
import typing as t

from mongospecs import (
    OID,
    Backref,
    CreatedAt,
    DeletedAt,
    Immutable,
    Index,
    OnDelete,
    Policy,
    Ref,
    Revision,
    SpecConfig,
    UpdatedAt,
)

READ_ONLY = Policy(insert=False, update=False)


@functools.cache
def build(backend: str = "msgspec") -> types.SimpleNamespace:
    if backend == "msgspec":
        import msgspec

        from mongospecs.msgspec import Spec

        define: t.Any = lambda cls: cls  # noqa: E731

        class OrderLine(msgspec.Struct, kw_only=True):
            product: OID
            sku: str
            name: str
            qty: int
            unit_price: float

    elif backend == "pydantic":
        import pydantic

        from mongospecs.pydantic import Spec  # type: ignore[assignment]

        define = lambda cls: cls  # noqa: E731

        class OrderLine(pydantic.BaseModel):  # type: ignore[no-redef]
            product: OID
            sku: str
            name: str
            qty: int
            unit_price: float

    elif backend == "attrs":
        import attrs

        from mongospecs.attrs import Spec  # type: ignore[assignment]

        define = attrs.define(kw_only=True)

        @attrs.define(kw_only=True)
        class OrderLine:  # type: ignore[no-redef]
            product: OID
            sku: str
            name: str
            qty: int
            unit_price: float

    else:
        raise ValueError(f"unknown backend {backend!r}")

    @define
    class User(Spec):
        __spec__ = SpecConfig(collection="users", indexes=[Index("email", unique=True)])
        email: t.Annotated[str, Policy(update=False)]
        name: str = ""
        password_hash: t.Annotated[str, Policy(private=True)] = ""
        is_admin: t.Annotated[bool, READ_ONLY] = False
        created_at: t.Annotated[dt.datetime | None, CreatedAt] = None

    @define
    class Product(Spec):
        __spec__ = SpecConfig(
            collection="products",
            indexes=[
                Index("sku", unique=True),
                # Cursor pagination sorts by the requested key and then by id, so the id
                # belongs at the end of every index that serves a listing.
                Index("category", "price", "id"),
                Index("price", "id"),
                Index("name", "id"),
            ],
        )
        sku: t.Annotated[str, Immutable, Policy(filter=("eq", "in"))]
        name: t.Annotated[str, Policy(filter=("eq", "prefix"), sort=True)]
        category: t.Annotated[str, Policy(filter=("eq", "in"))] = "general"
        price: t.Annotated[float, Policy(filter=("gte", "lte", "gt", "lt"), sort=True)] = 0.0
        stock: t.Annotated[int, Policy(update=False, filter=("gte", "eq"))] = 0
        """Changed only by orders and restocks, never by a plain PATCH."""
        tags: list[str] = []
        description: str = ""
        reviews: t.Annotated[list[t.Any], Backref("product", model="Review")] = []
        review_count: t.Annotated[int, Backref("product", model="Review", count=True)] = 0
        created_at: t.Annotated[dt.datetime | None, CreatedAt, Policy(sort=True)] = None
        updated_at: t.Annotated[dt.datetime | None, UpdatedAt] = None
        deleted_at: t.Annotated[dt.datetime | None, DeletedAt, Policy(private=True)] = None
        rev: t.Annotated[int, Revision] = 0

    @define
    class Review(Spec):
        __spec__ = SpecConfig(
            collection="reviews", indexes=[Index("product", "-created_at"), Index("author")]
        )
        product: t.Annotated[Ref[Product], OnDelete("cascade"), Policy(update=False, filter=("eq",))]
        rating: t.Annotated[int, Policy(filter=("eq", "gte", "lte"), sort=True)]
        body: str = ""
        author: t.Annotated[Ref[User] | None, Policy(insert=False, update=False, filter=("eq",))] = None
        created_at: t.Annotated[dt.datetime | None, CreatedAt, Policy(sort=True)] = None

    @define
    class Order(Spec):
        __spec__ = SpecConfig(collection="orders", indexes=[Index("user", "-created_at"), Index("status")])
        user: t.Annotated[Ref[User], Policy(filter=("eq",))]
        lines: list[OrderLine]
        total: float
        status: t.Annotated[str, Policy(filter=("eq",))] = "placed"
        created_at: t.Annotated[dt.datetime | None, CreatedAt, Policy(sort=True)] = None
        updated_at: t.Annotated[dt.datetime | None, UpdatedAt] = None
        rev: t.Annotated[int, Revision] = 0

    @define
    class SalesStat(Spec):
        """Units and revenue per SKU, maintained by the change-stream projector."""

        __spec__ = SpecConfig(collection="sales_stats", indexes=[Index("sku", unique=True)])
        sku: t.Annotated[str, Policy(filter=("eq",))]
        units: t.Annotated[int, Policy(sort=True)] = 0
        revenue: float = 0.0

    @define
    class ProjectorState(Spec):
        __spec__ = SpecConfig(collection="projector_state", indexes=[Index("name", unique=True)])
        name: str
        resume_token: dict[str, t.Any] | None = None
        events: int = 0

    return types.SimpleNamespace(
        backend=backend,
        OrderLine=OrderLine,
        User=User,
        Product=Product,
        Review=Review,
        Order=Order,
        SalesStat=SalesStat,
        ProjectorState=ProjectorState,
        ALL=[User, Product, Review, Order, SalesStat, ProjectorState],
    )
