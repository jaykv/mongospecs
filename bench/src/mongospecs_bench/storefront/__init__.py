"""Storefront: a production-shaped reference application built on mongospecs and mongospecs-web.

It is what the end-to-end tests boot as a real server process and what the load
benchmark drives over HTTP. It is deliberately not a toy:

* authentication with signed bearer tokens, admin-only operations, row scope on orders
* placing an order is a multi-document transaction that must never oversell stock
* products are soft-deleted, revisioned (ETag / If-Match) and carry reviews as a reverse relation
* a change-stream projector keeps sales statistics up to date, exactly once, resumable
* indexes and validators are applied at startup; shutdown is graceful; ``/healthz`` checks the database
* optional tracing through the Sentry SDK (``SENTRY_DSN``)

Run it::

    SHOP_DB=shop uv run uvicorn mongospecs_bench.storefront.app:create_app --factory
"""
