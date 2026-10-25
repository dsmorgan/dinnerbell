# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dinner call data access: events, responses, suggestions, proposals, votes, assignments.

Every query is scoped to a household. Reads use ``populate_existing`` so a use case that took
the event's write lock (``EventRepository.lock``) never sees rows cached from before it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import (
    AssignmentRow,
    EventRow,
    FeedbackRow,
    InvitationRow,
    MealLogRow,
    ProposalRow,
    ResponseRow,
    SuggestionRow,
    VoteRow,
)
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import (
    OPEN_STATES,
    Assignment,
    AssignmentRole,
    EndReason,
    Event,
    EventKind,
    EventState,
    Proposal,
    ProposalSource,
    Response,
    RoleOffer,
    Status,
    Suggestion,
)
from dinnerbell.domain.meals import Slot

FRESH: dict[str, Any] = {"populate_existing": True}

EVENT_ROWS: tuple[
    type[VoteRow | ProposalRow | SuggestionRow | ResponseRow | AssignmentRow | InvitationRow | MealLogRow], ...
] = (
    VoteRow,
    ProposalRow,
    SuggestionRow,
    ResponseRow,
    AssignmentRow,
    InvitationRow,
    MealLogRow,
)
"""Every table whose rows belong to one event, by ``event_id``, children before parents (a vote
before its proposal). Discarding a call (``EventRepository.discard``) deletes their rows; the
outbox, which refers to events too, is ``OutboxRepository.discard_event``'s. A test fails when
a table refers to ``event`` and isn't one of these or the outbox."""
FEEDBACK = "feedback"
"""Feedback belongs to the meal a call logged, not to the event, so it isn't in ``EVENT_ROWS``.
Discarding deletes it with the meal, explicitly rather than by the foreign key's cascade, so the
count is right whatever the connection's ``PRAGMA foreign_keys``. Only the number is reported."""


def _event_feedback(household_id: int, event_id: int) -> Any:
    """Feedback on the meal ``event_id`` logged."""
    meals = select(MealLogRow.id).where(MealLogRow.household_id == household_id, MealLogRow.event_id == event_id)
    return (FeedbackRow.household_id == household_id) & FeedbackRow.meal_id.in_(meals)


