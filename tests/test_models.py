import pytest
from pydantic import ValidationError

from app.db import to_document
from app.models import AdSet, AdVariant, Campaign
from app.seed import load_seed_file


def test_seed_files_validate() -> None:
    assert len(load_seed_file("campaigns.json", Campaign)) == 3
    assert len(load_seed_file("ad_sets.json", AdSet)) == 3
    assert len(load_seed_file("ad_variants.json", AdVariant)) == 10


def test_campaign_defaults() -> None:
    campaign = Campaign(campaign_id="camp_x", campaign_name="X", advertiser_company_id="acmp_x")
    assert campaign.active is False
    assert campaign.surface == "native"
    assert campaign.geo_targets == []
    assert campaign.created_at.tzinfo is not None


@pytest.mark.parametrize(
    "field",
    [
        {"geo_targets": ["usa"]},
        {"os_targets": ["windows"]},
        {"daily_budget": -1},
        {"ios_store_url": "not a url"},
        {"campaign_name": "   "},
    ],
)
def test_campaign_rejects_invalid(field: dict[str, object]) -> None:
    base = {"campaign_id": "camp_x", "campaign_name": "X", "advertiser_company_id": "acmp_x"}
    with pytest.raises(ValidationError):
        Campaign.model_validate(base | field)


def test_ad_set_requires_assets() -> None:
    with pytest.raises(ValidationError):
        AdSet(
            ad_set_id="adset_x",
            campaign_id="camp_x",
            ad_set_name="X",
            character_names=[],
            video_urls=["https://example.com/v.mp4"],
            ctas=["Go"],
            ai_prompts=["Say hi"],
            fallback_copy=["Hi"],
        )


def test_to_document_uses_business_id_and_plain_urls() -> None:
    campaign = Campaign(
        campaign_id="camp_x",
        campaign_name="X",
        advertiser_company_id="acmp_x",
        ios_store_url="https://apps.apple.com/app/id1",
    )
    doc = to_document(campaign, "campaign_id")
    assert doc["_id"] == "camp_x"
    assert doc["ios_store_url"] == "https://apps.apple.com/app/id1"
