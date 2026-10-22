# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nightly SQLite online backups into the backups directory beside the database."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from dinnerbell.domain.backups import backups_to_delete

log = logging.getLogger(__name__)
PREFIX = "dinnerbell-"
SUFFIX = ".db"
_NAME = re.compile(r"^dinnerbell-(\d{4}-\d{2}-\d{2})\.db$")


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


def last_backup_time(backup_dir: Path) -> datetime | None:
    backups = existing_backups(backup_dir)
    if not backups:
        return None
    newest = backups[max(backups)]
    return datetime.fromtimestamp(newest.stat().st_mtime, UTC)


def run_backup(database_path: Path, backup_dir: Path, today: date) -> BackupResult:
    """Back up ``database_path`` for ``today`` and prune old copies. Idempotent per day."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_path(backup_dir, today)
    created = False
    if not target.exists():
        _copy_online(database_path, target)
        created = True
        log.info("database backup written", extra={"backup": target.name})
    deleted = prune(backup_dir)
    return BackupResult(path=target, created=created, deleted=deleted)


def prune(backup_dir: Path) -> tuple[Path, ...]:
    backups = existing_backups(backup_dir)
    doomed = tuple(sorted(backups[day] for day in backups_to_delete(backups)))
    for path in doomed:
        path.unlink(missing_ok=True)
        log.info("old backup removed", extra={"backup": path.name})
    return doomed


def _copy_online(database_path: Path, target: Path) -> None:
    partial = target.with_suffix(".partial")
    source = sqlite3.connect(database_path)
    try:
        destination = sqlite3.connect(partial)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    os.replace(partial, target)
