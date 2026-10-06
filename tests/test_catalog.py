import pytest

from app.campaigns.cache import CampaignCache
from app.campaigns.schemas import CampaignUpdate
from app.campaigns.store import CampaignStore
from app.catalog import ActiveCatalog
from app.db import Database

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_holds_only_active_servable_items(async_db: Database, store: CampaignStore, cache: CampaignCache) -> None:
    catalog = ActiveCatalog(async_db, store, cache)
    items = {item.campaign.campaign_id: item for item in await catalog.campaigns()}
    assert set(items) == {"camp_baba", "camp_galaxy"}  # camp_kitchen is inactive
    baba_variants = {v.variant_id for s in items["camp_baba"].ad_sets for v in s.variants}
    assert baba_variants == {"var_a_01", "var_a_02", "var_a_03"}  # var_a_04 is inactive


async def test_activation_shows_up_after_version_bump(
    async_db: Database, store: CampaignStore, cache: CampaignCache
) -> None:
    catalog = ActiveCatalog(async_db, store, cache, check_interval=0)
    assert "camp_kitchen" not in {i.campaign.campaign_id for i in await catalog.campaigns()}
    await store.update("camp_kitchen", CampaignUpdate(active=True))
    assert "camp_kitchen" in {i.campaign.campaign_id for i in await catalog.campaigns()}


async def test_does_not_recheck_within_interval(async_db: Database, store: CampaignStore, cache: CampaignCache) -> None:
    catalog = ActiveCatalog(async_db, store, cache, check_interval=60)
    first = await catalog.campaigns()
    await store.update("camp_galaxy", CampaignUpdate(active=False))
    assert await catalog.campaigns() is first  # served from memory until the next check
