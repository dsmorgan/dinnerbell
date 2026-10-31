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
from dataclasses import dataclass, field, replace
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
    MAX_SHUFFLES,
    Assignment,
    AssignmentRole,
    Coverage,
    Event,
    EventState,
    Proposal,
    ProposalTally,
    Response,
    Status,
    Step,
    Suggestion,
    counted_votes,
    coverage,
    due_step,
    head_count,
    late_people,
    owed_since,
    posted_before_its_day,
    roles_unclear,
    shuffle_candidates,
    stale_after,
    tie_line,
    tied_leaders,
    veto_participants,
)
from dinnerbell.domain.household import Household
from dinnerbell.domain.members import Member
from dinnerbell.domain.offers import seats_taken
from dinnerbell.domain.ranking import GroupPreferences
from dinnerbell.domain.schedule import SlotSchedule, slot_schedule
from dinnerbell.services import activity
from dinnerbell.services.common import audit
from dinnerbell.services.errors import NotFound
from dinnerbell.services.preferences import group_preferences

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
        activity.about(event)  # for the refusal's log line, if the rules refuse it
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
    late_preferences: dict[int, GroupPreferences] = field(default_factory=dict)
    """Each late joiner's own stances, while they take part (#76). They never filter, and only
    that late joiner may see what they rule out (``fits_late_joiner``, FR-29)."""

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
        return frozenset(self.settled_guest_needs | self.late_guest_needs)

    @property
    def settled_guest_needs(self) -> frozenset[int]:
        """``guest_needs`` without those of guests that late joiners bring: what picks already
        proposed were held to. They warn instead (``late_warnings``, #76)."""
        who = veto_participants(self.responses)
        brought = {tag for r in self.responses if r.member_id in who and r.guests for tag in r.guest_needs}
        people = [self.members[m] for m in who if m in self.members]
        own = {tag for m in people if m.active for tag in m.dietary_needs}
        return frozenset(brought | own)

    @property
    def late_guest_needs(self) -> frozenset[int]:
        """Dietary needs of guests that late joiners taking part bring (#76). Like any guest's,
        they filter picks drawn or added from now on, and the roster shows them, so a pick
        already proposed that misses them warns everyone (``late_warnings``). A late joiner's
        own needs are not here: they stay private (FR-29)."""
        late = late_people(self.responses)
        return frozenset(tag for r in self.responses if r.member_id in late and r.guests for tag in r.guest_needs)

    def counted(self, proposal_id: int) -> frozenset[int]:
        """Who voted for ``proposal_id`` and counts (``domain.events.counts_vote``)."""
        votes = {proposal_id: self.votes.get(proposal_id, [])}
        return counted_votes(votes, self.responses)[proposal_id]

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
    responses = await ResponseRepository(db).for_event(household_id, event.id)
    late = late_people(responses)
    return CallContext(
        household=household,
        settings=await household_settings(db, household_id),
        event=fresh or event,
        members={m.id: m for m in await MemberRepository(db).list(household_id)},
        responses=responses,
        suggestions=await SuggestionRepository(db).for_event(household_id, event.id),
        proposals=await ProposalRepository(db).for_event(household_id, event.id),
        votes=await VoteRepository(db).for_event(household_id, event.id),
        assignments=await AssignmentRepository(db).for_event(household_id, event.id),
        items={i.id: i for i in await catalog.items(household_id)},
        tags={t.id: t for t in await catalog.tags(household_id)},
        invitations=await InvitationRepository(db).for_event(household_id, event.id),
        late_preferences={m: await group_preferences(db, household_id, frozenset({m})) for m in late},
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
        plan=plan_info(ctx),
        shuffle_note=event.shuffle_note if event.state is EventState.VOTING else None,
    )


