import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.temporal.activities import CacheActivities, CopyActivities

# LLM calls hit rate limits (especially free tiers): back off generously before giving up.
LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=6,
)
COPY_CONCURRENCY = 3  # variants generated at once; keeps free-tier rate limits happy


@workflow.defn(name="RefreshCampaignCache")
class RefreshCampaignCacheWorkflow:
    """Run by the hourly schedule: reload the campaign cache from the database."""

    @workflow.run
    async def run(self) -> int:
        return await workflow.execute_activity_method(
            CacheActivities.refresh_campaign_cache,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5),
        )


@workflow.defn(name="GenerateAdCopy")
class GenerateAdCopyWorkflow:
    """Pre-generate LLM lines for variants, so serving never waits on the LLM.

    Started for each new ad set (ad_set_id) and as a backfill on worker startup (None = every
    variant still missing lines, e.g. the seed data or an ad set whose trigger was missed).
    One variant failing for good doesn't stop the rest; serving falls back for that variant.
    """

    @workflow.run
    async def run(self, ad_set_id: str | None) -> int:
        jobs = await workflow.execute_activity_method(
            CopyActivities.find_variants_needing_copy, ad_set_id, start_to_close_timeout=timedelta(seconds=30)
        )
        generated = 0
        for start in range(0, len(jobs), COPY_CONCURRENCY):
            results = await asyncio.gather(
                *(
                    workflow.execute_activity_method(
                        CopyActivities.generate_variant_copy,
                        job,
                        start_to_close_timeout=timedelta(seconds=90),
                        retry_policy=LLM_RETRY,
                    )
                    for job in jobs[start : start + COPY_CONCURRENCY]
                ),
                return_exceptions=True,
            )
            generated += sum(r for r in results if isinstance(r, int))
        return generated
