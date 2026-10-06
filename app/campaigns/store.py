"""Campaign persistence: MongoDB is the source of truth, Redis a write-through cache.

Writes go to MongoDB first, then Redis. A failed cache write is logged rather than surfaced:
the data is already committed, and the hourly refresh (or the next write) repairs the cache.
Reads go to Redis first and fall back to MongoDB if Redis misses or is unavailable.
"""

import logging
from collections.abc import Callable

from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.errors import DuplicateKeyError
from redis.exceptions import RedisError

from app.campaigns.cache import CampaignCache
from app.campaigns.schemas import CampaignCreate, CampaignFilters, CampaignUpdate
from app.db import AD_SETS, AD_VARIANTS, CAMPAIGNS, Database, run_in_transaction, to_document
from app.idempotency import IdempotencyStore, IdempotentRequest
from app.models import Campaign, new_id, utcnow

logger = logging.getLogger(__name__)


class UnknownAdSetsError(ValueError):
    def __init__(self, ad_set_ids: list[str]) -> None:
        super().__init__(f"unknown ad set ids: {', '.join(ad_set_ids)}")
        self.ad_set_ids = ad_set_ids


class CacheRefreshConflictError(RuntimeError):
    """Campaigns kept changing while the cache was being rebuilt; safe to retry."""


