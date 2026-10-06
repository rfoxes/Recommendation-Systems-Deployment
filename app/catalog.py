"""In-memory copy of everything that can be served right now.

The serve path reads this on every request, so it lives in process memory instead of Redis.
It holds only active campaigns, and only their active ad sets and variants; campaigns with
nothing servable are dropped. Inactive campaigns stay in Redis (the source for GET /campaigns),
so when one is activated, the write bumps `catalog:version`, and every instance notices within
`check_interval` seconds and reloads. Between checks, serving never touches Redis.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from redis.exceptions import RedisError

from app.campaigns.cache import CampaignCache
from app.campaigns.schemas import CampaignFilters
from app.campaigns.store import CampaignStore
from app.db import AD_SETS, AD_VARIANTS, Database
from app.models import AdSet, AdVariant, Campaign

logger = logging.getLogger(__name__)

_UNKNOWN_VERSION = "unknown"  # never equals a real version, so recovery forces a reload


@dataclass(frozen=True)
class ServableAdSet:
    ad_set: AdSet
    variants: tuple[AdVariant, ...]


@dataclass(frozen=True)
class ServableCampaign:
    campaign: Campaign
    ad_sets: tuple[ServableAdSet, ...]


class ActiveCatalog:
    def __init__(self, db: Database, store: CampaignStore, cache: CampaignCache, check_interval: float = 1.0) -> None:
        self._db = db
        self._store = store
        self._cache = cache
        self._check_interval = check_interval
        self._items: tuple[ServableCampaign, ...] = ()
        self._loaded = False
        self._version: str | None = None
        self._checked_at = 0.0
        self._lock = asyncio.Lock()

    async def campaigns(self) -> tuple[ServableCampaign, ...]:
        if self._is_fresh():
            return self._items
        async with self._lock:
            if self._is_fresh():  # another request refreshed it while we waited
                return self._items
            try:
                version = await self._cache.catalog_version()
            except RedisError:
                logger.warning("Catalog version check failed; keeping current copy", exc_info=True)
                version = _UNKNOWN_VERSION
            # Read the version before loading, so a change made during the load triggers another reload.
            # If Redis is down, keep what we have (only load if we have nothing yet).
            stale = version != self._version and version != _UNKNOWN_VERSION
            if stale or not self._loaded:
                self._items = await self._load()
                self._loaded = True
            self._version = version
            self._checked_at = time.monotonic()
        return self._items

    def invalidate(self) -> None:
        """Re-check on the next request: this instance just changed a campaign, so don't wait out the interval.
        (Other instances still notice within `check_interval`.)"""
        self._checked_at = float("-inf")

    def _is_fresh(self) -> bool:
        return self._loaded and time.monotonic() - self._checked_at < self._check_interval

    async def _load(self) -> tuple[ServableCampaign, ...]:
        campaigns = await self._store.find(CampaignFilters(active=True))
        wanted_ad_set_ids = list({i for c in campaigns for i in c.native_ad_set_ids})
        ad_sets = {
            doc["ad_set_id"]: AdSet.model_validate(doc)
            async for doc in self._db[AD_SETS].find({"_id": {"$in": wanted_ad_set_ids}, "active": True}, {"_id": 0})
        }
        variants: defaultdict[str, list[AdVariant]] = defaultdict(list)
        async for doc in self._db[AD_VARIANTS].find({"ad_set_id": {"$in": list(ad_sets)}, "active": True}, {"_id": 0}):
            variant = AdVariant.model_validate(doc)
            variants[variant.ad_set_id].append(variant)

        servable = []
        for campaign in campaigns:
            sets = tuple(
                ServableAdSet(ad_sets[i], tuple(variants[i]))
                for i in campaign.native_ad_set_ids
                if i in ad_sets and variants[i]
            )
            if sets:
                servable.append(ServableCampaign(campaign, sets))
        return tuple(servable)
