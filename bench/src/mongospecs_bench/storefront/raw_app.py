"""The same hot paths as the storefront, written by hand with pymongo and Starlette.

This is the baseline for the load benchmark: what the three busiest endpoints
cost when there is no ODM and no resource layer, only the driver and a
framework. It implements the same behaviour (soft-delete filter, cursor
pagination, reviews embedded on demand, a transaction that cannot oversell) so
the comparison is like for like.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import os
import typing as t

import bson
from bson import ObjectId
from pymongo import AsyncMongoClient, ReturnDocument
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mongospecs_bench.storefront.auth import Tokens
from mongospecs_web import HTTPError

PRODUCT_FIELDS = (
    "sku",
    "name",
    "category",
    "price",
    "stock",
    "tags",
    "description",
    "created_at",
    "updated_at",
    "rev",
)


def _json(value: t.Any) -> t.Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def _product(doc: dict[str, t.Any]) -> dict[str, t.Any]:
    return {"id": str(doc["_id"]), **{k: _json(doc.get(k)) for k in PRODUCT_FIELDS}}


def _problem(status: int, title: str, detail: str) -> Response:
    body = json.dumps({"type": "about:blank", "title": title, "status": status, "detail": detail})
    return Response(body, status, media_type="application/problem+json")


def create_app() -> Starlette:
    uri = os.environ.get("SHOP_MONGO_URI", "mongodb://localhost:27117/?replicaSet=rs0")
    database = os.environ.get("SHOP_DB", "storefront")
    tokens = Tokens(os.environ.get("SHOP_SECRET", "change-me"))
    state: dict[str, t.Any] = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> t.AsyncIterator[None]:
        client: AsyncMongoClient[dict[str, t.Any]] = AsyncMongoClient(
            uri, tz_aware=True, maxPoolSize=int(os.environ.get("SHOP_POOL", "100"))
        )
        state["client"] = client
        state["db"] = client[database]
        await client.admin.command("ping")
        yield
        await client.close()

    async def health(request: Request) -> Response:
        await state["client"].admin.command("ping")
        return JSONResponse({"status": "ok", "backend": "none", "plug": "raw"})

    async def list_products(request: Request) -> Response:
        params = request.query_params
        flt: dict[str, t.Any] = {"deleted_at": None}
        if "category" in params:
            flt["category"] = params["category"]
        sort_param = params.get("sort", "")
        direction = -1 if sort_param.startswith("-") else 1
        key = sort_param.lstrip("-") or "_id"
        if key not in ("price", "name", "created_at", "_id"):
            return _problem(400, "Bad Request", f"{key!r} cannot be used for sorting")
        try:
            limit = min(max(int(params.get("limit", 20)), 1), 100)
        except ValueError:
            return _problem(400, "Bad Request", "limit must be an integer")
        sort = [(key, direction)] + ([("_id", 1)] if key != "_id" else [])
        if "after" in params:
            try:
                padded = params["after"] + "=" * (-len(params["after"]) % 4)
                values = bson.decode(base64.urlsafe_b64decode(padded))["v"]
            except Exception:
                return _problem(400, "Bad Request", "invalid pagination cursor")
            op = "$gt" if direction == 1 else "$lt"
            if key == "_id":
                flt["_id"] = {"$gt": values[0]}
            else:
                flt["$or"] = [{key: {op: values[0]}}, {key: values[0], "_id": {"$gt": values[1]}}]
        docs = await state["db"].products.find(flt, sort=sort, limit=limit + 1).to_list()
        has_next = len(docs) > limit
        docs = docs[:limit]
        cursor = None
        if has_next and docs:
            last = docs[-1]
            values = [last["_id"]] if key == "_id" else [last[key], last["_id"]]
            cursor = base64.urlsafe_b64encode(bson.encode({"v": values})).decode().rstrip("=")
        return JSONResponse(
            {
                "data": [_product(d) for d in docs],
                "page": {"size": limit, "has_next": has_next, "next_cursor": cursor},
            }
        )

    async def get_product(request: Request) -> Response:
        try:
            oid = ObjectId(request.path_params["id"])
        except Exception:
            return _problem(404, "Not Found", "The resource does not exist.")
        doc = await state["db"].products.find_one({"_id": oid, "deleted_at": None})
        if doc is None:
            return _problem(404, "Not Found", "The resource does not exist.")
        out = _product(doc)
        include = request.query_params.get("include", "").split(",")
        if "reviews" in include:
            reviews = await state["db"].reviews.find({"product": oid}).to_list()
            out["reviews"] = [
                {
                    "id": str(r["_id"]),
                    "product": str(r["product"]),
                    "rating": r["rating"],
                    "body": r["body"],
                    "author": _json(r.get("author")),
                    "created_at": _json(r.get("created_at")),
                }
                for r in reviews
            ]
        if "review_count" in include:
            out["review_count"] = await state["db"].reviews.count_documents({"product": oid})
        return JSONResponse({"data": out}, headers={"etag": f'"{doc.get("rev", 0)}"'})

    async def place_order(request: Request) -> Response:
        header = request.headers.get("authorization", "")
        try:
            principal = tokens.verify(header.partition(" ")[2]) if header else None
        except HTTPError:
            principal = None
        if principal is None:
            return _problem(401, "Unauthorized", "Log in first.")
        try:
            body = json.loads(await request.body())
            wanted: dict[str, int] = {}
            for item in body["lines"]:
                wanted[item["sku"]] = wanted.get(item["sku"], 0) + int(item["qty"])
        except Exception:
            return _problem(422, "Unprocessable Content", "lines must be a list of {sku, qty}.")
        db = state["db"]

        class OutOfStock(Exception):
            pass

        async def work(session: t.Any) -> dict[str, t.Any]:
            now = dt.datetime.now(dt.UTC)
            now = now.replace(microsecond=now.microsecond // 1000 * 1000)
            lines, total = [], 0.0
            for sku in sorted(wanted):
                qty = wanted[sku]
                product = await db.products.find_one_and_update(
                    {"sku": sku, "stock": {"$gte": qty}, "deleted_at": None},
                    {"$inc": {"stock": -qty, "rev": 1}, "$set": {"updated_at": now}},
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                if product is None:
                    raise OutOfStock(sku)
                lines.append(
                    {
                        "product": product["_id"],
                        "sku": sku,
                        "name": product["name"],
                        "qty": qty,
                        "unit_price": product["price"],
                    }
                )
                total += qty * product["price"]
            order = {
                "user": ObjectId(principal.id),
                "lines": lines,
                "total": round(total, 2),
                "status": "placed",
                "created_at": now,
                "updated_at": now,
                "rev": 0,
            }
            await db.orders.insert_one(order, session=session)
            return order

        try:
            async with state["client"].start_session() as session:
                order = await session.with_transaction(work)
        except OutOfStock as exc:
            return _problem(409, "Conflict", f"{exc} is out of stock or does not exist.")
        out = {"id": str(order["_id"]), **{k: _json(v) for k, v in order.items() if k != "_id"}}
        return JSONResponse({"data": out}, status_code=201, headers={"etag": '"0"'})

    return Starlette(
        routes=[
            Route("/healthz", health),
            Route("/api/products", list_products),
            Route("/api/products/{id}", get_product),
            Route("/api/orders", place_order, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
