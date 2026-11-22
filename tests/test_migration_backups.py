# SPDX-License-Identifier: AGPL-3.0-or-later
"""The checked backup ``serve`` and ``migrate`` take before migrating, ``dinnerbell backup``, and
pruning of one-off backups (#333)."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command

from dinnerbell import __version__
from dinnerbell import main as cli
from dinnerbell.config import Settings
from dinnerbell.db.migrate import _alembic_config, head_revision, migrating, migration_engine, pending_upgrade
from dinnerbell.services import backups

OLD = "0040_orphan_dietary_needs"
NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def revision(path: Path) -> str:
    with sqlite3.connect(path) as db:
        return str(db.execute("SELECT version_num FROM alembic_version").fetchone()[0])


def files_in(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


@pytest.fixture
def behind(migrated: Settings) -> Settings:
    """A database at an older release's schema, as an upgrade finds it."""
    engine = migration_engine(migrated.sync_database_url)
    try:
        with migrating(engine) as connection:
            command.downgrade(_alembic_config(connection), OLD)
    finally:
        engine.dispose()
    return migrated


def test_migrate_backs_up_a_database_that_needs_migrating(behind: Settings, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        assert cli._command(argparse.Namespace(command="migrate"), behind) == 0
    (name,) = files_in(behind.backup_dir)
    to = head_revision().split("_")[0]
    assert re.fullmatch(rf"pre-migrate-0040-to-{to}-\d{{8}}T\d{{6}}Z\.db", name)
    copy = behind.backup_dir / name
    assert revision(copy) == OLD  # the database as the upgrade found it
    with sqlite3.connect(copy) as db:
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    assert copy.stat().st_mode & 0o077 == 0  # it holds member emails
    assert revision(behind.database_path) == head_revision()
    logged = [r for r in caplog.records if r.getMessage() == "database backed up before migrating"]
    assert [(r.levelno, r.backup, r.from_revision) for r in logged] == [(logging.INFO, name, OLD)]  # type: ignore[attr-defined]


def test_serve_backs_up_before_it_migrates(behind: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[str] = []
    monkeypatch.setattr(cli, "_serve", lambda settings, host, port: served.append(revision(settings.database_path)))
    assert cli._command(argparse.Namespace(command="serve", host="127.0.0.1", port=8013), behind) == 0
    assert served == [head_revision()]
    assert [name.split("-to-")[0] for name in files_in(behind.backup_dir)] == ["pre-migrate-0040"]


def test_a_current_database_gets_no_backup(migrated: Settings) -> None:
    assert cli._command(argparse.Namespace(command="migrate"), migrated) == 0  # already at head
    assert files_in(migrated.backup_dir) == []


def test_a_fresh_database_gets_no_backup(settings: Settings) -> None:
    assert not settings.database_path.exists()
    assert pending_upgrade(settings.database_path) is None
    assert cli._command(argparse.Namespace(command="migrate"), settings) == 0
    assert revision(settings.database_path) == head_revision()
    assert files_in(settings.backup_dir) == []


def test_an_empty_file_or_unknown_revision_needs_no_backup(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    empty.touch()
    assert pending_upgrade(empty) is None
    newer = tmp_path / "newer.db"
    with sqlite3.connect(newer) as db:
        db.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        db.execute("INSERT INTO alembic_version VALUES ('9999_from_the_future')")
    db.close()
    assert pending_upgrade(newer) is None  # run_migrations refuses it, changing nothing


@pytest.mark.parametrize("error", [OSError("No space left on device"), backups.BackupCheckFailed("2 problems")])
def test_a_failed_backup_stops_serve_before_it_migrates(
    behind: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    def failing(*args: object, **kwargs: object) -> Path:
        raise error

    monkeypatch.setattr(backups, "run_one_off_backup", failing)
    monkeypatch.setattr(cli, "_serve", lambda *args: pytest.fail("served without a backup"))
    assert cli._command(argparse.Namespace(command="serve", host="127.0.0.1", port=8013), behind) == 1
    assert revision(behind.database_path) == OLD  # not migrated
    (record,) = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    reason = "OSError" if isinstance(error, OSError) else "2 problems"
    assert record.getMessage().startswith(f"Can't back up the database before migrating it from {OLD} to ")
    assert f"({reason})" in record.getMessage() and "No space left" not in record.getMessage()


def test_backup_command_takes_a_checked_copy_without_migrating(
    behind: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli._parser().parse_args(["backup"]).command == "backup"
    assert cli._command(argparse.Namespace(command="backup"), behind) == 0
    (name,) = files_in(behind.backup_dir)
    assert re.fullmatch(rf"pre-upgrade-{re.escape(__version__)}-\d{{8}}T\d{{6}}Z\.db", name)
    assert capsys.readouterr().out == f"Backed up the database to {behind.backup_dir / name}\n"
    assert revision(behind.database_path) == OLD  # never migrates
    assert revision(behind.backup_dir / name) == OLD


def test_backup_command_reports_a_missing_database(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli._command(argparse.Namespace(command="backup"), settings) == 1
    assert "No database at" in capsys.readouterr().err
    assert not settings.database_path.exists()


def test_backup_command_reports_a_failed_check(
    migrated: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(path: Path, require_tables: object) -> None:
        raise backups.BackupCheckFailed("the backup failed SQLite's quick_check (2 problems)")

    monkeypatch.setattr(backups, "_check", failing)
    assert cli._command(argparse.Namespace(command="backup"), migrated) == 1
    assert "quick_check (2 problems)" in capsys.readouterr().err
    assert files_in(migrated.backup_dir) == []
    monkeypatch.setattr(backups, "_copy_online", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    assert cli._command(argparse.Namespace(command="backup"), migrated) == 1
    assert "Can't back up the database (OSError)." in capsys.readouterr().err


def one_off(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"")
    return path


def test_pruning_keeps_the_newest_five_of_each_one_off_kind(migrated: Settings) -> None:
    folder = migrated.backup_dir
    upgrades = [one_off(folder, f"pre-upgrade-0.{i}.0-202611{i + 1:02d}T013000Z.db") for i in range(7)]
    resets = [one_off(folder, f"pre-reset-0.3.0-2025010{i}T000000Z.db") for i in range(1, 3)]
    migrations = [one_off(folder, f"pre-migrate-00{i}0-to-0044-202611{i:02d}T010000Z.db") for i in range(1, 7)]
    unnamed = one_off(folder, "pre-upgrade-before-the-move.db")  # no time in its name: left alone
    result = backups.run_backup(migrated.database_path, folder, date(2026, 11, 22), now=NOW)
    assert sorted(result.deleted) == sorted([*upgrades[:2], migrations[0]])
    assert all(p.exists() for p in [*upgrades[2:], *resets, *migrations[1:], unnamed, result.path])


def test_a_restart_loop_keeps_only_a_few_migration_backups(behind: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A migration that fails restarts the container, and each start backs up again: pruning
    after each one keeps the folder from filling the disk."""
    from dinnerbell.db import migrate

    stamps = iter(range(10))

    def blocked(*args: object) -> None:
        raise migrate.UpgradeBlocked("a row it can't carry over")

    monkeypatch.setattr(cli, "run_migrations", blocked)
    monkeypatch.setattr(
        "dinnerbell.services.clock.utc_now", lambda: datetime(2026, 11, 22, 12, 0, next(stamps), tzinfo=UTC)
    )
    for _ in range(8):
        assert cli._command(argparse.Namespace(command="serve", host="127.0.0.1", port=8013), behind) == 1
    names = files_in(behind.backup_dir)
    assert len(names) == 5 and names[-1].endswith("T120007Z.db")
    assert os.stat(behind.database_path).st_size > 0 and revision(behind.database_path) == OLD
