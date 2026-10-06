"""Writes serve records (and their clicks) to MongoDB in batches, off the request path.

Serving just enqueues; a background task flushes every `flush_seconds` or `batch_size` records, inserting
serves before applying clicks so a click can never arrive before its serve. The queue is bounded: if
MongoDB falls behind, records are dropped (and logged) rather than slowing ads down. On shutdown, the
queue is drained. Batching keeps us far under Atlas's free-tier limit of ~100 operations a second.
"""

import asyncio
import contextlib
import logging
from datetime import datetime

from pymongo import InsertOne, UpdateOne
from pymongo.errors import PyMongoError

from app.db import SERVES, Database, to_document
from app.models import Serve

logger = logging.getLogger(__name__)

_Op = InsertOne | UpdateOne  # type: ignore[type-arg]


class ServeWriter:
    def __init__(self, db: Database, *, batch_size: int, flush_seconds: float, max_queue: int = 10_000) -> None:
        self._db = db
        self._batch_size = batch_size
        self._flush_seconds = flush_seconds
        self._queue: asyncio.Queue[_Op] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="serve-writer")

    async def stop(self) -> None:
        """Flush everything still queued, then stop."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        await self._flush(self._drain())

    def record(self, serve: Serve) -> None:
        self._enqueue(InsertOne(to_document(serve, "impression_id")))

    def record_click(self, impression_id: str, clicked_at: datetime) -> None:
        self._enqueue(UpdateOne({"_id": impression_id, "clicked_at": None}, {"$set": {"clicked_at": clicked_at}}))

    def _enqueue(self, op: _Op) -> None:
        try:
            self._queue.put_nowait(op)
        except asyncio.QueueFull:
            logger.error("Serve queue full; dropping a record")

    def _drain(self) -> list[_Op]:
        ops: list[_Op] = []
        while not self._queue.empty() and len(ops) < self._batch_size:
            ops.append(self._queue.get_nowait())
        return ops

    async def _run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            await asyncio.sleep(self._flush_seconds)  # let a batch build up
            batch += self._drain()
            await self._flush(batch)

    async def _flush(self, batch: list[_Op]) -> None:
        if not batch:
            return
        inserts = [op for op in batch if isinstance(op, InsertOne)]
        updates = [op for op in batch if isinstance(op, UpdateOne)]
        for ops in (inserts, updates):  # serves first, so clicks always find their serve
            if not ops:
                continue
            try:
                await self._db[SERVES].bulk_write(ops, ordered=False)
            except PyMongoError:
                logger.exception("Failed to write %d serve operations", len(ops))