def plan_info(ctx: CallContext) -> note.PlanInfo | None:
    """What a decided call is having, how, and when."""
    winner, meal_time = ctx.winner, ctx.event.meal_time
    if winner is None or meal_time is None:
        return None
    return note.PlanInfo(item_info(ctx, winner).name, winner.mode, meal_time)


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
            # A late joiner switched off keeps their offers for when they're back on (#76).
            offers=tuple(sorted(r.offers)) if r.status is not Status.OUT else (),
            suggestions=_picks_of(ctx, r.member_id),
            note=r.note,
            late=r.late,
            vote_counts=r.late and r.vote_counts,
        )
        for r in ctx.responses
    )


def late_joiners(ctx: CallContext) -> tuple[note.LateJoinerInfo, ...]:
    """Everyone who joined late (#76), by name, with the coordinator's switches."""
    late = sorted((r for r in ctx.responses if r.late), key=lambda r: ctx.name(r.member_id).casefold())
    return tuple(note.LateJoinerInfo(ctx.person(r.member_id), r.is_in, r.vote_counts) for r in late)


LATE_GUEST_NEEDS = "⚠ A guest of someone who joined late needs {needs}, and this isn't marked that way."
NOT_FOR_YOU = "⚠ This proposal doesn't fit your preferences."


WARNING_STATES = frozenset({EventState.VOTING, EventState.DECIDED, EventState.DONE})
"""While voting, and on the decision, which keeps the warning."""


def late_warnings(ctx: CallContext, proposal: Proposal) -> tuple[str, ...]:
    """What everyone may see about ``proposal`` and the late joiners taking part (#76): only
    whether it misses the dietary needs of guests they bring, which the roster shows anyway. A
    late joiner's own stances and dietary needs never show here, nor to the coordinator: with
    one late joiner, a warning on a shared message would say what that person vetoes (FR-29).
    Only they see that (``fits_late_joiner``)."""
    needs = ctx.late_guest_needs
    if not needs:
        return ()
    item = ctx.items.get(proposal.item_id) if proposal.item_id is not None else None
    # Something new is known only by its cuisine, so it can't show it meets a need.
    missing = needs - item.tag_ids if item is not None else needs
    return (LATE_GUEST_NEEDS.format(needs=_or(_tag_names(ctx, missing))),) if missing else ()


def fits_late_joiner(ctx: CallContext, proposal: Proposal, member_id: int) -> bool:
    """Whether ``proposal`` suits ``member_id``, a late joiner taking part, by their own vetoes
    and dietary needs (#76). For their eyes only: it tells them nothing new, and anyone else
    would learn their stances (FR-29). True for anyone else."""
    preferences, member = ctx.late_preferences.get(member_id), ctx.members.get(member_id)
    if preferences is None or member is None:
        return True
    needs = member.dietary_needs
    item = ctx.items.get(proposal.item_id) if proposal.item_id is not None else None
    if item is not None:
        vetoed = item.id in preferences.vetoed_items or bool(item.tag_ids & preferences.vetoed_tags)
        return not vetoed and needs <= item.tag_ids
    return proposal.free_cuisine_id not in preferences.vetoed_tags and not needs


