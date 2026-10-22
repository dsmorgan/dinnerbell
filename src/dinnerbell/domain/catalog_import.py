# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import planning: a pure function from a parsed catalog file and a snapshot of the
household's catalog to the list of changes an import would make.

Imports never delete and are all or nothing: a plan with any error must not be applied.
Services re-plan against current state when a person confirms, so a stale plan is never saved.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from dinnerbell.domain.catalog import (
    DEFAULT_MODES,
    CatalogItem,
    ItemKind,
    Mode,
    Tag,
    TagKind,
    name_key,
    sorted_modes,
)
from dinnerbell.domain.catalog_file import FileError, HomeMealEntry, LocatedEntry, ParsedCatalog, RestaurantEntry
from dinnerbell.domain.members import Member

SECTION_NOUNS = {ItemKind.RESTAURANT: "restaurants", ItemKind.HOME: "home meals"}


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """The household's current tags, items, and members, as the planner needs them."""

    tags: tuple[Tag, ...]
    items: tuple[CatalogItem, ...]
    members: tuple[Member, ...]

    @classmethod
    def empty(cls) -> CatalogSnapshot:
        return cls(tags=(), items=(), members=())


@dataclass(frozen=True, slots=True)
class ItemState:
    """Everything an import can set on an item. Tags are referenced by name key because new
    tags have no ID until the plan is applied."""

    kind: ItemKind
    name: str
    modes: frozenset[Mode]
    tag_keys: frozenset[str]
    cook_ids: frozenset[int]
    url: str | None
    order_url: str | None
    notes: str | None
    default_servings: int | None
    active: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "modes": [m.value for m in sorted_modes(self.modes)],
            "tags": sorted(self.tag_keys),
            "cooks": sorted(self.cook_ids),
            "url": self.url,
            "order_url": self.order_url,
            "notes": self.notes,
            "servings": self.default_servings,
            "active": self.active,
        }


@dataclass(frozen=True, slots=True)
class NewTag:
    kind: TagKind
    name: str
    declared: bool
    """True when the file lists it under ``tags``; False when an entry uses it undeclared."""

    @property
    def key(self) -> str:
        return name_key(self.name)


@dataclass(frozen=True, slots=True)
class FieldChange:
    field: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class ItemCreate:
    location: str
    state: ItemState


@dataclass(frozen=True, slots=True)
class ItemUpdate:
    location: str
    item_id: int
    state: ItemState
    changes: tuple[FieldChange, ...]


@dataclass(frozen=True, slots=True)
class UnchangedItem:
    location: str
    item_id: int
    kind: ItemKind
    name: str


@dataclass(frozen=True, slots=True)
class UnknownCook:
    location: str
    email: str


