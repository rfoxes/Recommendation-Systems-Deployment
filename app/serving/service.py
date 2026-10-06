"""POST /load/native, step by step (README "Ad Serving"):

  session -> country + OS -> eligible campaigns (geo, OS, store link, brand safety) -> features -> CTR model
  -> ranker -> random ad set + variant (fatigue-capped) -> copy -> rendered template -> serve record

Everything on this path is in memory or a single Redis round trip, except a live LLM call when a variant
has no pre-generated lines yet. Recording the serve happens after the response is sent.
"""

import logging
import random
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.catalog import ActiveCatalog, ServableCampaign
from app.config import Settings
from app.copywriting.generator import CopyGenerator
from app.features.store import FeatureStore, ServeEvent
from app.models import OS, SAFETY_RANK, Campaign, Serve, ServeCandidate, new_id
from app.ranking.ctr_model import CTRModel
from app.ranking.features import RequestContext, context_genre, context_key, finite, model_rows
from app.ranking.ranker import Decision, RankInput, rank
from app.serving.geo import GeoIP, os_from_user_agent
from app.serving.render import AdTemplate
from app.sessions.store import Session, SessionStore

logger = logging.getLogger(__name__)


class AdContext(BaseModel):
    """Relevance signals about the chat the ad will appear in (all optional)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    search_term: str | None = Field(default=None, alias="searchTerm")
    tags: list[str] = []
    category: str | None = None
    title: str | None = None
    nsfw: bool = False


class LoadNativeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int = Field(ge=0, description="Feed index of the ad slot")
    session_id: str = Field(min_length=1, max_length=64)
    context: AdContext | None = None


class LoadNativeResponse(BaseModel):
    impression_id: str
    rendered_html: str


class UnknownSessionError(LookupError):
    pass


@dataclass(frozen=True)
class ServeResult:
    response: LoadNativeResponse
    serve: Serve
    event: ServeEvent
    session: Session


def store_url(campaign: Campaign, os: OS | None) -> str | None:
    urls = {"ios": campaign.ios_store_url, "android": campaign.android_store_url}
    url = urls[os] if os else (campaign.ios_store_url or campaign.android_store_url)
    return str(url) if url else None


def eligible(item: ServableCampaign, country: str | None, os: OS | None, safety_tier: str) -> bool:
    """README steps 3: geo and OS targeting (empty targets = everywhere), plus a store link for this OS and the
    advertiser's brand-safety tier."""
    campaign = item.campaign
    if campaign.geo_targets and country not in campaign.geo_targets:
        return False
    if campaign.os_targets and os not in campaign.os_targets:
        return False
    if store_url(campaign, os) is None:
        return False
    return SAFETY_RANK[safety_tier] <= SAFETY_RANK[campaign.max_safety_tier]


def author_handle(campaign_name: str) -> str:
    """The template shows "@" + this as the author: the brand part of the campaign name."""
    return campaign_name.split(" — ")[0].split(" - ")[0].strip()


