import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator

import pytest
from temporalio import activity
from temporalio.client import ScheduleIntervalSpec, ScheduleOverlapPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.temporal.jobs import VariantCopyJob
from app.temporal.runner import REFRESH_INTERVAL, SCHEDULE_ID, ensure_schedule
from app.temporal.workflows import LLM_TASK_QUEUE_SUFFIX, GenerateAdCopyWorkflow, RefreshCampaignCacheWorkflow

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TEMPORAL_CLI = shutil.which("temporal")


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    if TEMPORAL_CLI is None:
        pytest.skip("temporal CLI not installed (brew install temporal)")
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL_CLI) as env:
        yield env


@activity.defn(name="refresh_campaign_cache")
async def fake_refresh() -> int:
    return 3


async def test_workflow_runs_the_refresh_activity(env: WorkflowEnvironment) -> None:
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        env.client, task_queue=task_queue, workflows=[RefreshCampaignCacheWorkflow], activities=[fake_refresh]
    ):
        result = await env.client.execute_workflow(
            RefreshCampaignCacheWorkflow.run, id=f"wf-{uuid.uuid4()}", task_queue=task_queue
        )
    assert result == 3


async def test_schedule_is_hourly_skips_overlaps_and_is_idempotent(env: WorkflowEnvironment) -> None:
    await ensure_schedule(env.client, "simula")
    await ensure_schedule(env.client, "simula")  # second call updates instead of failing
    description = await env.client.get_schedule_handle(SCHEDULE_ID).describe()
    schedule = description.schedule
    assert schedule.spec.intervals == [ScheduleIntervalSpec(every=REFRESH_INTERVAL)]
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP


@activity.defn(name="find_variants_needing_copy")
async def fake_find(ad_set_id: str | None) -> list[VariantCopyJob]:
    return [VariantCopyJob(f"var_{i}", "Luna", "Say hi") for i in range(5)]


@activity.defn(name="generate_variant_copy")
async def fake_generate(job: VariantCopyJob) -> int:
    if job.variant_id == "var_3":
        raise RuntimeError("permanent LLM failure")  # one bad variant must not sink the rest
    return 3


async def test_copy_workflow_generates_on_the_rate_limited_queue(env: WorkflowEnvironment) -> None:
    task_queue = f"test-{uuid.uuid4()}"
    async with (
        Worker(env.client, task_queue=task_queue, workflows=[GenerateAdCopyWorkflow], activities=[fake_find]),
        Worker(env.client, task_queue=task_queue + LLM_TASK_QUEUE_SUFFIX, activities=[fake_generate]),
    ):
        handle = await env.client.start_workflow(
            GenerateAdCopyWorkflow.run, None, id=f"wf-{uuid.uuid4()}", task_queue=task_queue
        )
        # var_3 retries with backoff (5 s, 10 s, ...); the test only checks that the others finished.
        await asyncio.sleep(3)
        await handle.terminate()
    history = [e async for e in handle.fetch_history_events()]
    completed = [e for e in history if e.HasField("activity_task_completed_event_attributes")]
    assert len(completed) >= 5  # the find activity + 4 good variants, despite var_3 failing
