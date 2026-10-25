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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.events import (
    AssignmentRepository,
    EventRepository,
    InvitationRepository,
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
    posted_before_its_day,
    stale_after,
    veto_participants,
)
from dinnerbell.domain.household import Household
from dinnerbell.domain.members import Member
from dinnerbell.domain.offers import seats_taken
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
    invitations: dict[int, int | None] = field(default_factory=dict)
    """Guests invited to the call (#38), each with the member who invited them."""

    def invited(self, member_id: int) -> bool:
        return member_id in self.invitations

    def is_guest(self, member_id: int) -> bool:
        member = self.members.get(member_id)
        return member is not None and member.is_guest

    @property
    def guests(self) -> list[Member]:
        """Active guests invited to the call, by name."""
        found = [m for m in self.members.values() if m.is_guest and m.active and m.id in self.invitations]
        return sorted(found, key=lambda m: m.display_name.casefold())

    @property
    def veto_people(self) -> frozenset[int]:
        """Whose vetoes and soft preferences apply (FR-28): members in or maybe. A guest's
        stances never exist (#38); their dietary needs count through ``guest_needs``."""
        return frozenset(m for m in veto_participants(self.responses) if not self.is_guest(m))

    @property
    def guest_needs(self) -> frozenset[int]:
        """Dietary needs every pick must meet (spec 5.5, 7): those of the guests members bring,
        and people's own (#38: a guest's, and a promoted guest's, which they keep), for everyone
        who might be eating (in, or maybe). Needs left on a response with no guests don't count."""
        who = veto_participants(self.responses)
        brought = {tag for r in self.responses if r.member_id in who and r.guests for tag in r.guest_needs}
        people = [self.members[m] for m in who if m in self.members]
        own = {tag for m in people if m.active for tag in m.dietary_needs}
        return frozenset(brought | own)

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
        """The household's active members and admins, not guests: whom a call asks and waits for."""
        return [m for m in self.members.values() if m.is_member]

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
        invitations=await InvitationRepository(db).for_event(household_id, event.id),
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
        early=posted_before_its_day(event, ctx.zone),
        kind=event.kind,
    )


def _tag_names(ctx: CallContext, tag_ids: Iterable[int]) -> tuple[str, ...]:
    return tuple(sorted((ctx.tags[t].name for t in tag_ids if t in ctx.tags), key=str.casefold))


def suggestion_text(ctx: CallContext, suggestion: Suggestion) -> str:
    if suggestion.item_id is not None:
        item = ctx.items.get(suggestion.item_id)
        return item.name if item else "A removed item"
    return suggestion.free_text or ""


def _picks_of(ctx: CallContext, member_id: int) -> tuple[str, ...]:
    """What ``member_id`` suggested, and, while responses are open, the picks their offers added
    (FR-5): those are proposals already, but the proposals message comes only with the vote."""
    said = [suggestion_text(ctx, s) for s in ctx.suggestions if s.member_id == member_id]
    if ctx.event.state is EventState.COLLECTING:
        said += [item_info(ctx, p).name for p in ctx.proposals if p.proposed_by == member_id]
    return tuple(said)


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
            suggestions=_picks_of(ctx, r.member_id),
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
    return note.DecisionInfo(
        proposal=proposal_info(ctx, winner),
        meal_time=meal_time,
        head_count=ctx.head_count,
        participants=tuple(note.ParticipantInfo(ctx.person(r.member_id), r.guests) for r in ctx.participants),
        assignments=tuple(note.AssignmentInfo(a.role, ctx.person(a.member_id)) for a in ctx.assignments),
        dietary_notes=guest_dietary_notes(ctx),
    )


def guest_dietary_notes(ctx: CallContext) -> tuple[str, ...]:
    """Dietary needs of the guests of everyone in, and of everyone in who has their own (FR-8,
    FR-20, #38). Only the needs, never whose."""
    brought = {tag for r in ctx.participants if r.guests for tag in r.guest_needs}
    people = [ctx.members[r.member_id] for r in ctx.participants if r.member_id in ctx.members]
    own = {tag for m in people if m.active for tag in m.dietary_needs}
    return _tag_names(ctx, brought | own)