class AdServer:
    def __init__(
        self,
        settings: Settings,
        sessions: SessionStore,
        catalog: ActiveCatalog,
        features: FeatureStore,
        model: CTRModel,
        copy: CopyGenerator,
        template: AdTemplate,
        geoip: GeoIP,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._catalog = catalog
        self._features = features
        self._model = model
        self._copy = copy
        self._template = template
        self._geoip = geoip
        self._rng = rng or random.Random()
        self._known_genres = set(model.known_values("genre"))
        self._training_ctr = float(model.manifest["training"]["click_rate"])

    async def serve(self, body: LoadNativeRequest, ip: str | None, user_agent: str | None) -> ServeResult | None:
        """The ad for this slot, or None when no campaign is eligible (no fill)."""
        started = time.perf_counter()
        session = await self._sessions.get(body.session_id)
        if session is None:
            raise UnknownSessionError(body.session_id)

        ctx = body.context or AdContext()
        os = os_from_user_agent(user_agent)
        country = self._geoip.country(ip)
        request = RequestContext(
            user_from_ppid=session.id_source == "ppid",
            os=os,
            genre=context_genre(self._known_genres, ctx.category, ctx.tags, ctx.title),
            safety_tier="mature" if ctx.nsfw else "sfw",
            context_key=context_key(ctx.title, ctx.category),
        )
        candidates = [c for c in await self._catalog.campaigns() if eligible(c, country, os, request.safety_tier)]
        if not candidates:
            return None

        by_id = {c.campaign.campaign_id: c for c in candidates}
        snapshot = await self._features.snapshot(session.user_id, list(by_id), request.context_key)
        rows = model_rows(snapshot, list(by_id), request, fallback_prior=self._training_ctr)
        seen = Counter(e.variant_id for e in snapshot.seen_24h)
        seen_campaigns = {e.campaign_id for e in snapshot.seen_24h}

        # Fatigue: a variant shown to this user `fatigue_cap` times in 24h is out; so is a campaign with none left.
        variants = {
            cid: [
                (s.ad_set, v)
                for s in item.ad_sets
                for v in s.variants
                if seen[v.variant_id] < self._settings.fatigue_cap
            ]
            for cid, item in by_id.items()
        }
        open_ids = [cid for cid in by_id if variants[cid]]
        if not open_ids:
            return None

        decision, scored = self._rank(open_ids, rows, list(by_id), snapshot, seen_campaigns)
        chosen = by_id[decision.campaign_id]
        ad_set, variant = self._rng.choice(variants[decision.campaign_id])  # README step 5: random ad set + variant
        message, copy_source = await self._message(
            variant.copy_pool, variant.character_name, variant.ai_prompt, ad_set.fallback_copy
        )

        impression_id = new_id("imp")
        campaign = chosen.campaign
        html = self._template.render(
            {
                "CHAR_NAME": variant.character_name,
                "CAMPAIGN": author_handle(campaign.campaign_name),
                "CHAR_MESSAGE": message,
                "CTA": variant.cta,
                "MEDIA_URL": str(variant.video_url),
                "TRACKING_URL": store_url(campaign, os) or "",
                "IMPRESSION_URL": "",
                "AD_ID": impression_id,
                "API_URL": self._settings.api_url.rstrip("/"),
                "API_KEY": self._settings.click_api_key.get_secret_value(),
                "THEME": self._settings.ad_theme,
                "DOWNLOADS": campaign.downloads_label or "",
            }
        )
        chosen_row = rows[list(by_id).index(decision.campaign_id)]
        serve = Serve(
            impression_id=impression_id,
            session_id=session.session_id,
            user_id=session.user_id,
            campaign_id=campaign.campaign_id,
            ad_set_id=ad_set.ad_set_id,
            variant_id=variant.variant_id,
            position=body.position,
            context=ctx.model_dump(by_alias=True),
            country=country,
            os=os,
            pctr=scored.get(decision.campaign_id, {}).get("p_v1"),
            ranking_reason=decision.reason,
            candidates=[ServeCandidate(campaign_id=cid, **s) for cid, s in scored.items()],
            features=finite(chosen_row),
            char_message=message,
            copy_source=copy_source,
            latency_ms=round(1000 * (time.perf_counter() - started), 2),
        )
        event = ServeEvent(
            impression_id, session.user_id, campaign.campaign_id, variant.variant_id, request.context_key
        )
        return ServeResult(LoadNativeResponse(impression_id=impression_id, rendered_html=html), serve, event, session)

    async def after_response(self, result: ServeResult) -> None:
        """Count the impression (features, fatigue), keep the session alive, and queue the serve record."""
        try:
            await self._features.record_serve(result.event)
            await self._sessions.touch(result.session)  # README: ad serves are the session's activity
        except Exception:
            logger.exception("Failed to record serve %s in Redis", result.event.impression_id)

    def _rank(
        self,
        open_ids: list[str],
        rows: list[dict[str, Any]],
        all_ids: list[str],
        snapshot: Any,
        seen_campaigns: set[str],
    ) -> tuple[Decision, dict[str, dict[str, float]]]:
        open_rows = [rows[all_ids.index(cid)] for cid in open_ids]
        try:
            prediction = self._model.predict(open_rows)
        except Exception:  # never fail an ad request because of the model
            logger.exception("CTR model failed; picking at random among eligible campaigns")
            return Decision(self._rng.choice(open_ids), "model_unavailable", []), {}
        z, u, p_v1 = prediction.z, prediction.uncertainty, prediction.p_v1
        inputs = [
            RankInput(
                campaign_id=cid,
                p_v1=float(p_v1[i]),
                z_model=float(z[i]),
                u_model=float(u[i]),
                seen_campaign_24h=cid in seen_campaigns,
                impressions_24h=snapshot.campaigns[cid].recent_impressions(snapshot.hour, 24),
                impressions_72h=snapshot.campaigns[cid].recent_impressions(snapshot.hour, 72),
            )
            for i, cid in enumerate(open_ids)
        ]
        decision = rank(inputs, self._rng, self._settings.toss_up_explore_share, self._settings.cold_explore_share)
        scored = {
            r.campaign_id: {
                "p_v1": float(p_v1[i]),
                "p_lightgbm": float(prediction.p_lightgbm[i]),
                "p_fm": float(prediction.p_fm[i]),
                "score": r.score,
                "uncertainty": r.uncertainty,
            }
            for r in decision.ranked
            for i in [open_ids.index(r.campaign_id)]
        }
        return decision, scored

    async def _message(self, pool: list[str], character: str, ai_prompt: str, fallback: list[str]) -> tuple[str, str]:
        """README step 6: the LLM's line (pre-generated if possible), else the ad set's fallback copy."""
        if pool:
            return self._rng.choice(pool), "pool"
        if self._copy.enabled:
            try:
                line = await self._copy.line(character, ai_prompt, timeout=self._settings.live_copy_timeout_seconds)
                return line, "llm"
            except Exception:
                logger.warning("Live copy generation failed; using fallback copy", exc_info=True)
        return self._rng.choice(fallback), "fallback"
