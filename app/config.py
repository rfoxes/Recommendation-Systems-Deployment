from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    mongo_uri: str = "mongodb://localhost:27017/?directConnection=true"
    mongo_db: str = "simula"
    redis_url: str = "redis://localhost:6379/0"

    temporal_enabled: bool = True
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_api_key: SecretStr | None = None  # set for Temporal Cloud; unset for the local dev server
    temporal_task_queue: str = "simula"

    # Which LLM writes the ad copy; only the matching key is needed. With no key, ads use fallback copy.
    llm_provider: Literal["gemini", "anthropic", "openai"] = "gemini"
    llm_model: str | None = None  # None = the provider's default model
    gemini_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    copy_pool_size: int = 3  # lines pre-generated per variant
    llm_requests_per_minute: int = 12  # under the Gemini free tier's 15/minute; enforced by Temporal

    # Ad serving
    api_url: str = "http://localhost:8000"  # public base URL embedded in ads for click tracking
    click_api_key: SecretStr = SecretStr("dev-click-key")  # publishable: ships inside ad HTML, can only record clicks
    ad_theme: Literal["dark", "light"] = "dark"
    fatigue_cap: int = 3  # max times one variant is shown to one user per 24h
    live_copy_timeout_seconds: float = 1.5  # only when a variant has no pre-generated lines yet
    toss_up_explore_share: float = 0.10  # ranker: share of toss-ups that go to the least-known campaign
    cold_explore_share: float = 0.05  # ranker: share of picks that may go to a promising cold campaign
    serve_batch_size: int = 100
    serve_flush_seconds: float = 0.2
    demo_enabled: bool = True  # GET /demo: the test harness, serving real ads with simulated country/device

    ctr_model_dir: Path | None = None  # None = models/ctr-<release>, downloaded on first start if missing

    session_inactivity_seconds: int = 30  # README: only mint a new session after 30s without ad serves
    session_record_ttl_seconds: int = 24 * 3600  # how long a session id still resolves to its user
    session_create_limit: int = 30  # max POST /session/create per IP per window
    session_create_window_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
