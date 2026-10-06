"""Builds the CTR model's input rows for an ad request, from the request and the feature store.

The model was trained on a different setup (the model repo's data: an ad is a creative C14 from an
advertiser C17, shown while a user chats with a companion character). Mapping to our requests:
  - companion character  <- the request's `context` (genre from category/tags/title, safety tier from nsfw)
  - advertiser/creative  <- our campaign (the variant is picked after ranking, per the README)
  - user history, recent click rates, traffic volume <- our own serves and clicks (app.features.store)
  - publisher, device model and the hashed C-columns have no equivalent: left missing, which the model
    treats as unseen values
"""

import math
import re
from dataclasses import dataclass
from typing import Any

from app.features.store import Buckets, FeatureSnapshot
from app.models import OS

SMOOTHING = 20  # HISTORY_SMOOTHING in the model repo
RECENT_HOURS = 24


@dataclass(frozen=True)
class RequestContext:
    """What the request tells us about the user and the chat they're in."""

    user_from_ppid: bool
    os: OS | None
    genre: str | None
    safety_tier: str  # "sfw" or "mature"
    context_key: str  # feature store key for this chat context ("" if none)


def _tokens(*texts: str | None) -> list[str]:
    return [t for text in texts if text for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def context_genre(known_genres: set[str], category: str | None, tags: list[str], title: str | None) -> str | None:
    """First token from category, tags or title that is a genre the model knows ("sci-fi" -> "sci")."""
    for token in _tokens(category, *tags, title):
        if token in known_genres:
            return token
    return None


def context_key(title: str | None, category: str | None) -> str:
    return ":".join(_tokens(title or category))[:120]


def _smoothed(clicks: int, impressions: int, prior: float) -> float:
    return (clicks + SMOOTHING * prior) / (impressions + SMOOTHING)


def _rate(buckets: Buckets, hour: int, prior: float) -> float:
    n, c = buckets.window(hour, RECENT_HOURS)
    return _smoothed(c, n, prior)


def model_rows(
    snapshot: FeatureSnapshot, campaign_ids: list[str], request: RequestContext, fallback_prior: float
) -> list[dict[str, Any]]:
    """One input row per candidate campaign (keys are the model's input columns)."""
    hour = snapshot.hour
    n_all, c_all = snapshot.global_.before(hour)
    n_recent, c_recent = snapshot.global_.window(hour, RECENT_HOURS)
    # Smoothing priors are our own global click rates; until we have traffic, the model's training rate.
    prior_all = c_all / n_all if n_all else fallback_prior
    prior_recent = c_recent / n_recent if n_recent else fallback_prior

    user = snapshot.user
    user_n, user_c = user.before(hour)
    last = int(user.extra.get("last_hour", -1))
    if last == hour:
        last = int(user.extra.get("prev_hour", -1))

    shared: dict[str, Any] = {
        # chat context -> the model's character
        "genre": request.genre,
        "safety_tier": request.safety_tier,
        # device: native app traffic on a phone; a ppid is the closest thing to a device id
        "device_type": 1 if request.os else None,
        "is_app": 1,
        "has_device_id": int(request.user_from_ppid),
        "hour_of_day": hour % 24,
        # user history
        "user_prior_impressions": user_n,
        "user_prior_clicks": user_c,
        "user_prior_ctr": _smoothed(user_c, user_n, prior_all),
        "user_hours_since_last": float(hour - last) if last >= 0 else None,
        "user_seen_before": int(user_n > 0),
        "user_impressions_24h": user.window(hour, RECENT_HOURS)[0],
        "user_character_prior_impressions": int(user.extra.get(f"ctx:{request.context_key}", 0))
        if request.context_key
        else 0,
        # traffic volume
        "total_impressions_prev_hour": snapshot.global_.n.get(hour - 1, 0),
        "character_ctr_24h": _rate(snapshot.context, hour, prior_recent) if snapshot.context else None,
    }
    rows = []
    for campaign_id in campaign_ids:
        campaign_rate = _rate(snapshot.campaigns[campaign_id], hour, prior_recent)
        # The variant isn't chosen until after ranking, so the creative rate is the campaign's.
        rows.append({**shared, "C17_ctr_24h": campaign_rate, "creative_ctr_24h": campaign_rate})
    return rows


def finite(row: dict[str, Any]) -> dict[str, Any]:
    """The row as stored with a serve: NaN-free and JSON-friendly."""
    return {k: v for k, v in row.items() if v is not None and not (isinstance(v, float) and math.isnan(v))}
