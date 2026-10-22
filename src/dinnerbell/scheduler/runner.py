# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scheduler setup and the jobs it runs.

Every job is idempotent: running it twice, or after a missed run, leaves the same result.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.services import auth, backups
from dinnerbell.services.clock import Clock

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = timedelta(seconds=30)
BACKUP_HOUR = 3
BACKUP_MINUTE = 30
BACKUP_CATCH_UP_AFTER = timedelta(hours=24)
SHUTDOWN_GRACE_SECONDS = 10.0


@dataclass
class SchedulerState:
    """What /healthz reports about background work."""

    last_tick: datetime | None = None
    last_backup: datetime | None = None

    def tick_is_fresh(self, now: datetime) -> bool:
        return self.last_tick is not None and now - self.last_tick <= HEARTBEAT_INTERVAL * 3


@dataclass(frozen=True)
class JobContext:
    state: SchedulerState
    clock: Clock
    zone: ZoneInfo
    database_path: Path
    backup_dir: Path
    sessionmaker: async_sessionmaker[AsyncSession]
    running: set[asyncio.Task[object]] = field(default_factory=set)


Job = Callable[[JobContext], Awaitable[None]]


def _tracked(job: Job) -> Job:
    """Record the job's task so shutdown can wait for it to unwind before the database closes."""

    @functools.wraps(job)
    async def run(ctx: JobContext) -> None:
        task = asyncio.current_task()
        if task is not None:
            ctx.running.add(task)
        try:
            await job(ctx)
        finally:
            if task is not None:
                ctx.running.discard(task)

    return run


async def heartbeat(ctx: JobContext) -> None:
    ctx.state.last_tick = ctx.clock()


async def nightly_backup(ctx: JobContext) -> None:
    today = ctx.clock().astimezone(ctx.zone).date()
    try:
        await asyncio.to_thread(backups.run_backup, ctx.database_path, ctx.backup_dir, today)
    except Exception:
        log.exception("database backup failed")
        return
    ctx.state.last_backup = backups.last_backup_time(ctx.backup_dir)


async def purge_sessions(ctx: JobContext) -> None:
    async with ctx.sessionmaker() as db:
        removed = await auth.purge_expired_sessions(db, now=ctx.clock())
    if removed:
        log.info("expired sessions removed", extra={"count": removed})


async def stop_scheduler(scheduler: AsyncIOScheduler, ctx: JobContext) -> None:
    """Stop scheduling, cancel running jobs, and wait for them to finish cleaning up.

    APScheduler cancels asyncio jobs on shutdown but does not wait for them. Without the wait,
    a cancelled job can still be rolling back its database session when the engine is disposed.
    """
    scheduler.shutdown(wait=False)
    if ctx.running:
        await asyncio.wait(set(ctx.running), timeout=SHUTDOWN_GRACE_SECONDS)


def build_scheduler(ctx: JobContext) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        timezone=ctx.zone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    # APScheduler times jobs by the wall clock; ctx.clock only stamps what the jobs record.
    start = datetime.now(UTC)
    scheduler.add_job(
        _tracked(heartbeat),
        IntervalTrigger(seconds=HEARTBEAT_INTERVAL.total_seconds()),
        args=[ctx],
        id="heartbeat",
        next_run_time=start,
    )
    scheduler.add_job(
        _tracked(nightly_backup),
        CronTrigger(hour=BACKUP_HOUR, minute=BACKUP_MINUTE, timezone=ctx.zone),
        args=[ctx],
        id="nightly_backup",
    )
    scheduler.add_job(
        _tracked(purge_sessions), IntervalTrigger(hours=1), args=[ctx], id="purge_sessions", next_run_time=start
    )
    ctx.state.last_backup = backups.last_backup_time(ctx.backup_dir)
    if ctx.state.last_backup is None or ctx.clock() - ctx.state.last_backup > BACKUP_CATCH_UP_AFTER:
        scheduler.add_job(_tracked(nightly_backup), args=[ctx], id="catch_up_backup", next_run_time=start)
    return scheduler
