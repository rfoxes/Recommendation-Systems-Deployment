"""GET /demo: the README's test harness (template/test_harness.html), wired to real ads.

The harness scrolls an ad slot into view and simulates backgrounding/suspension; here the slot shows an ad
served by this API. A browser can't set its own IP or User-Agent, so POST /demo/serve takes the simulated
country IP and device, then runs the exact production path (session -> /load/native logic -> serve record,
feature counters and clicks) and adds debug info: why the ranker picked this ad and where the copy came from.
Disable with DEMO_ENABLED=false.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.ratelimit import RateLimiter
from app.request_context import client_ip
from app.serving.router import get_ad_server, get_serve_writer
from app.serving.serve_writer import ServeWriter
from app.serving.service import AdContext, AdServer, LoadNativeRequest
from app.sessions.router import get_rate_limiter, get_session_store
from app.sessions.store import SessionStore

PAGE = Path(__file__).with_name("demo.html")
DEVICE_USER_AGENTS = {
    "ios": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
    "android": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Mobile Safari/537.36",
    "desktop": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15",
}

router = APIRouter(prefix="/demo", tags=["demo"])


def _require_demo() -> None:
    if not get_settings().demo_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND)


class DemoServeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ip: str = Field(description="Simulated caller IP, e.g. one of the README's test IPs")
    device: Literal["ios", "android", "desktop"] = "ios"
    ppid: str | None = Field(default=None, max_length=256)
    position: int = Field(default=3, ge=0)
    context: AdContext | None = None


@router.get("", response_class=HTMLResponse, dependencies=[Depends(_require_demo)])
async def demo_page() -> str:
    return PAGE.read_text()


@router.post("/serve", dependencies=[Depends(_require_demo)])
async def demo_serve(
    body: DemoServeRequest,
    request: Request,
    background: BackgroundTasks,
    server: Annotated[AdServer, Depends(get_ad_server)],
    writer: Annotated[ServeWriter, Depends(get_serve_writer)],
    sessions: Annotated[SessionStore, Depends(get_session_store)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> dict[str, Any]:
    settings = get_settings()
    limit = await limiter.hit(
        "demo", client_ip(request) or "unknown", limit=120, window=settings.session_create_window_seconds
    )
    if not limit.allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, headers={"Retry-After": str(limit.retry_after)})

    session_id, created = (
        await sessions.get_or_create("ppid", body.ppid) if body.ppid else await sessions.get_or_create("ip", body.ip)
    )
    serve_request = LoadNativeRequest(position=body.position, session_id=session_id, context=body.context)
    result = await server.serve(serve_request, body.ip, DEVICE_USER_AGENTS[body.device])
    if result is None:
        return {"session_id": session_id, "session_created": created, "status": 204}

    async def record() -> None:
        await server.after_response(result)
        writer.record(result.serve)

    background.add_task(record)
    serve = result.serve
    return {
        "session_id": session_id,
        "session_created": created,
        "status": 200,
        **result.response.model_dump(),
        "debug": {
            "country": serve.country,
            "os": serve.os,
            "campaign_id": serve.campaign_id,
            "variant_id": serve.variant_id,
            "ranking_reason": serve.ranking_reason,
            "copy_source": serve.copy_source,
            "candidates": [c.model_dump() for c in serve.candidates],
            "latency_ms": serve.latency_ms,
        },
    }
