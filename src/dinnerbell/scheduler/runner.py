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

from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.domain.notifications import Channel
from dinnerbell.domain.push import PushSender
from dinnerbell.domain.weather import CHECK_INTERVAL
from dinnerbell.logs import start_unit
from dinnerbell.services import announcements, auth, backups, lifecycle, local_weather, outbox, push, recap, stuck
from dinnerbell.services import household as household_service
from dinnerbell.services.assistant import retention
from dinnerbell.services.clock import Clock
from dinnerbell.services.health import HEARTBEAT_INTERVAL, SchedulerState

log = logging.getLogger(__name__)

BACKUP_HOUR = 3
BACKUP_MINUTE = 30
BACKUP_CATCH_UP_AFTER = timedelta(hours=24)
PRUNE_HOUR = 4
PRUNE_MINUTE = 15
"""When the nightly outbox prune runs, household time: after the backup, so last night's copy
still has what it deletes."""
SHUTDOWN_GRACE_SECONDS = 10.0
CANCEL_UNWIND_SECONDS = 1.0
"""How long shutdown waits for a job it cancelled to finish cleaning up."""
CALL_TICK_INTERVAL = timedelta(seconds=30)
"""How often calls are opened and advanced. One idempotent job, not one job per deadline: the
database is the schedule, so a restart or downtime catches up on the next tick."""
CONVERSATION_PURGE_INTERVAL = timedelta(minutes=5)
"""How often idle assistant conversations are deleted: Conversation hours, to within minutes."""
DRAIN_INTERVAL = timedelta(seconds=5)
"""How often the outbox is drained between ticks, so a tap in Slack shows up within seconds."""
PUSH_DRAIN_INTERVAL = timedelta(seconds=5)
"""How often the push queue is drained (spec 6.15). Its own job, so neither queue waits on the other."""
WEATHER_INTERVAL = CHECK_INTERVAL
"""How often the scheduler looks for local weather that's due (ADR 0011). Each household asks only
when its own randomized wait is over; ``domain.weather.next_fetch`` leaves room for this, so on
schedule a fetch comes at most 3 hours after the last. A late or skipped run can stretch that."""
ZONE_CHECK_INTERVAL = timedelta(minutes=1)
"""How often the scheduler looks for a time zone that ``dinnerbell set-timezone`` changed (#334)."""


@dataclass
class JobContext:
    state: SchedulerState
    clock: Clock
    zone: ZoneInfo
    """The household's time zone, for the nightly backup and outbox prune. ``follow_zone``
    changes it when the household's does (#334)."""
    database_path: Path
    backup_dir: Path
    sessionmaker: async_sessionmaker[AsyncSession]
    channel: Channel
    running: set[asyncio.Task[object]] = field(default_factory=set)
    drain_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """One drain at a time in this process; the outbox's row claims cover other processes."""
    failures: lifecycle.FailureLog = field(default_factory=lifecycle.FailureLog)
    """Throttles the log line for a call whose step keeps failing (#142)."""
    stuck_call_failures: lifecycle.FailureLog = field(default_factory=stuck.call_check_log)
    stuck_check_failures: lifecycle.FailureLog = field(default_factory=stuck.whole_check_log)
    push_sender: PushSender | None = None
    """Sends web pushes (spec 6.15). None leaves the push queue alone."""
    base_url: str = ""
    """What a push's link starts with: ``DINNERBELL_BASE_URL``."""
    push_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """One push drain at a time in this process; row claims cover other processes."""
    weather: local_weather.WeatherSource | None = None
    """Open-Meteo, for Dinnerbone's local weather (ADR 0011). None leaves it alone."""


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


def _backup_trigger(zone: ZoneInfo) -> CronTrigger:
    return CronTrigger(hour=BACKUP_HOUR, minute=BACKUP_MINUTE, timezone=zone)


def _prune_trigger(zone: ZoneInfo) -> CronTrigger:
    return CronTrigger(hour=PRUNE_HOUR, minute=PRUNE_MINUTE, timezone=zone)


