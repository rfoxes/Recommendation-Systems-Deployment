import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from redis import Redis as SyncRedis
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.campaigns.cache import CampaignCache
from app.campaigns.store import CampaignStore
from app.config import Settings, get_settings
from app.db import Database, create_mongo_client
from app.idempotency import IdempotencyStore
from app.seed import seed

TEST_MONGO_DB = "simula_test"
TEST_REDIS_URL = "redis://localhost:6379/15"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Point the app at an isolated test database and Redis db, with Temporal off."""
    monkeypatch.setenv("MONGO_DB", TEST_MONGO_DB)
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("TEMPORAL_ENABLED", "false")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def services(settings: Settings) -> Iterator[tuple[MongoClient, SyncRedis]]:
    """Empty MongoDB test db + Redis db, seeded with the sample data. Skips if services are down."""
    mongo: MongoClient = MongoClient(settings.mongo_uri, tz_aware=True, serverSelectionTimeoutMS=1000)
    redis = SyncRedis.from_url(settings.redis_url, decode_responses=True)
    try:
        mongo.admin.command("ping")
        redis.ping()
    except (PyMongoError, RedisError):
        pytest.skip("MongoDB/Redis not reachable; start them with `docker compose up -d`")
    mongo.drop_database(TEST_MONGO_DB)
    redis.flushdb()
    asyncio.run(_seed(settings))
    yield mongo, redis
    mongo.drop_database(TEST_MONGO_DB)
    redis.flushdb()
    mongo.close()
    redis.close()


async def _seed(settings: Settings) -> None:
    client = create_mongo_client(settings)
    try:
        await seed(client[settings.mongo_db])
    finally:
        await client.close()


@pytest.fixture
def mongo(services: tuple[MongoClient, SyncRedis]) -> Database:
    return services[0][TEST_MONGO_DB]  # type: ignore[return-value]


@pytest.fixture
def redis_sync(services: tuple[MongoClient, SyncRedis]) -> SyncRedis:
    return services[1]


@pytest.fixture
def client(services: tuple[MongoClient, SyncRedis]) -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as test_client:  # runs the lifespan: connects and warms the cache
        yield test_client


@pytest.fixture
async def async_redis(services: tuple[MongoClient, SyncRedis]) -> AsyncIterator[Redis]:
    redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    yield redis
    await redis.aclose()


@pytest.fixture
async def async_db(settings: Settings, services: tuple[MongoClient, SyncRedis]) -> AsyncIterator[Database]:
    client = create_mongo_client(settings)
    yield client[TEST_MONGO_DB]
    await client.close()


@pytest.fixture
def cache(async_redis: Redis) -> CampaignCache:
    return CampaignCache(async_redis)


@pytest.fixture
def store(async_db: Database, cache: CampaignCache) -> CampaignStore:
    return CampaignStore(async_db, cache, IdempotencyStore(async_db))
