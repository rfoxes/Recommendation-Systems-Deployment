import shutil
import uuid
from collections.abc import AsyncIterator

import pytest
from temporalio import activity
from temporalio.client import ScheduleIntervalSpec, ScheduleOverlapPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.temporal.runner import REFRESH_INTERVAL, SCHEDULE_ID, ensure_schedule
from app.temporal.workflows import RefreshCampaignCacheWorkflow

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
    async with Worker(env.client, task_queue=task_queue, workflows=[RefreshCampaignCacheWorkflow], activities=[fake_refresh]):
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
