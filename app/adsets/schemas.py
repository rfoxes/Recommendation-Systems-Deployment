from math import prod
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.models import AdSet, AdVariant, NonEmptyStr, Url

# Every asset list multiplies the variant count; cap it so one request can't create thousands.
MAX_VARIANTS_PER_AD_SET = 500


def _dedupe[T](items: list[T]) -> list[T]:
    """Drop repeated entries (keeping order) so duplicates don't produce duplicate variants."""
    seen: set[str] = set()
    return [x for x in items if not (str(x) in seen or seen.add(str(x)))]


type AssetList[T] = Annotated[list[T], Field(min_length=1), AfterValidator(_dedupe)]


class AdSetCreate(BaseModel):
    """POST /adsets body. ad_set_id, active and timestamps are API-owned."""

    model_config = ConfigDict(extra="forbid")

    campaign_id: NonEmptyStr
    ad_set_name: NonEmptyStr
    character_names: AssetList[NonEmptyStr]
    video_urls: AssetList[Url]
    ctas: AssetList[NonEmptyStr]
    ai_prompts: AssetList[NonEmptyStr]
    fallback_copy: AssetList[NonEmptyStr]

    @model_validator(mode="after")
    def _check_variant_count(self) -> Self:
        count = self.variant_count
        if count > MAX_VARIANTS_PER_AD_SET:
            raise ValueError(f"would create {count} variants; the limit is {MAX_VARIANTS_PER_AD_SET}")
        return self

    @property
    def variant_count(self) -> int:
        return prod(map(len, (self.character_names, self.video_urls, self.ctas, self.ai_prompts)))


class AdSetWithVariants(AdSet):
    """POST /adsets response: the ad set plus every variant generated from it."""

    variants: list[AdVariant]
