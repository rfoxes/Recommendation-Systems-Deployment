import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

VIDEO = "https://cdn.simula.ad/baba/luna.mp4"
# The README's POST /adsets example, pointed at a seeded campaign.
README_AD_SET = {
    "campaign_id": "camp_baba",
    "ad_set_name": "Baba — Hero Characters",
    "character_names": ["Luna", "Rex"],
    "video_urls": [VIDEO],
    "ctas": ["Play Free", "Install Now"],
    "ai_prompts": ["Excitedly tell a friend about the daily bonus."],
    "fallback_copy": ["Check this out!"],
}


def counts(mongo) -> tuple[int, int]:
    return mongo["ad_sets"].count_documents({}), mongo["ad_variants"].count_documents({})


def test_variants_are_the_cartesian_product_and_the_campaign_is_linked(client: TestClient, mongo) -> None:
    response = client.post("/adsets", json=README_AD_SET)
    assert response.status_code == 201
    body = response.json()
    combos = {(v["character_name"], v["video_url"], v["cta"], v["ai_prompt"]) for v in body["variants"]}
    prompt = README_AD_SET["ai_prompts"][0]
    assert combos == {(c, VIDEO, t, prompt) for c in ("Luna", "Rex") for t in ("Play Free", "Install Now")}
    assert body["active"] is True and body["fallback_copy"] == ["Check this out!"]
    campaign = client.get("/campaigns/camp_baba").json()
    assert body["ad_set_id"] in campaign["native_ad_set_ids"]
    assert campaign["version"] == 2  # linking bumps the version (and the cache / serving catalog)
    assert mongo["ad_variants"].count_documents({"ad_set_id": body["ad_set_id"]}) == 4


def test_duplicate_assets_do_not_create_duplicate_variants(client: TestClient) -> None:
    body = {**README_AD_SET, "character_names": ["Luna", "Luna", "Rex"], "ctas": ["Play Free", "Play Free"]}
    assert len(client.post("/adsets", json=body).json()["variants"]) == 2


def test_variant_cap(client: TestClient) -> None:
    body = {
        **README_AD_SET,
        "character_names": [f"c{i}" for i in range(8)],
        "video_urls": [f"https://cdn.example.com/{i}.mp4" for i in range(8)],
        "ctas": [f"t{i}" for i in range(8)],
    }
    response = client.post("/adsets", json=body)
    assert response.status_code == 422 and "512 variants" in response.text


@pytest.mark.parametrize(
    "change",
    [{"character_names": []}, {"video_urls": ["not a url"]}, {"ad_set_id": "mine"}, {"campaign_id": ""}],
)
def test_invalid_bodies(client: TestClient, change: dict) -> None:
    assert client.post("/adsets", json={**README_AD_SET, **change}).status_code == 422


def test_unknown_campaign_rolls_everything_back(client: TestClient, mongo) -> None:
    before = counts(mongo)
    response = client.post("/adsets", json={**README_AD_SET, "campaign_id": "camp_missing"})
    assert response.status_code == 422
    assert counts(mongo) == before  # the transaction undid the ad set and variant inserts


def test_idempotency_key(client: TestClient, mongo) -> None:
    headers = {"Idempotency-Key": "adset-retry-1"}
    first = client.post("/adsets", json=README_AD_SET, headers=headers)
    before = counts(mongo)
    retry = client.post("/adsets", json=README_AD_SET, headers=headers)
    assert retry.status_code == 201 and retry.json()["ad_set_id"] == first.json()["ad_set_id"]
    assert counts(mongo) == before  # nothing new created
    other_body = client.post("/adsets", json={**README_AD_SET, "ad_set_name": "Different"}, headers=headers)
    assert other_body.status_code == 422  # same key, different request


def test_campaign_idempotency_key(client: TestClient) -> None:
    body = {"campaign_name": "Retry", "advertiser_company_id": "acmp_retry"}
    first = client.post("/campaigns", json=body, headers={"Idempotency-Key": "camp-retry-1"})
    retry = client.post("/campaigns", json=body, headers={"Idempotency-Key": "camp-retry-1"})
    assert first.json()["campaign_id"] == retry.json()["campaign_id"]
