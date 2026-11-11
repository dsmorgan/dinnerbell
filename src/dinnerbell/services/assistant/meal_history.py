# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the household has eaten, for the assistant's ``look_up_history`` tool (spec 6.14, #264).

The meal log, counted per catalog item: how often and when last, with the item's kind, cuisines,
and tags, and the household's hint (FR-49), from ``feedback.item_hints``, the same hint Catalog
shows. It never says who cooked, ordered, picked up, logged, or ate a meal, and never gives
anyone's rating, issues, or notes: an aggregate that skips those can't name anyone.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.meals import MealLogRepository
from dinnerbell.domain.catalog import CatalogItem, ItemKind, Tag, TagKind, name_key
from dinnerbell.domain.members import Member
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services import feedback
from dinnerbell.services.common import require_member


class Order(StrEnum):
    MOST = "most"
    """Eaten most often first."""
    RECENT = "recent"
    """Eaten most recently first."""


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    """Which meals count. ``text`` and ``tag`` are already ``name_key``ed; empty matches any.
    ``since`` and ``until`` are inclusive; None leaves that side open."""

    text: str = ""
    tag: str = ""
    kind: ItemKind | None = None
    since: date | None = None
    until: date | None = None
    order: Order = Order.MOST


@dataclass(frozen=True, slots=True)
class EatenItem:
    item: CatalogItem
    cuisines: tuple[str, ...]
    tags: tuple[str, ...]
    """Every tag but the cuisines, in name order."""
    times: int
    last: date
    hint: str | None = None
    """The household's hint, or None while the ratings and issues give none."""


@dataclass(frozen=True, slots=True)
class Eaten:
    items: list[EatenItem]
    """At most the limit asked for."""
    more: bool
    """True when more items matched than ``items`` holds."""


def _tag_names(item: CatalogItem, tags: dict[int, Tag], *, cuisine: bool) -> tuple[str, ...]:
    chosen = (tags[t] for t in item.tag_ids if t in tags and (tags[t].kind is TagKind.CUISINE) == cuisine)
    return tuple(sorted((t.name for t in chosen), key=name_key))


def _matches(item: CatalogItem, names: Iterable[str], query: HistoryQuery) -> bool:
    if query.kind is not None and item.kind is not query.kind:
        return False
    if query.text not in item.key:
        return False
    return not query.tag or any(query.tag in name_key(name) for name in names)


def _order(item: EatenItem, order: Order) -> tuple[int, int, str, int]:
    times, recency = -item.times, -item.last.toordinal()
    first, second = (recency, times) if order is Order.RECENT else (times, recency)
    return (first, second, item.item.key, item.item.id)


async def eaten_items(db: AsyncSession, actor: Member, query: HistoryQuery, *, today: date, limit: int) -> Eaten:
    """The catalog items the household ate between ``query.since`` and ``query.until`` that
    match ``query``, active or not, in ``query.order`` and then name order, at most ``limit``.
    Each carries its hint, which covers all of its ratings, whatever the dates asked. ``today``
    is in the household's time zone, for the hints."""
    require_member(actor)
    counts = await MealLogRepository(db).eaten(actor.household_id, query.since, query.until)
    data = await catalog_service.catalog(db, actor)
    tags = {t.id: t for t in data.tags}
    found: list[EatenItem] = []
    for item in data.items:
        if item.id not in counts:
            continue
        cuisines, others = _tag_names(item, tags, cuisine=True), _tag_names(item, tags, cuisine=False)
        if _matches(item, (*cuisines, *others), query):
            found.append(EatenItem(item, cuisines, others, *counts[item.id]))
    found.sort(key=lambda e: _order(e, query.order))
    shown = found[:limit]
    hints = await feedback.item_hints(db, actor, [e.item.id for e in shown], today=today) if shown else {}
    return Eaten([replace(e, hint=hints.get(e.item.id)) for e in shown], more=len(found) > limit)
