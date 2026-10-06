import pytest
from starlette.requests import Request

from app.request_context import client_ip
from app.serving.geo import GeoIP, os_from_user_agent

# Every test IP from the README's "Test IPs" table.
README_TEST_IPS = {
    "214.78.0.1": "US",
    "2.125.160.217": "GB",
    "89.160.20.113": "SE",
    "175.16.199.1": "CN",
    "202.196.224.1": "PH",
    "67.43.156.1": "BT",
}


@pytest.fixture(scope="module")
def geoip() -> GeoIP:
    return GeoIP()


@pytest.mark.parametrize(("ip", "country"), README_TEST_IPS.items())
def test_readme_test_ips_resolve(geoip: GeoIP, ip: str, country: str) -> None:
    assert geoip.country(ip) == country


@pytest.mark.parametrize("ip", ["8.8.8.8", "127.0.0.1", "10.0.0.1", "::1", "not-an-ip", "", None])
def test_unknown_or_invalid_ips_have_no_country(geoip: GeoIP, ip: str | None) -> None:
    assert geoip.country(ip) is None


@pytest.mark.parametrize(
    ("user_agent", "os"),
    [
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", "ios"),
        ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)", "ios"),
        ("Mozilla/5.0 (Linux; Android 15; Pixel 9)", "android"),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)", None),
        ("curl/8.7.1", None),
        ("", None),
        (None, None),
    ],
)
def test_os_from_user_agent(user_agent: str | None, os: str | None) -> None:
    assert os_from_user_agent(user_agent) == os


def _request(forwarded: str | None, peer: str = "10.1.2.3") -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


@pytest.mark.parametrize(
    ("forwarded", "expected"),
    [
        ("214.78.0.1", "214.78.0.1"),
        ("214.78.0.1, 35.191.0.1", "214.78.0.1"),  # first hop is the client; later hops are proxies
        ("garbage, 35.191.0.1", "10.1.2.3"),  # invalid header -> fall back to the connection address
        (None, "10.1.2.3"),
    ],
)
def test_client_ip(forwarded: str | None, expected: str) -> None:
    assert client_ip(_request(forwarded)) == expected
