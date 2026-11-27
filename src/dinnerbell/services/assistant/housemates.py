# SPDX-License-Identifier: AGPL-3.0-or-later
"""Housemates' Preferences for the assistant (#367, ADR 0012): each other active member's levels
on tags and catalog items (veto, or -2 to 2), their dietary needs, and their **I can usually**.

Only while **Memory** is on, and only in a private conversation (``tools``). FR-29 lets a member
see only their own Preferences; this is the one way a member's assistant reads anyone else's, so
it sends no more than those: never ratings, what ratings did to a score, feedback, notes, away
days, or anyone's answers. Guests and former members are never listed. Each member goes by the
conversation's label.

``summary`` is the same shape for one member, for the memory rollup, so it skips what that
member's Preferences already hold.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.preferences import PreferenceRepository
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.assistant_own import can_usually_flags
from dinnerbell.domain.catalog import CatalogItem, Tag, TagKind, name_key
from dinnerbell.domain.members import Member
from dinnerbell.domain.preferences import TargetType

MAX_LISTED = 25
"""The most tags and the most items listed for one member."""


async def preferences(
    db: AsyncSession, actor: Member, text: str, wanted: int | None, label: Callable[[int], str]
) -> dict[str, Any]:
    """Every active housemate of ``actor`` (``wanted`` alone, when given) with their Preferences:
    the tags and items they set a level on whose names contain ``text`` (all they set, with
    none), their dietary needs by tag name, and **I can usually**. ``label`` names each member."""
    household_id = actor.household_id
    members = [
        m
        for m in await MemberRepository(db).list(household_id)
        if m.is_member and m.id != actor.id and (wanted is None or m.id == wanted)
    ]
    catalog = CatalogRepository(db)
    tags, items = await catalog.tags(household_id), await catalog.items(household_id)
    found = [
        {"member": label(m.id), **await _of(db, m, tags, items, text)}
        for m in sorted(members, key=lambda m: label(m.id))
    ]
    return {"housemates": found}


async def summary(db: AsyncSession, member: Member) -> dict[str, Any]:
    """What ``member``'s own Preferences hold, in the same shape, for the memory rollup."""
    catalog = CatalogRepository(db)
    tags, items = await catalog.tags(member.household_id), await catalog.items(member.household_id)
    return await _of(db, member, tags, items, "")


async def _of(
    db: AsyncSession, member: Member, tags: Sequence[Tag], items: Sequence[CatalogItem], text: str
) -> dict[str, Any]:
    stances = {
        (p.target_type, p.target_id): p.stance
        for p in await PreferenceRepository(db).for_member(member.household_id, member.id)
    }
    key = name_key(text)
    tag_levels = _levels(stances, TargetType.TAG, {t.id: t.name for t in tags}, key)
    item_levels = _levels(stances, TargetType.ITEM, {i.id: i.name for i in items}, key)
    dietary = sorted((t for t in tags if t.kind is TagKind.DIETARY and t.id in member.dietary_needs), key=_tag_key)
    return {
        "tags": tag_levels[:MAX_LISTED],
        "items": item_levels[:MAX_LISTED],
        "more": len(tag_levels) > MAX_LISTED or len(item_levels) > MAX_LISTED,
        "dietary_needs": [t.name for t in dietary],
        "can_usually": can_usually_flags(member.capabilities),
    }


def _tag_key(tag: Tag) -> tuple[str, int]:
    return (tag.key, tag.id)


def _levels(
    stances: Mapping[tuple[TargetType, int], Any], kind: TargetType, names: Mapping[int, str], key: str
) -> list[dict[str, str]]:
    found = [
        {"name": names[target_id], "level": stance.level}
        for (target_type, target_id), stance in stances.items()
        if target_type is kind and target_id in names and not stance.is_neutral and key in name_key(names[target_id])
    ]
    return sorted(found, key=lambda f: name_key(f["name"]))
