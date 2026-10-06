import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture
def fast_sessions(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A 1-second inactivity window and a limit of 5 creates per minute, so the rules are testable quickly."""
    monkeypatch.setenv("SESSION_INACTIVITY_SECONDS", "1")
    monkeypatch.setenv("SESSION_CREATE_LIMIT", "5")
    yield


def create(client: TestClient, ip: str = "214.78.0.1", ppid: str | None = "user_42"):
    body = {"ppid": ppid} if ppid else None
    return client.post("/session/create", json=body, headers={"X-Forwarded-For": ip})


def test_session_is_reused_while_active(fast_sessions: None, client: TestClient) -> None:
    first, second = create(client), create(client)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["session_id"] == second.json()["session_id"]
    assert first.json()["session_id"].startswith("sess_")


def test_ppid_wins_over_ip_and_ip_is_the_fallback(fast_sessions: None, client: TestClient) -> None:
    by_ppid_us, by_ppid_gb = create(client, "214.78.0.1"), create(client, "2.125.160.217")
    assert by_ppid_us.json()["session_id"] == by_ppid_gb.json()["session_id"]  # same ppid, different IPs
    by_ip_us, by_ip_gb = create(client, "214.78.0.1", None), create(client, "2.125.160.217", None)
    assert by_ip_us.json()["session_id"] != by_ip_gb.json()["session_id"]  # no ppid: identified by IP
    assert by_ip_us.json()["session_id"] != by_ppid_us.json()["session_id"]


def test_new_session_after_inactivity(fast_sessions: None, client: TestClient) -> None:
    first = create(client).json()["session_id"]
    time.sleep(1.3)
    second = create(client)
    assert second.status_code == 201 and second.json()["session_id"] != first


def test_invalid_bodies(fast_sessions: None, client: TestClient) -> None:
    assert client.post("/session/create", json={"ppid": ""}).status_code == 422
    assert client.post("/session/create", json={"ppid": "x" * 257}).status_code == 422
    assert client.post("/session/create", json={"user": "x"}).status_code == 422


def test_rate_limit_per_ip(fast_sessions: None, client: TestClient) -> None:
    statuses = [create(client, "202.196.224.1", f"u{i}").status_code for i in range(6)]
    assert statuses == [201] * 5 + [429]
    limited = create(client, "202.196.224.1", "u_last")
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
    assert create(client, "67.43.156.1", "other_ip").status_code == 201  # other IPs are unaffected