@dataclass(frozen=True, slots=True)
class ImportPlan:
    tags_to_create: tuple[NewTag, ...] = ()
    items_to_create: tuple[ItemCreate, ...] = ()
    items_to_update: tuple[ItemUpdate, ...] = ()
    unchanged: tuple[UnchangedItem, ...] = ()
    unknown_cooks: tuple[UnknownCook, ...] = ()
    errors: tuple[FileError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def has_changes(self) -> bool:
        return bool(self.tags_to_create or self.items_to_create or self.items_to_update)

    def summary(self) -> dict[str, int]:
        return {
            "tags_created": len(self.tags_to_create),
            "items_created": len(self.items_to_create),
            "items_updated": len(self.items_to_update),
            "items_unchanged": len(self.unchanged),
            "unknown_cooks": len(self.unknown_cooks),
            "errors": len(self.errors),
        }

    def fingerprint(self) -> str:
        """A digest of what applying would do. Equal fingerprints mean the same outcome."""
        body = {
            "tags": [[t.kind.value, t.name] for t in self.tags_to_create],
            "create": [c.state.as_json() for c in self.items_to_create],
            "update": [[u.item_id, u.state.as_json()] for u in self.items_to_update],
            "errors": [str(e) for e in self.errors],
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan_import(parsed: ParsedCatalog, snapshot: CatalogSnapshot) -> ImportPlan:
    return _Planner(snapshot).plan(parsed)


@dataclass
class _KnownTag:
    kind: TagKind
    name: str


@dataclass
class _Planner:
    snapshot: CatalogSnapshot
    tags: dict[str, _KnownTag] = field(default_factory=dict)
    new_tags: list[NewTag] = field(default_factory=list)
    errors: list[FileError] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.tags = {tag.key: _KnownTag(tag.kind, tag.name) for tag in self.snapshot.tags}
        self.tag_keys_by_id = {tag.id: tag.key for tag in self.snapshot.tags}
        self.items = {(item.kind, item.key): item for item in self.snapshot.items}
        self.members_by_email = {member.email: member for member in self.snapshot.members}
        self.member_names = {member.id: member.display_name for member in self.snapshot.members}

    def plan(self, parsed: ParsedCatalog) -> ImportPlan:
        self.errors = list(parsed.errors)
        self._declare(parsed)
        self._register_undeclared(parsed.entries)
        creates: list[ItemCreate] = []
        updates: list[ItemUpdate] = []
        unchanged: list[UnchangedItem] = []
        unknown: list[UnknownCook] = []
        seen: set[tuple[ItemKind, str]] = set()
        for located in parsed.entries:
            entry, location = located.entry, located.location
            key = (located.kind, name_key(entry.name))
            if key in seen:
                self._error(
                    f"{location}.name", f'"{entry.name}" appears more than once among {SECTION_NOUNS[located.kind]}'
                )
                continue
            seen.add(key)
            cuisine = self._resolve(location, "cuisine", entry.cuisine)
            others = self._resolve(location, "tags", entry.tags or [])
            cook_ids = self._cooks(location, entry, unknown)
            existing = self.items.get(key)
            if existing is None:
                creates.append(ItemCreate(location, self._new_state(located, cuisine | others, cook_ids)))
                continue
            before = self._state_of(existing)
            after = self._merge(before, entry, cuisine, others, cook_ids)
            changes = self._changes(before, after)
            if changes:
                updates.append(ItemUpdate(location, existing.id, after, changes))
            else:
                unchanged.append(UnchangedItem(location, existing.id, existing.kind, existing.name))
        return ImportPlan(
            tags_to_create=tuple(self.new_tags),
            items_to_create=tuple(creates),
            items_to_update=tuple(updates),
            unchanged=tuple(unchanged),
            unknown_cooks=tuple(unknown),
            errors=tuple(sorted(self.errors, key=_error_order(parsed))),
        )

    def _error(self, location: str, message: str) -> None:
        self.errors.append(FileError(location, message))

    def _add_tag(self, kind: TagKind, name: str, *, declared: bool) -> None:
        self.tags[name_key(name)] = _KnownTag(kind, name)
        self.new_tags.append(NewTag(kind, name, declared))

    def _declare(self, parsed: ParsedCatalog) -> None:
        if parsed.tags is None:
            return
        for kind in TagKind:
            for index, name in enumerate(getattr(parsed.tags, kind.value) or []):
                known = self.tags.get(name_key(name))
                if known is None:
                    self._add_tag(kind, name, declared=True)
                elif known.kind is not kind:
                    self._error(
                        f"tags.{kind.value}[{index}]",
                        f'"{name}" is already {known.kind.a} tag; tag names are unique across kinds',
                    )

    def _register_undeclared(self, entries: Iterable[LocatedEntry]) -> None:
        """Create tags the entries use but the file doesn't declare: cuisine if any entry lists
        the tag under ``cuisine``, otherwise other. The first spelling in the file is kept."""
        first: dict[str, str] = {}
        cuisines: set[str] = set()
        for located in entries:
            for name in located.entry.cuisine:
                first.setdefault(name_key(name), name)
                cuisines.add(name_key(name))
            for name in located.entry.tags or []:
                first.setdefault(name_key(name), name)
        for key, name in first.items():
            if key not in self.tags:
                self._add_tag(TagKind.CUISINE if key in cuisines else TagKind.OTHER, name, declared=False)

    def _resolve(self, location: str, field_name: str, names: Iterable[str]) -> frozenset[str]:
        keys: set[str] = set()
        for index, name in enumerate(names):
            key = name_key(name)
            kind = self.tags[key].kind
            if field_name == "cuisine" and kind is not TagKind.CUISINE:
                self._error(f"{location}.cuisine[{index}]", f'"{name}" is {kind.a} tag, not a cuisine')
            elif field_name == "tags" and kind is TagKind.CUISINE:
                self._error(f"{location}.tags[{index}]", f'"{name}" is a cuisine tag; list it under cuisine')
            else:
                keys.add(key)
        return frozenset(keys)

    def _cooks(
        self, location: str, entry: RestaurantEntry | HomeMealEntry, unknown: list[UnknownCook]
    ) -> frozenset[int]:
        if not isinstance(entry, HomeMealEntry):
            return frozenset()
        ids: set[int] = set()
        for index, email in enumerate(entry.cooks or []):
            member = self.members_by_email.get(email)
            if member is None:
                unknown.append(UnknownCook(f"{location}.cooks[{index}]", email))
            else:
                ids.add(member.id)
        return frozenset(ids)

    def _new_state(self, located: LocatedEntry, tag_keys: frozenset[str], cook_ids: frozenset[int]) -> ItemState:
        entry = located.entry
        restaurant = entry if isinstance(entry, RestaurantEntry) else None
        home = entry if isinstance(entry, HomeMealEntry) else None
        return ItemState(
            kind=located.kind,
            name=entry.name,
            modes=frozenset(Mode(m) for m in restaurant.modes) if restaurant else DEFAULT_MODES[ItemKind.HOME],
            tag_keys=tag_keys,
            cook_ids=cook_ids,
            url=entry.url,
            order_url=restaurant.order_url if restaurant else None,
            notes=entry.notes,
            default_servings=home.servings if home else None,
            active=entry.active,
        )

    def _state_of(self, item: CatalogItem) -> ItemState:
        return ItemState(
            kind=item.kind,
            name=item.name,
            modes=item.modes,
            tag_keys=frozenset(self.tag_keys_by_id[i] for i in item.tag_ids if i in self.tag_keys_by_id),
            cook_ids=item.cook_ids,
            url=item.url,
            order_url=item.order_url,
            notes=item.notes,
            default_servings=item.default_servings,
            active=item.active,
        )

    def _merge(
        self,
        before: ItemState,
        entry: RestaurantEntry | HomeMealEntry,
        cuisine: frozenset[str],
        others: frozenset[str],
        cook_ids: frozenset[int],
    ) -> ItemState:
        """Fields present in the entry replace stored values; omitted fields stay as they are."""
        given = entry.model_fields_set
        kept_others = frozenset(k for k in before.tag_keys if self.tags[k].kind is not TagKind.CUISINE)
        values: dict[str, Any] = {"tag_keys": cuisine | (others if "tags" in given else kept_others)}
        for name in ("url", "notes", "active"):
            if name in given:
                values[name] = getattr(entry, name)
        if isinstance(entry, RestaurantEntry):
            if "modes" in given:
                values["modes"] = frozenset(Mode(m) for m in entry.modes)
            if "order_url" in given:
                values["order_url"] = entry.order_url
        else:
            if "servings" in given:
                values["default_servings"] = entry.servings
            if "cooks" in given:
                values["cook_ids"] = cook_ids
        return ItemState(**{**_fields(before), **values})

    def _changes(self, before: ItemState, after: ItemState) -> tuple[FieldChange, ...]:
        def tags(state: ItemState, cuisine: bool) -> str:
            keys = [k for k in state.tag_keys if (self.tags[k].kind is TagKind.CUISINE) == cuisine]
            return _list(self.tags[k].name for k in sorted(keys))

        def cooks(state: ItemState) -> str:
            return _list(sorted(self.member_names.get(i, f"member {i}") for i in state.cook_ids))

        shown: dict[str, Callable[[ItemState], str]] = {
            "cuisine": lambda s: tags(s, True),
            "tags": lambda s: tags(s, False),
            "modes": lambda s: _list(m.value for m in sorted_modes(s.modes)),
            "url": lambda s: s.url or "none",
            "order_url": lambda s: s.order_url or "none",
            "notes": lambda s: s.notes or "none",
            "servings": lambda s: "none" if s.default_servings is None else str(s.default_servings),
            "cooks": cooks,
            "active": lambda s: "yes" if s.active else "no",
        }
        changes = []
        for name, show in shown.items():
            old, new = show(before), show(after)
            if old != new:
                changes.append(FieldChange(name, old, new))
        return tuple(changes)


def _fields(state: ItemState) -> dict[str, Any]:
    return {name: getattr(state, name) for name in ItemState.__slots__}


def _list(values: Iterable[str]) -> str:
    return ", ".join(values) or "none"


def _error_order(parsed: ParsedCatalog) -> Any:
    """Sort errors by where they are in the file: tags first, then entries in file order."""
    entry_order = {located.location: i for i, located in enumerate(parsed.entries)}
    section_order = {"version": 0, "tags": 1}

    def key(error: FileError) -> tuple[int, int, int, str]:
        head = error.location.split(".", 1)[0]
        section = head.split("[", 1)[0]
        if head in entry_order:
            return (2, 0, entry_order[head], error.location)
        if section in {"restaurants", "home_meals"} and "[" in head:
            index = int(head.split("[", 1)[1].rstrip("]"))
            return (2, 1 if section == "home_meals" else 0, index, error.location)
        return (section_order.get(section, 3), 0, 0, error.location)

    return key
