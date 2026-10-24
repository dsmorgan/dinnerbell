# SPDX-License-Identifier: AGPL-3.0-or-later
"""What every call use case shares: the event's write lock, everything about one call loaded
in one place, and the notifications built from it.

Use cases follow one pattern (spec 8: a transition is one transaction that updates state,
writes the outbox, and records the audit row):

    async with locked_event(db, household_id, event_id, now) as event:
        check the rules against ``event``
        change rows
        ctx = await load(db, event)
        await publish(db, ctx, notification, ..., now=now)
        await audit(...)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.events import (
    AssignmentRepository,
    EventRepository,
    ProposalRepository,
    ResponseRepository,
    SuggestionRepository,
    VoteRepository,
)
from dinnerbell.db.meals import household_settings
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import CatalogItem, ItemKind, Mode, Tag, TagKind
from dinnerbell.domain.events import (
    DONE_AFTER_MEAL,
    Assignment,
    AssignmentRole,
    Coverage,
    Event,
    EventState,
    Proposal,
    Response,
    Step,
    Suggestion,
    coverage,
    due_step,
    head_count,
    stale_after,
)
from dinnerbell.domain.household import Household
from dinnerbell.domain.members import Member
from dinnerbell.domain.schedule import SlotSchedule, slot_schedule
from dinnerbell.services.errors import NotFound

NO_SUCH_CALL = "No such call."


@asynccontextmanager
async def locked_event(db: AsyncSession, household_id: int, event_id: int, now: datetime) -> AsyncIterator[Event]:
    """Lock the event for writing, yield it, and commit; roll back on any error.

    The lock is the transaction's first statement (see ``EventRepository.lock``), so the
    rules are checked against the latest state and nothing can change it before the commit.
    """
    events = EventRepository(db)
    try:
        event = await events.get(household_id, event_id) if await events.lock(household_id, event_id, now) else None
        if event is None:
            raise NotFound(NO_SUCH_CALL)
        yield event
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


@dataclass(frozen=True)
class CallContext:
    """One call and everything about it, read inside the event's lock."""

    household: Household
    settings: dict[str, Any]
    event: Event
    members: dict[int, Member]
    responses: list[Response]
    suggestions: list[Suggestion]
    proposals: list[Proposal]
    votes: dict[int, list[int]]
    assignments: list[Assignment]
    items: dict[int, CatalogItem]
    tags: dict[int, Tag]

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.household.timezone)

    @property
    def schedule(self) -> SlotSchedule:
        return slot_schedule(self.settings, self.event.slot)

    def too_late_for(self, day: date) -> datetime:
        """The slot's usual meal time on ``day`` plus two hours: after it, that day's call is over."""
        return datetime.combine(day, self.schedule.meal_time, tzinfo=self.zone).astimezone(UTC) + DONE_AFTER_MEAL

    def response_of(self, member_id: int) -> Response | None:
        return next((r for r in self.responses if r.member_id == member_id), None)

    def name(self, member_id: int) -> str:
        member = self.members.get(member_id)
        return member.display_name if member else "A former member"

    def person(self, member_id: int) -> note.PersonInfo:
        return note.PersonInfo(member_id, self.name(member_id))

    @property
    def active_members(self) -> list[Member]:
        return [m for m in self.members.values() if m.active]

    @property
    def coverage(self) -> Coverage:
        return coverage(self.responses, self.members)

    @property
    def head_count(self) -> int:
        return head_count(self.responses)

    @property
    def participants(self) -> list[Response]:
        return [r for r in self.responses if r.is_in]

    def cuisine_ids(self, tag_ids: Iterable[int]) -> list[int]:
        return [t for t in tag_ids if t in self.tags and self.tags[t].kind is TagKind.CUISINE]

    def proposal(self, proposal_id: int) -> Proposal | None:
        return next((p for p in self.proposals if p.id == proposal_id), None)

    @property
    def winner(self) -> Proposal | None:
        event = self.event
        return next(
            (p for p in self.proposals if p.item_id == event.decided_item_id and p.mode is event.decided_mode), None
        )