def _event(row: EventRow) -> Event:
    return Event(
        id=row.id,
        household_id=row.household_id,
        kind=EventKind(row.kind),
        slot=Slot(row.slot),
        day=row.date,
        state=EventState(row.state),
        run=row.run,
        opens_at=row.opens_at,
        respond_deadline=row.respond_deadline,
        reminder_minutes=row.reminder_minutes,
        vote_minutes=row.vote_minutes,
        created_by=row.created_by,
        coordinator_id=row.coordinator_id,
        vote_deadline=row.vote_deadline,
        reminder_sent_at=row.reminder_sent_at,
        decided_item_id=row.decided_item_id,
        decided_mode=Mode(row.decided_mode) if row.decided_mode else None,
        meal_time=row.meal_time,
        decided_at=row.decided_at,
        done_at=row.done_at,
        end_reason=EndReason(row.end_reason) if row.end_reason else None,
        locked_by=row.locked_by,
        ended_by=row.ended_by,
        end_note=row.end_note,
        capacity=row.capacity,
    )


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock(self, household_id: int, event_id: int, now: datetime) -> bool:
        """Take the event's write lock: bump its version as the transaction's first write.

        SQLite allows one writer at a time, so once this UPDATE runs, no other use case can
        change the event (or anything else) until this transaction ends, and reads after it see
        the latest committed state. Returns False if the household has no such event.
        """
        result = await self._session.execute(
            update(EventRow)
            .where(EventRow.household_id == household_id, EventRow.id == event_id)
            .values(version=EventRow.version + 1, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def linked_counts(self, household_id: int, event_id: int) -> dict[str, int]:
        """How many rows of each ``EVENT_ROWS`` table belong to the event, by table name, and
        how many feedback rows are on its meal (only the number, never whose)."""
        found = await self._session.scalar(
            select(func.count()).select_from(FeedbackRow).where(_event_feedback(household_id, event_id))
        )
        counts: dict[str, int] = {FEEDBACK: int(found or 0)}
        for model in EVENT_ROWS:
            found = await self._session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.household_id == household_id, model.event_id == event_id)
            )
            counts[model.__tablename__] = int(found or 0)
        return counts

    async def discard(self, household_id: int, event_id: int) -> dict[str, int]:
        """Delete the event and its rows in every ``EVENT_ROWS`` table, including the meal it
        logged. Returns how many rows went from each table, by table name. The outbox is the
        caller's (``OutboxRepository.discard_event``), before this. For a test instance only."""
        gone = await self._session.execute(
            delete(FeedbackRow)
            .where(_event_feedback(household_id, event_id))
            .execution_options(synchronize_session=False)
        )
        counts: dict[str, int] = {FEEDBACK: int(getattr(gone, "rowcount", 0) or 0)}
        for model in EVENT_ROWS:
            result = await self._session.execute(
                delete(model)
                .where(model.household_id == household_id, model.event_id == event_id)
                .execution_options(synchronize_session=False)
            )
            counts[model.__tablename__] = int(getattr(result, "rowcount", 0) or 0)
        await self._session.execute(
            delete(EventRow)
            .where(EventRow.household_id == household_id, EventRow.id == event_id)
            .execution_options(synchronize_session=False)
        )
        return counts

    async def get(self, household_id: int, event_id: int) -> Event | None:
        row = await self._row(household_id, event_id)
        return None if row is None else _event(row)

    async def _row(self, household_id: int, event_id: int) -> EventRow | None:
        return await self._session.scalar(
            select(EventRow)
            .where(EventRow.household_id == household_id, EventRow.id == event_id)
            .execution_options(**FRESH)
        )

    async def call_for(self, household_id: int, slot: Slot, day: date) -> Event | None:
        row = await self._session.scalar(
            select(EventRow)
            .where(
                EventRow.household_id == household_id,
                EventRow.kind == EventKind.CALL.value,
                EventRow.slot == slot.value,
                EventRow.date == day,
            )
            .execution_options(**FRESH)
        )
        return None if row is None else _event(row)

    async def offers_for(self, household_id: int, slot: Slot, day: date) -> list[Event]:
        """The household's offers for ``slot`` on ``day``, oldest first, in any state."""
        rows = await self._session.scalars(
            select(EventRow)
            .where(
                EventRow.household_id == household_id,
                EventRow.kind == EventKind.OFFER.value,
                EventRow.slot == slot.value,
                EventRow.date == day,
            )
            .order_by(EventRow.id)
            .execution_options(**FRESH)
        )
        return [_event(row) for row in rows]

    async def add_offer(
        self,
        *,
        household_id: int,
        slot: Slot,
        day: date,
        created_by: int,
        item_id: int,
        mode: Mode,
        at: datetime,
        capacity: int,
        now: datetime,
    ) -> Event:
        """An open offer (FR-4): posted now, taking people until ``at``, its meal time."""
        row = EventRow(
            household_id=household_id,
            kind=EventKind.OFFER.value,
            slot=slot.value,
            date=day,
            state=EventState.COLLECTING.value,
            run=1,
            version=1,
            created_by=created_by,
            opens_at=now,
            respond_deadline=at,
            reminder_minutes=0,
            vote_minutes=0,
            reminder_sent_at=now,  # an offer sends no reminder
            decided_item_id=item_id,
            decided_mode=mode.value,
            meal_time=at,
            capacity=capacity,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return _event(row)

    async def open_ids(self, household_id: int) -> list[int]:
        """Events the scheduler may still owe a step: not done, abandoned, or cancelled."""
        rows = await self._session.scalars(
            select(EventRow.id)
            .where(EventRow.household_id == household_id, EventRow.state.in_([s.value for s in OPEN_STATES]))
            .order_by(EventRow.id)
        )
        return list(rows)

    async def for_today(self, household_id: int, today: date) -> list[Event]:
        """Today's events, plus any still open from other days."""
        rows = await self._session.scalars(
            select(EventRow)
            .where(
                EventRow.household_id == household_id,
                (EventRow.date == today) | EventRow.state.in_([s.value for s in OPEN_STATES]),
            )
            .order_by(EventRow.date, EventRow.opens_at, EventRow.id)
            .execution_options(**FRESH)
        )
        return [_event(row) for row in rows]

    async def add(
        self,
        *,
        household_id: int,
        slot: Slot,
        day: date,
        state: EventState,
        opens_at: datetime,
        respond_deadline: datetime,
        reminder_minutes: int,
        vote_minutes: int,
        created_by: int | None,
        now: datetime,
        ended_by: int | None = None,
        end_note: str | None = None,
    ) -> Event:
        row = EventRow(
            household_id=household_id,
            kind=EventKind.CALL.value,
            slot=slot.value,
            date=day,
            state=state.value,
            run=1,
            version=1,
            created_by=created_by,
            opens_at=opens_at,
            respond_deadline=respond_deadline,
            reminder_minutes=reminder_minutes,
            vote_minutes=vote_minutes,
            ended_by=ended_by,
            end_note=end_note,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return _event(row)

    async def save(self, event: Event, now: datetime) -> Event:
        """Write every field a use case may change."""
        row = await self._row(event.household_id, event.id)
        if row is None:
            raise LookupError(event.id)
        row.state = event.state.value
        row.run = event.run
        row.created_by = event.created_by
        row.coordinator_id = event.coordinator_id
        row.opens_at = event.opens_at
        row.respond_deadline = event.respond_deadline
        row.vote_deadline = event.vote_deadline
        row.reminder_minutes = event.reminder_minutes
        row.vote_minutes = event.vote_minutes
        row.reminder_sent_at = event.reminder_sent_at
        row.decided_item_id = event.decided_item_id
        row.decided_mode = event.decided_mode.value if event.decided_mode else None
        row.meal_time = event.meal_time
        row.decided_at = event.decided_at
        row.done_at = event.done_at
        row.end_reason = event.end_reason.value if event.end_reason else None
        row.locked_by = event.locked_by
        row.ended_by = event.ended_by
        row.end_note = event.end_note
        row.updated_at = now
        await self._session.flush()
        return _event(row)


def _response(row: ResponseRow) -> Response:
    offers = {
        RoleOffer.COOK: row.offers_cook,
        RoleOffer.PICKUP: row.offers_pickup,
        RoleOffer.ORDER: row.offers_order,
    }
    return Response(
        member_id=row.member_id,
        status=Status(row.status),
        guests=row.guests,
        guest_needs=frozenset(int(t) for t in row.guest_needs or []),
        eat_from=row.eat_from,
        eat_until=row.eat_until,
        offers=frozenset(role for role, offered in offers.items() if offered),
        note=row.note,
        maybe_at_deadline=row.maybe_at_deadline,
        responded_at=row.responded_at,
        updated_at=row.updated_at,
    )


class ResponseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_event(self, household_id: int, event_id: int) -> list[Response]:
        rows = await self._session.scalars(
            select(ResponseRow)
            .where(ResponseRow.household_id == household_id, ResponseRow.event_id == event_id)
            .order_by(ResponseRow.responded_at, ResponseRow.id)
            .execution_options(**FRESH)
        )
        return [_response(row) for row in rows]

    async def save(self, household_id: int, event_id: int, response: Response, now: datetime) -> None:
        row = await self._session.scalar(
            select(ResponseRow)
            .where(
                ResponseRow.household_id == household_id,
                ResponseRow.event_id == event_id,
                ResponseRow.member_id == response.member_id,
            )
            .execution_options(**FRESH)
        )
        if row is None:
            row = ResponseRow(
                household_id=household_id,
                event_id=event_id,
                member_id=response.member_id,
                responded_at=response.responded_at or now,
            )
            self._session.add(row)
        row.status = response.status.value
        row.guests = response.guests
        row.guest_needs = sorted(response.guest_needs)
        row.eat_from, row.eat_until = response.eat_from, response.eat_until
        row.offers_cook = RoleOffer.COOK in response.offers
        row.offers_pickup = RoleOffer.PICKUP in response.offers
        row.offers_order = RoleOffer.ORDER in response.offers
        row.note = response.note
        row.maybe_at_deadline = response.maybe_at_deadline
        row.updated_at = now
        await self._session.flush()

    async def restore_maybes(self, household_id: int, event_id: int, now: datetime) -> None:
        """For a re-run: maybes the deadline turned out are maybes again."""
        await self._session.execute(
            update(ResponseRow)
            .where(
                ResponseRow.household_id == household_id,
                ResponseRow.event_id == event_id,
                ResponseRow.maybe_at_deadline.is_(True),
            )
            .values(status=Status.MAYBE.value, maybe_at_deadline=False, updated_at=now)
            .execution_options(synchronize_session=False)
        )

    async def repoint_guest_need(self, household_id: int, source_id: int, target_id: int) -> None:
        """After a tag merge, guest needs that named ``source_id`` name ``target_id``."""
        for row in await self._session.scalars(
            select(ResponseRow).where(ResponseRow.household_id == household_id).execution_options(**FRESH)
        ):
            needs = {int(t) for t in row.guest_needs or []}
            if source_id in needs:
                row.guest_needs = sorted((needs - {source_id}) | {target_id})
        await self._session.flush()


class SuggestionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_event(self, household_id: int, event_id: int) -> list[Suggestion]:
        rows = await self._session.scalars(
            select(SuggestionRow)
            .where(SuggestionRow.household_id == household_id, SuggestionRow.event_id == event_id)
            .order_by(SuggestionRow.id)
            .execution_options(**FRESH)
        )
        return [Suggestion(row.member_id, row.item_id, row.free_text) for row in rows]

    async def replace(
        self,
        household_id: int,
        event_id: int,
        member_id: int,
        item_ids: Iterable[int],
        texts: Iterable[str],
        now: datetime,
    ) -> None:
        await self._session.execute(
            delete(SuggestionRow).where(
                SuggestionRow.household_id == household_id,
                SuggestionRow.event_id == event_id,
                SuggestionRow.member_id == member_id,
            )
        )
        common = {"household_id": household_id, "event_id": event_id, "member_id": member_id, "created_at": now}
        self._session.add_all([SuggestionRow(item_id=i, **common) for i in item_ids])
        self._session.add_all([SuggestionRow(free_text=t, **common) for t in texts])
        await self._session.flush()


def _proposal(row: ProposalRow) -> Proposal:
    return Proposal(
        id=row.id,
        event_id=row.event_id,
        mode=Mode(row.mode),
        rank=row.rank,
        source=ProposalSource(row.source),
        item_id=row.item_id,
        free_text=row.free_text,
        free_cuisine_id=row.free_cuisine_id,
        score=row.score,
        reasons=tuple(str(r) for r in row.reasons or []),
        proposed_by=row.proposed_by,
    )


class ProposalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_event(self, household_id: int, event_id: int) -> list[Proposal]:
        rows = await self._session.scalars(
            select(ProposalRow)
            .where(ProposalRow.household_id == household_id, ProposalRow.event_id == event_id)
            .order_by(ProposalRow.rank, ProposalRow.id)
            .execution_options(**FRESH)
        )
        return [_proposal(row) for row in rows]

    async def add(
        self,
        *,
        household_id: int,
        event_id: int,
        mode: Mode,
        rank: int,
        source: ProposalSource,
        reasons: tuple[str, ...],
        now: datetime,
        item_id: int | None = None,
        free_text: str | None = None,
        free_cuisine_id: int | None = None,
        score: float | None = None,
        proposed_by: int | None = None,
    ) -> Proposal:
        row = ProposalRow(
            household_id=household_id,
            event_id=event_id,
            item_id=item_id,
            free_text=free_text,
            free_cuisine_id=free_cuisine_id,
            mode=mode.value,
            rank=rank,
            score=score,
            reasons=list(reasons),
            source=source.value,
            proposed_by=proposed_by,
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return _proposal(row)

    async def next_rank(self, household_id: int, event_id: int) -> int:
        top = await self._session.scalar(
            select(func.max(ProposalRow.rank)).where(
                ProposalRow.household_id == household_id, ProposalRow.event_id == event_id
            )
        )
        return int(top or 0) + 1

    async def link_item(self, household_id: int, proposal_id: int, item_id: int) -> None:
        """A free-text proposal that won now names the catalog item made from it."""
        await self._session.execute(
            update(ProposalRow)
            .where(ProposalRow.household_id == household_id, ProposalRow.id == proposal_id)
            .values(item_id=item_id, free_text=None, free_cuisine_id=None)
            .execution_options(synchronize_session=False)
        )

    async def set_rank(self, household_id: int, proposal_id: int, rank: int) -> None:
        await self._session.execute(
            update(ProposalRow)
            .where(ProposalRow.household_id == household_id, ProposalRow.id == proposal_id)
            .values(rank=rank)
            .execution_options(synchronize_session=False)
        )

    async def delete(self, household_id: int, proposal_id: int) -> None:
        """Delete one proposal and its votes, explicitly, whatever the connection's foreign keys."""
        await self._session.execute(
            delete(VoteRow).where(VoteRow.household_id == household_id, VoteRow.proposal_id == proposal_id)
        )
        await self._session.execute(
            delete(ProposalRow).where(ProposalRow.household_id == household_id, ProposalRow.id == proposal_id)
        )

    async def clear(self, household_id: int, event_id: int) -> None:
        """Delete the event's proposals; their votes go with them."""
        await self._session.execute(
            delete(ProposalRow).where(ProposalRow.household_id == household_id, ProposalRow.event_id == event_id)
        )

    async def repoint_cuisine(self, household_id: int, source_id: int, target_id: int) -> None:
        await self._session.execute(
            update(ProposalRow)
            .where(ProposalRow.household_id == household_id, ProposalRow.free_cuisine_id == source_id)
            .values(free_cuisine_id=target_id)
            .execution_options(synchronize_session=False)
        )


class VoteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_event(self, household_id: int, event_id: int) -> dict[int, list[int]]:
        """Proposal ID to the members who voted for it, in voting order."""
        rows = await self._session.execute(
            select(VoteRow.proposal_id, VoteRow.member_id)
            .where(VoteRow.household_id == household_id, VoteRow.event_id == event_id)
            .order_by(VoteRow.id)
        )
        votes: dict[int, list[int]] = {}
        for proposal_id, member_id in rows:
            votes.setdefault(proposal_id, []).append(member_id)
        return votes

    async def add(self, household_id: int, event_id: int, proposal_id: int, member_id: int, now: datetime) -> bool:
        existing = await self._session.scalar(
            select(VoteRow.id).where(VoteRow.proposal_id == proposal_id, VoteRow.member_id == member_id)
        )
        if existing is not None:
            return False
        self._session.add(
            VoteRow(
                household_id=household_id,
                event_id=event_id,
                proposal_id=proposal_id,
                member_id=member_id,
                created_at=now,
            )
        )
        await self._session.flush()
        return True

    async def remove(self, household_id: int, proposal_id: int, member_id: int) -> bool:
        result = await self._session.execute(
            delete(VoteRow).where(
                VoteRow.household_id == household_id,
                VoteRow.proposal_id == proposal_id,
                VoteRow.member_id == member_id,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0) > 0


class AssignmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_event(self, household_id: int, event_id: int) -> list[Assignment]:
        rows = await self._session.scalars(
            select(AssignmentRow)
            .where(AssignmentRow.household_id == household_id, AssignmentRow.event_id == event_id)
            .order_by(AssignmentRow.id)
            .execution_options(**FRESH)
        )
        return [Assignment(row.id, row.member_id, AssignmentRole(row.role)) for row in rows]

    async def assign(
        self, household_id: int, event_id: int, role: AssignmentRole, member_id: int, now: datetime
    ) -> Assignment:
        """Give ``role`` to ``member_id``, replacing whoever had it. Each assignment gets a new
        ID, so its notification key is new too."""
        await self.unassign(household_id, event_id, role)
        row = AssignmentRow(
            household_id=household_id, event_id=event_id, member_id=member_id, role=role.value, created_at=now
        )
        self._session.add(row)
        await self._session.flush()
        return Assignment(row.id, row.member_id, role)

    async def unassign(self, household_id: int, event_id: int, role: AssignmentRole) -> None:
        await self._session.execute(
            delete(AssignmentRow).where(
                AssignmentRow.household_id == household_id,
                AssignmentRow.event_id == event_id,
                AssignmentRow.role == role.value,
            )
        )
        await self._session.flush()

    async def clear(self, household_id: int, event_id: int) -> None:
        await self._session.execute(
            delete(AssignmentRow).where(AssignmentRow.household_id == household_id, AssignmentRow.event_id == event_id)
        )


class InvitationRepository:
    """Guests invited to calls (#38): guest ID to the member who invited them."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_event(self, household_id: int, event_id: int) -> dict[int, int | None]:
        rows = await self._session.execute(
            select(InvitationRow.guest_id, InvitationRow.invited_by)
            .where(InvitationRow.household_id == household_id, InvitationRow.event_id == event_id)
            .order_by(InvitationRow.id)
        )
        found: dict[int, int | None] = {}
        for guest, inviter in rows:
            found[int(guest)] = inviter
        return found

    async def events_for(self, household_id: int, guest_id: int) -> list[int]:
        """The events ``guest_id`` is invited to, oldest first."""
        rows = await self._session.scalars(
            select(InvitationRow.event_id)
            .where(InvitationRow.household_id == household_id, InvitationRow.guest_id == guest_id)
            .order_by(InvitationRow.event_id)
        )
        return list(rows)

    async def add(self, household_id: int, event_id: int, guest_id: int, invited_by: int, now: datetime) -> bool:
        """Invite ``guest_id``; False when they're invited already."""
        existing = await self._session.scalar(
            select(InvitationRow.id).where(InvitationRow.event_id == event_id, InvitationRow.guest_id == guest_id)
        )
        if existing is not None:
            return False
        self._session.add(
            InvitationRow(
                household_id=household_id, event_id=event_id, guest_id=guest_id, invited_by=invited_by, created_at=now
            )
        )
        await self._session.flush()
        return True

    async def remove(self, household_id: int, event_id: int, guest_id: int) -> bool:
        result = await self._session.execute(
            delete(InvitationRow).where(
                InvitationRow.household_id == household_id,
                InvitationRow.event_id == event_id,
                InvitationRow.guest_id == guest_id,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0) > 0
