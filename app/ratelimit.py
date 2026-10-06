"""Fixed-window rate limiting in Redis: at most `limit` hits per key per `window` seconds."""

import logging
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Count the hit and start the window's expiry in one atomic step.
_HIT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {count, redis.call('TTL', KEYS[1])}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int  # seconds until the window resets (0 when allowed)


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._hit = redis.register_script(_HIT)

    async def hit(self, name: str, identity: str, *, limit: int, window: int) -> RateLimitResult:
        try:
            count, ttl = await self._hit(keys=[f"ratelimit:{name}:{identity}"], args=[window])
        except RedisError:
            # Fail open: an unavailable limiter shouldn't take the endpoint down with it.
            logger.warning("Rate limiter unavailable; allowing request", exc_info=True)
            return RateLimitResult(allowed=True, retry_after=0)
        if int(count) <= limit:
            return RateLimitResult(allowed=True, retry_after=0)
        return RateLimitResult(allowed=False, retry_after=max(int(ttl), 1))
