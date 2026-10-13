"""Shared fixtures. Tests run against real MongoDB (see docker/)."""

import os
import uuid

import pytest
import pytest_asyncio

from mongospecs import Registry

from .models import BACKENDS, build

RS_URI = os.environ.get("MONGOSPECS_TEST_URI", "mongodb://localhost:27117/?replicaSet=rs0")
SHARDED_URI = os.environ.get("MONGOSPECS_TEST_SHARDED_URI", "mongodb://localhost:27118")


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


@pytest.fixture
def m(backend):
    """The model namespace for the current backend."""
    return build(backend)


@pytest.fixture
def db_name():
    return f"ms_test_{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def registry(m, db_name):
    async with Registry(RS_URI, database=db_name, models=m.ALL) as reg:
        yield reg
        await reg.client.drop_database(db_name)


@pytest_asyncio.fixture
async def make_registry(db_name):
    """Factory for registries with custom models/options; all are closed and their database dropped."""
    opened = []

    async def factory(*models, uri=RS_URI, database=None, **kwargs):
        reg = Registry(uri, database=database or db_name, models=models, **kwargs)
        await reg.open()
        opened.append((reg, database or db_name, uri))
        return reg

    yield factory
    for reg, name, uri in opened:
        if reg.is_open:
            await reg.client.drop_database(name)
            await reg.close()
        else:
            async with Registry(uri) as cleaner:
                await cleaner.client.drop_database(name)