class CampaignStore:
    def __init__(self, db: Database, cache: CampaignCache, idempotency: IdempotencyStore) -> None:
        self._db = db
        self._cache = cache
        self._idempotency = idempotency
        self._on_change: list[Callable[[], None]] = []

    def on_change(self, listener: Callable[[], None]) -> None:
        """Call `listener` after every campaign write made by this instance (e.g. to refresh the serving catalog)."""
        self._on_change.append(listener)

    def _changed(self) -> None:
        for listener in self._on_change:
            listener()

    async def create(self, data: CampaignCreate, request: IdempotentRequest | None = None) -> Campaign:
        if saved := await self._idempotency.saved_response(request):
            return Campaign.model_validate(saved)
        await self._check_ad_sets(data.native_ad_set_ids)
        campaign = Campaign(campaign_id=new_id("camp"), **data.model_dump())

        async def write(session: AsyncClientSession) -> None:
            await self._idempotency.save(request, campaign.model_dump(mode="json"), session)
            await self._db[CAMPAIGNS].insert_one(to_document(campaign, "campaign_id"), session=session)

        try:
            await run_in_transaction(self._db, write)
        except DuplicateKeyError:
            # A concurrent request with the same Idempotency-Key committed first: return its result.
            if saved := await self._idempotency.saved_response(request):
                return Campaign.model_validate(saved)
            raise
        await self.put_in_cache(campaign)
        return campaign

    async def get(self, campaign_id: str) -> Campaign | None:
        try:
            if cached := await self._cache.get(campaign_id):
                return cached
        except RedisError:
            logger.warning("Campaign cache read failed; falling back to MongoDB", exc_info=True)
        doc = await self._db[CAMPAIGNS].find_one({"_id": campaign_id}, {"_id": 0})
        if doc is None:
            return None
        campaign = Campaign.model_validate(doc)
        await self.put_in_cache(campaign)
        return campaign

    async def find(self, filters: CampaignFilters) -> list[Campaign]:
        campaigns: list[Campaign] | None = None
        try:
            campaigns = await self._cache.get_all()
            if campaigns is None:  # cold cache: load it now
                campaigns = await self.refresh_cache()
        except (RedisError, CacheRefreshConflictError):
            logger.warning("Campaign cache unavailable; listing from MongoDB", exc_info=True)
            campaigns = await self._load_all()
        matching = [c for c in campaigns if filters.matches(c)]
        return sorted(matching, key=lambda c: (c.created_at, c.campaign_id))

    async def update(self, campaign_id: str, patch: CampaignUpdate) -> Campaign | None:
        changes = patch.changes()
        if "native_ad_set_ids" in changes:
            await self._check_ad_sets(changes["native_ad_set_ids"])
        doc = await self._db[CAMPAIGNS].find_one_and_update(
            {"_id": campaign_id},
            {"$set": {**changes, "updated_at": utcnow()}, "$inc": {"version": 1}},
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            await self._cache_remove(campaign_id)  # drop any stale cached copy
            return None
        campaign = Campaign.model_validate(doc)
        await self.put_in_cache(campaign)
        return campaign

    async def delete(self, campaign_id: str) -> bool:
        """Delete a campaign with its ad sets and variants, in one transaction. Serves are kept.

        Any other campaign that referenced the deleted ad sets is unlinked in the same transaction.
        """

        async def write(session: AsyncClientSession) -> tuple[bool, list[Campaign]]:
            ad_set_ids = await self._db[AD_SETS].distinct("_id", {"campaign_id": campaign_id}, session=session)
            await self._db[AD_VARIANTS].delete_many({"campaign_id": campaign_id}, session=session)
            await self._db[AD_SETS].delete_many({"campaign_id": campaign_id}, session=session)
            unlinked = await self._unlink_ad_sets(ad_set_ids, campaign_id, session)
            result = await self._db[CAMPAIGNS].delete_one({"_id": campaign_id}, session=session)
            return result.deleted_count == 1, unlinked

        deleted, unlinked = await run_in_transaction(self._db, write)
        # Evict even on 404: clears a stale entry if an earlier delete committed but its eviction failed.
        await self._cache_remove(campaign_id)
        for campaign in unlinked:
            await self.put_in_cache(campaign)
        return deleted

    async def refresh_cache(self, attempts: int = 3) -> list[Campaign]:
        """Rebuild the whole cache from MongoDB and swap it in atomically. Returns what was cached."""
        for _ in range(attempts):
            # Read the version before the snapshot: if anything changes in between, the swap is refused.
            expected = await self._cache.catalog_version()
            campaigns = await self._load_all()
            if await self._cache.replace_all(campaigns, expected):
                return campaigns
        raise CacheRefreshConflictError(f"campaigns changed during {attempts} refresh attempts")

    async def _load_all(self) -> list[Campaign]:
        return [Campaign.model_validate(doc) async for doc in self._db[CAMPAIGNS].find({}, {"_id": 0})]

    async def _unlink_ad_sets(
        self, ad_set_ids: list[str], except_campaign: str, session: AsyncClientSession
    ) -> list[Campaign]:
        """Remove deleted ad set ids from any other campaign that referenced them; returns those campaigns."""
        if not ad_set_ids:
            return []
        query = {"_id": {"$ne": except_campaign}, "native_ad_set_ids": {"$in": ad_set_ids}}
        unlinked = []
        for other_id in await self._db[CAMPAIGNS].distinct("_id", query, session=session):
            doc = await self._db[CAMPAIGNS].find_one_and_update(
                {"_id": other_id},
                {
                    "$pull": {"native_ad_set_ids": {"$in": ad_set_ids}},
                    "$set": {"updated_at": utcnow()},
                    "$inc": {"version": 1},
                },
                projection={"_id": 0},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if doc is not None:
                unlinked.append(Campaign.model_validate(doc))
        return unlinked

    async def _cache_remove(self, campaign_id: str) -> None:
        try:
            await self._cache.remove(campaign_id)
        except RedisError:
            logger.warning("Campaign cache delete failed for %s", campaign_id, exc_info=True)
        self._changed()

    async def put_in_cache(self, campaign: Campaign) -> None:
        """Best-effort cache write (MongoDB is already committed; a refresh repairs any miss)."""
        try:
            await self._cache.put(campaign)
        except RedisError:
            logger.warning("Campaign cache write failed for %s", campaign.campaign_id, exc_info=True)
        self._changed()

    async def _check_ad_sets(self, ad_set_ids: list[str]) -> None:
        if not ad_set_ids:
            return
        found = await self._db[AD_SETS].distinct("_id", {"_id": {"$in": ad_set_ids}})
        if missing := sorted(set(ad_set_ids) - set(found)):
            raise UnknownAdSetsError(missing)