async def follow_zone(ctx: JobContext, scheduler: AsyncIOScheduler) -> None:
    """Move the nightly backup and the nightly outbox prune to the household's time zone once
    ``dinnerbell set-timezone`` changes it (#334), so the change takes effect within a minute,
    with no restart. They are the only jobs that keep a zone: everything else reads the
    household row each time.

    Backups are one per date in the household's zone, so after a move west the next night's
    date may already have its copy, taken in the old zone, and that run writes nothing. The gap
    between two copies is then up to a day plus the difference between the zones."""
    try:
        async with ctx.sessionmaker() as db:
            household = await household_service.current_household(db)
        if household.timezone == ctx.zone.key:
            return
        zone = ZoneInfo(household.timezone)
    except Exception:
        log.exception("time zone check failed")
        return
    try:
        scheduler.reschedule_job("nightly_backup", trigger=_backup_trigger(zone))
        scheduler.reschedule_job("prune_outbox", trigger=_prune_trigger(zone))
    except JobLookupError:  # shutting down: stop_scheduler removed the jobs
        return
    ctx.zone = zone
    log.info(
        "the nightly backup and outbox prune now run in the household's new time zone",
        extra={"household_id": household.id, "zone": zone.key},
    )


async def purge_sessions(ctx: JobContext) -> None:
    async with ctx.sessionmaker() as db:
        removed = await auth.purge_expired_sessions(db, now=ctx.clock())
    if removed:
        log.info("expired sessions removed", extra={"count": removed})
    async with ctx.sessionmaker() as db:
        refusals = await auth.prune_refusals(db, now=ctx.clock())
    if refusals:
        log.info("old sign-in refusals removed", extra={"count": refusals})


async def purge_conversations(ctx: JobContext) -> None:
    """Delete assistant conversations past Admin's Conversation hours, and drafts that expired
    (ADR 0009). Runs whether or not the assistant is on, so turning it off never keeps either
    longer. A failure in one never stops the other."""
    try:
        removed = await retention.purge_idle_conversations(ctx.sessionmaker, ctx.clock())
    except Exception:
        log.exception("assistant conversation purge failed")
    else:
        if removed:
            log.info("idle assistant conversations removed", extra={"count": removed})
    try:
        drafts = await retention.purge_expired_drafts(ctx.sessionmaker, ctx.clock())
    except Exception:
        log.exception("assistant draft purge failed")
        return
    if drafts:
        log.info("expired assistant drafts removed", extra={"count": drafts})


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
        ctx.sessionmaker,
        now,
        running_since=ctx.state.started_at or now,
        failures=ctx.stuck_call_failures,
        check_failures=ctx.stuck_check_failures,
    )
    ctx.state.stuck_checked_at = now
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


async def drain_push(ctx: JobContext) -> None:
    """Send due web pushes. With the web app off, the queue is empty, so this reads one index."""
    if ctx.push_sender is None or ctx.push_lock.locked():
        return
    async with ctx.push_lock:
        try:
            await push.drain(ctx.sessionmaker, ctx.push_sender, ctx.clock, ctx.base_url)
        except Exception:
            log.exception("push drain failed")


async def refresh_weather(ctx: JobContext) -> None:
    """Ask Open-Meteo for each household whose local weather is due (ADR 0011). Its own job, apart
    from the call tick, so a slow or failing weather service never holds up a call. With local
    weather off everywhere, it finds nothing due and asks nothing."""
    if ctx.weather is None:
        return
    try:
        await local_weather.refresh(ctx.sessionmaker, ctx.weather, ctx.clock())
    except Exception as exc:  # pragma: no cover - refresh catches everything itself
        # The traceback only at debug: an exception's text can carry a request URL, and with it the
        # postal code or coordinates (ADR 0011).
        log.warning("local weather refresh failed", extra={"error": type(exc).__name__})
        log.debug("local weather refresh failed", exc_info=True)


async def purge_pushes(ctx: JobContext) -> None:
    """Delete pushes that ended a week ago, hourly, whether or not anything was due."""
    try:
        removed = await push.purge(ctx.sessionmaker, ctx.clock())
    except Exception:
        log.exception("push purge failed")
        return
    if removed:
        log.info("old pushes removed", extra={"count": removed})


async def prune_outbox(ctx: JobContext) -> None:
    """Delete old delivered and failed notifications (#340), nightly. Logs counts only."""
    try:
        removed = await outbox.prune(ctx.sessionmaker, ctx.clock())
    except Exception:
        log.exception("outbox prune failed")
        return
    if removed:
        log.info("old notifications removed", extra={"count": removed})


