# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``dinnerbell`` command: ``serve``, ``migrate``, ``backup``, ``init``, ``config``, ``catalog``,
``slack``, ``reset-history``, ``set-timezone``, ``set-household-name``, ``admin``, and, on a test
instance only, ``scrub-test`` and ``discard-call``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from dinnerbell import __version__, catalog_cli, init_cli
from dinnerbell.config import (
    LEGACY_WARNINGS,
    ConfigError,
    Settings,
    is_test_instance,
    load_settings,
    retired_feature_names,
    unknown_setting_names,
)
from dinnerbell.db.migrate import (
    IntegrityCheckFailed,
    MigrationLocked,
    SchemaTooNewError,
    UpgradeBlocked,
    run_migrations,
)
from dinnerbell.logs import configure_logging, start_unit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("dinnerbell")
DEFAULT_PORT = 8013
PRIVATE_UMASK = 0o077
"""The database, its WAL files, and backups hold member emails; keep them owner-only."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dinnerbell", description="Household meal coordination.")
    parser.add_argument("--version", action="version", version=f"dinnerbell {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    serve = commands.add_parser("serve", help="migrate the database, then run the web app and scheduler")
    serve.add_argument("--host", default="0.0.0.0", help="address to listen on (default: 0.0.0.0)")  # noqa: S104 container
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT})")
    commands.add_parser(
        "migrate",
        help="upgrade the database schema and exit",
        description="Upgrade the database schema and exit. If the database needs migrating, first back it up "
        "to backups/pre-migrate-FROM-TO-TIME.db, and stop if the backup fails. serve does the same.",
    )
    commands.add_parser(
        "backup",
        help="back up the database now, such as before an upgrade",
        description="Copy the database to backups/pre-upgrade-VERSION-TIME.db, check the copy with SQLite's "
        "quick_check, flush it to disk, and print its path. Changes nothing else, and never migrates. Run it in "
        "the running container.",
    )
    init_cli.add_parser(commands)
    config = commands.add_parser("config", help="configuration tools")
    config_commands = config.add_subparsers(dest="config_command", required=True, metavar="COMMAND")
    check = config_commands.add_parser(
        "check",
        help="check the DINNERBELL_* settings and exit",
        description="Read the settings from the environment and the secret files, as serve does, and name every one "
        "that is missing or invalid. Never prints a value. Exits 0 when the configuration is valid, 2 when it isn't. "
        "Touches no database.",
    )
    check.add_argument(
        "--env-file",
        metavar="FILE",
        help="also read settings from FILE, in compose's .env syntax, over the environment; - reads stdin",
    )
    check.add_argument(
        "--secrets-dir",
        metavar="DIR",
        help="read the secret files from DIR instead of /run/secrets",
    )
    scrub = commands.add_parser(
        "scrub-test",
        help="make a restored production backup safe for a test instance",
        description="On a labeled instance (DINNERBELL_INSTANCE_LABEL), deactivate and unlink every member "
        "not kept, turn off every slot, end open calls quietly, and empty the outbox. Stop the instance first.",
    )
    scrub.add_argument(
        "--keep",
        action="append",
        required=True,
        metavar="EMAIL",
        help="a member to keep linked and active; repeat for each. At least one must be an active admin.",
    )
    scrub.add_argument(
        "--yes",
        action="store_true",
        help="skip the typed confirmation, after checking the database it names; needed without a terminal",
    )
    discard = commands.add_parser(
        "discard-call",
        help="on a test instance, delete one call and everything in it",
        description="On a labeled instance (DINNERBELL_INSTANCE_LABEL), delete the call EVENT_ID and everything "
        "in it, and mark its Slack messages discarded, so a fresh call can start for that slot and day. Shows "
        "what it will delete first. The running instance sends the Slack edits.",
    )
    discard.add_argument("event_id", type=_event_id, metavar="EVENT_ID", help="the call's ID, as in /events/ID")
    discard.add_argument(
        "--yes",
        action="store_true",
        help="skip the typed confirmation, after checking the call it names; needed without a terminal",
    )
    reset = commands.add_parser(
        "reset-history",
        help="before going live, delete calls, meals, and feedback, and keep the setup",
        description="Delete every call and everything in it, the meal log, feedback on meals, the outbox, and the "
        "contribution counts and badges earned from them. "
        "Keep the catalog, members and roles, Slack links, preferences and vetoes, the schedule and settings, "
        "sign-ins, and the audit log. Shows what it will delete first, backs up the database to "
        "backups/pre-reset-VERSION-TIME.db, and refuses while a call is open. Works on production; run it in "
        "the running container.",
    )
    reset.add_argument(
        "--yes",
        action="store_true",
        help="skip the typed confirmation, after checking what it will delete; needed without a terminal",
    )
    set_timezone = commands.add_parser(
        "set-timezone",
        help="change the household's time zone",
        description="Change the household's time zone to ZONE. Refuses while a call is open, scheduled calls "
        "included. Prints the old and new zone, and writes an audit row. The running app moves the nightly "
        "backup and the nightly notification prune to the new zone within a minute. Set DINNERBELL_TIMEZONE "
        "to ZONE too, or each start warns that it differs. Run it in the running container.",
    )
    set_timezone.add_argument("zone", metavar="ZONE", help="an IANA time zone name, such as America/Chicago")
    set_name = commands.add_parser(
        "set-household-name",
        help="rename the household",
        description="Change the name the app shows to NAME, up to 80 characters. Prints the old and new name, and "
        "writes an audit row. Pages show it on their next load. Set DINNERBELL_HOUSEHOLD_NAME to NAME too, or "
        "each start warns that it differs. Run it in the running container.",
    )
    set_name.add_argument("name", metavar="NAME", help="the new name; quote it if it has spaces")
    admin = commands.add_parser("admin", help="emergency admin tools, for when no admin can sign in")
    admin_commands = admin.add_subparsers(dest="admin_command", required=True, metavar="COMMAND")
    add_admin = admin_commands.add_parser(
        "add",
        help="make the member with EMAIL an admin that their next sign-in claims",
        description="Make the member with EMAIL an active admin: invite them if there's none, promote or "
        "reactivate them, and clear their sign-in and Slack links, so their next sign-in with EMAIL claims it. "
        "Prints what it did by member ID, and writes an audit row. Run it in the running container.",
    )
    add_admin.add_argument("email", metavar="EMAIL", help="the email they sign in with")
    catalog_cli.add_parser(commands)
    slack = commands.add_parser("slack", help="Slack tools that need no workspace")
    slack_commands = slack.add_subparsers(dest="slack_command", required=True, metavar="COMMAND")
    preview = slack_commands.add_parser(
        "preview",
        help="print a sample message's Block Kit JSON, for Slack's Block Kit Builder",
        description="Print Block Kit JSON for a sample message, modal, or Home tab. Paste it into "
        "https://app.slack.com/block-kit-builder to see it. With no KIND, list the kinds.",
    )
    preview.add_argument("kind", nargs="?", metavar="KIND", help="what to preview; omit to list the kinds")
    preview.add_argument("--base-url", default="https://dinnerbell.example.com", help="web app URL for links")
    return parser


def _event_id(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("must be a call's ID, a whole number such as 12")
    return number


@contextmanager
def _environment(values: Mapping[str, str]) -> Iterator[None]:
    """Set ``values`` in ``os.environ`` for the duration, then put back what was there."""
    saved = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous


def _config_check(env_file: str | None, secrets_dir: str | None) -> int:
    """Validate the settings ``serve`` would read. The deploy/dinnerbell-test wrapper runs this in a
    one-off container before it starts test, with the env file on stdin, so the file is parsed by
    python-dotenv, as podman-compose 1.2.0 and later parse an ``env_file``.

    It prints errors and "Configuration is valid.", nothing else: ``serve`` logs the warnings, about
    a secret in the environment, an unknown setting, or a retired flag, on every start. Logging isn't
    set up here, and a warning logged anyway would reach the terminal through Python's last-resort
    handler, so the secret warnings are collected and dropped."""
    from dotenv import dotenv_values

    values: dict[str, str] = {}
    if env_file is not None:
        try:
            if env_file == "-":
                parsed = dotenv_values(stream=sys.stdin)
            else:
                with open(env_file, encoding="utf-8") as stream:
                    parsed = dotenv_values(stream=stream)
        except (OSError, UnicodeDecodeError) as exc:
            # Never the exception text: a decode error quotes bytes from the file.
            print(f"Can't read the env file ({type(exc).__name__}).", file=sys.stderr)
            return 2
        # A bare NAME line passes a variable through from the engine's environment; there's none here.
        values = {name: value for name, value in parsed.items() if value is not None}
    with _environment(values):
        try:
            load_settings(Path(secrets_dir) if secrets_dir else None, warnings=[])  # serve logs them
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    print("Configuration is valid.")
    return 0


def _slack_preview(kind: str | None, base_url: str) -> int:
    from dinnerbell.channels.slack.preview import KINDS, preview

    if kind is None:
        print("\n".join(KINDS))
        return 0
    if kind not in KINDS:
        print(f"Unknown kind {kind!r}. Choose one of: {', '.join(KINDS)}", file=sys.stderr)
        return 2
    print(json.dumps(preview(kind, base_url.rstrip("/")), indent=2, ensure_ascii=False))
    return 0


MIGRATE_BACKS_UP = frozenset({"serve", "migrate"})
"""The commands that back up a database that needs migrating, before they migrate it. The others
that migrate either back up first anyway (reset-history) or run on test (scrub-test, discard-call),
whose copy of production's data comes from a backup."""


