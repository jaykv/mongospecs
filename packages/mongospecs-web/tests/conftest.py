import os
import uuid

import pytest
import pytest_asyncio

RS_URI = os.environ.get("MONGOSPECS_TEST_URI", "mongodb://localhost:27117/?replicaSet=rs0")


@pytest.fixture
def db_name():
    return f"ms_web_{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def make_registry(db_name):
    from mongospecs import Registry

    opened = []

    async def factory(*models, **kwargs):
        reg = Registry(RS_URI, database=db_name, models=models, **kwargs)
        await reg.open()
        opened.append(reg)
        return reg

    yield factory
    for reg in opened:
        if reg.is_open:
            await reg.client.drop_database(db_name)
            await reg.close()