def offer_info(ctx: CallContext) -> note.OfferInfo:
    """The offer ``ctx.event`` (FR-4). An offer keeps its item, mode, and time on the event."""
    event = ctx.event
    if not event.is_offer or event.decided_item_id is None or event.decided_mode is None or event.meal_time is None:
        raise ValueError("not an offer")  # pragma: no cover - offers are created with all three
    item = ctx.items.get(event.decided_item_id)
    found = (
        note.ItemInfo(
            item.id, item.name, item.kind, item.url, item.order_url, _tag_names(ctx, ctx.cuisine_ids(item.tag_ids))
        )
        if item is not None
        else note.ItemInfo(None, "A removed item", ItemKind.RESTAURANT)  # pragma: no cover - items are never deleted
    )
    offerer = ctx.person(event.created_by) if event.created_by is not None else note.PersonInfo(0, "A former member")
    return note.OfferInfo(
        offerer=offerer,
        item=found,
        mode=event.decided_mode,
        at=event.meal_time,
        capacity=event.capacity or 0,
        taken=seats_taken(event, ctx.responses),
        dietary_notes=guest_dietary_notes(ctx),
    )


def offer_posted(ctx: CallContext) -> note.OfferPosted:
    return note.OfferPosted(event_info(ctx), offer_info(ctx), roster(ctx), ctx.head_count)


def offer_updated(ctx: CallContext) -> note.OfferUpdated:
    return note.OfferUpdated(event_info(ctx), offer_info(ctx), roster(ctx), ctx.head_count)


def message_updated(ctx: CallContext) -> note.CallUpdated | note.OfferUpdated:
    """The update for the event's own message: the call message, or the offer message."""
    return offer_updated(ctx) if ctx.event.is_offer else call_updated(ctx)


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
    via = answering_for(ctx, member_id)
    return note.AssignmentMade(event_info(ctx), assignment_id, ctx.person(member_id), tuple(roles), decision, via)


def guest_invitations(ctx: CallContext) -> list[note.GuestInvited]:
    """Invitations to send again when a call starts a new run (#38): to each registered guest
    invited to it who hasn't answered this run. The key names the run, so each run sends once."""
    info = event_info(ctx)
    sent: list[note.GuestInvited] = []
    for guest in ctx.guests:
        answer = ctx.response_of(guest.id)
        fresh = answer is not None and answer.updated_at is not None and answer.updated_at >= ctx.event.opens_at
        if guest.is_registered_guest and not fresh:
            inviter = ctx.invitations.get(guest.id)
            by = ctx.person(inviter) if inviter is not None else note.PersonInfo(0, "A member")
            sent.append(note.GuestInvited(info, ctx.person(guest.id), by))
    return sent


def answering_for(ctx: CallContext, member_id: int) -> note.PersonInfo | None:
    """Who gets a guest's direct messages when the guest has no Slack (#38): the member who
    invited them. None for anyone who gets their own."""
    member = ctx.members.get(member_id)
    if member is None or not member.is_guest or member.is_registered_guest:
        return None
    inviter = ctx.invitations.get(member_id)
    return ctx.person(inviter) if inviter is not None else None


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
    responses, the household's active members, and the coverage, for the early close (FR-56)."""
    cutoff = await stale_cutoff(db, event)
    if event.state is not EventState.COLLECTING:
        return due_step(event, now, stale_after=cutoff)
    responses = await ResponseRepository(db).for_event(event.household_id, event.id)
    members = {m.id: m for m in await MemberRepository(db).list(event.household_id)}
    active = [m.id for m in members.values() if m.is_member]  # nobody waits for a guest (#38)
    invited = await InvitationRepository(db).for_event(event.household_id, event.id)
    guests = [g for g in invited if g in members and members[g].active]
    covered = coverage(responses, members)
    return due_step(
        event,
        now,
        stale_after=cutoff,
        responses=responses,
        active_member_ids=active,
        coverage=covered,
        guest_ids=guests,
    )
