# SPDX-License-Identifier: AGPL-3.0-or-later
"""Telling the channel about new badges (FR-39).

The scheduler calls ``announce_badges`` on every tick. It reads the awards still ``pending``
(``record_done`` makes them when a call is marked done), queues one post per call, and marks the
awards ``announced``, all in one transaction. So a crash before the commit leaves them pending
for the next tick, and a crash after leaves them announced and queued: a restart, a retry, or two
ticks at once never post a badge twice or lose one. The outbox row is also keyed by the post's
first award, so even a repeated write queues nothing new.

A post covers one done call: every badge it earned, for however many members. It keeps to a size
a channel reads at a glance: a few members and a few badges each, with the rest counted. It lists
members by name, never by what they earned, and says nothing about preferences or feedback.
Awards for members who are no longer active, or for a guest unlinked from Slack before the post
(#201), are marked ``skipped``, not left pending.

Awards of ``SILENT_BADGES`` (Food Critic, earned when feedback is given, long after the call) queue
no post and join none: they are marked ``announced`` in the same transaction, so they are not
pending, and the recap and the newest badges list them like any other.

When an admin's correction takes an award back after its post or the monthly recap was queued,
``take_back`` drops it from them before they're sent (#193).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.contributions import BadgeRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.badges import BY_KEY, SILENT_BADGES, TIERS, Award, news_order
from dinnerbell.domain.members import Member
from dinnerbell.domain.notifications import (
    BadgeNews,
    BadgesEarned,
    BadgeTier,
    MonthlyRecap,
    Notification,
    PayloadError,
    PersonInfo,
    from_payload,
    to_payload,
)

log = logging.getLogger(__name__)

MAX_PEOPLE = 10
"""Members mentioned in one post. Slack posts of this size are the most that stay readable."""
MAX_BADGES = 6
"""Badges named for one member in one post. The rest are counted in the text, and drawn when art is shown."""


def news_for(awards: Iterable[Award], members: dict[int, Member]) -> tuple[list[BadgeNews], int]:
    """``awards`` as one entry per member in name order, and how many members were left off
    (``MAX_PEOPLE``). A badge earned at several tiers at once shows its highest. Each member's
    badges are in ``news_order`` (highest tier, then rarest ladder, then catalog order) before
    ``MAX_BADGES`` cuts them, so ``badges`` holds the most notable and ``rest`` the most common.
    Members not in ``members``, and guests no longer linked to Slack, are left out without being
    counted: a post never names an unregistered guest (#201)."""
    best: dict[int, dict[str, Award]] = defaultdict(dict)
    for award in awards:
        if award.member_id not in members or not members[award.member_id].earns_badges:
            continue
        held = best[award.member_id].get(award.badge.value)
        if held is None or TIERS.index(award.tier) > TIERS.index(held.tier):
            best[award.member_id][award.badge.value] = award
    people: list[BadgeNews] = []
    for member_id in sorted(best, key=lambda m: (members[m].display_name.casefold(), m)):
        earned = sorted(best[member_id].values(), key=lambda a: news_order(a.badge, a.tier))
        shown = tuple(BadgeTier(BY_KEY[a.badge].name, a.tier.label) for a in earned[:MAX_BADGES])
        person = PersonInfo(member_id, members[member_id].display_name)
        rest = tuple(BadgeTier(BY_KEY[a.badge].name, a.tier.label) for a in earned[MAX_BADGES:])
        people.append(BadgeNews(person, shown, more=len(rest), rest=rest))
    return people[:MAX_PEOPLE], max(len(people) - MAX_PEOPLE, 0)


async def announce_badges(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Queue a post for every household's pending awards. Returns how many posts it queued. A
    household that fails is logged and rolled back; the awards stay pending for the next tick."""
    async with sessionmaker() as db:
        households = await HouseholdRepository(db).all()
    queued = 0
    for household in households:
        async with sessionmaker() as db:
            try:
                queued += await announce_household(db, household.id, now=now)
            except Exception:
                await db.rollback()
                log.exception("could not announce badges", extra={"household_id": household.id})
    return queued


async def announce_household(db: AsyncSession, household_id: int, *, now: datetime) -> int:
    """One household's pending awards, queued and marked in a single transaction."""
    badges = BadgeRepository(db)
    if not await badges.pending(household_id):  # the usual tick: no write lock for nothing
        return 0
    async with write_transaction(db):
        pending = await badges.pending(household_id)  # again, under the lock: another tick may have won
        if not pending:
            return 0
        members = {m.id: m for m in await MemberRepository(db).list(household_id) if m.active and m.earns_badges}
        silent = [a for a in pending if a.badge in SILENT_BADGES and a.member_id in members]
        heard = [a for a in pending if a.badge not in SILENT_BADGES and a.member_id in members]
        by_call: dict[int | None, list[Award]] = defaultdict(list)
        for award in heard:
            by_call[award.event_id].append(award)
        outbox = OutboxRepository(db)
        queued = 0
        for group in sorted(by_call.values(), key=lambda g: min(a.id for a in g)):
            news, more = news_for(group, members)
            post = BadgesEarned(household_id, min(a.id for a in group), tuple(news), more)
            await outbox.enqueue(household_id, None, post, now)
            queued += 1
        await badges.mark_announced(household_id, [a.id for a in (*heard, *silent)], now)
        gone = [a.id for a in pending if a not in heard and a not in silent]
        await badges.mark_skipped(household_id, gone)
        await db.commit()
    if queued:
        log.info(
            "badge announcements queued", extra={"household_id": household_id, "posts": queued, "badges": len(heard)}
        )
    return queued


async def take_back(db: AsyncSession, household_id: int, revoked: Sequence[Award]) -> int:
    """Drop ``revoked`` from the household's badge posts and monthly recaps that are queued and
    not sent yet, in the caller's transaction (#193), as revision 0028 does for unregistered
    guests. A member left with no badge leaves the post, and a badge post left naming nobody is
    closed unsent when a drainer reaches it (``outbox._overtaken``); a recap still goes, without
    them. A post in a drainer's hands right now may still go out as it was; if that try fails,
    the retry doesn't. Returns how many posts changed.

    A post names each badge at the highest tier earned in it, so taking back that tier drops the
    badge from the post even when a lower tier earned with it still holds."""
    if not revoked:
        return 0
    gone = {(a.member_id, BadgeTier(BY_KEY[a.badge].name, a.tier.label)) for a in revoked}
    outbox = OutboxRepository(db)
    changed = 0
    for kind in (BadgesEarned.kind, MonthlyRecap.kind):
        for entry in await outbox.unsent(household_id, kind):
            try:
                post = from_payload(entry.kind, entry.payload)
            except PayloadError:
                continue  # the drainer closes it as undeliverable
            trimmed = _trimmed(post, gone)
            if trimmed is not None and await outbox.set_payload(entry.id, to_payload(trimmed)):
                changed += 1
                log.info(
                    "badge post trimmed", extra={"household_id": household_id, "outbox_id": entry.id, "kind": kind}
                )
    return changed


def _trimmed(post: Notification, gone: set[tuple[int, BadgeTier]]) -> Notification | None:
    """``post`` without the badges in ``gone``, or None when it names none of them."""
    if isinstance(post, BadgesEarned):
        news = _all_without(post.news, gone)
        return None if news == post.news else replace(post, news=news)
    if isinstance(post, MonthlyRecap):
        badges = _all_without(post.badges, gone)
        return None if badges == post.badges else replace(post, badges=badges)
    return None  # pragma: no cover - only those two kinds are read


def _all_without(people: tuple[BadgeNews, ...], gone: set[tuple[int, BadgeTier]]) -> tuple[BadgeNews, ...]:
    return tuple(kept for n in people if (kept := _without(n, gone)) is not None)


def _without(news: BadgeNews, gone: set[tuple[int, BadgeTier]]) -> BadgeNews | None:
    """``news`` without the badges in ``gone``, or None when it has none left. The badges still
    shown fill up to ``MAX_BADGES`` from ``rest``, as ``news_for`` would."""
    every = (*news.badges, *news.rest)
    kept = [b for b in every if (news.person.id, b) not in gone]
    if len(kept) == len(every):
        return news
    unnamed = news.more - len(news.rest)  # counted but not named, by a post queued before ``rest``
    if not kept and unnamed <= 0:
        return None
    rest = tuple(kept[MAX_BADGES:])
    return replace(news, badges=tuple(kept[:MAX_BADGES]), rest=rest, more=unnamed + len(rest))
