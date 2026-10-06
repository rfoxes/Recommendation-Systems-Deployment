import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.cache import bump_catalog_version
from app.campaigns.store import CampaignStore
from app.copywriting.generator import CopyGenerator
from app.copywriting.writers import CopyGenerationError
from app.db import AD_VARIANTS, Database
from app.models import utcnow
from app.temporal.jobs import (
    FIND_VARIANTS_NEEDING_COPY,
    GENERATE_VARIANT_COPY,
    REFRESH_CAMPAIGN_CACHE,
    VariantCopyJob,
)

logger = logging.getLogger(__name__)


class CacheActivities:
    def __init__(self, store: CampaignStore) -> None:
        self._store = store

    @activity.defn(name=REFRESH_CAMPAIGN_CACHE)
    async def refresh_campaign_cache(self) -> int:
        """Reload every campaign from MongoDB into the Redis cache. Returns how many were cached."""
        campaigns = await self._store.refresh_cache()
        activity.logger.info("Refreshed campaign cache with %d campaigns", len(campaigns))
        return len(campaigns)


class CopyActivities:
    def __init__(self, db: Database, redis: Redis, generator: CopyGenerator, pool_size: int) -> None:
        self._db = db
        self._redis = redis
        self._generator = generator
        self._pool_size = pool_size

    @activity.defn(name=FIND_VARIANTS_NEEDING_COPY)
    async def find_variants_needing_copy(self, ad_set_id: str | None) -> list[VariantCopyJob]:
        """Active variants with fewer than pool_size lines (all variants, or one ad set's)."""
        if not self._generator.enabled:
            activity.logger.info("No LLM API key configured; skipping copy generation (ads use fallback copy)")
            return []
        query: dict[str, object] = {"active": True, f"copy_pool.{self._pool_size - 1}": {"$exists": False}}
        if ad_set_id is not None:
            query["ad_set_id"] = ad_set_id
        projection = {"_id": 0, "variant_id": 1, "character_name": 1, "ai_prompt": 1}
        return [VariantCopyJob(**doc) async for doc in self._db[AD_VARIANTS].find(query, projection)]

    @activity.defn(name=GENERATE_VARIANT_COPY)
    async def generate_variant_copy(self, job: VariantCopyJob) -> int:
        """Generate the variant's lines, save them, and tell serving instances to reload."""
        try:
            lines = await self._generator.lines(job.character_name, job.ai_prompt, self._pool_size, timeout=30)
        except CopyGenerationError as exc:
            # Rate limits / outages are retried by Temporal with backoff; bad keys or refusals are not.
            raise ApplicationError(str(exc), non_retryable=not exc.retryable) from exc
        await self._db[AD_VARIANTS].update_one(
            {"_id": job.variant_id}, {"$set": {"copy_pool": lines, "updated_at": utcnow()}}
        )
        try:
            await bump_catalog_version(self._redis)
        except RedisError:
            logger.warning("Could not bump catalog version after generating copy", exc_info=True)
        return len(lines)