def _migrate(settings: Settings, *, back_up: bool = False) -> bool:
    from dinnerbell.services.backups import MigrationBackupFailed, backup_before_migrating
    from dinnerbell.services.clock import utc_now

    try:
        if back_up:
            backup_before_migrating(settings.database_path, settings.backup_dir, now=utc_now())
        run_migrations(settings.sync_database_url, settings.database_path)
    except MigrationBackupFailed as exc:
        log.critical(str(exc))
        return False
    except (SchemaTooNewError, IntegrityCheckFailed, MigrationLocked, UpgradeBlocked) as exc:
        log.critical(str(exc))
        return False
    return True


NEEDS_HOUSEHOLD = frozenset({"admin", "set-timezone", "set-household-name"})
"""The commands that change an existing household, which refuse without a database instead of
migrating an empty one into being."""

CONFIRM_WORD = "scrub"


def _confirm_scrub(settings: Settings, yes: bool) -> bool:
    """Show which database the scrub would change and what's in it, and ask for a typed
    confirmation, before anything (even a migration) writes to it. ``--yes`` skips the question;
    without a terminal to ask on, it's required."""
    from sqlalchemy.exc import SQLAlchemyError

    from dinnerbell.services.clock import utc_now
    from dinnerbell.services.database import open_readonly_database
    from dinnerbell.services.scrub import preview

    path = settings.database_path
    if not path.exists():
        print(f"No database at {path}. Nothing changed.", file=sys.stderr)
        return False

    async def read() -> list[str] | None:
        database = open_readonly_database(path)  # read-only: no journal-mode change, no -wal or -shm files
        try:
            async with database.sessionmaker() as db:
                found = await preview(db)
        finally:
            await database.close()
        return None if found is None else found.lines(utc_now())

    try:
        lines = asyncio.run(read())
    except SQLAlchemyError as exc:
        print(f"Can't read the database at {path} ({type(exc).__name__}). Nothing changed.", file=sys.stderr)
        return False
    if lines is None:
        print(f"The database at {path} has no household yet, so there's nothing to scrub.", file=sys.stderr)
        return False
    print("scrub-test will change this database:")
    print("\n".join(f"  {line}" for line in [f"Database: {path}", *lines]))
    if yes:
        return True
    if not sys.stdin.isatty():
        print("No terminal to confirm on. Check the database above, then run again with --yes.", file=sys.stderr)
        return False
    try:
        answer = input(f'Type "{CONFIRM_WORD}" to scrub it: ')
    except EOFError:
        answer = ""
    if answer.strip() != CONFIRM_WORD:
        print("Not scrubbed. Nothing changed.", file=sys.stderr)
        return False
    return True


