"""GET /ready: is every dependency actually working, or would serving fall back?

Unlike /health (is the process up), this checks MongoDB, Redis and Temporal, and reports the CTR model,
the LLM provider and how much ad copy has been pre-generated. 503 if MongoDB or Redis is down.
Browsers get a simple status page; scripts (or ?format=json) get JSON.
"""

import asyncio
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse

from app.campaigns.cache import CampaignCache
from app.db import AD_VARIANTS, Database
from app.ranking.model_files import MODEL_RELEASE, MODEL_REPO

router = APIRouter(tags=["health"])

READY_PAGE = Path(__file__).parent / "web" / "ready.html"


async def _check(coro: Any) -> str:
    try:
        await asyncio.wait_for(coro, timeout=2)
        return "ok"
    except Exception as exc:  # report, don't raise: this endpoint exists to describe failures
        return f"error: {type(exc).__name__}"


def _state(request: Request) -> Any:
    return request.app.state


@router.get("/ready", response_model=None)
async def ready(
    request: Request, response: Response, state: Annotated[Any, Depends(_state)], format: str | None = None
) -> dict[str, Any] | HTMLResponse:
    if format != "json" and "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(READY_PAGE.read_text())
    db: Database = state.db
    cache: CampaignCache = state.campaign_cache
    mongo = await _check(db.command("ping"))
    redis = await _check(state.redis.ping())
    temporal = state.temporal
    copy = state.copy_generator
    variants = await db[AD_VARIANTS].count_documents({"active": True}) if mongo == "ok" else None
    with_copy = await db[AD_VARIANTS].count_documents({"active": True, "copy_pool.0": {"$exists": True}}) if mongo == "ok" else None
    if mongo != "ok" or redis != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "mongo": mongo,
        "redis": redis,
        "temporal": "disabled" if temporal is None else ("connected" if temporal.connected else "connecting"),
        "ctr_model": {"source": f"{MODEL_REPO}@{MODEL_RELEASE}", "golden_sample_max_diff": state.ctr_model_drift},
        "llm": {"provider": copy.provider, "model": copy.model} if copy.enabled else "not configured: ads use fallback copy",
        "ad_copy": {"active_variants": variants, "with_pregenerated_lines": with_copy},
        "campaign_cache_last_refresh": await cache.last_refresh() if redis == "ok" else None,
    }