async def load(db: AsyncSession, event: Event) -> CallContext:
    household_id = event.household_id
    household = await HouseholdRepository(db).get(household_id)
    if household is None:  # pragma: no cover - events cascade with their household
        raise NotFound(NO_SUCH_CALL)
    catalog = CatalogRepository(db)
    fresh = await EventRepository(db).get(household_id, event.id)
    return CallContext(
        household=household,
        settings=await household_settings(db, household_id),
        event=fresh or event,
        members={m.id: m for m in await MemberRepository(db).list(household_id)},
        responses=await ResponseRepository(db).for_event(household_id, event.id),
        suggestions=await SuggestionRepository(db).for_event(household_id, event.id),
        proposals=await ProposalRepository(db).for_event(household_id, event.id),
        votes=await VoteRepository(db).for_event(household_id, event.id),
        assignments=await AssignmentRepository(db).for_event(household_id, event.id),
        items={i.id: i for i in await catalog.items(household_id)},
        tags={t.id: t for t in await catalog.tags(household_id)},
    )


# Notification building blocks ------------------------------------------------------------------


def event_info(ctx: CallContext) -> note.EventInfo:
    event = ctx.event
    return note.EventInfo(
        id=event.id,
        household_id=event.household_id,
        slot=event.slot,
        day=event.day,
        state=event.state,
        run=event.run,
        path=event.path,
        timezone=ctx.household.timezone,
        respond_deadline=event.respond_deadline,
        vote_deadline=event.vote_deadline,
        coordinator=ctx.person(event.coordinator_id) if event.coordinator_id is not None else None,
        end_reason=event.end_reason,
        ended_by=ctx.person(event.ended_by) if event.ended_by is not None else None,
        end_note=event.end_note,
    )


def _tag_names(ctx: CallContext, tag_ids: Iterable[int]) -> tuple[str, ...]:
    return tuple(sorted((ctx.tags[t].name for t in tag_ids if t in ctx.tags), key=str.casefold))


def suggestion_text(ctx: CallContext, suggestion: Suggestion) -> str:
    if suggestion.item_id is not None:
        item = ctx.items.get(suggestion.item_id)
        return item.name if item else "A removed item"
    return suggestion.free_text or ""


def roster(ctx: CallContext) -> tuple[note.RosterEntry, ...]:
    return tuple(
        note.RosterEntry(
            member=ctx.person(r.member_id),
            status=r.status,
            guests=r.guests,
            guest_needs=_tag_names(ctx, r.guest_needs),
            eat_from=r.eat_from,
            eat_until=r.eat_until,
            offers=tuple(sorted(r.offers)),
            suggestions=tuple(suggestion_text(ctx, s) for s in ctx.suggestions if s.member_id == r.member_id),
            note=r.note,
        )
        for r in ctx.responses
    )


def waiting_on(ctx: CallContext) -> tuple[note.PersonInfo, ...]:
    answered = {r.member_id for r in ctx.responses}
    return tuple(
        ctx.person(m.id)
        for m in sorted(ctx.active_members, key=lambda m: m.display_name.casefold())
        if m.id not in answered
    )


def item_info(ctx: CallContext, proposal: Proposal) -> note.ItemInfo:
    if proposal.item_id is not None and proposal.item_id in ctx.items:
        item = ctx.items[proposal.item_id]
        return note.ItemInfo(
            id=item.id,
            name=item.name,
            kind=item.kind,
            url=item.url,
            order_url=item.order_url,
            cuisines=_tag_names(ctx, ctx.cuisine_ids(item.tag_ids)),
        )
    cuisine = [proposal.free_cuisine_id] if proposal.free_cuisine_id is not None else []
    return note.ItemInfo(
        id=None,
        name=proposal.free_text or "A removed item",
        kind=ItemKind.HOME if proposal.mode is Mode.COOK else ItemKind.RESTAURANT,
        cuisines=_tag_names(ctx, cuisine),
    )


def proposal_info(ctx: CallContext, proposal: Proposal) -> note.ProposalInfo:
    return note.ProposalInfo(
        id=proposal.id,
        rank=proposal.rank,
        item=item_info(ctx, proposal),
        mode=proposal.mode,
        reasons=proposal.reasons,
        source=proposal.source,
        proposed_by=ctx.person(proposal.proposed_by) if proposal.proposed_by is not None else None,
        voters=tuple(ctx.person(m) for m in ctx.votes.get(proposal.id, [])),
    )


