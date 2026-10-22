# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``dinnerbell`` command: ``serve`` and ``migrate``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from dinnerbell import __version__
from dinnerbell.config import ConfigError, Settings, load_settings
from dinnerbell.db.migrate import SchemaTooNewError, run_migrations
from dinnerbell.logs import configure_logging

log = logging.getLogger("dinnerbell")
DEFAULT_PORT = 8000
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
    return parser


def _migrate(settings: Settings) -> bool:
    try:
        run_migrations(settings.sync_database_url, settings.database_path)
    except SchemaTooNewError as exc:
        log.critical(str(exc))
        return False
    return True


def _serve(settings: Settings, host: str, port: int) -> None:
    import uvicorn

    from dinnerbell.web.app import create_app

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
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    os.umask(PRIVATE_UMASK)
    if not _migrate(settings):
        return 1
    if args.command == "serve":
        _serve(settings, args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
