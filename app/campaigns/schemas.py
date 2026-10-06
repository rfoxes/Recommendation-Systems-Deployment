"""Request bodies and list filters for the campaign routes."""

from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import OS, Campaign, CampaignFields, CountryCode, NonEmptyStr, Surface, Url


class CampaignCreate(CampaignFields):
    """POST /campaigns body. Unknown or API-owned fields (campaign_id, active, ...) are rejected."""

    model_config = ConfigDict(extra="forbid")


# Fields a PATCH may change but not clear; the rest (budget, attribution, store URLs) accept null.
_NON_NULLABLE = frozenset(
    {
        "campaign_name",
        "advertiser_company_id",
        "active",
        "surface",
        "publisher_ids",
        "geo_targets",
        "os_targets",
        "native_ad_set_ids",
    }
)


class CampaignUpdate(BaseModel):
    """PATCH /campaigns/{id} body: only the fields sent are changed.

    `active` is API-owned on create, but toggling it here is how a campaign gets (de)activated.
    """

    model_config = ConfigDict(extra="forbid")

    campaign_name: NonEmptyStr | None = None
    advertiser_company_id: NonEmptyStr | None = None
    active: bool | None = None
    surface: Surface | None = None
    publisher_ids: list[str] | None = None
    daily_budget: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    geo_targets: list[CountryCode] | None = None
    os_targets: list[OS] | None = None
    attribution_provider: str | None = None
    ios_store_url: Url | None = None
    android_store_url: Url | None = None
    native_ad_set_ids: list[str] | None = None
    downloads_label: str | None = None

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        nulls = sorted(f for f in self.model_fields_set & _NON_NULLABLE if getattr(self, f) is None)
        if nulls:
            raise ValueError(f"fields cannot be null: {', '.join(nulls)}")
        return self

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


@dataclass(frozen=True)
class CampaignFilters:
    """GET /campaigns filters; None means "don't filter on this"."""

    ids: frozenset[str] | None = None
    surface: Surface | None = None
    publisher_id: str | None = None
    active: bool | None = None

    def matches(self, campaign: Campaign) -> bool:
        return (
            (self.ids is None or campaign.campaign_id in self.ids)
            and (self.surface is None or campaign.surface == self.surface)
            and (self.active is None or campaign.active == self.active)
            # A campaign with no publisher list runs on every publisher.
            and (self.publisher_id is None or not campaign.publisher_ids or self.publisher_id in campaign.publisher_ids)
        )
