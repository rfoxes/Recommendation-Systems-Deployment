import json

import pytest
from fastapi.testclient import TestClient
from redis import Redis

from app.campaigns.cache import CAMPAIGNS_KEY

pytestmark = pytest.mark.integration

# The README's POST /campaigns example body, verbatim.
README_CAMPAIGN = {
    "campaign_name": "Baba Casino — Summer Push",
    "advertiser_company_id": "acmp_baba",
    "daily_budget": 500.0,
    "geo_targets": ["US", "CA"],
    "os_targets": ["ios", "android"],
    "attribution_provider": "appsflyer",
    "ios_store_url": "https://apps.apple.com/app/id1234567890",
    "android_store_url": "https://play.google.com/store/apps/details?id=com.baba.casino",
    "native_ad_set_ids": ["adset_native_a"],
}


def cached(redis: Redis, campaign_id: str) -> dict | None:
    raw = redis.hget(CAMPAIGNS_KEY, campaign_id)
    return json.loads(raw) if raw else None


def test_create_sets_api_owned_fields_and_writes_db_and_cache(client: TestClient, mongo, redis_sync) -> None:
    response = client.post("/campaigns", json=README_CAMPAIGN)
    assert response.status_code == 201
    body = response.json()
    assert body["campaign_id"].startswith("camp_")
    assert body["active"] is False
    assert body["version"] == 1
    assert body["created_at"] and body["updated_at"]
    assert (
        mongo["campaigns"].find_one({"_id": body["campaign_id"]})["campaign_name"] == README_CAMPAIGN["campaign_name"]
    )
    assert cached(redis_sync, body["campaign_id"])["version"] == 1


@pytest.mark.parametrize(
    "body",
    [
        {"advertiser_company_id": "acmp_x"},  # missing campaign_name
        {**README_CAMPAIGN, "campaign_id": "camp_mine"},  # API-owned
        {**README_CAMPAIGN, "active": True},  # API-owned on create
        {**README_CAMPAIGN, "geo_targets": ["usa"]},
        {**README_CAMPAIGN, "native_ad_set_ids": ["adset_missing"]},  # unknown ad set
    ],
)
def test_create_rejects_invalid(client: TestClient, body: dict) -> None:
    assert client.post("/campaigns", json=body).status_code == 422


def test_get(client: TestClient) -> None:
    response = client.get("/campaigns/camp_galaxy")
    assert response.status_code == 200
    assert response.json()["os_targets"] == ["ios"]
    assert client.get("/campaigns/camp_missing").status_code == 404


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", {"camp_baba", "camp_galaxy", "camp_kitchen"}),
        ("?active=true", {"camp_baba", "camp_galaxy"}),
        ("?active=false", {"camp_kitchen"}),
        ("?surface=native&active=true", {"camp_baba", "camp_galaxy"}),
        ("?ids=camp_baba,camp_kitchen", {"camp_baba", "camp_kitchen"}),
        ("?ids=camp_baba&ids=camp_galaxy", {"camp_baba", "camp_galaxy"}),
        ("?publisher_id=pub_any", {"camp_baba", "camp_galaxy", "camp_kitchen"}),  # no lists = all
    ],
)
def test_list_filters(client: TestClient, query: str, expected: set[str]) -> None:
    response = client.get(f"/campaigns{query}")
    assert response.status_code == 200
    assert {c["campaign_id"] for c in response.json()} == expected


def test_list_publisher_filter_respects_publisher_list(client: TestClient) -> None:
    created = client.post("/campaigns", json={**README_CAMPAIGN, "publisher_ids": ["pub_1"]}).json()
    assert created["campaign_id"] in {c["campaign_id"] for c in client.get("/campaigns?publisher_id=pub_1").json()}
    assert created["campaign_id"] not in {c["campaign_id"] for c in client.get("/campaigns?publisher_id=pub_2").json()}


def test_patch_updates_db_and_cache(client: TestClient, mongo, redis_sync) -> None:
    before = client.get("/campaigns/camp_kitchen").json()
    response = client.patch("/campaigns/camp_kitchen", json={"active": True, "daily_budget": 750.0})
    assert response.status_code == 200
    after = response.json()
    assert after["active"] is True
    assert after["daily_budget"] == 750.0
    assert after["version"] == before["version"] + 1
    assert after["updated_at"] > before["updated_at"]
    assert mongo["campaigns"].find_one({"_id": "camp_kitchen"})["active"] is True
    assert cached(redis_sync, "camp_kitchen")["active"] is True


def test_patch_can_clear_nullable_fields(client: TestClient) -> None:
    response = client.patch("/campaigns/camp_baba", json={"daily_budget": None})
    assert response.status_code == 200
    assert response.json()["daily_budget"] is None


@pytest.mark.parametrize(
    "body",
    [
        {},  # nothing to update
        {"campaign_name": None},  # required field cannot be cleared
        {"campaign_id": "camp_other"},  # API-owned
        {"os_targets": ["windows"]},
        {"native_ad_set_ids": ["adset_missing"]},
    ],
)
def test_patch_rejects_invalid(client: TestClient, body: dict) -> None:
    assert client.patch("/campaigns/camp_baba", json=body).status_code == 422


def test_patch_missing_campaign(client: TestClient) -> None:
    assert client.patch("/campaigns/camp_missing", json={"active": True}).status_code == 404


def test_delete_cascades_and_clears_cache(client: TestClient, mongo, redis_sync) -> None:
    assert client.delete("/campaigns/camp_baba").status_code == 204
    assert mongo["campaigns"].find_one({"_id": "camp_baba"}) is None
    assert mongo["ad_sets"].count_documents({"campaign_id": "camp_baba"}) == 0
    assert mongo["ad_variants"].count_documents({"campaign_id": "camp_baba"}) == 0
    assert cached(redis_sync, "camp_baba") is None
    assert client.get("/campaigns/camp_baba").status_code == 404
    assert client.delete("/campaigns/camp_baba").status_code == 404
