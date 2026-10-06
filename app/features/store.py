"""Online feature store in Redis: the history features the CTR model was trained on, from our own serves
and clicks, with the same definitions as the model repo's src/history.py (hourly buckets; history uses
only hours strictly before the request's hour; click rates smoothed toward a recent global prior).

Keys (hash fields `n:<hour>` / `c:<hour>` = impressions / clicks in that UTC hour, kept 72h):
  fs:global              hourly buckets + n_all, c_all: smoothing priors and traffic volume
  fs:user:<user>         hourly buckets + n_all, c_all, last_hour, prev_hour, ctx:<context> counts
  fs:user:<user>:seen    sorted set of the user's exposures in the last 24h, "<campaign>|<variant>|<impression>"
  fs:campaign:<id>       hourly buckets (the model's advertiser/creative rates)
  fs:context:<key>       hourly buckets for the chat context (the model's "character")
  fs:imp:<impression>    what a click needs to attribute itself, kept 24h (the attribution window)
Each serve and each click is a single atomic script call.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from redis.asyncio import Redis

from app.cache import text

HOURS_KEPT = 72
ATTRIBUTION_SECONDS = 24 * 3600
ENTITY_TTL_SECONDS = 30 * 24 * 3600

_RECORD_SERVE = """
local hour = tonumber(ARGV[1])
local oldest = hour - tonumber(ARGV[2])
local function count(key)
  redis.call('HINCRBY', key, 'n:' .. hour, 1)
  redis.call('HINCRBY', key, 'n_all', 1)
  for _, f in ipairs(redis.call('HKEYS', key)) do
    -- Only hourly fields (n:<hour>, c:<hour>); check the match before tonumber, which errors on nil in
    -- some Lua engines (Upstash), unlike Redis's own.
    local hour_field = string.match(f, '^[nc]:(%d+)$')
    if hour_field and tonumber(hour_field) < oldest then redis.call('HDEL', key, f) end
  end
  redis.call('EXPIRE', key, ARGV[3])
end
-- KEYS: 1 global, 2 user, 3 seen, 4 campaign, 5 context, 6 impression
count(KEYS[1]); count(KEYS[2]); count(KEYS[4])
if ARGV[9] ~= '' then
  count(KEYS[5])
  redis.call('HINCRBY', KEYS[2], 'ctx:' .. ARGV[9], 1)
end
local last = tonumber(redis.call('HGET', KEYS[2], 'last_hour') or '-1')
if last ~= hour then redis.call('HSET', KEYS[2], 'prev_hour', last, 'last_hour', hour) end
local now = tonumber(ARGV[4])
redis.call('ZADD', KEYS[3], now, ARGV[5] .. '|' .. ARGV[6] .. '|' .. ARGV[7])
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now - 86400)
redis.call('EXPIRE', KEYS[3], 86400)
redis.call('HSET', KEYS[6], 'hour', hour, 'user', ARGV[8], 'campaign', ARGV[5], 'variant', ARGV[6], 'context', ARGV[9])
redis.call('EXPIRE', KEYS[6], ARGV[10])
return 1
"""

# First click per impression only; the click counts toward the hour the ad was served (as in training).
_RECORD_CLICK = """
if redis.call('HSETNX', KEYS[1], 'clicked', 1) == 0 then return 0 end
local hourly = 'c:' .. ARGV[1]
-- KEYS: 1 impression, 2 global, 3 user, 4 campaign, 5 context
for i = 2, 5 do
  if i ~= 5 or ARGV[2] ~= '' then
    redis.call('HINCRBY', KEYS[i], hourly, 1)
    redis.call('HINCRBY', KEYS[i], 'c_all', 1)
  end
