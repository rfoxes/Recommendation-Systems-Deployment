"""Redis cache of every campaign, kept in sync with MongoDB (the source of truth).

Keys:
  campaigns              hash  campaign_id -> campaign JSON
  campaigns:versions     hash  campaign_id -> version, so out-of-order writes can't go backwards
  campaigns:loaded       flag  set once a full load from MongoDB has completed
  campaigns:deleted:<id> flag  short-lived tombstone so a late write can't resurrect a delete
  catalog:version        int   bumped on every change (see app.cache)
"""

import secrets

from redis.asyncio import Redis

from app.cache import CATALOG_VERSION_KEY, text
from app.models import Campaign, utcnow

CAMPAIGNS_KEY = "campaigns"
VERSIONS_KEY = "campaigns:versions"
LOADED_KEY = "campaigns:loaded"
LAST_REFRESH_KEY = "campaigns:last_refresh"
TOMBSTONE_PREFIX = "campaigns:deleted:"
TOMBSTONE_TTL_SECONDS = 300
TMP_TTL_SECONDS = 60

# Write a campaign only if it is newer than the cached copy and has not just been deleted.
_PUT = """
if redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
local cached = redis.call('HGET', KEYS[2], ARGV[1])
if cached and tonumber(cached) >= tonumber(ARGV[3]) then return 0 end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[3])
redis.call('INCR', KEYS[4])
return 1
"""

_REMOVE = """
redis.call('HDEL', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('SET', KEYS[3], '1', 'EX', ARGV[2])
redis.call('INCR', KEYS[4])
return 1
"""

# Atomic swap: replace the live hashes with freshly built ones in a single step, so readers see
# the complete old cache or the complete new one, never an empty or half-written one. If any
# write happened after the snapshot was read from MongoDB, the snapshot may be stale: discard it
# and let the caller retry.
_SWAP = """
if (redis.call('GET', KEYS[5]) or '0') ~= ARGV[1] then
  redis.call('DEL', KEYS[1], KEYS[2])
  return 0
end
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('RENAME', KEYS[1], KEYS[3])
  redis.call('RENAME', KEYS[2], KEYS[4])
  redis.call('PERSIST', KEYS[3])
  redis.call('PERSIST', KEYS[4])
else
  redis.call('DEL', KEYS[3], KEYS[4])
end
redis.call('SET', KEYS[6], '1')
redis.call('INCR', KEYS[5])
return 1
"""


def _tombstone(campaign_id: str) -> str:
    return f"{TOMBSTONE_PREFIX}{campaign_id}"


class CampaignCache:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._put = redis.register_script(_PUT)
        self._remove = redis.register_script(_REMOVE)
        self._swap = redis.register_script(_SWAP)

    async def get(self, campaign_id: str) -> Campaign | None:
        raw = await self._redis.hget(CAMPAIGNS_KEY, campaign_id)
        return Campaign.model_validate_json(raw) if raw else None

    async def get_all(self) -> list[Campaign] | None:
        """Every cached campaign, or None if the cache has not been fully loaded yet."""
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.exists(LOADED_KEY)
            pipe.hvals(CAMPAIGNS_KEY)
            loaded, values = await pipe.execute()
        if not loaded:
            return None
        return [Campaign.model_validate_json(value) for value in values]

    async def put(self, campaign: Campaign) -> bool:
        """Cache a campaign unless the cache already holds a newer version. Returns True if written."""
        written = await self._put(
            keys=[CAMPAIGNS_KEY, VERSIONS_KEY, _tombstone(campaign.campaign_id), CATALOG_VERSION_KEY],
            args=[campaign.campaign_id, campaign.model_dump_json(), campaign.version],
        )
        return bool(written)

    async def remove(self, campaign_id: str) -> None:
        await self._remove(
            keys=[CAMPAIGNS_KEY, VERSIONS_KEY, _tombstone(campaign_id), CATALOG_VERSION_KEY],
            args=[campaign_id, TOMBSTONE_TTL_SECONDS],
        )

    async def catalog_version(self) -> str:
        return text(await self._redis.get(CATALOG_VERSION_KEY) or "0")

    async def replace_all(self, campaigns: list[Campaign], expected_version: str) -> bool:
        """Atomically replace the whole cache, unless catalog_version moved past expected_version."""
        suffix = secrets.token_hex(4)
        tmp_campaigns, tmp_versions = f"{CAMPAIGNS_KEY}:tmp:{suffix}", f"{VERSIONS_KEY}:tmp:{suffix}"
        if campaigns:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.hset(tmp_campaigns, mapping={c.campaign_id: c.model_dump_json() for c in campaigns})
                pipe.hset(tmp_versions, mapping={c.campaign_id: c.version for c in campaigns})
                # Clean up after ourselves if we die before the swap; the swap removes the TTL.
                pipe.expire(tmp_campaigns, TMP_TTL_SECONDS)
                pipe.expire(tmp_versions, TMP_TTL_SECONDS)
                await pipe.execute()
        swapped = await self._swap(
            keys=[tmp_campaigns, tmp_versions, CAMPAIGNS_KEY, VERSIONS_KEY, CATALOG_VERSION_KEY, LOADED_KEY],
            args=[expected_version],
        )
        if swapped:
            await self._redis.set(LAST_REFRESH_KEY, utcnow().isoformat())
        return bool(swapped)

    async def last_refresh(self) -> str | None:
        value = await self._redis.get(LAST_REFRESH_KEY)
        return text(value) if value else None
