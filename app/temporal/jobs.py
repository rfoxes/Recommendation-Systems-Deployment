"""Activity names and payloads shared by workflows and activities.

Workflows import only this module, so Temporal's deterministic workflow sandbox never loads I/O libraries
(Redis, MongoDB, LLM SDKs); those live in app.temporal.activities, which runs outside the sandbox.
"""

from dataclasses import dataclass

REFRESH_CAMPAIGN_CACHE = "refresh_campaign_cache"
FIND_VARIANTS_NEEDING_COPY = "find_variants_needing_copy"
GENERATE_VARIANT_COPY = "generate_variant_copy"


@dataclass(frozen=True)
class VariantCopyJob:
    variant_id: str
    character_name: str
    ai_prompt: str
