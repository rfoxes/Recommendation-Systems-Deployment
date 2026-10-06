"""Safe retries for POST requests via the `Idempotency-Key` header.

The key and the response are saved in the same MongoDB transaction as the resource itself, so
either both exist or neither does. A retry with the same key and body returns the saved
response instead of creating a duplicate; the same key with a different body is rejected.
Keys expire after 24 hours (TTL index in app.db).
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from pymongo.asynchronous.client_session import AsyncClientSession

from app.db import IDEMPOTENCY_KEYS, Database
from app.models import utcnow


class IdempotencyKeyReusedError(ValueError):
    def __init__(self) -> None:
        super().__init__("Idempotency-Key was already used with a different request body")


@dataclass(frozen=True)
class IdempotentRequest:
    scope: str  # e.g. "POST /adsets", so one key can't collide across endpoints
    key: str
    body_hash: str

    @classmethod
    def build(cls, scope: str, key: str | None, body: BaseModel) -> "IdempotentRequest | None":
        if key is None:
            return None
        canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return cls(scope=scope, key=key, body_hash=hashlib.sha256(canonical.encode()).hexdigest())

    @property
    def doc_id(self) -> str:
        return f"{self.scope}:{self.key}"


class IdempotencyStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def saved_response(self, request: IdempotentRequest | None) -> dict[str, Any] | None:
        """The response saved for this key, or None if the key is new."""
        if request is None:
            return None
        doc = await self._db[IDEMPOTENCY_KEYS].find_one({"_id": request.doc_id})
        if doc is None:
            return None
        if doc["body_hash"] != request.body_hash:
            raise IdempotencyKeyReusedError()
        response: dict[str, Any] = doc["response"]
        return response

    async def save(
        self, request: IdempotentRequest | None, response: dict[str, Any], session: AsyncClientSession
    ) -> None:
        """Record the key inside the caller's transaction. A concurrent duplicate raises DuplicateKeyError."""
        if request is None:
            return
        await self._db[IDEMPOTENCY_KEYS].insert_one(
            {"_id": request.doc_id, "body_hash": request.body_hash, "response": response, "created_at": utcnow()},
            session=session,
        )
