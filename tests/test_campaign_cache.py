import pytest
from redis.asyncio import Redis

from app.campaigns.cache import CAMPAIGNS_KEY, LOADED_KEY, VERSIONS_KEY, CampaignCache
from app.campaigns.store import CampaignStore
from app.db import CAMPAIGNS, Database
from app.models import Campaign

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def campaign(version: int, budget: float) -> Campaign:
    return Campaign(
        campaign_id="camp_race", campaign_name="Race", advertiser_company_id="acmp_x", version=version, daily_budget=budget
    )


async def test_out_of_order_writes_keep_the_newest_version(cache: CampaignCache) -> None:
    # Two PATCHes: MongoDB applied v2 last, but v2's cache write lands before v1's.
    assert await cache.put(campaign(version=2, budget=700))
    assert not await cache.put(campaign(version=1, budget=600))
    cached = await cache.get("camp_race")
    assert cached is not None and cached.daily_budget == 700


async def test_late_write_cannot_resurrect_a_deleted_campaign(cache: CampaignCache) -> None:
    await cache.put(campaign(version=1, budget=100))
    await cache.remove("camp_race")
    assert not await cache.put(campaign(version=2, budget=200))
    assert await cache.get("camp_race") is None


async def test_refresh_repairs_drift_and_leaves_no_ttl(store: CampaignStore, async_db: Database, async_redis: Redis) -> None:
    await store.refresh_cache()
    await async_db[CAMPAIGNS].delete_one({"_id": "camp_kitchen"})  # changed behind the cache's back
    refreshed = await store.refresh_cache()
    assert {c.campaign_id for c in refreshed} == {"camp_baba", "camp_galaxy"}
    assert set(await async_redis.hkeys(CAMPAIGNS_KEY)) == {"camp_baba", "camp_galaxy"}
    assert await async_redis.ttl(CAMPAIGNS_KEY) == -1  # swapped-in hash must not inherit the temp TTL
    assert await async_redis.ttl(VERSIONS_KEY) == -1
    assert await async_redis.exists(LOADED_KEY)


async def test_swap_is_refused_if_a_write_happened_after_the_snapshot(cache: CampaignCache) -> None:
    expected = await cache.catalog_version()
    snapshot = [campaign(version=1, budget=100)]
    await cache.put(campaign(version=2, budget=200))  # a PATCH lands mid-refresh
    assert not await cache.replace_all(snapshot, expected)
    cached = await cache.get("camp_race")
    assert cached is not None and cached.version == 2  # the stale snapshot was discarded


async def test_reads_fall_back_to_mongo_when_cache_is_cold(store: CampaignStore, async_redis: Redis) -> None:
    await async_redis.flushdb()
    from app.campaigns.schemas import CampaignFilters

    assert {c.campaign_id for c in await store.find(CampaignFilters())} == {"camp_baba", "camp_galaxy", "camp_kitchen"}
    fetched = await store.get("camp_galaxy")
    assert fetched is not None and fetched.campaign_id == "camp_galaxy"
