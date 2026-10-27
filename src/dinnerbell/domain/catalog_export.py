# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catalog export: the household's catalog written as a catalog file (docs/catalog-format.md).

An export is what import reads, so it can move a catalog between instances or be edited and
imported back. It writes every field, with ``null`` or ``[]`` for empty ones, because import
leaves out-of-file fields alone: importing an export makes each matching item exactly what the
export says. Tags and items are sorted by name, so two exports of the same catalog are
byte-identical and diff cleanly.

An export never holds personal data. Cooks are member emails, so they're left out, and an import
of an export keeps the cooks each item already has.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import yaml

from dinnerbell.domain import catalog_file
from dinnerbell.domain.catalog import CatalogItem, ItemKind, Tag, TagKind, sorted_meals, sorted_modes
from dinnerbell.domain.catalog_file import FORMAT_VERSION

HEADER = (
    "# A Dinnerbell catalog file. The format is described in docs/catalog-format.md in the Dinnerbell source.\n"
    "# Cooks aren't exported: an import keeps the cooks each item already has.\n"
)
MAX_LINE = 10_000
"""Wider than any one written value, even a 500-character note with every character escaped, so
PyYAML never folds a name, note, or link across lines. A very long inline tag list can still
wrap between tags, which reads back the same."""


@dataclass(frozen=True, slots=True)
class CatalogExport:
    text: str
    restaurants: int
    home_meals: int
    tags: int
    values: int
    """How many YAML values the file holds, counted the way import counts them."""

    @property
    def data(self) -> bytes:
        return self.text.encode()

    @property
    def too_big_to_import(self) -> str | None:
        """Why import would refuse this file in one piece, or None if it wouldn't."""
        if len(self.data) > catalog_file.MAX_FILE_BYTES:
            limit = f"{catalog_file.MAX_FILE_BYTES // 1_000_000} MB"
        elif self.values > catalog_file.MAX_NODES:
            limit = f"{catalog_file.MAX_NODES:,} values"
        else:
            return None
        return f"This export is over import's limit of {limit}. To import it, split it into smaller files."


class _Inline(list[str]):
    """A list of tag names, modes, or meals, written on one line: ``[Thai, Lao]``."""


class _Dumper(yaml.SafeDumper):
    """``yaml.SafeDumper`` with the layout of the catalog format's examples.

    - Never writes anchors or aliases, which import refuses, even if a value object repeats.
    - Indents a list under its key, as the docs do.
    - Writes multi-line notes as a literal block (``|``) when YAML allows it.

    Only plain mappings, lists, strings, numbers, booleans, and null reach it, so the output has
    no tags (``!!python/...``) and loads with any safe loader.
    """

    def ignore_aliases(self, data: Any) -> bool:
        return True

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def _represent_inline(dumper: yaml.SafeDumper, data: _Inline) -> yaml.SequenceNode:
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None)


_Dumper.add_representer(_Inline, _represent_inline)
_Dumper.add_representer(str, _represent_str)


def export_catalog(tags: Iterable[Tag], items: Iterable[CatalogItem]) -> CatalogExport:
    """Write a household's tags and items as a catalog file, sorted by name."""
    by_id = {tag.id: tag for tag in tags}
    ordered = sorted(by_id.values(), key=lambda tag: (tag.key, tag.name))
    sections: dict[ItemKind, list[dict[str, Any]]] = {ItemKind.RESTAURANT: [], ItemKind.HOME: []}
    for item in sorted(items, key=lambda item: (item.key, item.name)):
        # Tags and items are read in one snapshot, so every link resolves; skip one that doesn't,
        # as import planning does, rather than fail the whole export.
        sections[item.kind].append(_entry(item, [by_id[i] for i in item.tag_ids if i in by_id]))
    document = {
        "version": FORMAT_VERSION,
        "tags": {kind.value: _Inline(t.name for t in ordered if t.kind is kind) for kind in TagKind},
        "restaurants": sections[ItemKind.RESTAURANT],
        "home_meals": sections[ItemKind.HOME],
    }
    body = yaml.dump(document, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=MAX_LINE)
    return CatalogExport(
        text=HEADER + body,
        restaurants=len(sections[ItemKind.RESTAURANT]),
        home_meals=len(sections[ItemKind.HOME]),
        tags=len(ordered),
        values=_count_values(document),
    )


def _count_values(value: Any) -> int:
    """Count YAML nodes as import's loader does: every mapping, list, key, and scalar."""
    if isinstance(value, dict):
        return 1 + sum(1 + _count_values(v) for v in value.values())
    if isinstance(value, list):
        return 1 + sum(_count_values(v) for v in value)
    return 1


def _entry(item: CatalogItem, tags: list[Tag]) -> dict[str, Any]:
    tags = sorted(tags, key=lambda tag: (tag.key, tag.name))
    entry: dict[str, Any] = {
        "name": item.name,
        "cuisine": _Inline(t.name for t in tags if t.kind is TagKind.CUISINE),
        "tags": _Inline(t.name for t in tags if t.kind is not TagKind.CUISINE),
    }
    if item.kind is ItemKind.RESTAURANT:
        entry["modes"] = _Inline(mode.value for mode in sorted_modes(item.modes))
    entry["meals"] = _Inline(meal.value for meal in sorted_meals(item.meals))
    if item.kind is ItemKind.HOME:
        entry["servings"] = item.default_servings
    entry["url"] = item.url
    if item.kind is ItemKind.RESTAURANT:
        entry["order_url"] = item.order_url
    entry["notes"] = item.notes
    entry["active"] = item.active
    return entry