end
return 1
"""


def current_hour(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // 3600)


@dataclass(frozen=True)
class Buckets:
    """Hourly impressions/clicks for one key, as read from its hash."""

    n: dict[int, int] = field(default_factory=dict)
    c: dict[int, int] = field(default_factory=dict)
    n_all: int = 0
    c_all: int = 0
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: dict[str, str]) -> "Buckets":
        n: dict[int, int] = {}
        c: dict[int, int] = {}
        extra: dict[str, str] = {}
        for key, value in raw.items():
            kind, _, hour = key.partition(":")
            if kind in ("n", "c") and hour.isdigit():
                (n if kind == "n" else c)[int(hour)] = int(value)
            else:
                extra[key] = value
        return cls(n=n, c=c, n_all=int(extra.get("n_all", 0)), c_all=int(extra.get("c_all", 0)), extra=extra)

    def window(self, hour: int, hours: int) -> tuple[int, int]:
        """(impressions, clicks) in the `hours` hours strictly before `hour`."""
        span = range(hour - hours, hour)
        return sum(self.n.get(h, 0) for h in span), sum(self.c.get(h, 0) for h in span)

    def before(self, hour: int) -> tuple[int, int]:
        """All-time (impressions, clicks) strictly before `hour`."""
        return self.n_all - self.n.get(hour, 0), self.c_all - self.c.get(hour, 0)

    def recent_impressions(self, hour: int, hours: int) -> int:
        """Impressions in the last `hours` hours including the current one (for exploration, not the model)."""
        return sum(self.n.get(h, 0) for h in range(hour - hours + 1, hour + 1))


@dataclass(frozen=True)
class Exposure:
    campaign_id: str
    variant_id: str


@dataclass(frozen=True)
class FeatureSnapshot:
    """Everything the features of one ad request need, read in a single Redis round trip."""

    hour: int
    global_: Buckets
    user: Buckets
    seen_24h: list[Exposure]
    campaigns: dict[str, Buckets]
    context: Buckets | None


@dataclass(frozen=True)
class ServeEvent:
    impression_id: str
    user_id: str
    campaign_id: str
    variant_id: str
    context_key: str  # "" when the request had no usable context


class FeatureStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._record_serve = redis.register_script(_RECORD_SERVE)
        self._record_click = redis.register_script(_RECORD_CLICK)

    async def snapshot(self, user_id: str, campaign_ids: Sequence[str], context_key: str) -> FeatureSnapshot:
        now = time.time()
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.hgetall("fs:global")
            pipe.hgetall(f"fs:user:{user_id}")
            pipe.zrangebyscore(f"fs:user:{user_id}:seen", now - 86400, "+inf")
            for campaign_id in campaign_ids:
                pipe.hgetall(f"fs:campaign:{campaign_id}")
            if context_key:
                pipe.hgetall(f"fs:context:{context_key}")
            results = await pipe.execute()
        global_raw, user_raw, seen_raw, *rest = results
        campaigns = {cid: Buckets.parse(raw) for cid, raw in zip(campaign_ids, rest[: len(campaign_ids)], strict=True)}
        seen = [Exposure(*member.split("|")[:2]) for member in seen_raw]
        return FeatureSnapshot(
            hour=current_hour(now),
            global_=Buckets.parse(global_raw),
            user=Buckets.parse(user_raw),
            seen_24h=seen,
            campaigns=campaigns,
            context=Buckets.parse(rest[-1]) if context_key else None,
        )

    async def record_serve(self, event: ServeEvent) -> None:
        now = time.time()
        await self._record_serve(
            keys=[
                "fs:global",
                f"fs:user:{event.user_id}",
                f"fs:user:{event.user_id}:seen",
                f"fs:campaign:{event.campaign_id}",
                f"fs:context:{event.context_key}",
                f"fs:imp:{event.impression_id}",
            ],
            args=[
                current_hour(now),
                HOURS_KEPT,
                ENTITY_TTL_SECONDS,
                now,
                event.campaign_id,
                event.variant_id,
                event.impression_id,
                event.user_id,
                event.context_key,
                ATTRIBUTION_SECONDS,
            ],
        )

    async def record_click(self, impression_id: str) -> bool | None:
        """True for the first click, False for a repeat, None if the impression is unknown or past attribution."""
        raw = await self._redis.hgetall(f"fs:imp:{impression_id}")
        if not raw:
            return None
        impression = {text(k): text(v) for k, v in raw.items()}
        context = impression.get("context", "")
        recorded = await self._record_click(
            keys=[
                f"fs:imp:{impression_id}",
                "fs:global",
                f"fs:user:{impression['user']}",
                f"fs:campaign:{impression['campaign']}",
                f"fs:context:{context}",
            ],
            args=[impression["hour"], context],
        )
        return bool(recorded)
