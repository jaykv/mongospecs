"""The storefront application: resources, custom operations, lifecycle."""

from __future__ import annotations

import json
import os
import typing as t
from dataclasses import dataclass, field

from mongospecs import DocumentNotFound, DuplicateKeyError, Inc, Q, Registry, sentry_listener
from mongospecs_bench.storefront import models as shop_models
from mongospecs_bench.storefront.auth import Principal, Tokens, hash_password, verify_password
from mongospecs_bench.storefront.projector import SalesProjector
from mongospecs_web import (
    Api,
    Conflict,
    Create,
    Ctx,
    Cursor,
    Delete,
    Forbidden,
    Get,
    List,
    NotFound,
    Patch,
    Resource,
    Unauthorized,
    Unprocessable,
)

PLUGS = ("asgi", "starlette", "fastapi", "litestar", "quart")


@dataclass
class Config:
    mongo_uri: str = "mongodb://localhost:27117/?replicaSet=rs0"
    database: str = "storefront"
    backend: str = "msgspec"
    plug: str = "asgi"
    secret: str = "change-me"
    sentry_dsn: str | None = None
    projector: bool = True
    max_pool_size: int = 100
    max_time_ms: int = 10_000
    options: dict[str, t.Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        return cls(
            mongo_uri=env.get("SHOP_MONGO_URI", cls.mongo_uri),
            database=env.get("SHOP_DB", cls.database),
            backend=env.get("SHOP_BACKEND", cls.backend),
            plug=env.get("SHOP_PLUG", cls.plug),
            secret=env.get("SHOP_SECRET", cls.secret),
            sentry_dsn=env.get("SENTRY_DSN") or None,
            projector=env.get("SHOP_PROJECTOR", "1") != "0",
            max_pool_size=int(env.get("SHOP_POOL", cls.max_pool_size)),
        )


def require_login(ctx: Ctx[t.Any]) -> bool:
    if ctx.principal is None:
        raise Unauthorized("Log in first.", headers={"www-authenticate": "Bearer"})
    return True


def require_admin(ctx: Ctx[t.Any]) -> bool:
    require_login(ctx)
    return bool(ctx.principal.is_admin)


def build_api(m: t.Any, registry: Registry, tokens: Tokens) -> Api:
    """Declare the storefront's resources. ``m`` is the model namespace from ``models.build``."""

    # -- products: public reads, admin writes, soft delete, ETags -------------------------
    products: Resource[t.Any] = Resource(
        m.Product,
        ops=[List, Get, Create, Patch, Delete],
        guards={Create: require_admin, Patch: require_admin, Delete: require_admin},
        include=["reviews", "review_count"],
        page=Cursor(default=20, max=100),
    )

    @products.op("restock", method="POST", guards=[require_admin], summary="Add stock to a product")
    async def restock(ctx: Ctx[t.Any]) -> t.Any:
        amount = (ctx.body() or {}).get("amount")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise Unprocessable(
                "amount must be a positive integer.",
                errors=[{"field": "amount", "message": "must be a positive integer"}],
            )
        try:
            return await m.Product.update(ctx.id, Inc(stock=amount))
        except DocumentNotFound:
            raise NotFound("The product does not exist.") from None

    # -- reviews: anyone reads, logged-in users write, the author is set by the server -----
    reviews: Resource[t.Any] = Resource(
        m.Review,
        ops=[List, Get, Create, Delete],
        guards={Create: require_login, Delete: require_login},
        server_fields=lambda ctx: {"author": ctx.principal.id},
        page=Cursor(default=20, max=100),
    )

    @reviews.before(Create)
    async def check_review(ctx: Ctx[t.Any]) -> None:
        body = ctx.body() or {}
        rating = body.get("rating")
        if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
            raise Unprocessable(
                "rating must be between 1 and 5.",
                errors=[{"field": "rating", "message": "must be between 1 and 5"}],
            )
        if not await m.Product.exists(Q.id == body.get("product")):
            raise Unprocessable(
                "The product does not exist.", errors=[{"field": "product", "message": "does not exist"}]
            )

    @reviews.before(Delete)
    async def only_own_review(ctx: Ctx[t.Any]) -> None:
        review = await ctx.load()
        if not ctx.principal.is_admin and (
            review.author is None or str(review.author.id) != ctx.principal.id
        ):
            raise Forbidden("You can only delete your own reviews.")

    # -- orders: each user sees their own; placing and cancelling are transactions ----------
    def own_orders(ctx: Ctx[t.Any]) -> t.Any:
        require_login(ctx)
        return None if ctx.principal.is_admin else Q.user == ctx.principal.id

    orders: Resource[t.Any] = Resource(
        m.Order, ops=[List, Get], scope=own_orders, page=Cursor(default=20, max=100)
    )

    @orders.op("place", method="POST", path="", status=201, guards=[require_login], summary="Place an order")
    async def place(ctx: Ctx[t.Any]) -> t.Any:
        wanted = _order_lines(ctx.body())

        async def work(tx: t.Any) -> t.Any:
            lines: list[t.Any] = []
            total = 0.0
            # A fixed order of products keeps concurrent orders from deadlocking each other.
            for sku in sorted(wanted):
                qty = wanted[sku]
                try:
                    # One atomic step: only decrement if enough stock is left.
                    product = await m.Product.update(
                        (Q.sku == sku) & (Q.stock >= qty), Inc(stock=-qty), session=tx
                    )
                except DocumentNotFound:
                    raise Conflict(
                        f"{sku} is out of stock or does not exist.",
                        errors=[{"field": "lines", "message": f"{sku}: insufficient stock"}],
                    ) from None
                lines.append(
                    m.OrderLine(
                        product=product.id, sku=sku, name=product.name, qty=qty, unit_price=product.price
                    )
                )
                total += qty * product.price
            return await m.Order(user=ctx.principal.id, lines=lines, total=round(total, 2)).insert(session=tx)

        return await registry.transaction(work)

    @orders.op(
        "cancel", method="POST", guards=[require_login], summary="Cancel an order and return its stock"
    )
    async def cancel(ctx: Ctx[t.Any]) -> t.Any:
        scope = await ctx.scope()

        async def work(tx: t.Any) -> t.Any:
            condition = (Q.id == ctx.id) & (Q.status == "placed")
            try:
                order = await m.Order.update(
                    condition if scope is None else condition & scope, status="cancelled", session=tx
                )
            except DocumentNotFound:
                raise Conflict("The order does not exist or was already cancelled.") from None
            for line in order.lines:
                await (
                    m.Product.find(Q.id == line.product, session=tx)
                    .unscoped("soft_delete")
                    .update(Inc(stock=line.qty))
                )
            return order

        return await registry.transaction(work)

    # -- accounts -------------------------------------------------------------------------
    users: Resource[t.Any] = Resource(m.User, ops=[Get], scope=lambda ctx: _self_only(ctx))

    @users.op("signup", method="POST", path="/signup", status=201)
    async def signup(ctx: Ctx[t.Any]) -> t.Any:
        body = ctx.body() or {}
        email, password = body.get("email"), body.get("password")
        if not isinstance(email, str) or "@" not in email:
            raise Unprocessable(
                "A valid email is required.", errors=[{"field": "email", "message": "is not a valid email"}]
            )
        if not isinstance(password, str) or len(password) < 8:
            raise Unprocessable(
                "The password is too short.",
                errors=[{"field": "password", "message": "must be at least 8 characters"}],
            )
        try:
            user = await m.User(
                email=email.lower(), name=str(body.get("name", "")), password_hash=hash_password(password)
            ).insert()
        except DuplicateKeyError:
            raise Conflict(
                "That email is already registered.", errors=[{"field": "email", "message": "must be unique"}]
            ) from None
        return {"user": users.view.serialize(user), "token": tokens.issue(user.id)}

    @users.op("login", method="POST", path="/login")
    async def login(ctx: Ctx[t.Any]) -> t.Any:
        body = ctx.body() or {}
        user = (
            await m.User.find(Q.email == str(body.get("email", "")).lower()).select("+password_hash").first()
        )
        if user is None or not verify_password(str(body.get("password", "")), user.password_hash):
            raise Unauthorized("Wrong email or password.")
        return {"token": tokens.issue(user.id, is_admin=user.is_admin)}

    stats: Resource[t.Any] = Resource(m.SalesStat, name="sales", ops=[List], guards=[require_admin])

    return Api(
        products,
        reviews,
        orders,
        users,
        stats,
        prefix="/api",
        title="Storefront",
        authenticate=tokens.authenticate,
    )


def _self_only(ctx: Ctx[t.Any]) -> t.Any:
    require_login(ctx)
    return None if ctx.principal.is_admin else Q.id == ctx.principal.id


def _order_lines(body: t.Any) -> dict[str, int]:
    lines = body.get("lines") if isinstance(body, dict) else None
    if not isinstance(lines, list) or not lines or len(lines) > 50:
        raise Unprocessable(
            "lines must be a list of 1 to 50 items.",
            errors=[{"field": "lines", "message": "must have 1 to 50 items"}],
        )
    wanted: dict[str, int] = {}
    for item in lines:
        sku = item.get("sku") if isinstance(item, dict) else None
        qty = item.get("qty") if isinstance(item, dict) else None
        if (
            not isinstance(sku, str)
            or not isinstance(qty, int)
            or isinstance(qty, bool)
            or not 1 <= qty <= 1000
        ):
            raise Unprocessable(
                "Each line needs a sku and a qty from 1 to 1000.",
                errors=[{"field": "lines", "message": "each line needs sku and qty (1-1000)"}],
            )
        wanted[sku] = wanted.get(sku, 0) + qty
    return wanted


class Storefront:
    """The ASGI application: lifecycle and ``/healthz`` around the framework-specific app."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.m = shop_models.build(config.backend)
        listeners = [sentry_listener()] if config.sentry_dsn else None
        self.registry = Registry(
            config.mongo_uri,
            database=config.database,
            models=self.m.ALL,
            max_time_ms=config.max_time_ms,
            on_command=listeners,
            maxPoolSize=config.max_pool_size,
            **config.options,
        )
        self.tokens = Tokens(config.secret)
        self.api = build_api(self.m, self.registry, self.tokens)
        self.projector = SalesProjector(self.registry, self.m) if config.projector else None
        self.inner = self._mount(config.plug)
        if config.sentry_dsn:
            import sentry_sdk
            from sentry_sdk.integrations.asgi import SentryAsgiMiddleware

            sentry_sdk.init(
                dsn=config.sentry_dsn,
                traces_sample_rate=1.0,
                default_integrations=False,
                auto_enabling_integrations=False,
            )
            self.inner = SentryAsgiMiddleware(self.inner, transaction_style="url")

    def _mount(self, plug: str) -> t.Any:
        if plug == "asgi":
            return self.api.asgi()
        if plug == "starlette":
            from starlette.applications import Starlette

            from mongospecs_web.plugs import starlette as starlette_plug

            return Starlette(routes=starlette_plug.routes(self.api))
        if plug == "fastapi":
            from fastapi import FastAPI

            from mongospecs_web.plugs import fastapi as fastapi_plug

            app = FastAPI(title="Storefront", openapi_url="/api/openapi.json")
            fastapi_plug.mount(app, self.api)
            return app
        if plug == "litestar":
            from litestar import Litestar

            from mongospecs_web.plugs.litestar import MongospecsPlugin

            return Litestar(plugins=[MongospecsPlugin(self.api)], openapi_config=None)
        if plug == "quart":
            from quart import Quart

            from mongospecs_web.plugs import quart as quart_plug

            quart_app = Quart(__name__)
            quart_plug.init_app(quart_app, self.api)
            return quart_app
        raise ValueError(f"unknown plug {plug!r}; expected one of {PLUGS}")

    async def startup(self) -> None:
        await self.registry.open()
        await self.registry.ping()
        await self.registry.apply_indexes()
        if self.projector is not None:
            await self.projector.start()

    async def shutdown(self) -> None:
        if self.projector is not None:
            await self.projector.stop()
        await self.registry.close()
        print("storefront: shutdown complete", flush=True)
        if self.config.sentry_dsn:
            import sentry_sdk

            sentry_sdk.flush(timeout=5)

    async def __call__(self, scope: dict[str, t.Any], receive: t.Any, send: t.Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    try:
                        await self.startup()
                    except Exception as exc:
                        await send(
                            {"type": "lifespan.startup.failed", "message": f"{type(exc).__name__}: {exc}"}
                        )
                        return
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await self.shutdown()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        elif scope["type"] == "http" and scope["path"] == "/healthz":
            await self._health(send)
        else:
            await self.inner(scope, receive, send)

    async def _health(self, send: t.Any) -> None:
        try:
            await self.registry.ping()
            status, body = 200, {"status": "ok", "backend": self.config.backend, "plug": self.config.plug}
        except Exception as exc:
            status, body = 503, {"status": "unavailable", "error": type(exc).__name__}
        payload = json.dumps(body).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def create_app() -> Storefront:
    """ASGI factory: ``uvicorn mongospecs_bench.storefront.app:create_app --factory``."""
    return Storefront(Config.from_env())


def issue_admin_token(secret: str, user_id: t.Any) -> str:
    return Tokens(secret).issue(user_id, is_admin=True)


__all__ = ["PLUGS", "Config", "Principal", "Storefront", "build_api", "create_app", "issue_admin_token"]
