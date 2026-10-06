import re
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.ranking.ctr_model import CTRModel
from tests.test_geo import README_TEST_IPS

pytestmark = pytest.mark.integration

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"
ANDROID = "Mozilla/5.0 (Linux; Android 15; Pixel 9)"
DESKTOP = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)"
README_CONTEXT = {"searchTerm": "space adventure", "tags": ["sci-fi", "rpg"], "category": "roleplay",
                  "title": "Galaxy Companion", "nsfw": False}  # fmt: skip
AUTHORS = {"camp_baba": "Baba Casino", "camp_galaxy": "Galaxy Quest RPG", "camp_kitchen": "Kitchen Story Saga"}


def serve(client: TestClient, ip: str, ua: str, ppid: str = "user_42", context: dict | None = None):
    session = client.post("/session/create", json={"ppid": ppid}, headers={"X-Forwarded-For": ip}).json()
    body = {"position": 3, "session_id": session["session_id"], "context": context or README_CONTEXT}
    return client.post("/load/native", json=body, headers={"X-Forwarded-For": ip, "User-Agent": ua})


def author(response) -> str:
    return re.search(r'const CAMPAIGN\s*=\s*"(.*?)";', response.json()["rendered_html"]).group(1)


def campaigns_served(client: TestClient, ip: str, ua: str, runs: int = 12) -> set[str]:
    return {author(r) for i in range(runs) if (r := serve(client, ip, ua, f"user_{i}")).status_code == 200}


def test_readme_request(client: TestClient) -> None:
    response = serve(client, "214.78.0.1", IPHONE)
    assert response.status_code == 200
    body = response.json()
    assert body["impression_id"].startswith("imp_")
    html = body["rendered_html"]
    assert html.startswith("<!DOCTYPE html>") and "{{" not in html
    assert f'const AD_ID         = "{body["impression_id"]}"' in html


# Seed data: Baba (US/CA, iOS + Android) and Galaxy (US, iOS) are active; Kitchen (GB/SE, Android) is inactive.
@pytest.mark.parametrize(
    ("country", "ua", "expected"),
    [
        ("US", IPHONE, {"Baba Casino", "Galaxy Quest RPG"}),
        ("US", ANDROID, {"Baba Casino"}),  # Galaxy targets iOS only
        ("US", DESKTOP, set()),  # every campaign targets iOS/Android
        ("GB", ANDROID, set()),  # only the inactive Kitchen campaign targets GB
        ("SE", ANDROID, set()),
        ("CN", IPHONE, set()),
        ("PH", IPHONE, set()),
        ("BT", ANDROID, set()),
    ],
)
def test_geo_and_os_targeting_for_every_readme_test_ip(
    client: TestClient, country: str, ua: str, expected: set
) -> None:
    ip = next(ip for ip, c in README_TEST_IPS.items() if c == country)
    assert campaigns_served(client, ip, ua) == expected
    if not expected:
        assert serve(client, ip, ua).status_code == 204


def test_activating_a_campaign_makes_it_eligible_within_a_second(client: TestClient) -> None:
    assert client.patch("/campaigns/camp_kitchen", json={"active": True}).status_code == 200
    time.sleep(1.1)  # the in-memory serving catalog checks for changes at most once a second
    for country in ("GB", "SE"):
        ip = next(ip for ip, c in README_TEST_IPS.items() if c == country)
        assert campaigns_served(client, ip, ANDROID, runs=3) == {"Kitchen Story Saga"}
        assert serve(client, ip, IPHONE).status_code == 204  # Kitchen targets Android only


def test_unknown_session_and_bad_requests(client: TestClient) -> None:
    body = {"position": 3, "session_id": "sess_nope"}
    assert client.post("/load/native", json=body, headers={"User-Agent": IPHONE}).status_code == 404
    assert client.post("/load/native", json={"position": -1, "session_id": "s"}).status_code == 422
    assert client.post("/load/native", json={"session_id": "s"}).status_code == 422


def test_clicks(client: TestClient, mongo) -> None:
    impression = serve(client, "214.78.0.1", IPHONE).json()["impression_id"]
    url, key = f"/impressions/{impression}/click", {"Authorization": "Bearer dev-click-key"}
    assert client.post(url).status_code == 401
    assert client.post(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post(url, headers=key).status_code == 204
    assert client.post(url, headers=key).status_code == 204  # repeat: accepted, counted once
    assert client.post("/impressions/imp_unknown/click", headers=key).status_code == 404
    time.sleep(0.6)  # serve records and clicks are written in batches
    serve_doc = mongo["serves"].find_one({"_id": impression})
    assert serve_doc is not None and serve_doc["clicked_at"] is not None
    assert serve_doc["copy_source"] == "fallback" and serve_doc["ranking_reason"]  # no LLM key in tests
    assert serve_doc["features"] and serve_doc["candidates"]


@pytest.fixture
def model_fails_its_check(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def fail(self: CTRModel) -> float:
        raise ValueError("CTR model disagrees with the trained V1")

    monkeypatch.setattr(CTRModel, "verify_golden_sample", fail)
    yield


def test_ads_keep_serving_when_the_model_fails_its_check(
    model_fails_its_check: None, client: TestClient, mongo
) -> None:
    ready = client.get("/ready?format=json").json()
    assert ready["ctr_model"]["status"].startswith("unavailable") and "disagrees" in ready["ctr_model"]["error"]
    response = serve(client, "214.78.0.1", IPHONE)
    assert response.status_code == 200
    time.sleep(0.6)  # serve records are written in batches
    serve_doc = mongo["serves"].find_one({"_id": response.json()["impression_id"]})
    assert serve_doc is not None and serve_doc["ranking_reason"] == "model_unavailable"


@pytest.fixture
def serve_limit_of_three(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SERVE_LIMIT_PER_MINUTE", "3")
    yield


def test_ad_requests_are_rate_limited_per_ip(serve_limit_of_three: None, client: TestClient) -> None:
    statuses = [serve(client, "214.78.0.1", IPHONE, f"limited_{i}").status_code for i in range(4)]
    assert statuses == [200, 200, 200, 429]
    limited = serve(client, "214.78.0.1", IPHONE, "limited_again")
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
    assert (
        serve(client, "2.125.160.217", ANDROID, "other_ip").status_code == 204
    )  # other IPs unaffected (no fill in GB)


def test_a_deleted_or_deactivated_campaign_stops_serving_immediately(client: TestClient) -> None:
    # No sleep: the instance that made the change re-checks its serving catalog on the very next request.
    client.patch("/campaigns/camp_galaxy", json={"active": False})
    assert campaigns_served(client, "214.78.0.1", IPHONE, runs=6) == {"Baba Casino"}
    assert client.delete("/campaigns/camp_baba").status_code == 204
    assert serve(client, "214.78.0.1", IPHONE, "after_delete").status_code == 204
