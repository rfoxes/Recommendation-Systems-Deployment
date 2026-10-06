"""Connects to Temporal, keeps the hourly schedule in place, and runs the worker in-process.

The worker runs as a background task inside the API container, so the deployed app is a
single Cloud Run service that can scale to zero. While it is scaled to zero, a scheduled run
waits for a worker instead of being lost, and the SKIP overlap policy means later runs are
skipped rather than queued: waking up triggers one refresh, not one per missed hour.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.worker import Worker

from app.config import Settings
from app.temporal.workflows import LLM_TASK_QUEUE_SUFFIX, GenerateAdCopyWorkflow, RefreshCampaignCacheWorkflow

logger = logging.getLogger(__name__)

SCHEDULE_ID = "campaign-cache-refresh"
REFRESH_INTERVAL = timedelta(hours=1)


async def connect(settings: Settings) -> Client:
    if settings.temporal_api_key:  # Temporal Cloud
        return await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
            api_key=settings.temporal_api_key.get_secret_value(),
            tls=True,
        )
    return await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)


def build_schedule(task_queue: str) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            RefreshCampaignCacheWorkflow.run,
            id=SCHEDULE_ID,
            task_queue=task_queue,
            # A run still waiting for a worker can't block the next hour forever (startup warms the cache anyway).
            execution_timeout=REFRESH_INTERVAL - timedelta(minutes=5),
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=REFRESH_INTERVAL)]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(minutes=1)),
    )


async def ensure_schedule(client: Client, task_queue: str) -> None:
    """Create the schedule, or update it to match this code if it already exists."""
    schedule = build_schedule(task_queue)
    try:
        await client.create_schedule(SCHEDULE_ID, schedule)
        logger.info("Created Temporal schedule %s", SCHEDULE_ID)
    except ScheduleAlreadyRunningError:

        def _replace(current: ScheduleUpdateInput) -> ScheduleUpdate:
            schedule.state = current.description.schedule.state  # e.g. keep it paused if someone paused it
            return ScheduleUpdate(schedule=schedule)

        await client.get_schedule_handle(SCHEDULE_ID).update(_replace)


class TemporalRunner:
    """Runs the worker as a background task, so the API starts serving without waiting on Temporal."""

    def __init__(
        self,
        settings: Settings,
        activities: Sequence[Callable[..., Any]],
        llm_activities: Sequence[Callable[..., Any]] = (),
    ) -> None:
        self._settings = settings
        self._activities = activities
        self._llm_activities = llm_activities
        self._client: Client | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def start_copy_generation(self, ad_set_id: str) -> None:
        """Kick off copy pre-generation for a new ad set (no-op if Temporal isn't connected yet)."""
        if self._client is None:
            logger.info("Temporal not connected; the startup backfill will generate copy for %s", ad_set_id)
            return
        await self._client.start_workflow(
            GenerateAdCopyWorkflow.run,
            ad_set_id,
            id=f"generate-copy-{ad_set_id}",
            task_queue=self._settings.temporal_task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="temporal-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()  # cancelling Worker.run() shuts the worker down gracefully
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        delay = 1.0
        while True:
            try:
                client = await connect(self._settings)
                await ensure_schedule(client, self._settings.temporal_task_queue)
                worker = Worker(
                    client,
                    task_queue=self._settings.temporal_task_queue,
                    workflows=[RefreshCampaignCacheWorkflow, GenerateAdCopyWorkflow],
                    activities=self._activities,
                    graceful_shutdown_timeout=timedelta(seconds=5),
                )
                # LLM calls get their own task queue and worker, rate-limited so pre-generation stays under the
                # LLM's per-minute quota (each activity makes `copy_pool_size` requests). The limit is enforced
                # in this worker: exact, and equivalent to a global limit because Cloud Run runs at most one
                # instance. (A server-side task-queue limit also works across many instances, but measured on
                # Temporal Cloud it dispatched ~1 activity/minute instead of every 15 s, because the queue is
                # split into partitions.)
                llm_worker = Worker(
                    client,
                    task_queue=self._settings.temporal_task_queue + LLM_TASK_QUEUE_SUFFIX,
                    activities=self._llm_activities,
                    max_activities_per_second=(
                        self._settings.llm_requests_per_minute / 60 / self._settings.copy_pool_size
                    ),
                    graceful_shutdown_timeout=timedelta(seconds=5),
                )
                self._client = client
                # Fill in copy for any variant still missing it (seed data, missed triggers).
                await client.start_workflow(
                    GenerateAdCopyWorkflow.run,
                    None,
                    id="generate-copy-backfill",
                    task_queue=self._settings.temporal_task_queue,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                )
                logger.info("Temporal worker polling task queue %s", self._settings.temporal_task_queue)
                await asyncio.gather(worker.run(), llm_worker.run())
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Temporal worker unavailable; retrying in %.0fs", delay, exc_info=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
