"""Seed data for the storefront, written with the raw driver for speed."""

from __future__ import annotations

import datetime as dt
import random
import typing as t
from dataclasses import dataclass

from bson import ObjectId

from mongospecs import Registry
from mongospecs_bench.storefront.auth import Tokens

CATEGORIES = ("books", "games", "garden", "kitchen", "music", "office", "outdoor", "toys")


@dataclass
class Seeded:
    product_ids: list[str]
    skus: list[str]
    user_tokens: list[str]
    admin_token: str
    initial_stock: int
    products: int


async def seed(
    registry: Registry,
    m: t.Any,
    tokens: Tokens,
    *,
    products: int = 2000,
    users: int = 50,
    reviews_per_product: int = 3,
    stock: int = 1_000_000,
    seed_value: int = 7,
) -> Seeded:
    rng = random.Random(seed_value)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    user_docs = [
        {
            "_id": ObjectId(),
            "email": f"user{i}@example.com",
            "name": f"User {i}",
            "password_hash": "",
            "is_admin": False,
            "created_at": now,
        }
        for i in range(users)
    ]
    admin = {
        "_id": ObjectId(),
        "email": "admin@example.com",
        "name": "Admin",
        "password_hash": "",
        "is_admin": True,
        "created_at": now,
    }
    await registry.manager(m.User).collection.insert_many([*user_docs, admin])

    product_docs = []
    for i in range(products):
        product_docs.append(
            {
                "_id": ObjectId(),
                "sku": f"SKU-{i:06d}",
                "name": f"{rng.choice(('Classic', 'Deluxe', 'Compact', 'Pro', 'Eco'))} item {i}",
                "category": CATEGORIES[i % len(CATEGORIES)],
                "price": round(rng.uniform(1, 500), 2),
                "stock": stock,
                "tags": rng.sample(("new", "sale", "gift", "limited", "popular", "eco"), k=2),
                "description": "Lorem ipsum dolor sit amet. " * 6,
                "created_at": now - dt.timedelta(minutes=i),
                "updated_at": now - dt.timedelta(minutes=i),
                "deleted_at": None,
                "rev": 0,
            }
        )
    collection = registry.manager(m.Product).collection
    for start in range(0, len(product_docs), 5000):
        await collection.insert_many(product_docs[start : start + 5000])

    review_docs = [
        {
            "product": product["_id"],
            "rating": rng.randint(1, 5),
            "body": "Works as described. " * rng.randint(1, 4),
            "author": rng.choice(user_docs)["_id"],
            "created_at": now - dt.timedelta(minutes=rng.randint(0, 100_000)),
        }
        for product in product_docs
        for _ in range(reviews_per_product)
    ]
    reviews = registry.manager(m.Review).collection
    for start in range(0, len(review_docs), 5000):
        await reviews.insert_many(review_docs[start : start + 5000])

    return Seeded(
        product_ids=[str(p["_id"]) for p in product_docs],
        skus=[str(p["sku"]) for p in product_docs],
        user_tokens=[tokens.issue(u["_id"]) for u in user_docs],
        admin_token=tokens.issue(admin["_id"], is_admin=True),
        initial_stock=stock,
        products=products,
    )


async def stock_invariant(registry: Registry, m: t.Any, seeded: Seeded) -> dict[str, t.Any]:
    """Every unit is either still in stock or on a placed order: nothing lost, nothing oversold."""
    products = registry.manager(m.Product).collection
    orders = registry.manager(m.Order).collection
    stock_now = {d["sku"]: d["stock"] async for d in products.find({}, {"sku": 1, "stock": 1})}
    sold: dict[str, int] = {}
    cursor = await orders.aggregate(
        [
            {"$match": {"status": "placed"}},
            {"$unwind": "$lines"},
            {"$group": {"_id": "$lines.sku", "units": {"$sum": "$lines.qty"}}},
        ]
    )
    async for row in cursor:
        sold[row["_id"]] = row["units"]
    negative = [sku for sku, value in stock_now.items() if value < 0]
    mismatched = [sku for sku, value in stock_now.items() if value + sold.get(sku, 0) != seeded.initial_stock]
    return {
        "orders_placed": await orders.count_documents({"status": "placed"}),
        "units_sold": sum(sold.values()),
        "negative_stock": len(negative),
        "stock_mismatches": len(mismatched),
        "ok": not negative and not mismatched,
    }
