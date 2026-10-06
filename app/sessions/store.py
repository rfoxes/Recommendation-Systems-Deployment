"""Sessions live in Redis; expiry implements the README's 30-seconds-of-inactivity rule.

Keys:
  user_session:<user_id>  current session id, expires after `inactivity` seconds without an ad serve
  session:<session_id>    the session's record (user id, ...), kept ~24h so serves can resolve it
"""

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from redis.asyncio import Redis

from app.models import new_id, utcnow

# Return the user's current session, or create one: a single atomic step, so concurrent
# requests for the same user can never mint two sessions.
_GET_OR_CREATE = """
local current = redis.call('GET', KEYS[1])
if current then return {current, 0} end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4])
return {ARGV[1], 1}
"""

# Extend the inactivity window, but only if this is still the user's current session:
# a stale session id can't keep a session alive or revive one that already expired.
_TOUCH = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""

IdSource = Literal["ppid", "ip"]


class Session(BaseModel):
    session_id: str
    user_id: str
    id_source: IdSource
    created_at: datetime


def resolve_user_id(source: IdSource, value: str) -> str:
    """A stable user id for a ppid or IP. Hashed, so raw identifiers aren't used as keys."""
    return "user_" + hashlib.sha256(f"{source}:{value}".encode()).hexdigest()[:16]


def _user_key(user_id: str) -> str:
    return f"user_session:{user_id}"


def _session_key(session_id: str) -> str:
    return f"session:{session_id}"


class SessionStore:
    def __init__(self, redis: Redis, *, inactivity_seconds: int, record_ttl_seconds: int) -> None:
        self._redis = redis
        self._inactivity = inactivity_seconds
        self._record_ttl = record_ttl_seconds
        self._get_or_create = redis.register_script(_GET_OR_CREATE)
        self._touch = redis.register_script(_TOUCH)

    async def get_or_create(self, source: IdSource, value: str) -> tuple[str, bool]:
        """The user's active session id, minting one if none is active. Returns (session_id, created)."""
        user_id = resolve_user_id(source, value)
        candidate = Session(session_id=new_id("sess"), user_id=user_id, id_source=source, created_at=utcnow())
        session_id, created = await self._get_or_create(
            keys=[_user_key(user_id), _session_key(candidate.session_id)],
            args=[candidate.session_id, self._inactivity, candidate.model_dump_json(), self._record_ttl],
        )
        return session_id, bool(created)

    async def get(self, session_id: str) -> Session | None:
        raw = await self._redis.get(_session_key(session_id))
        return Session.model_validate_json(raw) if raw else None

    async def touch(self, session: Session) -> bool:
        """Record activity (an ad serve). Returns False if the session had already gone inactive."""
        return bool(await self._touch(keys=[_user_key(session.user_id)], args=[session.session_id, self._inactivity]))
