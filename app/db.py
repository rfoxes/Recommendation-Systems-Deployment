from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from pymongo import ASCENDING, AsyncMongoClient
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase

from app.config import Settings

CAMPAIGNS = "campaigns"
AD_SETS = "ad_sets"
AD_VARIANTS = "ad_variants"
SERVES = "serves"
IDEMPOTENCY_KEYS = "idempotency_keys"
IDEMPOTENCY_TTL = timedelta(hours=24)

Database = AsyncDatabase[dict[str, Any]]


def create_mongo_client(settings: Settings) -> AsyncMongoClient[dict[str, Any]]:
    # tz_aware so datetimes read back as UTC-aware, matching what the models write
    return AsyncMongoClient(settings.mongo_uri, tz_aware=True)


def to_document(model: BaseModel, id_field: str) -> dict[str, Any]:
    """Serialize a model for storage, using its business id as the Mongo `_id` (unique for free)."""
    return {"_id": getattr(model, id_field), **model.model_dump()}


async def run_in_transaction[T](db: Database, write: Callable[[AsyncClientSession], Awaitable[T]]) -> T:
    """Run `write` in a MongoDB transaction: every write commits together or none do.

    Transient conflicts are retried by the driver. Requires a replica set (Atlas, or the
    one-node replica set in docker-compose.yml).
    """
    async with db.client.start_session() as session:
        return await session.with_transaction(write)


async def ensure_indexes(db: Database) -> None:
    await db[CAMPAIGNS].create_index([("active", ASCENDING)])
    await db[AD_SETS].create_index([("campaign_id", ASCENDING)])
    await db[AD_VARIANTS].create_index([("ad_set_id", ASCENDING)])
    await db[AD_VARIANTS].create_index([("campaign_id", ASCENDING)])
    await db[SERVES].create_index([("session_id", ASCENDING)])
    await db[SERVES].create_index([("created_at", ASCENDING)])
    await db[IDEMPOTENCY_KEYS].create_index(
        [("created_at", ASCENDING)], expireAfterSeconds=int(IDEMPOTENCY_TTL.total_seconds())
    )
