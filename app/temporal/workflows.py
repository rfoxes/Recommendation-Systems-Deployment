import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from app.temporal.jobs import (
    FIND_VARIANTS_NEEDING_COPY,
    GENERATE_VARIANT_COPY,
    REFRESH_CAMPAIGN_CACHE,
    VariantCopyJob,
)

# LLM calls hit rate limits (especially free tiers): back off generously before giving up.
LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=6,
)
COPY_CONCURRENCY = 3  # variants in flight at once; the LLM task queue's rate limit does the pacing
LLM_TASK_QUEUE_SUFFIX = "-llm"


@workflow.defn(name="RefreshCampaignCache")
class RefreshCampaignCacheWorkflow:
    """Run by the hourly schedule: reload the campaign cache from the database."""

    @workflow.run
    async def run(self) -> int:
        refreshed: int = await workflow.execute_activity(
            REFRESH_CAMPAIGN_CACHE,
            result_type=int,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5),
        )
        return refreshed


@workflow.defn(name="GenerateAdCopy")
class GenerateAdCopyWorkflow:
    """Pre-generate LLM lines for variants, so serving never waits on the LLM.

    Started for each new ad set (ad_set_id) and as a backfill on worker startup (None = every
    variant still missing lines, e.g. the seed data or an ad set whose trigger was missed).
    One variant failing for good doesn't stop the rest; serving falls back for that variant.
    """

    @workflow.run
    async def run(self, ad_set_id: str | None) -> int:
        jobs: list[VariantCopyJob] = await workflow.execute_activity(
            FIND_VARIANTS_NEEDING_COPY,
            ad_set_id,
            result_type=list[VariantCopyJob],
            start_to_close_timeout=timedelta(seconds=30),
        )
        generated = 0
        for start in range(0, len(jobs), COPY_CONCURRENCY):
            results = await asyncio.gather(
                *(
                    workflow.execute_activity(
                        GENERATE_VARIANT_COPY,
                        job,
                        result_type=int,
                        task_queue=workflow.info().task_queue + LLM_TASK_QUEUE_SUFFIX,
                        schedule_to_start_timeout=timedelta(hours=1),  # may queue behind the rate limit
                        start_to_close_timeout=timedelta(seconds=90),
                        retry_policy=LLM_RETRY,
                    )
                    for job in jobs[start : start + COPY_CONCURRENCY]
                ),
                return_exceptions=True,
            )
            generated += sum(r for r in results if isinstance(r, int))
        return generated
