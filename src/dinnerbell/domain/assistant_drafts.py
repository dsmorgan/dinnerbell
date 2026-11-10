# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's drafts and candidates (ADR 0009, spec 6.14, **Tools** and **Drafts**).

A draft is a change the assistant prepared for one member: a proposal, an offer, or new catalog
items. It says what **Confirm** will do, in lines worked out here with the same rules the
services apply, and lasts ``DRAFT_MINUTES``. Only its member confirms it, at most once.

Candidates are what ranking's hard filters let through for a voting call. Their reasons come
from a snapshot with no preferences, no hints, and pseudonyms in place of names
(``reasons_snapshot``), so no reason can say who likes what (FR-29).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from dinnerbell.domain.catalog import MODE_ORDER, CatalogItem, ItemKind, Mode, name_key
from dinnerbell.domain.catalog_export import dump_document
from dinnerbell.domain.catalog_file import FORMAT_VERSION, HomeMealEntry, ParsedCatalog, RestaurantEntry
from dinnerbell.domain.events import Event, EventState
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.offers import OfferPath, offer_path
from dinnerbell.domain.ranking import (
    MODE_COMPONENTS,
    GroupPreferences,
    RankingSnapshot,
    RankItem,
    components,
    explain,
)

DRAFT_MINUTES = 30
"""How long a draft waits for **Confirm** (spec 6.14, **Drafts**)."""
MAX_OPEN_DRAFTS = 10
"""Drafts one member may have waiting at once, so a runaway conversation can't pile them up."""

DOING = {Mode.COOK: "cook it", Mode.PICKUP: "pick it up", Mode.DELIVERY: "order it"}
"""What whoever takes a mode's role does, as the offer service words it."""
STRIPPED_FIELDS = frozenset({"cooks", "active"})
"""Fields a catalog draft never carries: cooks are other people's emails, and a draft only adds
items people can pick."""


class DraftKind(StrEnum):
    PROPOSAL = "proposal"
    OFFER = "offer"
    CATALOG = "catalog"


@dataclass(frozen=True, slots=True)
class Draft:
    """A stored draft. ``payload`` holds what the service needs, by kind:

    - proposal: ``item_id``, ``mode``, ``join_late``
    - offer: ``item_id``, ``mode``, ``slot``, ``day``, ``at`` (``HH:MM``), ``capacity``,
      ``outcome``
    - catalog: ``content`` (the cleaned catalog file) and ``dropped`` (names left out)

    ``event_id`` and ``run`` are the call the draft was made against, where there is one."""

    id: int
    household_id: int
    member_id: int
    conversation_key: str
    kind: DraftKind
    payload: Mapping[str, Any]
    title: str
    lines: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    event_id: int | None = None
    run: int | None = None
    used_at: datetime | None = None

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


def expires_at(now: datetime) -> datetime:
    return now + timedelta(minutes=DRAFT_MINUTES)


def day_words(day: date, today: date) -> str:
    """``day`` as a member reads it: "today", "tomorrow", or "Thu, Oct 22"."""
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return f"{day:%a, %b} {day.day}"


# What Confirm does -------------------------------------------------------------------------------


def proposal_lines(*, slot: Slot, when: str, join_late: bool, volunteers: bool, mode: Mode) -> tuple[str, ...]:
    """What confirming a proposal does (spec 6.14, **Drafts**): it's added to the vote, the
    member joins late when they aren't in (``calls.add_proposal`` with ``join_late``), and they
    take the role the mode needs when they can and haven't offered it yet (``picks.volunteer``)."""
    lines = [f"It's added to {when}'s {slot.value} vote as a proposal."]
    if join_late:
        lines.append("You'll join late.")
    if volunteers:
        lines.append(f"You'll be down to {DOING[mode]}.")
    return tuple(lines)


class OfferOutcome(StrEnum):
    """What posting an offer does to its slot's call that day (FR-5): ``offer_path``, with a
    cooking offer for a day that has no call yet told apart, because it keeps that call from
    opening (``offers.replaces_call``)."""

    JOIN = "join"
    REPLACE = "replace"
    HOLD = "hold"
    ALONE = "alone"


def offer_outcome(call: Event | None, mode: Mode) -> OfferOutcome:
    path = offer_path(call, mode)
    if path is OfferPath.ATTACH:
        return OfferOutcome.JOIN
    if path is OfferPath.REPLACE:
        return OfferOutcome.REPLACE
    if call is None and mode is Mode.COOK:
        return OfferOutcome.HOLD
    return OfferOutcome.ALONE


def offer_lines(
    outcome: OfferOutcome, *, slot: Slot, when: str, state: EventState | None, mode: Mode
) -> tuple[str, ...]:
    """What confirming an offer does, one of four (spec 6.14, **Drafts**), then the role the
    offerer takes. ``when`` is the day in words; ``state`` is the call's, for a join."""
    if outcome is OfferOutcome.JOIN:
        stage = "voting" if state is EventState.VOTING else "collecting answers"
        first = f"It joins {when}'s {slot.value} call, which is {stage}, as a pick, and you're in."
    elif outcome is OfferOutcome.REPLACE:
        first = f"It replaces {when}'s scheduled {slot.value} call, which won't open."
    elif outcome is OfferOutcome.HOLD:
        first = f"It keeps {when}'s {slot.value} call from opening while your offer is on."
    else:
        first = "It's posted as an offer of its own."
    return (first, f"You'll {DOING[mode]}.")


# Candidates --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Narrowing:
    """How a member narrowed a candidate search. Each is optional: 0, "", or None means any."""

    not_eaten_in_days: int = 0
    cuisine: str = ""
    mode: Mode | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    item: RankItem
    modes: tuple[Mode, ...]
    """Every way it passes the filters, in ``MODE_ORDER``."""
    reasons: tuple[str, ...]
    for_meal: bool


