# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catalog items and tags, and the rules every write path shares: the add form, the edit
form, and the catalog file import all normalize names, notes, and URLs the same way."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

MAX_TAG_NAME_LENGTH = 40
MAX_ITEM_NAME_LENGTH = 100
MAX_NOTES_LENGTH = 500
MAX_URL_LENGTH = 2000
MAX_SERVINGS = 99


class TagKind(StrEnum):
    CUISINE = "cuisine"
    PROTEIN = "protein"
    DIETARY = "dietary"
    OTHER = "other"

    @property
    def a(self) -> str:
        """The kind with its article, for messages: "a cuisine", "an other"."""
        return f"an {self.value}" if self is TagKind.OTHER else f"a {self.value}"


class ItemKind(StrEnum):
    HOME = "home"
    RESTAURANT = "restaurant"

    @property
    def label(self) -> str:
        return "Home meal" if self is ItemKind.HOME else "Restaurant"


class Mode(StrEnum):
    COOK = "cook"
    PICKUP = "pickup"
    DELIVERY = "delivery"

    @property
    def label(self) -> str:
        return {"cook": "Cook", "pickup": "Pickup", "delivery": "Delivery"}[self.value]


ALLOWED_MODES: dict[ItemKind, frozenset[Mode]] = {
    ItemKind.HOME: frozenset({Mode.COOK}),
    ItemKind.RESTAURANT: frozenset({Mode.PICKUP, Mode.DELIVERY}),
}
DEFAULT_MODES: dict[ItemKind, frozenset[Mode]] = {
    ItemKind.HOME: frozenset({Mode.COOK}),
    ItemKind.RESTAURANT: frozenset({Mode.PICKUP}),
}
MODE_ORDER = (Mode.COOK, Mode.PICKUP, Mode.DELIVERY)


class InvalidCatalogInput(ValueError):
    """Input that breaks a catalog rule. The message is written for the person who typed it."""


class CatalogRuleViolation(Exception):
    """An action the catalog rules forbid, such as a duplicate name. The message is safe to show."""


def _control_characters(value: str, allowed: str = "") -> bool:
    return any(unicodedata.category(char).startswith("C") and char not in allowed for char in value)


def collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def name_key(name: str) -> str:
    """The comparison key for tag and item names: whitespace collapsed and case folded."""
    return collapse_whitespace(name).casefold()


def _normalize_name(value: str, what: str, max_length: int) -> str:
    name = collapse_whitespace(value)
    if not name:
        raise InvalidCatalogInput(f"Enter a {what}.")
    if len(name) > max_length:
        raise InvalidCatalogInput(f"A {what} is at most {max_length} characters.")
    if _control_characters(name):
        raise InvalidCatalogInput(f"A {what} can't contain control characters.")
    return name


def normalize_tag_name(value: str) -> str:
    return _normalize_name(value, "tag name", MAX_TAG_NAME_LENGTH)


def normalize_item_name(value: str) -> str:
    return _normalize_name(value, "name", MAX_ITEM_NAME_LENGTH)


def normalize_notes(value: str | None) -> str | None:
    """Trim notes; empty means none. Newlines and tabs are kept."""
    if value is None:
        return None
    notes = value.replace("\r\n", "\n").strip()
    if not notes:
        return None
    if len(notes) > MAX_NOTES_LENGTH:
        raise InvalidCatalogInput(f"Notes are at most {MAX_NOTES_LENGTH} characters.")
    if _control_characters(notes, allowed="\n\t"):
        raise InvalidCatalogInput("Notes can't contain control characters.")
    return notes


def normalize_url(value: str | None) -> str | None:
    """Accept only absolute http and https links; empty means none."""
    if value is None:
        return None
    url = value.strip()
    if not url:
        return None
    if len(url) > MAX_URL_LENGTH:
        raise InvalidCatalogInput(f"A link is at most {MAX_URL_LENGTH} characters.")
    if any(char.isspace() for char in url) or _control_characters(url):
        raise InvalidCatalogInput("A link can't contain spaces.")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise InvalidCatalogInput("That link isn't a valid web address.") from None
    if parts.scheme not in {"http", "https"} or not parts.netloc or not parts.hostname:
        raise InvalidCatalogInput("A link must start with http:// or https://.")
    return url


def normalize_servings(value: int | None) -> int | None:
    if value is None:
        return None
    if not 1 <= value <= MAX_SERVINGS:
        raise InvalidCatalogInput(f"Servings must be a whole number from 1 to {MAX_SERVINGS}.")
    return value


def check_modes(kind: ItemKind, modes: Iterable[Mode]) -> frozenset[Mode]:
    chosen = frozenset(modes)
    if not chosen:
        raise InvalidCatalogInput("Pick at least one way to get it: pickup or delivery.")
    if not chosen <= ALLOWED_MODES[kind]:
        allowed = ", ".join(m.value for m in MODE_ORDER if m in ALLOWED_MODES[kind])
        raise InvalidCatalogInput(f"A {kind.label.lower()} supports only: {allowed}.")
    return chosen


def sorted_modes(modes: Iterable[Mode]) -> list[Mode]:
    chosen = set(modes)
    return [mode for mode in MODE_ORDER if mode in chosen]


@dataclass(frozen=True, slots=True)
class Tag:
    id: int
    household_id: int
    kind: TagKind
    name: str

    @property
    def key(self) -> str:
        return name_key(self.name)


@dataclass(frozen=True, slots=True)
class CatalogItem:
    id: int
    household_id: int
    kind: ItemKind
    name: str
    modes: frozenset[Mode]
    tag_ids: frozenset[int]
    cook_ids: frozenset[int]
    url: str | None = None
    order_url: str | None = None
    notes: str | None = None
    default_servings: int | None = None
    active: bool = True
    created_by: int | None = None

    @property
    def key(self) -> str:
        return name_key(self.name)