async def stop_scheduler(
    scheduler: AsyncIOScheduler, ctx: JobContext, *, grace: float = SHUTDOWN_GRACE_SECONDS
) -> None:
    """Stop scheduling, let running jobs finish for up to ``grace`` seconds, then cancel what is
    left and wait for it to unwind.

    The wait matters: a job still holding a database connection when the engine is disposed
    leaves aiosqlite's worker thread to outlive the event loop (see ``_tracked``).

    ``AsyncIOScheduler.shutdown`` only queues the shutdown on the loop. A wakeup queued before it,
    such as the 5-second drains' timer firing, still starts the jobs that are due, so this reads
    the running set only after the shutdown event (#338). Removing the jobs first keeps a wakeup
    queued after the shutdown, which APScheduler doesn't guard against, from starting any.
    """
    stopped = asyncio.Event()
    scheduler.add_listener(lambda _event: stopped.set(), EVENT_SCHEDULER_SHUTDOWN)
    scheduler.remove_all_jobs()
    scheduler.shutdown(wait=False)
    await stopped.wait()
    if ctx.running:
        _, unfinished = await asyncio.wait(set(ctx.running), timeout=grace)
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.wait(unfinished, timeout=CANCEL_UNWIND_SECONDS)


def build_scheduler(ctx: JobContext) -> AsyncIOScheduler:
    # UTC, not the household's zone: only the nightly backup's and outbox prune's triggers have
    # a zone, and follow_zone keeps both current, so no job is left on an old zone (#334).
    scheduler = AsyncIOScheduler(
        timezone=UTC,
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
    # No misfire grace: a host suspended across 03:30 for longer than the hour every other job
    # allows backs up when it wakes (coalesced to one run) instead of skipping the night. Where
    # 03:30 doesn't exist one night (a spring-forward gap), APScheduler runs it once, an hour
    # later on the wall clock; where it happens twice, the second run finds the day's copy.
    ctx.state.last_backup = backups.last_backup_time(ctx.backup_dir)
    catch_up = ctx.state.last_backup is None or ctx.clock() - ctx.state.last_backup > BACKUP_CATCH_UP_AFTER
    scheduler.add_job(
        _tracked(nightly_backup),
        _backup_trigger(ctx.zone),
        args=[ctx],
        id="nightly_backup",
        misfire_grace_time=None,
        **({"next_run_time": start} if catch_up else {}),
    )
    scheduler.add_job(
        _tracked(purge_sessions), IntervalTrigger(hours=1), args=[ctx], id="purge_sessions", next_run_time=start
    )
    scheduler.add_job(
        _tracked(purge_conversations),
        IntervalTrigger(seconds=CONVERSATION_PURGE_INTERVAL.total_seconds()),
        args=[ctx],
        id="purge_conversations",
        next_run_time=start,
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
    scheduler.add_job(
        _tracked(drain_push),
        IntervalTrigger(seconds=PUSH_DRAIN_INTERVAL.total_seconds()),
        args=[ctx],
        id="drain_push",
        next_run_time=start,
    )
    scheduler.add_job(
        _tracked(purge_pushes), IntervalTrigger(hours=1), args=[ctx], id="purge_pushes", next_run_time=start
    )
    # No misfire grace, as for the backup: a host asleep at 04:15 prunes when it wakes.
    scheduler.add_job(
        _tracked(prune_outbox),
        _prune_trigger(ctx.zone),
        args=[ctx],
        id="prune_outbox",
        misfire_grace_time=None,
    )
    scheduler.add_job(
        _tracked(refresh_weather),
        IntervalTrigger(seconds=WEATHER_INTERVAL.total_seconds()),
        args=[ctx],
        id="refresh_weather",
        next_run_time=start,
    )

    async def follow(job_ctx: JobContext) -> None:
        await follow_zone(job_ctx, scheduler)

    scheduler.add_job(
        _tracked(follow),
        IntervalTrigger(seconds=ZONE_CHECK_INTERVAL.total_seconds()),
        args=[ctx],
        id="follow_zone",
    )
    return scheduler
