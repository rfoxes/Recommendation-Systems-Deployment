from collections.abc import AsyncIterator

import pytest
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from app.config import get_settings
from app.db import AD_SETS, AD_VARIANTS, CAMPAIGNS, Database
from app.seed import seed

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TEST_DB = "simula_test"


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    client: AsyncMongoClient = AsyncMongoClient(get_settings().mongo_uri, tz_aware=True, serverSelectionTimeoutMS=1000)
    try:
        await client.admin.command("ping")
    except PyMongoError:
        await client.close()
        pytest.skip("MongoDB not reachable; start it with `docker compose up -d`")
    await client.drop_database(TEST_DB)
    yield client[TEST_DB]
    await client.drop_database(TEST_DB)
    await client.close()


async def test_seed_is_idempotent(db: Database) -> None:
    first = await seed(db)
    second = await seed(db)
    assert first == second == {CAMPAIGNS: 3, AD_SETS: 3, AD_VARIANTS: 10}
    assert await db[CAMPAIGNS].count_documents({}) == 3
    assert await db[AD_VARIANTS].count_documents({}) == 10


async def test_seeded_campaign_round_trips(db: Database) -> None:
    await seed(db)
    doc = await db[CAMPAIGNS].find_one({"_id": "camp_galaxy"})
    assert doc is not None
    assert doc["os_targets"] == ["ios"]
    assert doc["android_store_url"] is None
    assert doc["created_at"].tzinfo is not None
