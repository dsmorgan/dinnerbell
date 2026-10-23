# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``dinnerbell`` command: ``serve``, ``migrate``, ``catalog``, ``slack``, and ``scrub-test``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence

from dinnerbell import __version__, catalog_cli
from dinnerbell.config import ConfigError, Settings, load_settings
from dinnerbell.db.migrate import IntegrityCheckFailed, SchemaTooNewError, run_migrations
from dinnerbell.logs import configure_logging

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
    commands.add_parser("migrate", help="upgrade the database schema and exit")
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


def _migrate(settings: Settings) -> bool:
    try:
        run_migrations(settings.sync_database_url, settings.database_path)
    except (SchemaTooNewError, IntegrityCheckFailed) as exc:
        log.critical(str(exc))
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


def _serve(settings: Settings, host: str, port: int) -> None:
    import uvicorn

    from dinnerbell.app_factory import create_app

    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_config=None,
        access_log=False,
        proxy_headers=False,  # Dinnerbell resolves client IPs itself from DINNERBELL_TRUSTED_PROXIES
        server_header=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "catalog":
        return catalog_cli.run(args)
    if args.command == "slack":
        return _slack_preview(args.kind, args.base_url)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "scrub-test" and not settings.instance_label:
        from dinnerbell.services.scrub import NOT_LABELED

        print(NOT_LABELED, file=sys.stderr)
        return 1  # before migrating: never touch a database that may be production's
    configure_logging(settings.log_level, settings.instance_label)
    os.umask(PRIVATE_UMASK)
    if not _migrate(settings):
        return 1
    if args.command == "serve":
        _serve(settings, args.host, args.port)
    if args.command == "scrub-test":
        return _scrub_test(settings, args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
