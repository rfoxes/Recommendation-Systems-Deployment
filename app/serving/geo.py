from pathlib import Path

import maxminddb

from app.models import OS

DEFAULT_GEOIP_DB = Path(__file__).resolve().parents[2] / "data" / "GeoLite2-City-Test.mmdb"


class GeoIP:
    """IP -> ISO country code, from the MaxMind database held in memory."""

    def __init__(self, path: Path = DEFAULT_GEOIP_DB) -> None:
        self._reader = maxminddb.open_database(str(path), maxminddb.MODE_MEMORY)

    def country(self, ip: str | None) -> str | None:
        if not ip:
            return None
        try:
            record = self._reader.get(ip)
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        country = record.get("country") or record.get("registered_country") or {}
        code = country.get("iso_code") if isinstance(country, dict) else None
        return code if isinstance(code, str) else None

    def close(self) -> None:
        self._reader.close()


def os_from_user_agent(user_agent: str | None) -> OS | None:
    """The two OSes campaigns target; anything else (desktop, bots, curl) is None."""
    if not user_agent:
        return None
    ua = user_agent.lower()
    if "android" in ua:
        return "android"
    if any(token in ua for token in ("iphone", "ipad", "ipod", "ios")):
        return "ios"
    return None
