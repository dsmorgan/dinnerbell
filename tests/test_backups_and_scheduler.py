# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nightly backups and scheduler jobs with a fake clock."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from dinnerbell.config import Settings
from dinnerbell.db.engine import create_app_engine, create_sessionmaker
from dinnerbell.scheduler import runner
from dinnerbell.services import backups
from dinnerbell.services.health import SchedulerState
from tests.support.clock import FakeClock


def make_backup(directory: Path, day: date) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = backups.backup_path(directory, day)
    path.write_bytes(b"")
    return path


def test_backup_is_a_consistent_copy(migrated: Settings) -> None:
    result = backups.run_backup(migrated.database_path, migrated.backup_dir, date(2026, 10, 22))
    assert result.created
    assert result.path == migrated.backup_dir / "dinnerbell-2026-10-22.db"
    with sqlite3.connect(result.path) as copy:
        tables = {row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"household", "member", "session", "audit"} <= tables
    assert not list(migrated.backup_dir.glob("*.partial"))


def test_backup_is_idempotent_per_day(migrated: Settings) -> None:
    first = backups.run_backup(migrated.database_path, migrated.backup_dir, date(2026, 10, 22))
    mtime = first.path.stat().st_mtime_ns
    second = backups.run_backup(migrated.database_path, migrated.backup_dir, date(2026, 10, 22))
    assert not second.created
    assert second.path.stat().st_mtime_ns == mtime


def test_backup_prunes_by_retention(migrated: Settings) -> None:
    today = date(2026, 10, 22)
    for i in range(1, 60):
        make_backup(migrated.backup_dir, today - timedelta(days=i))
    stranger = migrated.backup_dir / "notes.txt"
    stranger.write_text("not ours")
    result = backups.run_backup(migrated.database_path, migrated.backup_dir, today)
    remaining = backups.existing_backups(migrated.backup_dir)
    assert len(remaining) == 14 + 5  # see test_domain.TestBackupRetention
    assert today in remaining
    assert len(result.deleted) == 60 - len(remaining)
    assert stranger.exists()


def test_last_backup_time(tmp_path: Path) -> None:
    assert backups.last_backup_time(tmp_path / "none") is None
    make_backup(tmp_path, date(2026, 1, 1))
    assert backups.last_backup_time(tmp_path) is not None


@pytest.fixture
def job_context(migrated: Settings, clock: FakeClock) -> runner.JobContext:
    engine = create_app_engine(migrated.async_database_url)
    return runner.JobContext(
        state=SchedulerState(),
        clock=clock,
        zone=ZoneInfo(migrated.timezone),
        database_path=migrated.database_path,
        backup_dir=migrated.backup_dir,
        sessionmaker=create_sessionmaker(engine),
    )


@pytest.mark.anyio
async def test_heartbeat_records_tick(job_context: runner.JobContext, clock: FakeClock) -> None:
    await runner.heartbeat(job_context)
    assert job_context.state.last_tick == clock.now
    assert job_context.state.tick_is_fresh(clock.now + timedelta(seconds=89))
    assert not job_context.state.tick_is_fresh(clock.now + timedelta(seconds=91))


@pytest.mark.anyio
async def test_nightly_backup_uses_household_date(job_context: runner.JobContext, clock: FakeClock) -> None:
    # 2026-10-22 18:00 UTC, then 04:00 UTC the next day, which is still 2026-10-22 in Chicago.
    clock.advance(timedelta(hours=10))
    await runner.nightly_backup(job_context)
    assert list(backups.existing_backups(job_context.backup_dir)) == [date(2026, 10, 22)]
    assert job_context.state.last_backup is not None
    await runner.nightly_backup(job_context)  # same day again: nothing new
    assert len(backups.existing_backups(job_context.backup_dir)) == 1


@pytest.mark.anyio
async def test_backup_failure_is_logged_not_raised(
    job_context: runner.JobContext, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(backups, "run_backup", explode)
    await runner.nightly_backup(job_context)
    assert job_context.state.last_backup is None
    assert "database backup failed" in caplog.text


@pytest.mark.anyio
async def test_purge_sessions_job(job_context: runner.JobContext) -> None:
    await runner.purge_sessions(job_context)  # nothing to purge; must not fail


async def _started_jobs(scheduler: Any) -> dict[str, Any]:
    """Start paused so job defaults and next run times are filled in, then stop."""
    scheduler.start(paused=True)
    try:
        return {job.id: job for job in scheduler.get_jobs()}
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.anyio
async def test_build_scheduler_schedules_jobs(job_context: runner.JobContext) -> None:
    from datetime import UTC

    scheduler = runner.build_scheduler(job_context)
    jobs = await _started_jobs(scheduler)
    # Regression: catch-up used to be a second job that could overlap the nightly one and
    # delete its .partial file. Now there is one backup job, moved to run now.
    assert set(jobs) == {"heartbeat", "nightly_backup", "purge_sessions"}
    backup = jobs["nightly_backup"]
    assert "hour='3'" in str(backup.trigger) and "minute='30'" in str(backup.trigger)
    assert backup.max_instances == 1
    assert backup.next_run_time - datetime.now(UTC) < timedelta(minutes=1)


@pytest.mark.anyio
async def test_no_catch_up_when_recent_backup_exists(job_context: runner.JobContext, clock: FakeClock) -> None:
    import os
    from datetime import UTC

    path = make_backup(job_context.backup_dir, clock.now.date())
    os.utime(path, (clock.now.timestamp(), clock.now.timestamp()))
    jobs = await _started_jobs(runner.build_scheduler(job_context))
    nightly = jobs["nightly_backup"].next_run_time.astimezone(job_context.zone)
    assert (nightly.hour, nightly.minute) == (3, 30)
    assert nightly > datetime.now(UTC)


@pytest.mark.anyio
async def test_stop_waits_for_cancelled_jobs_to_clean_up(job_context: runner.JobContext) -> None:
    """Regression: shutdown used to dispose the engine while a cancelled job was still unwinding."""
    import asyncio
    from datetime import UTC

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    started = asyncio.Event()
    cleaned_up: list[bool] = []

    async def slow_job(_ctx: runner.JobContext) -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0.05)  # async cleanup, like a session rollback
            cleaned_up.append(True)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(runner._tracked(slow_job), args=[job_context], next_run_time=datetime.now(UTC))
    scheduler.start()
    await asyncio.wait_for(started.wait(), timeout=5)
    assert len(job_context.running) == 1

    await runner.stop_scheduler(scheduler, job_context)
    assert cleaned_up == [True]
    assert job_context.running == set()


def test_stale_partial_backups_are_removed(migrated: Settings) -> None:
    migrated.backup_dir.mkdir(parents=True)
    stale = migrated.backup_dir / "dinnerbell-2026-10-01.partial"
    stale.write_bytes(b"half a database")
    backups.run_backup(migrated.database_path, migrated.backup_dir, date(2026, 10, 22))
    assert not stale.exists()


def test_backup_opens_the_live_database_read_only(migrated: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[tuple[str, bool]] = []
    real_connect = sqlite3.connect

    def recording_connect(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        opened.append((str(database), bool(kwargs.get("uri"))))
        return real_connect(database, *args, **kwargs)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(backups.sqlite3, "connect", recording_connect)
    backups.run_backup(migrated.database_path, migrated.backup_dir, date(2026, 10, 22))
    source = opened[0]
    assert source[0].startswith("file:///") and source[0].endswith("dinnerbell.db?mode=ro")
    assert source[1] is True
