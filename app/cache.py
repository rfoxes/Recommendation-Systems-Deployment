from redis.asyncio import Redis

from app.config import Settings

# Bumped on every change to anything servable (campaigns, ad sets, variants), so each app
# instance can tell whether its in-memory catalog is stale with a single cheap GET.
CATALOG_VERSION_KEY = "catalog:version"


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


async def bump_catalog_version(redis: Redis) -> None:
    await redis.incr(CATALOG_VERSION_KEY)
