from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.config import get_settings
from app.ratelimit import RateLimiter
from app.request_context import client_ip
from app.sessions.store import SessionStore

router = APIRouter(tags=["sessions"])


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ppid: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)] | None = None


class SessionCreated(BaseModel):
    session_id: str


def get_session_store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.session_store
    return store


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


@router.post(
    "/session/create",
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "The caller's session is still active and was returned"}},
)
async def create_session(
    request: Request,
    response: Response,
    store: Annotated[SessionStore, Depends(get_session_store)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    body: Annotated[SessionCreate | None, Body()] = None,
) -> SessionCreated:
    """Resolve the caller to a user (ppid, else IP) and return their active session, minting one if needed."""
    ip = client_ip(request)
    if ip is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="could not determine the caller's IP address")

    settings = get_settings()
    limit = await limiter.hit(
        "session_create", ip, limit=settings.session_create_limit, window=settings.session_create_window_seconds
    )
    if not limit.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many sessions created from this IP; slow down",
            headers={"Retry-After": str(limit.retry_after)},
        )

    ppid = body.ppid if body else None
    session_id, created = await store.get_or_create("ppid", ppid) if ppid else await store.get_or_create("ip", ip)
    if not created:
        response.status_code = status.HTTP_200_OK
    return SessionCreated(session_id=session_id)
