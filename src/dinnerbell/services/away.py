# SPDX-License-Identifier: AGPL-3.0-or-later
"""Away (FR-63, #196): set and end a member's away window, and answer the calls it covers.

A member sets their own window, from **Away until…** on a call or from Preferences; an admin
sets or ends a member's from Admin > Members (#181). Either way it's audited, with the actor and
the member: ``member.away_set`` and ``member.away_cleared``.

The window answers calls by writing real outs, marked with the day the member is back
(``Response.away_until``), so the head count, the early close (FR-56), maybes, the random
coordinator (FR-10), and the reminder (FR-11) need no rule of their own. They're written:

- when a call opens, or is posted early, and when it's re-run or started again (``apply_to_call``,
  under the call's lock, so the outs count as answers in that run);
- when a window is set or ended while calls are taking answers (``_sync_open_calls``).

Only a call nobody answered is answered Out: an answer the member gave stays. An out the window
wrote follows the window while answers are open: a new back day updates it, and a window that
no longer covers the call takes it back, so the member is asked again. Once answers close, it
stays as it is, and the member can still join late. Their own answer, In included, clears the
mark for that call (``domain.events.with_status``); the window stays.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.events import EventRepository, ResponseRepository
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.away import Away, away_from_call, check_away
from dinnerbell.domain.events import Event, EventRuleViolation, EventState, Response, Status
from dinnerbell.domain.members import Member
from dinnerbell.services import call_context as cc
from dinnerbell.services.call_context import NO_SUCH_CALL, load, publish
from dinnerbell.services.common import audit, require_member
from dinnerbell.services.errors import NotFound
from dinnerbell.services.preferences import member_for_admin
from dinnerbell.services.variety import household_today

NOT_ON_AN_OFFER = "An offer doesn't ask who's in. Set your away days on Preferences."


async def _today(db: AsyncSession, member: Member, now: datetime) -> date:
    household = await HouseholdRepository(db).get(member.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    return household_today(household, now)


# Setting and ending a window --------------------------------------------------------------------


async def set_my_away(db: AsyncSession, actor: Member, first: date, last: date, *, now: datetime) -> Member:
    """The actor sets their window on Preferences, replacing any they had."""
    require_member(actor)
    away = check_away(first, last, await _today(db, actor, now))
    return await _set(db, actor, actor, away, source="preferences", now=now)


async def set_member_away(
    db: AsyncSession, actor: Member, member_id: int, first: date, last: date, *, now: datetime
) -> Member:
    """An admin sets a member's window from Admin > Members (#181)."""
    member = await member_for_admin(db, actor, member_id)
    away = check_away(first, last, await _today(db, member, now))
    return await _set(db, actor, member, away, source="admin", now=now)


async def away_until(db: AsyncSession, actor: Member, event_id: int, back: date, *, now: datetime) -> Member:
    """**Away until…** on a call: the actor is away from the call's day through the day before
    ``back``, or from the first day of the window they have, when it reaches the call's day
    (``away_from_call``). Any call in the household: the window is about days, not the call."""
    require_member(actor)
    event = await EventRepository(db).get(actor.household_id, event_id)
    if event is None:
        raise NotFound(NO_SUCH_CALL)
    if event.is_offer:
        raise EventRuleViolation(NOT_ON_AN_OFFER, code="is_offer")
    me = await MemberRepository(db).get(actor.household_id, actor.id) or actor  # the window as it is now
    today = await _today(db, me, now)
    away = away_from_call(event.day, back, today, me.away_now(today))
    return await _set(db, actor, me, away, source="call", now=now)


async def clear_my_away(db: AsyncSession, actor: Member, *, now: datetime) -> Member:
    """**I'm back**, or **Clear** before it starts: the actor's window ends now."""
    require_member(actor)
    return await _clear(db, actor, actor, now=now)


async def back_on_call(db: AsyncSession, actor: Member, event_id: int, *, now: datetime) -> bool:
    """**I'm back** on a call's card, or Home: ``clear_my_away``, for a call in the actor's
    household, as every button on a call is. Returns whether the window had started, so the
    reply can welcome them back or only say it's cleared."""
    require_member(actor)
    if await EventRepository(db).get(actor.household_id, event_id) is None:
        raise NotFound(NO_SUCH_CALL)
    today = await _today(db, actor, now)
    me = await MemberRepository(db).get(actor.household_id, actor.id) or actor
    current = me.away_now(today)
    await _clear(db, actor, me, now=now)
    return current is not None and current.under_way(today)


