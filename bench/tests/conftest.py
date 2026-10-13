import os
import uuid

import pytest_asyncio

from mongospecs import Registry
from mongospecs_bench.storefront import models as shop_models
from mongospecs_bench.storefront.auth import Tokens
from mongospecs_bench.storefront.seed import seed
from mongospecs_bench.storefront.server import start_server

RS_URI = os.environ.get("MONGOSPECS_TEST_URI", "mongodb://localhost:27117/?replicaSet=rs0")
SECRET = "e2e-secret"


class Shop:
    """A running storefront server plus direct database access for assertions."""

    def __init__(self, server, registry, m, seeded, database, options):
        self.server = server
        self.registry = registry
        self.m = m
        self.seeded = seeded
        self.database = database
        self.options = options
        self.tokens = Tokens(SECRET)

    @property
    def url(self):
        return self.server.base_url

    def user(self, index=0):
        return {"authorization": f"Bearer {self.seeded.user_tokens[index]}"}

    @property
    def admin(self):
        return {"authorization": f"Bearer {self.seeded.admin_token}"}

    def manager(self, name):
        return self.registry.manager(getattr(self.m, name))

    async def restart(self, **overrides):
        """Stop the server gracefully and start a new one on the same database."""
        await self.server.stop()
        assert self.server.stopped_gracefully(), self.server.output()
        self.server = await start_server(
            database=self.database, mongo_uri=RS_URI, secret=SECRET, **{**self.options, **overrides}
        )


@pytest_asyncio.fixture
async def make_shop():
    """Factory: seed a fresh database and boot a real server process against it."""
    shops = []

    async def factory(
        *, plug="asgi", backend="msgspec", app="storefront", products=40, users=6, stock=100, projector=True
    ):
        database = f"ms_e2e_{uuid.uuid4().hex[:10]}"
        m = shop_models.build(backend)
        registry = Registry(RS_URI, database=database)
        await registry.open()
        seeded = await seed(
            registry, m, Tokens(SECRET), products=products, users=users, stock=stock, reviews_per_product=2
        )
        options = {"plug": plug, "backend": backend, "app": app, "projector": projector}
        server = await start_server(database=database, mongo_uri=RS_URI, secret=SECRET, **options)
        shop = Shop(server, registry, m, seeded, database, options)
        shops.append(shop)
        return shop

    yield factory
    for shop in shops:
        code = await shop.server.stop()
        await shop.registry.client.drop_database(shop.database)
        await shop.registry.close()
        assert shop.server.stopped_gracefully(), (
            f"server did not shut down cleanly (exit {code}):\n{shop.server.output()}"
        )
        assert "Traceback" not in shop.server.output(), shop.server.output()
