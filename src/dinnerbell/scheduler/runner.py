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

from dinnerbell.domain.notifications import Channel
from dinnerbell.logs import start_unit
from dinnerbell.services import announcements, auth, backups, lifecycle, outbox, recap, stuck
from dinnerbell.services.clock import Clock
from dinnerbell.services.health import HEARTBEAT_INTERVAL, SchedulerState

log = logging.getLogger(__name__)

BACKUP_HOUR = 3
BACKUP_MINUTE = 30
BACKUP_CATCH_UP_AFTER = timedelta(hours=24)
SHUTDOWN_GRACE_SECONDS = 10.0
CANCEL_UNWIND_SECONDS = 1.0
"""How long shutdown waits for a job it cancelled to finish cleaning up."""
CALL_TICK_INTERVAL = timedelta(seconds=30)
"""How often calls are opened and advanced. One idempotent job, not one job per deadline: the
database is the schedule, so a restart or downtime catches up on the next tick."""
DRAIN_INTERVAL = timedelta(seconds=5)
"""How often the outbox is drained between ticks, so a tap in Slack shows up within seconds."""


@dataclass(frozen=True)
class JobContext:
    state: SchedulerState
    clock: Clock
    zone: ZoneInfo
    database_path: Path
    backup_dir: Path
    sessionmaker: async_sessionmaker[AsyncSession]
    channel: Channel
    running: set[asyncio.Task[object]] = field(default_factory=set)
    drain_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """One drain at a time in this process; the outbox's row claims cover other processes."""
    failures: lifecycle.FailureLog = field(default_factory=lifecycle.FailureLog)
    """Throttles the log line for a call whose step keeps failing (#142)."""
    stuck_check_failures: lifecycle.FailureLog = field(
        default_factory=lambda: lifecycle.FailureLog("could not check a call for being stuck")
    )


Job = Callable[[JobContext], Awaitable[None]]


def _tracked(job: Job) -> Job:
    """Run the job as its own task, shielded from APScheduler's cancel, and record the task so
    shutdown can wait for it to finish before the database closes.

    APScheduler cancels asyncio jobs on shutdown. Cancelled mid-query, a job leaves aiosqlite's
    worker thread running a query or a close whose result it then delivers to a loop that may
    have closed, and the thread dies with "Event loop is closed". The shield lets a job that
    is mid-query finish instead; ``stop_scheduler`` cancels only what outlasts its grace.
    """

    @functools.wraps(job)
    async def run(ctx: JobContext) -> None:
        end = start_unit("scheduler")  # one request ID per run, on every line it logs (#75)
        try:
            task = asyncio.ensure_future(job(ctx))  # copies the context, request ID included
            ctx.running.add(task)
            task.add_done_callback(ctx.running.discard)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                task.add_done_callback(_log_late_failure)  # nobody awaits it now
                raise
        finally:
            end()

    return run


def _log_late_failure(task: asyncio.Task[object]) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        log.error("scheduled job failed during shutdown", exc_info=error)


async def heartbeat(ctx: JobContext) -> None:
    ctx.state.last_tick = ctx.clock()


async def nightly_backup(ctx: JobContext) -> None:
    today = ctx.clock().astimezone(ctx.zone).date()
    try:
        await asyncio.to_thread(backups.run_backup, ctx.database_path, ctx.backup_dir, today, now=ctx.clock())
    except Exception:
        log.exception("database backup failed")
        ctx.state.backup_failing = True
        return
    ctx.state.backup_failing = False
    ctx.state.last_backup = backups.last_backup_time(ctx.backup_dir)


async def purge_sessions(ctx: JobContext) -> None:
    async with ctx.sessionmaker() as db:
        removed = await auth.purge_expired_sessions(db, now=ctx.clock())
    if removed:
        log.info("expired sessions removed", extra={"count": removed})
    async with ctx.sessionmaker() as db:
        refusals = await auth.prune_refusals(db, now=ctx.clock())
    if refusals:
        log.info("old sign-in refusals removed", extra={"count": refusals})


async def call_tick(ctx: JobContext) -> None:
    """Open due calls and walk every open call through the steps it owes, in each household's
    time zone, look for stuck calls (#142), queue the news (new badges, the monthly recap),
    then deliver what that produced. Replaces one job per deadline (ADR 0004)."""
    try:
        await lifecycle.tick(ctx.sessionmaker, ctx.clock(), failures=ctx.failures)
    except Exception:
        log.exception("call tick failed")
    # Look for stuck calls here, once per tick, so /healthz and Admin only read the result.
    now = ctx.clock()
    ctx.state.stuck = await stuck.find_stuck(
        ctx.sessionmaker, now, running_since=ctx.state.started_at or now, failures=ctx.stuck_check_failures
    )
    try:
        await announcements.announce_badges(ctx.sessionmaker, ctx.clock())
    except Exception:
        log.exception("badge announcements failed")
    try:
        await recap.post_recaps(ctx.sessionmaker, ctx.clock())
    except Exception:
        log.exception("monthly recap failed")
    # Never wait behind a drain that's already running: a hung channel call would otherwise
    # stall every later tick (reminders, deadlines, decisions). The 5-second drain job catches up.
    if not ctx.drain_lock.locked():
        await drain_outbox(ctx)


async def drain_outbox(ctx: JobContext) -> None:
    """Deliver due notifications. The tick and the drain job both come here, and the lock keeps
    them from draining at the same time, so updates to a message reach the channel in order."""
    async with ctx.drain_lock:
        try:
            await outbox.drain(ctx.sessionmaker, ctx.channel, ctx.clock)
        except Exception:
            log.exception("outbox drain failed")


async def stop_scheduler(
    scheduler: AsyncIOScheduler, ctx: JobContext, *, grace: float = SHUTDOWN_GRACE_SECONDS
) -> None:
    """Stop scheduling, let running jobs finish for up to ``grace`` seconds, then cancel what is
    left and wait for it to unwind.

    The wait matters: a job still holding a database connection when the engine is disposed
    leaves aiosqlite's worker thread to outlive the event loop (see ``_tracked``).
    """
    scheduler.shutdown(wait=False)
    if ctx.running:
        _, unfinished = await asyncio.wait(set(ctx.running), timeout=grace)
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.wait(unfinished, timeout=CANCEL_UNWIND_SECONDS)


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
    # A catch-up backup at start runs as the nightly job itself (its first run is moved to now),
    # not as a second job, so max_instances=1 keeps two backups from ever overlapping.
    ctx.state.last_backup = backups.last_backup_time(ctx.backup_dir)
    catch_up = ctx.state.last_backup is None or ctx.clock() - ctx.state.last_backup > BACKUP_CATCH_UP_AFTER
    scheduler.add_job(
        _tracked(nightly_backup),
        CronTrigger(hour=BACKUP_HOUR, minute=BACKUP_MINUTE, timezone=ctx.zone),
        args=[ctx],
        id="nightly_backup",
        **({"next_run_time": start} if catch_up else {}),
    )
    scheduler.add_job(
        _tracked(purge_sessions), IntervalTrigger(hours=1), args=[ctx], id="purge_sessions", next_run_time=start
    )
    scheduler.add_job(
        _tracked(call_tick),
        IntervalTrigger(seconds=CALL_TICK_INTERVAL.total_seconds()),
        args=[ctx],
        id="call_tick",
        next_run_time=start,
    )
    scheduler.add_job(
        _tracked(drain_outbox),
        IntervalTrigger(seconds=DRAIN_INTERVAL.total_seconds()),
        args=[ctx],
        id="drain_outbox",
        next_run_time=start,
    )
    return scheduler