async def clear_member_away(db: AsyncSession, actor: Member, member_id: int, *, now: datetime) -> Member:
    """An admin ends a member's window (#181)."""
    member = await member_for_admin(db, actor, member_id)
    return await _clear(db, actor, member, now=now)


async def _set(db: AsyncSession, actor: Member, member: Member, away: Away, *, source: str, now: datetime) -> Member:
    """Store ``away`` as ``member``'s window, answer the open calls it covers, audit, and commit.
    The member's row is the transaction's first write, so it holds the write lock from there.
    The window they have already changes nothing, and writes no audit row."""
    if member.away == away:
        return member
    updated = await MemberRepository(db).update(
        member.household_id, member.id, now, away_first=away.first, away_last=away.last
    )
    payload = {"first": away.first.isoformat(), "last": away.last.isoformat(), "from": source}
    await audit(db, member.household_id, actor, "member.away_set", f"member:{member.id}", payload, now)
    await _sync_open_calls(db, updated, now)
    await db.commit()
    return updated


async def _clear(db: AsyncSession, actor: Member, member: Member, *, now: datetime) -> Member:
    """End ``member``'s window now, give back the open calls it answered, audit, and commit. A
    window that has ended already ends nothing, and writes no audit row."""
    current = member.away_now(await _today(db, member, now))
    if current is None:
        return member
    updated = await MemberRepository(db).update(member.household_id, member.id, now, away_first=None, away_last=None)
    payload = {"first": current.first.isoformat(), "last": current.last.isoformat()}
    await audit(db, member.household_id, actor, "member.away_cleared", f"member:{member.id}", payload, now)
    await _sync_open_calls(db, updated, now)
    await db.commit()
    return updated


async def _sync_open_calls(db: AsyncSession, member: Member, now: datetime) -> None:
    """Bring ``member``'s answers to every call taking answers in line with their window, under
    each call's lock, and update the call messages that changed."""
    if not member.is_member:
        return
    events = EventRepository(db)
    for event_id in await events.open_ids(member.household_id):
        event = await events.get(member.household_id, event_id)
        if event is None or event.is_offer or event.state is not EventState.COLLECTING:
            continue
        await events.lock(member.household_id, event_id, now)
        responses = await ResponseRepository(db).for_event(member.household_id, event_id)
        mine = next((r for r in responses if r.member_id == member.id), None)
        if await _answer(db, event, member, mine, now, touch=False):
            ctx = await load(db, event)
            await publish(db, ctx, cc.call_updated(ctx), now=now)


# Answering calls --------------------------------------------------------------------------------


async def apply_to_call(db: AsyncSession, event: Event, now: datetime) -> None:
    """Answer ``event`` Out for every active member away on its day, as it opens, or opens
    again after a re-run or restart. The caller holds the call's lock and publishes the call.
    An out the window wrote before is saved again, so it counts as answered in this run
    (FR-56)."""
    if event.is_offer or event.state is not EventState.COLLECTING:
        return
    responses = {r.member_id: r for r in await ResponseRepository(db).for_event(event.household_id, event.id)}
    for member in await MemberRepository(db).list(event.household_id):
        if member.is_member:
            await _answer(db, event, member, responses.get(member.id), now, touch=True)


async def _answer(
    db: AsyncSession, event: Event, member: Member, mine: Response | None, now: datetime, *, touch: bool
) -> bool:
    """Make ``member``'s answer to ``event`` match their window. Returns whether it changed. An
    answer of their own is never touched. ``touch`` saves an away out that's right already, so
    it counts in a new run."""
    responses = ResponseRepository(db)
    if member.away is not None and member.away_on(event.day):
        back = member.away.back
        if mine is None:
            fresh = Response(member_id=member.id, status=Status.OUT, responded_at=now, away_until=back)
            await responses.save(event.household_id, event.id, fresh, now)
            return True
        if mine.away_until is not None and (touch or mine.away_until != back):
            await responses.save(event.household_id, event.id, replace(mine, away_until=back), now)
            return True
        return False
    if mine is not None and mine.away_until is not None:  # the window no longer covers it
        await responses.delete(event.household_id, event.id, member.id)
        return True
    return False
