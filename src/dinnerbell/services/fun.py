# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fun mode (#153): each person's switch for Dinnerbone's Den, and what the den shows them.

Members turn it on or off on Preferences, and guests on their dietary-needs page. Everything
here acts on ``actor`` only. None of it is audited, like the other settings people change about
themselves on Preferences: it changes nothing anyone else sees.

``den`` is what Today's den shows: the scene, the call he follows and his pose, the quiet
caption for that call, and the newest badge the actor earned that the den hasn't flipped for.
``celebration`` is what the member who locked or marked done a call on the web gets instead of
the quiet caption.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.contributions import BadgeRepository
from dinnerbell.db.events import EventRepository
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.badges import BY_KEY, Announcement
from dinnerbell.domain.events import Event, EventKind, EventState
from dinnerbell.domain.fun import (
    DONE_CAPTION,
    Pose,
    Scene,
    badge_caption,
    decided_caption,
    followed_call,
    pose,
    scene,
)
from dinnerbell.domain.household import Household
from dinnerbell.domain.members import Member
from dinnerbell.services import guests as guest_rules
from dinnerbell.services.errors import Forbidden
from dinnerbell.services.variety import household_today

Celebrate = Literal["decided", "done", "badge"]


@dataclass(frozen=True, slots=True)
class BadgeFlip:
    """A badge the den hasn't flipped for yet: the newest award, and its caption."""

    award_id: int
    caption: str


@dataclass(frozen=True, slots=True)
class Den:
    scene: Scene
    pose: Pose
    call: Event | None
    """The call he follows (``domain.fun.followed_call``), or None."""
    caption: str | None
    """The quiet caption for that call once it's decided or done, such as "Pad thai it is!"."""
    badge: BadgeFlip | None = None


async def set_my_fun_mode(db: AsyncSession, actor: Member, on: bool, *, now: datetime) -> Member:
    """Turn the actor's fun mode on or off. A member or a guest, as long as they're active."""
    if not actor.active:
        raise Forbidden("Sign in again.")
    updated = await MemberRepository(db).update(actor.household_id, actor.id, now, fun_mode=on)
    await db.commit()
    return updated


async def _caption(db: AsyncSession, event: Event) -> str | None:
    if event.state is EventState.DONE:
        return DONE_CAPTION
    if event.state is not EventState.DECIDED:
        return None
    item = None
    if event.decided_item_id is not None:
        item = await CatalogRepository(db).item(event.household_id, event.decided_item_id)
    return decided_caption(None if item is None else item.name, event.slot.value)


async def den(db: AsyncSession, actor: Member, household: Household, *, now: datetime, with_badge: bool = False) -> Den:
    """What Today's den shows the actor at ``now``. He follows the calls they see on Today: a
    guest's, only the ones they're invited to. ``with_badge`` adds the newest live badge they
    earned since the den last flipped for one (announced or waiting to be; never one made
    quietly, such as a catch-up)."""
    events = await EventRepository(db).for_today(household.id, household_today(household, now))
    if actor.is_guest:
        invited = set(await guest_rules.invited_events(db, actor)) if actor.active else set()
        events = [e for e in events if e.id in invited]
    call = followed_call(events, now, household.timezone)
    at = scene(now, household.timezone)
    badge = await _unseen_badge(db, actor) if with_badge else None
    caption = None if call is None else await _caption(db, call)
    return Den(at, pose(at, None if call is None else call.state), call, caption, badge)


async def _unseen_badge(db: AsyncSession, actor: Member) -> BadgeFlip | None:
    awards = [
        a
        for a in await BadgeRepository(db).for_member(actor.household_id, actor.id)
        if a.id > actor.fun_seen_award and a.announcement is not Announcement.SKIPPED
    ]
    if not awards:
        return None
    newest = max(awards, key=lambda a: (a.awarded_at, a.id))
    return BadgeFlip(max(a.id for a in awards), badge_caption(BY_KEY[newest.badge].name, newest.tier.label))


async def saw_badges(db: AsyncSession, actor: Member, award_id: int, *, now: datetime) -> None:
    """The den flipped for the actor's awards up to ``award_id``, one of their own: it won't
    again, on any device. Never moves the marker back."""
    if not actor.active:
        raise Forbidden("Sign in again.")
    mine = {a.id for a in await BadgeRepository(db).for_member(actor.household_id, actor.id)}
    if award_id not in mine:
        return
    if await MemberRepository(db).raise_fun_seen_award(actor.household_id, actor.id, award_id, now):
        await db.commit()


async def celebration(db: AsyncSession, actor: Member, event_id: int, kind: Celebrate) -> dict[str, object] | None:
    """The celebration for the member who just locked (``decided``) or marked done (``done``)
    call ``event_id`` on the web, or None: fun mode off, or the call isn't in that state (a
    refusal, or someone else moved it on first)."""
    if not actor.fun_mode:
        return None
    event = await EventRepository(db).get(actor.household_id, event_id)
    wanted = EventState.DECIDED if kind == "decided" else EventState.DONE
    if event is None or event.kind is not EventKind.CALL or event.state is not wanted:
        return None
    caption = await _caption(db, event)
    return {"kind": kind, "event": event.id, "run": event.run, "state": event.state.value, "caption": caption}
