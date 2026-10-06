"""Stored document models for the four collections: campaigns, ad sets, ad variants, serves."""

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, PlainSerializer, StringConstraints

OS = Literal["ios", "android"]
Surface = Literal["native"]
CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]  # ISO 3166-1 alpha-2
# Validated as a URL, stored as a plain string so it can be written to the database as-is.
Url = Annotated[HttpUrl, PlainSerializer(str, return_type=str)]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def utcnow() -> datetime:
    """Current UTC time at millisecond precision, the precision MongoDB stores."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class Document(BaseModel):
    model_config = ConfigDict(extra="ignore")

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class CampaignFields(BaseModel):
    """The client-settable campaign fields. Empty geo/OS/publisher targets mean "no restriction"."""

    campaign_name: NonEmptyStr
    advertiser_company_id: NonEmptyStr
    surface: Surface = "native"
    publisher_ids: list[str] = []
    daily_budget: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    geo_targets: list[CountryCode] = []
    os_targets: list[OS] = []
    attribution_provider: str | None = None
    ios_store_url: Url | None = None
    android_store_url: Url | None = None
    native_ad_set_ids: list[str] = []
    downloads_label: str | None = None  # shown on the ad, e.g. "1.2M"


class Campaign(Document, CampaignFields):
    """An advertiser's app or offer. campaign_id, active, version and timestamps are API-owned."""

    campaign_id: str
    active: bool = False
    version: int = Field(default=1, ge=1)  # bumped on every update; orders concurrent cache writes


class AdSet(Document):
    """A group of creative assets; its variants are the cartesian product of the asset lists."""

    ad_set_id: str
    campaign_id: str
    ad_set_name: NonEmptyStr
    active: bool = True
    character_names: list[NonEmptyStr] = Field(min_length=1)
    video_urls: list[Url] = Field(min_length=1)
    ctas: list[NonEmptyStr] = Field(min_length=1)
    ai_prompts: list[NonEmptyStr] = Field(min_length=1)
    fallback_copy: list[NonEmptyStr] = Field(min_length=1)


class AdVariant(Document):
    """One character + video + CTA + prompt combination; the unit that gets rendered."""

    variant_id: str
    ad_set_id: str
    campaign_id: str
    character_name: NonEmptyStr
    video_url: Url
    cta: NonEmptyStr
    ai_prompt: NonEmptyStr
    active: bool = True
    copy_pool: list[str] = []  # LLM lines generated ahead of time; serving picks one at random


RankingReason = Literal["best_score", "explore_toss_up", "explore_cold", "only_candidate", "model_unavailable"]


class ServeCandidate(BaseModel):
    """How the ranker saw one eligible campaign."""

    campaign_id: str
    p_v1: float
    p_lightgbm: float
    p_fm: float
    score: float  # logit(V1) + log(repeat penalty)
    uncertainty: float


class Serve(Document):
    """One ad served: what was shown, why, and with which features. Written in batches after the response."""

    impression_id: str
    session_id: str
    user_id: str
    campaign_id: str
    ad_set_id: str
    variant_id: str
    position: int = Field(ge=0)
    context: dict[str, Any] | None = None
    country: CountryCode | None = None
    os: OS | None = None
    pctr: float | None = None
    ranking_reason: RankingReason
    candidates: list[ServeCandidate] = []
    features: dict[str, Any] = {}  # the CTR model's input row for the chosen campaign (training data later)
    char_message: str
    copy_source: Literal["pool", "llm", "fallback"]
    latency_ms: float
    clicked_at: datetime | None = None