def decision_info(ctx: CallContext) -> note.DecisionInfo | None:
    winner, meal_time = ctx.winner, ctx.event.meal_time
    if winner is None or meal_time is None:
        return None
    needs = {tag for r in ctx.participants if r.guests for tag in r.guest_needs}
    return note.DecisionInfo(
        proposal=proposal_info(ctx, winner),
        meal_time=meal_time,
        head_count=ctx.head_count,
        participants=tuple(note.ParticipantInfo(ctx.person(r.member_id), r.guests) for r in ctx.participants),
        assignments=tuple(note.AssignmentInfo(a.role, ctx.person(a.member_id)) for a in ctx.assignments),
        dietary_notes=_tag_names(ctx, needs),
    )


def call_opened(ctx: CallContext) -> note.CallOpened:
    mentions = tuple(ctx.person(m.id) for m in sorted(ctx.active_members, key=lambda m: m.display_name.casefold()))
    return note.CallOpened(event_info(ctx), mentions, roster(ctx), waiting_on(ctx), ctx.head_count)


def call_updated(ctx: CallContext) -> note.CallUpdated:
    return note.CallUpdated(event_info(ctx), roster(ctx), waiting_on(ctx), ctx.head_count)


def proposals(ctx: CallContext) -> tuple[note.ProposalInfo, ...]:
    return tuple(proposal_info(ctx, p) for p in ctx.proposals)


def proposals_updated(ctx: CallContext) -> note.ProposalsUpdated:
    return note.ProposalsUpdated(event_info(ctx), proposals(ctx), ctx.head_count)


def locked_by(ctx: CallContext) -> note.PersonInfo | None:
    return ctx.person(ctx.event.locked_by) if ctx.event.locked_by is not None else None


def decision_updated(ctx: CallContext) -> list[note.Notification]:
    decision = decision_info(ctx)
    return [] if decision is None else [note.DecisionUpdated(event_info(ctx), decision, locked_by(ctx))]


def assignment_made(
    ctx: CallContext, member_id: int, assignment_id: int, decision: note.DecisionInfo
) -> note.AssignmentMade:
    """The direct message to ``member_id`` about all their roles, prompted by ``assignment_id``."""
    order = list(AssignmentRole)
    roles = sorted({a.role for a in ctx.assignments if a.member_id == member_id}, key=order.index)
    return note.AssignmentMade(event_info(ctx), assignment_id, ctx.person(member_id), tuple(roles), decision)


async def publish(db: AsyncSession, ctx: CallContext, *notifications: note.Notification, now: datetime) -> None:
    """Write notifications to the outbox in the caller's transaction."""
    outbox = OutboxRepository(db)
    for notification in notifications:
        await outbox.enqueue(ctx.event.household_id, ctx.event.id, notification, now)


async def stale_cutoff(db: AsyncSession, event: Event) -> datetime:
    """``domain.events.stale_after`` for ``event``, from its household's zone and schedule."""
    household = await HouseholdRepository(db).get(event.household_id)
    if household is None:  # pragma: no cover - events cascade with their household
        raise NotFound(NO_SUCH_CALL)
    schedule = slot_schedule(await household_settings(db, event.household_id), event.slot)
    return stale_after(event, usual_meal=schedule.meal_time, zone=ZoneInfo(household.timezone))


async def due(db: AsyncSession, event: Event, now: datetime) -> Step | None:
    """``domain.events.due_step`` for ``event``. A call that's collecting also gets its
    responses and the household's active members, for the early close (FR-56)."""
    cutoff = await stale_cutoff(db, event)
    if event.state is not EventState.COLLECTING:
        return due_step(event, now, stale_after=cutoff)
    responses = await ResponseRepository(db).for_event(event.household_id, event.id)
    active = [m.id for m in await MemberRepository(db).list(event.household_id) if m.active]
    return due_step(event, now, stale_after=cutoff, responses=responses, active_member_ids=active)
