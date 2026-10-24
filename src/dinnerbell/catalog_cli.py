# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell catalog``: validate catalog files, print their JSON Schema, and export the catalog.

``validate`` and ``schema`` need no configuration and no database, so they work from any checkout.
``export`` reads the configured database, read-only, so it's safe to run beside a live instance.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import tempfile
from contextlib import closing, suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dinnerbell.domain.catalog import ItemKind, TagKind
from dinnerbell.domain.catalog_export import CatalogExport
from dinnerbell.domain.catalog_file import MAX_FILE_BYTES, parse_catalog, render_schema
from dinnerbell.domain.catalog_import import CatalogSnapshot, plan_import

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNREADABLE = 2


def add_parser(commands: Any) -> None:
    catalog = commands.add_parser("catalog", help="work with catalog files, and export the catalog")
    actions = catalog.add_subparsers(dest="catalog_command", required=True, metavar="ACTION")
    validate = actions.add_parser("validate", help="check a catalog file and summarize it, without importing")
    validate.add_argument("path", type=Path, help="the catalog file, such as seed/example.yaml")
    actions.add_parser("schema", help="print the JSON Schema for catalog files")
    export = actions.add_parser(
        "export",
        help="write the catalog as a catalog file, reading the database read-only",
        description="Write the household's tags and items as a catalog file (docs/catalog-format.md) to "
        "standard output or FILE. Cooks, members, and preferences aren't exported. Needs the app's "
        "configuration, to find the database.",
    )
    export.add_argument("--output", "-o", type=Path, metavar="FILE", help="write to FILE instead of standard output")
    export.add_argument("--force", action="store_true", help="replace FILE if it already exists")


def run(args: argparse.Namespace) -> int:
    if args.catalog_command == "schema":
        sys.stdout.write(render_schema())
        return EXIT_OK
    return validate(args.path)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def validate(path: Path) -> int:
    try:
        with path.open("rb") as file:
            source = file.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        print(f"Can't read {path}: {exc.strerror or exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    plan = plan_import(parse_catalog(source), CatalogSnapshot.empty())
    if not plan.ok:
        print(f"{path}: {_plural(len(plan.errors), 'problem')}. Nothing would be imported.")
        for error in plan.errors:
            print(f"  {error}")
        return EXIT_INVALID
    kinds = [c.state.kind for c in plan.items_to_create]
    print(f"{path} is valid.")
    restaurants = _plural(kinds.count(ItemKind.RESTAURANT), "restaurant")
    print(f"  {restaurants}, {_plural(kinds.count(ItemKind.HOME), 'home meal')}")
    by_kind = ", ".join(f"{sum(1 for t in plan.tags_to_create if t.kind is kind)} {kind.value}" for kind in TagKind)
    print(f"  {_plural(len(plan.tags_to_create), 'tag')}: {by_kind}")
    undeclared = [t for t in plan.tags_to_create if not t.declared]
    if undeclared:
        names = ", ".join(f"{t.name} ({t.kind.value})" for t in undeclared)
        print(f"  Not declared under tags, so they'd be created: {names}")
    if plan.unknown_cooks:
        count = _plural(len(plan.unknown_cooks), "cook email")
        print(f"  {count} will be matched to members at import; emails that match no member are skipped.")
    return EXIT_OK


def _exists(output: Path) -> int:
    print(
        f"{output} already exists. Nothing exported. Choose another file, or add --force to replace it.",
        file=sys.stderr,
    )
    return EXIT_INVALID


def _schema_hint(database: Path) -> str:
    """Why reading may have failed, when the database's schema isn't this build's."""
    from dinnerbell.db.migrate import head_revision

    try:
        with closing(sqlite3.connect(f"file:{quote(str(database.resolve()))}?mode=ro", uri=True)) as db:
            row = db.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return ""
    head = head_revision()
    if row is None or row[0] == head:
        return ""
    return (
        f" The database is at schema revision {row[0]!r}, and this Dinnerbell expects {head!r}. If the database is"
        " older, start the instance once or run `dinnerbell migrate`, then export again. If it's newer, run the"
        " image that wrote it."
    )


def _write_stdout(data: bytes) -> bool:
    try:
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        # The reader went away, such as `| head`. Point stdout at /dev/null, so Python's own flush
        # at exit doesn't raise again, and stop without a traceback.
        with suppress(OSError, ValueError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return False
    return True


def _write_file(output: Path, data: bytes, *, force: bool) -> int | None:
    """Write ``data`` to ``output``; return an exit code on failure. Without ``force``, the file is
    created exclusively, so one created meanwhile is never replaced. With it, the data goes to a
    temporary file next to ``output`` that then replaces it in one step, so a failed write never
    leaves half a file in place of the old one."""
    try:
        if not force:
            with output.open("xb") as file:
                file.write(data)
            return None
        handle, temporary = tempfile.mkstemp(dir=output.parent, prefix=f".{output.name}.", suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            os.replace(temporary, output)
        except BaseException:
            with suppress(OSError):
                os.unlink(temporary)
            raise
    except FileExistsError:
        return _exists(output)
    except OSError as exc:
        print(f"Can't write {output}: {exc.strerror or exc}. Nothing exported.", file=sys.stderr)
        return EXIT_UNREADABLE
    return None


def export(database: Path, output: Path | None, *, force: bool = False) -> int:
    """Export the catalog in the database at ``database``, opened read-only so it never changes
    the database or its journal mode. With the app stopped, SQLite may still create empty -wal
    and -shm files next to a WAL-mode database; run as the app's user so the app can open them."""
    from sqlalchemy.exc import SQLAlchemyError

    from dinnerbell.services import catalog as catalog_service
    from dinnerbell.services.database import open_readonly_database
    from dinnerbell.services.household import NoHousehold, current_household

    if output is not None and not force and output.exists():
        return _exists(output)  # checked again when the file is created; this saves reading first
    if not database.exists():
        print(f"No database at {database}. Nothing exported.", file=sys.stderr)
        return EXIT_UNREADABLE

    async def read() -> CatalogExport:
        opened = open_readonly_database(database)
        try:
            async with opened.sessionmaker() as db:
                household = await current_household(db)
                return await catalog_service.export(db, household.id)
        finally:
            await opened.close()

    try:
        exported = asyncio.run(read())
    except SQLAlchemyError as exc:
        hint = _schema_hint(database)
        print(f"Can't read the database at {database} ({type(exc).__name__}). Nothing exported.{hint}", file=sys.stderr)
        return EXIT_UNREADABLE
    except NoHousehold:
        print(f"The database at {database} has no household yet, so there's no catalog.", file=sys.stderr)
        return EXIT_UNREADABLE
    if output is None:
        if not _write_stdout(exported.data):
            return EXIT_INVALID
    elif (failed := _write_file(output, exported.data, force=force)) is not None:
        return failed
    restaurants = _plural(exported.restaurants, "restaurant")
    summary = f"{restaurants}, {_plural(exported.home_meals, 'home meal')}, {_plural(exported.tags, 'tag')}"
    print(f"Exported {summary}{'' if output is None else f' to {output}'}.", file=sys.stderr)
    if exported.too_big_to_import:
        print(f"Warning: {exported.too_big_to_import}", file=sys.stderr)
    return EXIT_OK
