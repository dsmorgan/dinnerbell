# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLite online backups into the backups directory beside the database: nightly, and one-off
copies before something risky."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from dinnerbell.db.migrate import pending_upgrade
from dinnerbell.domain.backups import backups_to_delete, one_offs_to_delete

log = logging.getLogger(__name__)
PREFIX = "dinnerbell-"
SUFFIX = ".db"
PARTIAL = ".partial"
BACKUP_ATTEMPTS = 2
# A one-off backup (``pre-*``) that is still being written is younger than this; an older
# ``pre-*.partial`` was left by a crash.
STALE_PARTIAL_AFTER = timedelta(hours=1)
_NAME = re.compile(r"^dinnerbell-(\d{4}-\d{2}-\d{2})\.db$")
_ONE_OFF = re.compile(r"^pre-([a-z]+)-.+-(\d{8}T\d{6}Z)\.db$")
"""A one-off backup's name, as ``one_off_name`` and ``migration_backup_name`` make it: its kind
and its UTC time."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    created: bool
    deleted: tuple[Path, ...]


def backup_path(backup_dir: Path, day: date) -> Path:
    return backup_dir / f"{PREFIX}{day.isoformat()}{SUFFIX}"


def existing_backups(backup_dir: Path) -> dict[date, Path]:
    if not backup_dir.is_dir():
        return {}
    found: dict[date, Path] = {}
    for path in backup_dir.iterdir():
        match = _NAME.match(path.name)
        if match and path.is_file():
            found[date.fromisoformat(match.group(1))] = path
    return found


def existing_one_offs(backup_dir: Path) -> dict[str, tuple[str, str]]:
    """Each one-off backup's name, with its kind and the UTC time in its name. A ``pre-*`` file
    named any other way isn't counted, so pruning leaves it alone."""
    if not backup_dir.is_dir():
        return {}
    found: dict[str, tuple[str, str]] = {}
    for path in backup_dir.iterdir():
        match = _ONE_OFF.match(path.name)
        if match and path.is_file():
            found[path.name] = (match.group(1), match.group(2))
    return found


def last_backup_time(backup_dir: Path) -> datetime | None:
    backups = existing_backups(backup_dir)
    if not backups:
        return None
    newest = backups[max(backups)]
    return datetime.fromtimestamp(newest.stat().st_mtime, UTC)


def run_backup(database_path: Path, backup_dir: Path, today: date, *, now: datetime) -> BackupResult:
    """Back up ``database_path`` for ``today`` and prune old copies. Idempotent per day. ``now``
    (the injected clock) dates the leftovers to clean up first, so they go even while backups fail."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    _remove_partials(backup_dir, now)
    target = backup_path(backup_dir, today)
    created = False
    if not target.exists():
        _copy_verified(database_path, target)
        created = True
        log.info("database backup written", extra={"backup": target.name})
    deleted = prune(backup_dir)
    return BackupResult(path=target, created=created, deleted=deleted)


def _copy_verified(database_path: Path, target: Path) -> None:
    """Copy the database to ``target`` and run ``quick_check`` on the copy. A copy that fails is
    deleted and made once more; one that fails twice is deleted, logged as an error, and raises
    BackupCheckFailed, so a bad file never counts as the day's backup."""
    for attempt in range(BACKUP_ATTEMPTS):
        try:
            _copy_online(database_path, target)
        except BaseException:
            target.with_suffix(PARTIAL).unlink(missing_ok=True)
            raise
        try:
            _check(target, ())
        except (BackupCheckFailed, sqlite3.DatabaseError) as exc:
            target.unlink(missing_ok=True)
            log.error("database backup failed its check", extra={"backup": target.name, "attempt": attempt + 1})
            if attempt == BACKUP_ATTEMPTS - 1:
                raise BackupCheckFailed("the nightly backup failed SQLite's quick_check twice") from exc
        else:
            return


def one_off_name(purpose: str, version: str, now: datetime) -> str:
    """The name of a backup taken before something risky, such as
    ``pre-reset-0.3.0-20261024T013000Z.db``: what it's for, the version, and the UTC time.
    ``dinnerbell backup`` names its copies ``pre-upgrade-...``."""
    return f"pre-{purpose}-{version}-{_stamp(now)}{SUFFIX}"


def migration_backup_name(from_revision: str, to_revision: str, now: datetime) -> str:
    """The name of the backup taken before migrating, such as
    ``pre-migrate-0040-to-0044-20261122T013000Z.db``: the two revisions' numbers and the UTC time."""
    return f"pre-migrate-{_revision_number(from_revision)}-to-{_revision_number(to_revision)}-{_stamp(now)}{SUFFIX}"


def _stamp(now: datetime) -> str:
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}"


def _revision_number(revision: str) -> str:
    """``0040`` for ``0040_orphan_dietary_needs``."""
    return revision.split("_", 1)[0]