def _or(names: tuple[str, ...]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


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
    voters = ctx.votes.get(proposal.id, [])
    counted = ctx.counted(proposal.id)
    return note.ProposalInfo(
        id=proposal.id,
        rank=proposal.rank,
        item=item_info(ctx, proposal),
        mode=proposal.mode,
        reasons=proposal.reasons,
        source=proposal.source,
        proposed_by=ctx.person(proposal.proposed_by) if proposal.proposed_by is not None else None,
        voters=tuple(ctx.person(m) for m in voters),
        uncounted=tuple(m for m in voters if m not in counted),
        warnings=late_warnings(ctx, proposal) if ctx.event.state in WARNING_STATES else (),
        tied=proposal.id in tied_ids(ctx),
    )


def tied_ids(ctx: CallContext) -> frozenset[int]:
    """The picks sharing the most votes, while voting and at least one vote counts (#87): what
    **Break the tie** chooses among. Coverage is checked as the decision will; the vetoes and
    needs the use case checks too were met when each pick was made."""
    if ctx.event.state is not EventState.VOTING:
        return frozenset()
    counted = counted_votes(ctx.votes, ctx.responses)

    def covered(proposal: Proposal) -> bool:
        item = ctx.items.get(proposal.item_id) if proposal.item_id is not None else None
        return ctx.coverage.covers(proposal.mode, item.cook_ids if item else frozenset())

    tallies = [ProposalTally(p.id, p.rank, covered(p), counted.get(p.id, frozenset())) for p in ctx.proposals]
    tied = tied_leaders(tallies)
    return frozenset(t.id for t in tied) if tied and tied[0].voters else frozenset()


def shuffle_picks(ctx: CallContext) -> list[Proposal]:
    """The picks a shuffle would redraw right now (``shuffle_candidates``): suggestions are the
    ones members made for this call."""
    suggested = [s.item_id for s in ctx.suggestions if s.item_id is not None]
    return shuffle_candidates(ctx.proposals, ctx.votes, suggested)


def shuffles_left(ctx: CallContext) -> int:
    """How many more times the coordinator can shuffle: 0 once they've used all of them, and when
    there's nothing a shuffle could redraw."""
    if ctx.event.state is not EventState.VOTING or not shuffle_picks(ctx):
        return 0
    return max(MAX_SHUFFLES - ctx.event.shuffles, 0)


def decision_info(ctx: CallContext) -> note.DecisionInfo | None:
    winner, meal_time = ctx.winner, ctx.event.meal_time
    if winner is None or meal_time is None:
        return None
    event = ctx.event
    return note.DecisionInfo(
        proposal=proposal_info(ctx, winner),
        meal_time=meal_time,
        head_count=ctx.head_count,
        participants=tuple(note.ParticipantInfo(ctx.person(r.member_id), r.guests, r.late) for r in ctx.participants),
        assignments=tuple(note.AssignmentInfo(a.role, ctx.person(a.member_id)) for a in ctx.assignments),
        dietary_notes=guest_dietary_notes(ctx),
        tie_note=tie_note(ctx),
        roles_due_at=event.roles_due_at if event.state is EventState.DECIDED else None,
        assign_roles=assign_roles_open(ctx),
    )


def tie_note(ctx: CallContext) -> str | None:
    """How a tie for the most votes was broken, in words (FR-16), or None if there wasn't one."""
    event = ctx.event
    if event.tie_how is None:
        return None
    by = ctx.name(event.tie_by) if event.tie_by is not None else None
    return tie_line(event.tie_how, by, coordinator=event.tie_by is not None and event.tie_by == event.coordinator_id)


def role_volunteers(ctx: CallContext) -> tuple[int, ...]:
    """Who offered for the decided pick's role, best first (FR-17)."""
    event = ctx.event
    if event.decided_mode is None or event.decided_item_id is None:
        return ()
    item = ctx.items.get(event.decided_item_id)
    return ctx.coverage.volunteers(event.decided_mode, item.cook_ids if item else frozenset())


def assign_roles_open(ctx: CallContext) -> bool:
    """Whether the decision message shows **Assign roles** (#87): while the coordinator's choice
    is pending, and, with nobody coordinating, while the role has no clear owner, for an admin."""
    event = ctx.event
    if event.state is not EventState.DECIDED or event.is_offer:
        return False
    if event.roles_due_at is not None:
        return True
    return event.coordinator_id is None and roles_unclear(role_volunteers(ctx))


def guest_dietary_notes(ctx: CallContext) -> tuple[str, ...]:
    """Dietary needs of the guests of everyone in, and of everyone in who has their own, except
    late joiners' own (FR-8, FR-20, #38, #76). Only the needs, never whose."""
    brought = {tag for r in ctx.participants if r.guests for tag in r.guest_needs}
    # A late joiner's own needs stay private: with one, subtracting the guests' would show them (#76).
    people = [ctx.members[r.member_id] for r in ctx.participants if r.member_id in ctx.members and not r.late]
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


def who_is_in(ctx: CallContext) -> tuple[note.PersonInfo, ...]:
    """Everyone who's in, guests and late joiners included, by name."""
    people = (ctx.person(r.member_id) for r in ctx.participants)
    return tuple(sorted(people, key=lambda p: p.name.casefold()))


def proposals_updated(ctx: CallContext) -> note.ProposalsUpdated:
    return note.ProposalsUpdated(
        event_info(ctx), proposals(ctx), ctx.head_count, late_joiners(ctx), who_is_in(ctx), shuffles_left(ctx)
    )


def proposals_ready(ctx: CallContext) -> note.ProposalsReady:
    return note.ProposalsReady(
        event_info(ctx), proposals(ctx), ctx.head_count, late_joiners(ctx), who_is_in(ctx), shuffles_left(ctx)
    )


def locked_by(ctx: CallContext) -> note.PersonInfo | None:
    return ctx.person(ctx.event.locked_by) if ctx.event.locked_by is not None else None


def decision_updated(ctx: CallContext) -> list[note.Notification]:
    decision = decision_info(ctx)
    return [] if decision is None else [note.DecisionUpdated(event_info(ctx), decision, locked_by(ctx))]


def assignment_made(
    ctx: CallContext,
    member_id: int,
    assignment_id: int,
    decision: note.DecisionInfo,
    assigned_by: note.PersonInfo | None = None,
) -> note.AssignmentMade:
    """The direct message to ``member_id`` about all their roles, prompted by ``assignment_id``.
    ``assigned_by`` is the coordinator who gave them a role they hadn't offered for (#87)."""
    order = list(AssignmentRole)
    roles = sorted({a.role for a in ctx.assignments if a.member_id == member_id}, key=order.index)
    via = answering_for(ctx, member_id)
    return note.AssignmentMade(
        event_info(ctx), assignment_id, ctx.person(member_id), tuple(roles), decision, via, assigned_by
    )


def roles_to_choose(ctx: CallContext, decision: note.DecisionInfo, due_at: datetime) -> note.RolesToChoose | None:
    """The coordinator's direct message when the decision left the role without a clear owner
    (#87), or None when nobody coordinates."""
    coordinator_id = ctx.event.coordinator_id
    if coordinator_id is None:
        return None
    return note.RolesToChoose(
        event_info(ctx), ctx.person(coordinator_id), decision, due_at, answering_for(ctx, coordinator_id)
    )


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


def before_the_meal(event: Event, now: datetime) -> bool:
    """Whether the meal is still ahead: who does what is news to send. After the meal time, a
    change corrects the record of who did it, and messages nobody (#154)."""
    return event.meal_time is None or now < event.meal_time


def assignment_ended(
    ctx: CallContext, member_id: int, roles: tuple[AssignmentRole, ...], by: Member, now: datetime
) -> list[note.Notification]:
    """The direct message to ``member_id``, whose ``roles`` went to someone else or whose way of
    getting it was switched (#154): none for whoever made the change, or someone inactive."""
    member = ctx.members.get(member_id)
    if member is None or not member.active or member_id == by.id:
        return []
    person, via = ctx.person(member_id), answering_for(ctx, member_id)
    return [note.AssignmentEnded(event_info(ctx), person, roles, ctx.person(by.id), now, via=via)]


def messages_for_state(ctx: CallContext) -> list[note.Notification]:
    """Updates for every message that shows the event: the call (or the offer), and the
    proposals or decision."""
    messages: list[note.Notification] = [message_updated(ctx)]
    if ctx.proposals:
        messages.append(proposals_updated(ctx))
    messages += decision_updated(ctx)
    return messages


async def release_coordinator(
    db: AsyncSession, event: Event, actor: Member | None, *, why: str, now: datetime, update: bool = True
) -> Event:
    """Nobody coordinates ``event`` any more, because its coordinator is out (#154,
    ``domain.events.drops_coordinating``), or was deactivated: ``why`` is ``out``,
    ``taken_off``, ``maybe`` (a maybe when answers closed), or ``deactivated``. The call's
    messages update, as when an admin sets it to nobody, so anyone who's in can claim it;
    ``update`` False leaves that to a caller that updates them anyway. Audited as
    ``event.coordinator_released``, with ``actor`` whoever changed the answer or deactivated
    them (None for the scheduler). Returns the event as saved."""
    member_id = event.coordinator_id
    if member_id is None:  # pragma: no cover - callers check there's a coordinator to release
        return event
    event = await EventRepository(db).save(replace(event, coordinator_id=None), now)
    if event.state is EventState.DECIDED:  # a deactivated coordinator's
        await AssignmentRepository(db).unassign(event.household_id, event.id, AssignmentRole.COORDINATOR)
    if update:
        ctx = await load(db, event)
        await publish(db, ctx, *messages_for_state(ctx), now=now)
    payload = {"member_id": member_id, "why": why}
    await audit(db, event.household_id, actor, "event.coordinator_released", f"event:{event.id}", payload, now)
    activity.action(db, "coordinator_released", event, actor, to_member_id=member_id, why=why)
    return event


async def stale_cutoff(db: AsyncSession, event: Event) -> datetime:
    """``domain.events.stale_after`` for ``event``, from its household's zone and schedule."""
    household = await HouseholdRepository(db).get(event.household_id)
    if household is None:  # pragma: no cover - events cascade with their household
        raise NotFound(NO_SUCH_CALL)
    schedule = slot_schedule(await household_settings(db, event.household_id), event.slot)
    return stale_after(event, usual_meal=schedule.meal_time, zone=ZoneInfo(household.timezone))


@dataclass(frozen=True)
class StepInputs:
    """What ``domain.events.due_step`` and ``owed_since`` need besides the event and now."""

    stale_after: datetime
    responses: list[Response] = field(default_factory=list)
    active_member_ids: list[int] = field(default_factory=list)
    coverage: Coverage | None = None
    guest_ids: list[int] = field(default_factory=list)


async def step_inputs(db: AsyncSession, event: Event) -> StepInputs:
    """A call that's collecting also gets its responses, the household's active members, and
    the coverage, for the early close (FR-56); any other call needs only its stale time."""
    cutoff = await stale_cutoff(db, event)
    if event.state is not EventState.COLLECTING:
        return StepInputs(cutoff)
    responses = await ResponseRepository(db).for_event(event.household_id, event.id)
    members = {m.id: m for m in await MemberRepository(db).list(event.household_id)}
    active = [m.id for m in members.values() if m.is_member]  # nobody waits for a guest (#38)
    invited = await InvitationRepository(db).for_event(event.household_id, event.id)
    guests = [g for g in invited if g in members and members[g].active]
    return StepInputs(cutoff, responses, active, coverage(responses, members), guests)


async def due(db: AsyncSession, event: Event, now: datetime) -> Step | None:
    """``domain.events.due_step`` for ``event``."""
    i = await step_inputs(db, event)
    return due_step(
        event,
        now,
        stale_after=i.stale_after,
        responses=i.responses,
        active_member_ids=i.active_member_ids,
        coverage=i.coverage,
        guest_ids=i.guest_ids,
    )


async def owed(db: AsyncSession, event: Event) -> datetime | None:
    """``domain.events.owed_since`` for ``event``: when it started owing the scheduler a step."""
    i = await step_inputs(db, event)
    return owed_since(
        event,
        stale_after=i.stale_after,
        responses=i.responses,
        active_member_ids=i.active_member_ids,
        coverage=i.coverage,
        guest_ids=i.guest_ids,
    )