def tags_by_kind(tags: Iterable[Tag]) -> dict[TagKind, list[Tag]]:
    """Group tags by kind in display order, each group sorted by name."""
    grouped: dict[TagKind, list[Tag]] = {kind: [] for kind in TagKind}
    for tag in tags:
        grouped[tag.kind].append(tag)
    for group in grouped.values():
        group.sort(key=lambda tag: tag.key)
    return grouped


@dataclass(frozen=True, slots=True)
class ItemInput:
    """An item as a person entered it in the add or edit form."""

    kind: ItemKind
    name: str
    modes: frozenset[Mode]
    tag_ids: frozenset[int]
    new_cuisines: tuple[str, ...] = ()
    new_tags: tuple[str, ...] = ()
    new_tags_kind: TagKind = TagKind.OTHER
    cook_ids: frozenset[int] = frozenset()
    url: str | None = None
    order_url: str | None = None
    notes: str | None = None
    default_servings: int | None = None


@dataclass(frozen=True, slots=True)
class CleanItem:
    """A validated item: existing tags by ID plus new tags to create, as (kind, name)."""

    kind: ItemKind
    name: str
    modes: frozenset[Mode]
    tag_ids: frozenset[int]
    tags_to_create: tuple[tuple[TagKind, str], ...]
    cook_ids: frozenset[int]
    url: str | None
    order_url: str | None
    notes: str | None
    default_servings: int | None


def clean_item(data: ItemInput, tags: Iterable[Tag], member_ids: Iterable[int]) -> CleanItem:
    """Validate form input against the household's tags and members.

    Typed tag names match existing tags ignoring case. Fields that don't apply to the kind,
    such as an order link on a home meal, are dropped rather than rejected, because the form
    keeps them in the page while a person switches kinds.
    """
    known = {tag.id: tag for tag in tags}
    by_key = {tag.key: tag for tag in known.values()}
    name = normalize_item_name(data.name)
    is_home = data.kind is ItemKind.HOME
    modes = DEFAULT_MODES[ItemKind.HOME] if is_home else check_modes(data.kind, data.modes)
    unknown_ids = data.tag_ids - known.keys()
    if unknown_ids:
        raise InvalidCatalogInput("One of the chosen tags no longer exists. Reload the page and try again.")
    tag_ids = set(data.tag_ids)
    to_create: dict[str, tuple[TagKind, str]] = {}
    typed = [(TagKind.CUISINE, n) for n in data.new_cuisines] + [(data.new_tags_kind, n) for n in data.new_tags]
    for kind, raw in typed:
        if not raw.strip():
            continue
        tag_name = normalize_tag_name(raw)
        key = name_key(tag_name)
        existing = by_key.get(key)
        if existing is None:
            pending = to_create.get(key)
            if pending is not None and pending[0] is not kind:
                raise InvalidCatalogInput(f'"{tag_name}" can\'t be both {pending[0].a} tag and {kind.a} tag.')
            to_create.setdefault(key, (kind, tag_name))
        elif existing.kind is not kind:
            raise InvalidCatalogInput(f'"{existing.name}" is already {existing.kind.a} tag. Tick it in that list.')
        else:
            tag_ids.add(existing.id)
    has_cuisine = any(known[i].kind is TagKind.CUISINE for i in tag_ids) or any(
        kind is TagKind.CUISINE for kind, _ in to_create.values()
    )
    if not has_cuisine:
        raise InvalidCatalogInput("Pick at least one cuisine.")
    members = set(member_ids)
    cook_ids = frozenset(data.cook_ids) if is_home else frozenset()
    if not cook_ids <= members:
        raise InvalidCatalogInput("One of the chosen cooks isn't a member. Reload the page and try again.")
    return CleanItem(
        kind=data.kind,
        name=name,
        modes=modes,
        tag_ids=frozenset(tag_ids),
        tags_to_create=tuple(to_create.values()),
        cook_ids=cook_ids,
        url=normalize_url(data.url),
        order_url=None if is_home else normalize_url(data.order_url),
        notes=normalize_notes(data.notes),
        default_servings=normalize_servings(data.default_servings) if is_home else None,
    )


def check_merge(source: Tag, target: Tag) -> None:
    """Raise CatalogRuleViolation if ``source`` may not be merged into ``target``."""
    if source.id == target.id:
        raise CatalogRuleViolation("Pick a different tag to merge into.")
    if source.kind is TagKind.CUISINE and target.kind is not TagKind.CUISINE:
        raise CatalogRuleViolation(
            "Merge a cuisine only into another cuisine, so every item keeps a cuisine for variety tracking."
        )


class Status(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class ItemFilter:
    """The Catalog page's search: text, kind, mode, tag, and active status."""

    query: str = ""
    kind: ItemKind | None = None
    mode: Mode | None = None
    tag_id: int | None = None
    status: Status = Status.ACTIVE

    def matches(self, item: CatalogItem, tag_names: dict[int, str]) -> bool:
        if self.status is Status.ACTIVE and not item.active:
            return False
        if self.status is Status.INACTIVE and item.active:
            return False
        if self.kind is not None and item.kind is not self.kind:
            return False
        if self.mode is not None and self.mode not in item.modes:
            return False
        if self.tag_id is not None and self.tag_id not in item.tag_ids:
            return False
        needle = collapse_whitespace(self.query).casefold()
        if not needle:
            return True
        haystack = [item.name, item.notes or "", *(tag_names.get(i, "") for i in item.tag_ids)]
        return any(needle in text.casefold() for text in haystack)

    @property
    def is_default(self) -> bool:
        return self == ItemFilter()
