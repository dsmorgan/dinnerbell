# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell catalog``: validate catalog files and print their JSON Schema.

These commands need no configuration and no database, so they work from any checkout.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from dinnerbell.domain.catalog import ItemKind, TagKind
from dinnerbell.domain.catalog_file import MAX_FILE_BYTES, parse_catalog, render_schema
from dinnerbell.domain.catalog_import import CatalogSnapshot, plan_import

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNREADABLE = 2


def add_parser(commands: Any) -> None:
    catalog = commands.add_parser("catalog", help="work with catalog files (no database needed)")
    actions = catalog.add_subparsers(dest="catalog_command", required=True, metavar="ACTION")
    validate = actions.add_parser("validate", help="check a catalog file and summarize it, without importing")
    validate.add_argument("path", type=Path, help="the catalog file, such as seed/example.yaml")
    actions.add_parser("schema", help="print the JSON Schema for catalog files")


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
