"""End-to-end tests: a real server process, real HTTP, real MongoDB.

Every test boots ``uvicorn`` with the storefront application against a freshly
seeded database, talks to it over TCP, and checks the database directly for the
invariants that matter in production (no oversold stock, no partial orders,
nothing private in a response, a clean shutdown).
"""

import asyncio

import httpx
import pytest

from mongospecs_bench.storefront.app import PLUGS
from mongospecs_bench.storefront.seed import stock_invariant


def client(shop, **kwargs):
    return httpx.AsyncClient(base_url=shop.url, timeout=30, **kwargs)


async def eventually(check, *, seconds=20.0, every=0.1):
    """Poll ``check`` (async, returns a truthy value when satisfied)."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        value = await check()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(every)


@pytest.mark.parametrize("plug", PLUGS)
async def test_customer_journey_on_every_plug(make_shop, plug):
    shop = await make_shop(plug=plug)
    async with client(shop) as http:
        health = (await http.get("/healthz")).json()
        assert health == {"status": "ok", "backend": "msgspec", "plug": plug}

        # -- sign up and log in ---------------------------------------------------------
        signup = await http.post(
            "/api/users/signup", json={"email": "New@Example.com", "password": "correct horse", "name": "New"}
        )
        assert signup.status_code == 201, signup.text
        me = signup.json()["data"]
        assert me["user"]["email"] == "new@example.com" and "password_hash" not in me["user"]
        assert (
            await http.post(
                "/api/users/signup", json={"email": "new@example.com", "password": "correct horse"}
            )
        ).status_code == 409
        assert (
            await http.post("/api/users/signup", json={"email": "bad", "password": "x"})
        ).status_code == 422
        assert (
            await http.post("/api/users/login", json={"email": "new@example.com", "password": "wrong"})
        ).status_code == 401
        login = await http.post(
            "/api/users/login", json={"email": "new@example.com", "password": "correct horse"}
        )
        token = {"authorization": f"Bearer {login.json()['data']['token']}"}
        assert (await http.get(f"/api/users/{me['user']['id']}", headers=token)).json()["data"][
            "name"
        ] == "New"
        assert (await http.get(f"/api/users/{me['user']['id']}", headers=shop.user(0))).status_code == 404
        assert (await http.get(f"/api/users/{me['user']['id']}")).status_code == 401

        # -- browse ------------------------------------------------------------------------
        page = (await http.get("/api/products?category=books&sort=price&limit=3")).json()
        prices = [p["price"] for p in page["data"]]
        assert len(prices) == 3 and prices == sorted(prices) and page["page"]["has_next"]
        assert all(p["category"] == "books" and "deleted_at" not in p for p in page["data"])
        following = (
            await http.get(
                f"/api/products?category=books&sort=price&limit=3&after={page['page']['next_cursor']}"
            )
        ).json()
        assert following["data"][0]["price"] >= prices[-1]
        assert {p["id"] for p in following["data"]}.isdisjoint(p["id"] for p in page["data"])
        assert (await http.get("/api/products?stock[$gt]=1")).status_code == 400
        assert (await http.get("/api/products?description=x")).status_code == 400
        product = page["data"][0]
        detail = (await http.get(f"/api/products/{product['id']}?include=reviews,review_count")).json()[
            "data"
        ]
        assert (
            detail["review_count"] == 2
            and len(detail["reviews"]) == 2
            and detail["reviews"][0]["rating"] in range(1, 6)
        )

        # -- order: stock moves, the order is mine alone -------------------------------------
        other = page["data"][1]
        placed = await http.post(
            "/api/orders",
            json={"lines": [{"sku": product["sku"], "qty": 2}, {"sku": other["sku"], "qty": 1}]},
            headers=token,
        )
        assert placed.status_code == 201, placed.text
        order = placed.json()["data"]
        assert order["status"] == "placed" and order["user"] == me["user"]["id"]
        assert order["total"] == round(2 * product["price"] + other["price"], 2)
        assert sorted(line["sku"] for line in order["lines"]) == sorted([product["sku"], other["sku"]])
        assert (await http.get(f"/api/products/{product['id']}")).json()["data"]["stock"] == 98
        assert (await http.post("/api/orders", json={"lines": []}, headers=token)).status_code == 422
        assert (
            await http.post("/api/orders", json={"lines": [{"sku": product["sku"], "qty": 1}]})
        ).status_code == 401
        mine = (await http.get("/api/orders", headers=token)).json()["data"]
        assert [o["id"] for o in mine] == [order["id"]]
        assert (await http.get("/api/orders", headers=shop.user(0))).json()["data"] == []
        assert (await http.get(f"/api/orders/{order['id']}", headers=shop.user(0))).status_code == 404
        assert (await http.get("/api/orders")).status_code == 401
        assert len((await http.get("/api/orders", headers=shop.admin)).json()["data"]) == 1

        # -- cancel: stock comes back, once ----------------------------------------------------
        assert (await http.post(f"/api/orders/{order['id']}/cancel", headers=shop.user(0))).status_code == 409
        cancelled = await http.post(f"/api/orders/{order['id']}/cancel", headers=token)
        assert cancelled.status_code == 200 and cancelled.json()["data"]["status"] == "cancelled"
        assert (await http.post(f"/api/orders/{order['id']}/cancel", headers=token)).status_code == 409
        assert (await http.get(f"/api/products/{product['id']}")).json()["data"]["stock"] == 100

        # -- reviews: the server decides who the author is -----------------------------------------
        forged = await http.post(
            "/api/reviews",
            json={"product": product["id"], "rating": 5, "author": shop.seeded.product_ids[0]},
            headers=token,
        )
        assert forged.status_code == 422
        assert (
            await http.post("/api/reviews", json={"product": product["id"], "rating": 9}, headers=token)
        ).status_code == 422
        assert (
            await http.post("/api/reviews", json={"product": product["id"], "rating": 5})
        ).status_code == 401
        review = await http.post(
            "/api/reviews", json={"product": product["id"], "rating": 5, "body": "great"}, headers=token
        )
        assert review.status_code == 201 and review.json()["data"]["author"] == me["user"]["id"]
        assert (
            await http.delete(f"/api/reviews/{review.json()['data']['id']}", headers=shop.user(1))
        ).status_code == 403
        listed = (await http.get(f"/api/reviews?product={product['id']}&sort=-created_at")).json()["data"]
        assert len(listed) == 3 and listed[0]["body"] == "great"

        # -- admin: conditional update, protected fields, soft delete -------------------------------
        assert (
            await http.patch(f"/api/products/{product['id']}", json={"price": 1}, headers=token)
        ).status_code == 403
        current = await http.get(f"/api/products/{product['id']}")
        etag = current.headers["etag"]
        changed = await http.patch(
            f"/api/products/{product['id']}", json={"price": 9.5}, headers={**shop.admin, "if-match": etag}
        )
        assert changed.status_code == 200 and changed.json()["data"]["price"] == 9.5
        stale = await http.patch(
            f"/api/products/{product['id']}", json={"price": 1}, headers={**shop.admin, "if-match": etag}
        )
        assert stale.status_code == 412
        assert (
            await http.patch(f"/api/products/{product['id']}", json={"stock": 10**6}, headers=shop.admin)
        ).status_code == 422
        assert (
            await http.patch(f"/api/products/{product['id']}", json={"sku": "X"}, headers=shop.admin)
        ).status_code == 422
        restocked = await http.post(
            f"/api/products/{product['id']}/restock", json={"amount": 5}, headers=shop.admin
        )
        assert restocked.json()["data"]["stock"] == 105
        assert (await http.delete(f"/api/products/{product['id']}", headers=shop.admin)).status_code == 204
        assert (await http.get(f"/api/products/{product['id']}")).status_code == 404
        assert (
            await http.post("/api/orders", json={"lines": [{"sku": product["sku"], "qty": 1}]}, headers=token)
        ).status_code == 409
    raw = await shop.manager("Product").collection.find_one({"sku": product["sku"]})
    assert raw["deleted_at"] is not None and raw["price"] == 9.5  # soft-deleted, still there
    assert await shop.manager("Review").collection.count_documents({"product": raw["_id"]}) == 3
    for problem in (stale, forged):
        assert problem.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("backend", ["pydantic", "attrs"])
async def test_ordering_works_on_every_model_backend(make_shop, backend):
    shop = await make_shop(backend=backend, plug="starlette", products=8)
    async with client(shop) as http:
        sku = shop.seeded.skus[0]
        placed = await http.post(
            "/api/orders", json={"lines": [{"sku": sku, "qty": 3}]}, headers=shop.user(0)
        )
        assert placed.status_code == 201, placed.text
        order = placed.json()["data"]
        assert order["lines"][0]["qty"] == 3 and order["lines"][0]["product"] == shop.seeded.product_ids[0]
        detail = (
            await http.get(f"/api/products/{shop.seeded.product_ids[0]}?include=reviews,review_count")
        ).json()["data"]
        assert detail["stock"] == 97 and detail["review_count"] == 2
        assert (await http.post(f"/api/orders/{order['id']}/cancel", headers=shop.user(0))).status_code == 200
    assert (await stock_invariant(shop.registry, shop.m, shop.seeded))["ok"]


async def test_concurrent_buyers_never_oversell(make_shop):
    shop = await make_shop(products=3, users=40, stock=10)
    sku = shop.seeded.skus[0]
    async with client(shop, limits=httpx.Limits(max_connections=40)) as http:
        responses = await asyncio.gather(
            *(
                http.post("/api/orders", json={"lines": [{"sku": sku, "qty": 1}]}, headers=shop.user(i))
                for i in range(40)
            )
        )
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [201] * 10 + [409] * 30, statuses
        assert (await http.get(f"/api/products/{shop.seeded.product_ids[0]}")).json()["data"]["stock"] == 0

        # the change-stream projector catches up: exactly the ten sold units, counted once
        async def stats_ready():
            rows = (await http.get(f"/api/sales?sku={sku}", headers=shop.admin)).json()["data"]
            return rows if rows and rows[0]["units"] == 10 else None

        (stat,) = await eventually(stats_ready)
        assert stat["units"] == 10
    invariant = await stock_invariant(shop.registry, shop.m, shop.seeded)
    assert invariant == {
        "orders_placed": 10,
        "units_sold": 10,
        "negative_stock": 0,
        "stock_mismatches": 0,
        "ok": True,
    }


async def test_multi_item_orders_are_all_or_nothing(make_shop):
    shop = await make_shop(products=4, stock=5)
    plenty, scarce = shop.seeded.skus[0], shop.seeded.skus[1]
    async with client(shop) as http:
        refused = await http.post(
            "/api/orders",
            json={"lines": [{"sku": plenty, "qty": 2}, {"sku": scarce, "qty": 6}]},
            headers=shop.user(0),
        )
        assert refused.status_code == 409 and scarce in refused.json()["detail"]
        missing = await http.post(
            "/api/orders",
            json={"lines": [{"sku": plenty, "qty": 1}, {"sku": "NOPE", "qty": 1}]},
            headers=shop.user(0),
        )
        assert missing.status_code == 409
        # contention across two products in opposite line order must neither deadlock nor oversell
        mixed = await asyncio.gather(
            *(
                http.post(
                    "/api/orders",
                    json={"lines": [{"sku": a, "qty": 1}, {"sku": b, "qty": 1}]},
                    headers=shop.user(i % 6),
                )
                for i in range(12)
                for a, b in ([plenty, scarce] if i % 2 else [scarce, plenty],)
            )
        )
        assert sorted(r.status_code for r in mixed) == [201] * 5 + [409] * 7
    stocks = {d["sku"]: d["stock"] async for d in shop.manager("Product").collection.find({})}
    assert stocks[plenty] == 0 and stocks[scarce] == 0  # nothing leaked from the refused orders
    assert await shop.manager("Order").collection.count_documents({}) == 5
    assert (await stock_invariant(shop.registry, shop.m, shop.seeded))["ok"]


async def test_projector_resumes_after_a_restart_without_double_counting(make_shop):
    shop = await make_shop(products=3, stock=1000)
    sku = shop.seeded.skus[0]

    async def units(http):
        rows = (await http.get(f"/api/sales?sku={sku}", headers=shop.admin)).json()["data"]
        return rows[0]["units"] if rows else 0

    async with client(shop) as http:
        for _ in range(5):
            assert (
                await http.post("/api/orders", json={"lines": [{"sku": sku, "qty": 2}]}, headers=shop.user(0))
            ).status_code == 201

        async def ten():
            return await units(http) == 10

        await eventually(ten)
    # Orders written while no projector is running must be picked up from the stored resume token.
    await shop.restart(projector=False)
    async with client(shop) as http:
        for _ in range(3):
            assert (
                await http.post("/api/orders", json={"lines": [{"sku": sku, "qty": 1}]}, headers=shop.user(1))
            ).status_code == 201
        cancel_me = (await http.get("/api/orders?sort=-created_at&limit=1", headers=shop.user(1))).json()[
            "data"
        ][0]
        assert (
            await http.post(f"/api/orders/{cancel_me['id']}/cancel", headers=shop.user(1))
        ).status_code == 200
        assert await units(http) == 10  # nothing is projecting right now
    await shop.restart()  # back with the projector
    async with client(shop) as http:

        async def caught_up():
            return await units(http) == 12  # 10 + 3 placed - 1 cancelled

        await eventually(caught_up)
        await asyncio.sleep(1)
        assert await units(http) == 12  # and it stays there: no event was applied twice
    state = await shop.manager("ProjectorState").collection.find_one({"name": "sales"})
    assert state["events"] == 9  # 5 + 3 inserts and 1 cancellation


async def test_requests_in_flight_finish_on_graceful_shutdown(make_shop):
    shop = await make_shop(products=200, stock=1000)
    async with client(shop) as http:
        slow = [
            asyncio.create_task(http.get("/api/products?limit=100&include=reviews,review_count&sort=price"))
            for _ in range(20)
        ]
        await asyncio.sleep(0.02)
        stopping = asyncio.create_task(shop.server.stop())
        responses = await asyncio.gather(*slow, return_exceptions=True)
        await stopping
    assert shop.server.stopped_gracefully(), shop.server.output()
    finished = [r for r in responses if isinstance(r, httpx.Response)]
    # requests the server had accepted were answered in full before it exited
    assert finished and all(r.status_code == 200 and len(r.json()["data"]) == 100 for r in finished)
    # anything else was refused at the connection level, never answered with a broken body
    assert all(isinstance(r, httpx.Response | httpx.HTTPError) for r in responses)


@pytest.mark.chaos
async def test_orders_stay_consistent_across_a_primary_step_down(make_shop):
    shop = await make_shop(products=20, users=10, stock=100_000)
    statuses = []
    stop = asyncio.Event()

    async def shopper(index, http):
        while not stop.is_set():
            sku = shop.seeded.skus[index % 20]
            try:
                response = await http.post(
                    "/api/orders", json={"lines": [{"sku": sku, "qty": 1}]}, headers=shop.user(index)
                )
                statuses.append(response.status_code)
                if response.status_code >= 400:
                    assert response.headers["content-type"].startswith("application/problem+json")
                listing = await http.get("/api/products?limit=5")
                statuses.append(listing.status_code)
            except httpx.HTTPError:
                statuses.append(0)
            await asyncio.sleep(0.01)

    async with client(shop, limits=httpx.Limits(max_connections=20)) as http:
        shoppers = [asyncio.create_task(shopper(i, http)) for i in range(10)]
        await asyncio.sleep(1.5)
        before = len(statuses)
        try:
            await shop.registry.client.admin.command("replSetStepDown", 3, force=True)
        except Exception:
            pass  # the command closes connections; that is expected
        await asyncio.sleep(1)

        async def healthy_again():
            try:
                return (
                    await http.post(
                        "/api/orders",
                        json={"lines": [{"sku": shop.seeded.skus[0], "qty": 1}]},
                        headers=shop.user(0),
                    )
                ).status_code == 201
            except httpx.HTTPError:
                return False

        await eventually(healthy_again, seconds=90, every=0.5)
        await asyncio.sleep(1.5)
        stop.set()
        await asyncio.gather(*shoppers)
        assert (await http.get("/healthz")).status_code == 200
    assert before > 20 and statuses.count(201) > before // 2
    # during the election requests may fail, but only as declared errors: never a 500, never a hang
    assert set(statuses) <= {200, 201, 503, 504, 409}, sorted(set(statuses))
    invariant = await stock_invariant(shop.registry, shop.m, shop.seeded)
    assert invariant["ok"], (
        invariant
    )  # every accepted order moved stock exactly once; no rejected order moved any
    assert invariant["orders_placed"] == statuses.count(201) + 1