def _scrub_test(settings: Settings, keep: list[str]) -> int:
    from dinnerbell.services.clock import utc_now
    from dinnerbell.services.database import open_database
    from dinnerbell.services.scrub import ScrubRefused, scrub_test

    async def scrub() -> list[str]:
        database = open_database(settings.async_database_url)
        try:
            async with database.sessionmaker() as db:
                summary = await scrub_test(db, instance_label=settings.instance_label, keep=keep, now=utc_now())
        finally:
            await database.close()
        return summary.lines()

    try:
        lines = asyncio.run(scrub())
    except ScrubRefused as exc:
        print(exc, file=sys.stderr)
        return 1
    print("Scrubbed for test.")
    print("\n".join(f"  {line}" for line in lines))
    return 0


DISCARD_WORD = "discard"


def _discard_call(settings: Settings, event_id: int, yes: bool) -> int:
    """Show what discarding ``event_id`` would delete, ask for a typed confirmation unless
    ``--yes``, then discard it. The instance's label was checked before anything opened the
    database."""
    from dinnerbell.services import discard
    from dinnerbell.services.clock import utc_now
    from dinnerbell.services.database import open_database
    from dinnerbell.services.errors import NotFound
    from dinnerbell.services.household import NoHousehold, current_household

    async def with_database(work: Callable[[AsyncSession, int], Awaitable[list[str]]]) -> list[str]:
        database = open_database(settings.async_database_url)
        try:
            async with database.sessionmaker() as db:
                household = await current_household(db)
                return await work(db, household.id)
        finally:
            await database.close()

    async def show(db: AsyncSession, household_id: int) -> list[str]:
        return (await discard.preview(db, household_id, event_id)).lines()

    async def act(db: AsyncSession, household_id: int) -> list[str]:
        summary = await discard.discard_call(
            db, household_id, event_id, actor=None, instance_label=settings.instance_label, now=utc_now()
        )
        return summary.lines()

    try:
        lines = asyncio.run(with_database(show))
        print("discard-call will delete this call and everything in it:")
        print("\n".join(f"  {line}" for line in [f"Database: {settings.database_path}", *lines]))
        if not yes:
            if not sys.stdin.isatty():
                print("No terminal to confirm on. Check the call above, then run again with --yes.", file=sys.stderr)
                return 1
            try:
                answer = input(f'Type "{DISCARD_WORD}" to discard it: ')
            except EOFError:
                answer = ""
            if answer.strip() != DISCARD_WORD:
                print("Not discarded. Nothing changed.", file=sys.stderr)
                return 1
        lines = asyncio.run(with_database(act))
    except NoHousehold:
        print("The database has no household yet, so there's no call to discard.", file=sys.stderr)
        return 1
    except NotFound:
        print(f"There's no call with ID {event_id}. Nothing changed.", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


RESET_WORD = "reset"


def _confirm_reset(settings: Settings, yes: bool) -> bool:
    """Show what the reset would delete and keep, refuse while a call is open, and ask for a
    typed confirmation unless ``--yes``. It reads the database read-only, before anything (even
    a migration or the backup) writes."""
    from sqlalchemy.exc import SQLAlchemyError

    from dinnerbell.services import reset
    from dinnerbell.services.clock import utc_now
    from dinnerbell.services.database import open_readonly_database

    path = settings.database_path
    if not path.exists():
        print(f"No database at {path}. Nothing changed.", file=sys.stderr)
        return False

    async def read() -> reset.ResetPreview | None:
        database = open_readonly_database(path)
        try:
            async with database.sessionmaker() as db:
                return await reset.preview(db, now=utc_now())
        finally:
            await database.close()

    try:
        found = asyncio.run(read())
    except SQLAlchemyError as exc:
        print(f"Can't read the database at {path} ({type(exc).__name__}). Nothing changed.", file=sys.stderr)
        return False
    if found is None:
        print(f"The database at {path} has no household yet, so there's no history to reset.", file=sys.stderr)
        return False
    print("reset-history will delete this household's history and keep its setup:")
    print("\n".join(f"  {line}" for line in [f"Database: {path}", *found.lines()]))
    if found.open_calls:
        print(reset.refusal(found.open_calls), file=sys.stderr)
        return False
    if yes:
        return True
    if not sys.stdin.isatty():
        print("No terminal to confirm on. Check the counts above, then run again with --yes.", file=sys.stderr)
        return False
    try:
        answer = input(f'Type "{RESET_WORD}" to delete the history: ')
    except EOFError:
        answer = ""
    if answer.strip() != RESET_WORD:
        print("Not reset. Nothing changed.", file=sys.stderr)
        return False
    return True


def _backup_before_reset(settings: Settings) -> Path | None:
    """Back up the database as ``pre-reset-VERSION-TIME.db``, the way upgrades are backed up."""
    import sqlite3

    from dinnerbell.services.backups import BackupCheckFailed, one_off_name, run_one_off_backup
    from dinnerbell.services.clock import utc_now

    name = one_off_name("reset", __version__, utc_now())
    try:
        backup = run_one_off_backup(settings.database_path, settings.backup_dir, name, require_tables=("event",))
    except BackupCheckFailed as exc:
        print(f"Can't back up the database: {exc}. Nothing changed.", file=sys.stderr)
        return None
    except (OSError, sqlite3.Error) as exc:
        print(f"Can't back up the database ({type(exc).__name__}). Nothing changed.", file=sys.stderr)
        return None
    print(f"Backed up the database to {backup}")
    return backup


def _backup_now(settings: Settings) -> int:
    """Back up the database as ``pre-upgrade-VERSION-TIME.db``, checked, without migrating it."""
    import sqlite3

    from dinnerbell.services.backups import BackupCheckFailed, one_off_name, run_one_off_backup
    from dinnerbell.services.clock import utc_now

    if not settings.database_path.is_file():
        print(f"No database at {settings.database_path}. Nothing to back up.", file=sys.stderr)
        return 1
    name = one_off_name("upgrade", __version__, utc_now())
    try:
        backup = run_one_off_backup(
            settings.database_path, settings.backup_dir, name, require_tables=("alembic_version",)
        )
    except BackupCheckFailed as exc:
        print(f"Can't back up the database: {exc}.", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as exc:
        print(f"Can't back up the database ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(f"Backed up the database to {backup}")
    return 0


def _reset_history(settings: Settings, backup: Path) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from dinnerbell.services.clock import utc_now
    from dinnerbell.services.database import open_database
    from dinnerbell.services.reset import ResetRefused, reset_history

    async def run() -> list[str]:
        database = open_database(settings.async_database_url)
        try:
            async with database.sessionmaker() as db:
                summary = await reset_history(db, backup=backup.name, now=utc_now())
        finally:
            await database.close()
        return summary.lines()

    try:
        lines = asyncio.run(run())
    except ResetRefused as exc:
        print(exc, file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:  # rolled back: write_transaction ends uncommitted work
        print(f"Can't reset the history ({type(exc).__name__}). Nothing changed. Backup: {backup}", file=sys.stderr)
        return 1
    print("Reset the history.")
    print("\n".join(f"  {line}" for line in lines))
    return 0


def _admin_add(settings: Settings, email: str) -> int:
    """``admin add``: make the member with ``email`` an admin that their next sign-in claims (#360)."""
    from dinnerbell.domain.members import InvalidMemberInput
    from dinnerbell.services.admin_add import add_admin
    from dinnerbell.services.clock import utc_now
    from dinnerbell.services.database import open_database
    from dinnerbell.services.household import NoHousehold

    async def run() -> list[str]:
        database = open_database(settings.async_database_url)
        try:
            async with database.sessionmaker() as db:
                return (await add_admin(db, email, now=utc_now())).lines()
        finally:
            await database.close()

    try:
        lines = asyncio.run(run())
    except InvalidMemberInput as exc:
        print(f"{exc} Nothing changed.", file=sys.stderr)
        return 2
    except NoHousehold:
        print("The database has no household yet. Start Dinnerbell once, then run this again.", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


def _set_household(settings: Settings, command: str, value: str) -> int:
    """``set-timezone`` and ``set-household-name`` (#334): change what the settings only seeded."""
    from dinnerbell.domain.household import InvalidHouseholdInput
    from dinnerbell.services import household
    from dinnerbell.services.clock import utc_now
    from dinnerbell.services.database import open_database

    timezone = command == "set-timezone"

    async def run() -> household.Changed:
        database = open_database(settings.async_database_url)
        try:
            async with database.sessionmaker() as db:
                if timezone:
                    return await household.set_timezone(db, value, now=utc_now())
                return await household.set_name(db, value, now=utc_now())
        finally:
            await database.close()

    try:
        result = asyncio.run(run())
    except InvalidHouseholdInput as exc:
        print(f"{exc} Nothing changed.", file=sys.stderr)
        return 2
    except household.NoHousehold:
        print("The database has no household yet. Start Dinnerbell once, then run this again.", file=sys.stderr)
        return 1
    except household.TimezoneRefused as exc:
        print(exc, file=sys.stderr)
        return 1
    if timezone and not result.changed:
        print(f"The household's time zone is already {result.after}. Nothing changed.")
    elif timezone:
        print(f"Changed the household's time zone from {result.before} to {result.after}.")
        print("The running app moves the nightly backup and notification prune to the new zone within a minute.")
        print(f"Set DINNERBELL_TIMEZONE={result.after} in your env file too, or each start warns that it differs.")
    elif not result.changed:
        print(f"The household is already named {result.after!r}. Nothing changed.")
    else:
        print(f"Renamed the household from {result.before!r} to {result.after!r}. Pages show it on their next load.")
        print("Set DINNERBELL_HOUSEHOLD_NAME to it in your env file too, or each start warns that it differs.")
    return 0


def _serve(settings: Settings, host: str, port: int) -> None:
    import uvicorn

    from dinnerbell.app_factory import create_app

    uvicorn.run(
        create_app(settings, prewarm_badge_art=True),
        host=host,
        port=port,
        log_config=None,
        access_log=False,
        proxy_headers=False,  # Dinnerbell resolves client IPs itself from DINNERBELL_TRUSTED_PROXIES
        server_header=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "catalog" and args.catalog_command != "export":
        return catalog_cli.run(args)
    if args.command == "slack":
        return _slack_preview(args.kind, args.base_url)
    if args.command == "config":
        return _config_check(args.env_file, args.secrets_dir)
    if args.command == "init":
        return init_cli.run(args)
    warnings: list[str] = []
    try:
        settings = load_settings(warnings=warnings)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "catalog":
        # No secret warning here: stdout carries the export, and `serve` warns on every start.
        # Before logging is configured: logs go to stdout, where the export goes. The read-only
        # open may create -wal and -shm files next to the database; SQLite gives them the
        # database file's own mode, whatever the umask, so they're as private as the database,
        # which the app creates owner-only. The umask covers anything else this creates.
        os.umask(PRIVATE_UMASK)
        return catalog_cli.export(settings.database_path, args.output, force=args.force)
    if args.command == "scrub-test" and not is_test_instance(settings.instance_label):
        from dinnerbell.services.scrub import NOT_LABELED

        print(NOT_LABELED, file=sys.stderr)
        return 1  # before migrating: never touch a database that may be production's
    if args.command == "discard-call" and not is_test_instance(settings.instance_label):
        from dinnerbell.services.discard import NOT_LABELED as NOT_LABELED_FOR_DISCARD

        print(NOT_LABELED_FOR_DISCARD, file=sys.stderr)
        return 1  # the same: before anything opens the database
    configure_logging(settings.log_level, settings.instance_label)
    for field in warnings:  # secrets still in the environment: constant messages, never values
        logging.getLogger("dinnerbell.config").warning(LEGACY_WARNINGS[field])
    if unknown := unknown_setting_names():  # names only: a typo may sit next to a real value
        logging.getLogger("dinnerbell.config").warning(
            "ignoring environment variables that match no setting (a typo?): %s", ", ".join(unknown)
        )
    if retired := retired_feature_names():
        logging.getLogger("dinnerbell.config").warning(
            "ignoring retired feature flags in DINNERBELL_FEATURES; remove them: %s", ", ".join(retired)
        )
    if args.command == "serve":  # the server's requests, taps, and jobs each start their own unit
        return _command(args, settings)
    end = start_unit("cli")
    try:
        return _command(args, settings)
    finally:
        end()


def _command(args: argparse.Namespace, settings: Settings) -> int:
    """Run a command that needs the database, once logging is configured."""
    os.umask(PRIVATE_UMASK)
    if args.command == "backup":  # never migrates: it backs up the database as the running release left it
        return _backup_now(settings)
    if args.command == "scrub-test" and not _confirm_scrub(settings, args.yes):
        return 1
    if args.command in NEEDS_HOUSEHOLD and not settings.database_path.is_file():
        print(f"No database at {settings.database_path}. Nothing changed.", file=sys.stderr)
        return 1  # before migrating, which would create an empty one
    backup: Path | None = None
    if args.command == "reset-history":
        # Before migrating: the backup is of the database as the operator found it.
        if not _confirm_reset(settings, args.yes):
            return 1
        backup = _backup_before_reset(settings)
        if backup is None:
            return 1
    if not _migrate(settings, back_up=args.command in MIGRATE_BACKS_UP):
        return 1
    if args.command == "serve":
        _serve(settings, args.host, args.port)
    if args.command == "scrub-test":
        return _scrub_test(settings, args.keep)
    if args.command == "discard-call":
        return _discard_call(settings, args.event_id, args.yes)
    if args.command == "reset-history" and backup is not None:
        return _reset_history(settings, backup)
    if args.command == "admin":
        return _admin_add(settings, args.email)
    if args.command == "set-timezone":
        return _set_household(settings, args.command, args.zone)
    if args.command == "set-household-name":
        return _set_household(settings, args.command, args.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