class BackupCheckFailed(Exception):
    """A backup was written but didn't pass its checks, so it was deleted."""


def run_one_off_backup(
    database_path: Path, backup_dir: Path, name: str, *, require_tables: Collection[str] = ()
) -> Path:
    """Back up ``database_path`` now, as ``name`` in ``backup_dir``, and check the copy before
    anything relies on it: SQLite's ``quick_check`` passes, every table in ``require_tables``
    is there, and the file is on disk (fsync). Pruning keeps the newest ``ONE_OFF_COPIES`` of
    each kind (``domain.backups``). Raises FileExistsError rather than replace a backup, and
    BackupCheckFailed, deleting the copy, when a check fails. Leaves no partial copy."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / name
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    try:
        _copy_online(database_path, target)
    except BaseException:
        target.with_suffix(PARTIAL).unlink(missing_ok=True)
        raise
    try:
        _check(target, require_tables)
        _fsync(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    log.info("database backup written", extra={"backup": target.name})
    return target


class MigrationBackupFailed(Exception):
    """The backup before migrating failed, so the database wasn't migrated."""


def backup_before_migrating(database_path: Path, backup_dir: Path, *, now: datetime) -> Path | None:
    """If the database exists at a known revision older than this build's newest, back it up as
    ``pre-migrate-FROM-TO-TIME.db`` and return the copy's path, then prune old one-off copies, so a
    migration that fails and restarts in a loop keeps only a few. Returns None, backing up
    nothing, for a missing or empty database, one already current, or one at a revision this
    build doesn't know (``run_migrations`` refuses that). Raises MigrationBackupFailed, with a
    message for the operator that holds no database contents, when the backup fails."""
    pending = pending_upgrade(database_path)
    if pending is None:
        return None
    current, head = pending
    name = migration_backup_name(current, head, now)
    try:
        backup = run_one_off_backup(database_path, backup_dir, name, require_tables=("alembic_version",))
    except (BackupCheckFailed, OSError, sqlite3.Error) as exc:
        reason = str(exc) if isinstance(exc, BackupCheckFailed) else type(exc).__name__
        raise MigrationBackupFailed(
            f"Can't back up the database before migrating it from {current} to {head} ({reason}), so it "
            f"wasn't migrated and nothing was changed. Check that {backup_dir} has free space and is "
            "writable, then start again."
        ) from exc
    log.info(
        "database backed up before migrating",
        extra={"backup": backup.name, "from_revision": current, "to_revision": head},
    )
    prune(backup_dir)
    return backup


def _check(path: Path, require_tables: Collection[str]) -> None:
    # immutable: nothing else has the new copy open, and a WAL-mode copy has no -shm file that a
    # plain read-only connection could use; this one creates no files beside it.
    copy = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        result = copy.execute("PRAGMA quick_check").fetchall()
        tables = {row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        copy.close()
    if result != [("ok",)]:
        raise BackupCheckFailed(f"the backup failed SQLite's quick_check ({len(result)} problems)")
    missing = sorted(set(require_tables) - tables)
    if missing:
        raise BackupCheckFailed(f"the backup has no {', '.join(missing)} table")


def _fsync(path: Path) -> None:
    """Flush the file, and the directory entry that names it, to disk."""
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def prune(backup_dir: Path) -> tuple[Path, ...]:
    """Delete the nightly copies past their daily and weekly retention, and all but the newest
    few one-off copies of each kind. Leaves every other file alone."""
    backups = existing_backups(backup_dir)
    one_offs = existing_one_offs(backup_dir)
    doomed = tuple(
        sorted(
            [backups[day] for day in backups_to_delete(backups)]
            + [backup_dir / name for name in one_offs_to_delete(one_offs)]
        )
    )
    for path in doomed:
        path.unlink(missing_ok=True)
        log.info("old backup removed", extra={"backup": path.name})
    return doomed


def _remove_partials(backup_dir: Path, now: datetime) -> None:
    """Delete copies left half-written by a crash or restart mid-backup, nightly and one-off."""
    for path in backup_dir.glob(f"{PREFIX}*{PARTIAL}"):
        path.unlink(missing_ok=True)
        log.warning("removed incomplete backup", extra={"backup": path.name})
    # A one-off backup (reset-history) may be writing in another process: leave a fresh one.
    cutoff = now - STALE_PARTIAL_AFTER
    for path in backup_dir.glob(f"pre-*{PARTIAL}"):
        try:
            stale = datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff
        except FileNotFoundError:  # finished, or removed, since the glob
            continue
        if stale:
            path.unlink(missing_ok=True)
            log.warning("removed incomplete backup", extra={"backup": path.name})


def _copy_online(database_path: Path, target: Path) -> None:
    partial = target.with_suffix(PARTIAL)
    # Read-only: a backup must never be able to change the live database.
    source = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(partial)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    os.replace(partial, target)