def reasons_snapshot(
    snapshot: RankingSnapshot, *, labels: Mapping[int, str], suggesters: Mapping[int, tuple[int, ...]]
) -> RankingSnapshot:
    """``snapshot`` with nothing a reason could reveal about a person: no preferences, so no
    favorite sentences (``SOFT_PREFERENCE`` is 0); no hints; and members as the conversation's
    pseudonyms. ``suggesters`` holds member IDs by item, and ``labels`` each member's label."""
    return replace(
        snapshot,
        preferences=GroupPreferences(),
        hints={},
        member_names=dict(labels),
        suggesters={item: tuple(labels[m] for m in ids if m in labels) for item, ids in suggesters.items()},
    )


def _best_mode(item: RankItem, modes: Iterable[Mode], snapshot: RankingSnapshot) -> Mode:
    """Ranking's choice among an item's modes (step 4): the mode's own components, then
    ``MODE_ORDER``."""

    def key(mode: Mode) -> tuple[float, int]:
        parts = components(item, mode, snapshot)
        return (-round(sum(parts[c] for c in MODE_COMPONENTS), 6), MODE_ORDER.index(mode))

    return min(modes, key=key)


def candidate_reasons(
    item: RankItem, modes: Iterable[Mode], clean: RankingSnapshot, *, for_meal: bool
) -> tuple[str, ...]:
    """Reasons for ``item`` from ``clean`` (``reasons_snapshot``): variety, novelty, suggestions,
    the default mode, volunteer fit, and, for an item usually had at other meals, meal fit."""
    mode = _best_mode(item, modes, clean)
    return explain(item, mode, components(item, mode, clean), clean, for_meal=for_meal)


def narrowed(
    pairs: Iterable[tuple[RankItem, Mode]],
    narrowing: Narrowing,
    *,
    last_eaten_days: Mapping[int, int],
    cuisine_names: Mapping[int, str],
) -> dict[int, tuple[RankItem, tuple[Mode, ...]]]:
    """``pairs`` that pass ``narrowing``, grouped by item, each with its modes in order."""
    cuisine = name_key(narrowing.cuisine)
    found: dict[int, tuple[RankItem, list[Mode]]] = {}
    for item, mode in pairs:
        if narrowing.mode is not None and mode is not narrowing.mode:
            continue
        days = last_eaten_days.get(item.id)
        if narrowing.not_eaten_in_days and days is not None and days < narrowing.not_eaten_in_days:
            continue
        if cuisine and not any(cuisine in name_key(cuisine_names.get(t, "")) for t in item.cuisine_ids):
            continue
        found.setdefault(item.id, (item, []))[1].append(mode)
    return {i: (item, tuple(m for m in MODE_ORDER if m in modes)) for i, (item, modes) in found.items()}


# Catalog drafts ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CleanCatalog:
    """A catalog draft: the file to import, the names it adds, and the ones it left out because
    the catalog has them already."""

    text: str
    added: tuple[str, ...]
    dropped: tuple[str, ...]
    tags: Mapping[str, list[str]] = field(default_factory=dict)


def _entry_document(entry: RestaurantEntry | HomeMealEntry) -> dict[str, Any]:
    return entry.model_dump(mode="json", exclude_unset=True, exclude=set(STRIPPED_FIELDS))


def matching_names(parsed: ParsedCatalog, items: Iterable[CatalogItem]) -> frozenset[tuple[ItemKind, str]]:
    """The (kind, name key) of each entry in ``parsed`` that names an item ``items`` has, in
    either state: an import would change that item, and a draft only adds."""
    existing = {(item.kind, item.key) for item in items}
    return frozenset(
        (located.kind, name_key(located.entry.name))
        for located in parsed.entries
        if (located.kind, name_key(located.entry.name)) in existing
    )


def clean_catalog(parsed: ParsedCatalog, items: Iterable[CatalogItem]) -> CleanCatalog:
    """``parsed`` as a draft (spec 6.14, **Tools**): entries naming an item the catalog has
    (same kind, ignoring case) are dropped and listed, and ``cooks`` and ``active`` are stripped.
    The caller checks ``parsed.errors`` first."""
    matches = matching_names(parsed, items)
    sections: dict[str, list[dict[str, Any]]] = {"restaurants": [], "home_meals": []}
    added: list[str] = []
    dropped: list[str] = []
    for located in parsed.entries:
        entry = located.entry
        if (located.kind, name_key(entry.name)) in matches:
            dropped.append(entry.name)
            continue
        added.append(entry.name)
        sections["restaurants" if located.kind is ItemKind.RESTAURANT else "home_meals"].append(_entry_document(entry))
    document: dict[str, Any] = {"version": FORMAT_VERSION}
    tags = parsed.tags.model_dump(mode="json", exclude_none=True) if parsed.tags is not None else {}
    if tags:
        document["tags"] = tags
    document.update({name: entries for name, entries in sections.items() if entries})
    return CleanCatalog(text=dump_document(document), added=tuple(added), dropped=tuple(dropped), tags=tags)


def catalog_lines(clean: CleanCatalog) -> tuple[str, ...]:
    lines = [f"Opens the import preview to add {_list(clean.added)}; it changes no item the catalog has."]
    if clean.dropped:
        lines.append(f"Left out, already in the catalog: {_list(clean.dropped)}.")
    return tuple(lines)


def _list(names: Iterable[str]) -> str:
    shown = list(names)
    if len(shown) > 8:
        shown = [*shown[:8], f"{len(shown) - 8} more"]
    if len(shown) <= 2:
        return " and ".join(shown)
    return f"{', '.join(shown[:-1])}, and {shown[-1]}"
