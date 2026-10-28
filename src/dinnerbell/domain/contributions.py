# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contributions (spec 12, FR-38): what a member did to help a meal happen, worked out from a
done event.

``credits_for_done`` is the whole rule set, as a pure function of ``DoneFacts``, so the service
that records them (live, and again for the backfill) and the tests share one definition.

Who earns what:

- **Pitching in** (``cooked``, ``picked_up``, ``ordered``, ``coordinated``) goes to whoever the
  event assigned, a guest included: a guest who cooks cooked.
- **Everything else** goes to members only. A guest comes by invitation, never chooses what's
  eaten, and gives no feedback (#38), so being at the table or answering quickly isn't theirs
  to earn. The member who brought them earns ``hosted``.
- A late joiner (#76) who is taking part earned ``participated`` like anyone. They never earn
  ``responded_on_time``: they missed the answers.
- A meal logged by hand has no assignments or answers, so it records nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from dinnerbell.domain.events import AssignmentRole, Event, EventKind, Proposal, ProposalSource, Response
from dinnerbell.domain.meals import MealLog, newest_first

ON_TIME = timedelta(minutes=15)
"""How soon after a call opens an answer is on time (``responded_on_time``)."""


class Kind(StrEnum):
    COOKED = "cooked"
    PICKED_UP = "picked_up"
    ORDERED = "ordered"
    COORDINATED = "coordinated"
    PARTICIPATED = "participated"
    RESPONDED_ON_TIME = "responded_on_time"
    SUGGESTED = "suggested"
    ADDED_ITEM = "added_item"
    EXPLORED = "explored"
    """A suggestion that won and was new to the household: an item or a cuisine it had never
    eaten. The Explorer badge counts these (spec 12)."""
    HOSTED = "hosted"
    GAVE_FEEDBACK = "gave_feedback"


ROLE_KIND = {
    AssignmentRole.COOK: Kind.COOKED,
    AssignmentRole.PICKUP: Kind.PICKED_UP,
    AssignmentRole.ORDER: Kind.ORDERED,
    AssignmentRole.COORDINATOR: Kind.COORDINATED,
}
PITCHING_IN = frozenset(ROLE_KIND.values())


@dataclass(frozen=True, slots=True)
class Credit:
    """One contribution to record. ``item_id`` is set only for ``added_item``, so an item is
    credited to its creator once."""

    member_id: int
    kind: Kind
    item_id: int | None = None


@dataclass(frozen=True, slots=True)
class Newness:
    """What a meal was, against what the household had eaten before it."""

    item_new: bool
    cuisine_new: bool
    first_use: bool
    """The first meal of a done call that had this item."""

    @property
    def explored(self) -> bool:
        return self.item_new or self.cuisine_new


def newness_by_meal(meals: Iterable[MealLog]) -> dict[int, Newness]:
    """Each meal's ``Newness`` against the meals before it (by day, then slot, then ID), in one
    pass over the log in that order, so replaying history in any order gives each meal the same
    answer and a long log costs no more than its length."""
    items: set[int] = set()
    chosen: set[int] = set()
    cuisines: set[int] = set()
    found: dict[int, Newness] = {}
    for meal in sorted(meals, key=newest_first):
        found[meal.id] = Newness(
            item_new=meal.item_id not in items,
            cuisine_new=any(c.id not in cuisines for c in meal.cuisines),
            first_use=meal.item_id not in chosen,
        )
        items.add(meal.item_id)
        cuisines.update(c.id for c in meal.cuisines)
        if meal.event_id is not None:
            chosen.add(meal.item_id)
    return found


@dataclass(frozen=True, slots=True)
class DoneFacts:
    """A done event, as the rules need it."""

    event: Event
    assignments: tuple[tuple[int, AssignmentRole], ...]
    responses: tuple[Response, ...]
    guest_ids: frozenset[int]
    """Everyone with the guest role, whoever invited them."""
    invitations: dict[int, int | None] = field(default_factory=dict)
    """Each guest invited to the call, with the member who invited them."""
    winner: Proposal | None = None
    newness: Newness | None = None
    """None when the call logged no meal."""
    item_creator: int | None = None


def credits_for_done(facts: DoneFacts) -> list[Credit]:
    """Every contribution a done event earns, once per member and kind."""
    event = facts.event
    found: dict[tuple[int, Kind], Credit] = {}

    def add(member_id: int, kind: Kind, item_id: int | None = None) -> None:
        found.setdefault((member_id, kind), Credit(member_id, kind, item_id))

    for member_id, role in facts.assignments:
        add(member_id, ROLE_KIND[role])
    present = {r.member_id for r in facts.responses if r.is_in}
    # A guest a member invited who is in counts as brought, even when the member answered for
    # them from their card instead of adding a head count.
    hosts = {by for guest, by in facts.invitations.items() if by is not None and guest in present}
    for response in facts.responses:
        if response.member_id in facts.guest_ids:
            continue
        if response.is_in:
            add(response.member_id, Kind.PARTICIPATED)
            if response.guests or response.member_id in hosts:
                add(response.member_id, Kind.HOSTED)
        if _on_time(event, response):
            add(response.member_id, Kind.RESPONDED_ON_TIME)
    winner, fresh = facts.winner, facts.newness
    pitcher = winner.proposed_by if winner is not None and winner.source is not ProposalSource.SYSTEM else None
    if pitcher is not None and pitcher not in facts.guest_ids:
        add(pitcher, Kind.SUGGESTED)
        if fresh is not None and fresh.explored:
            add(pitcher, Kind.EXPLORED)
    creator, item_id = facts.item_creator, event.decided_item_id
    first = fresh is not None and fresh.first_use
    if first and creator is not None and creator not in facts.guest_ids and item_id is not None:
        add(creator, Kind.ADDED_ITEM, item_id)
    return list(found.values())


def _on_time(event: Event, response: Response) -> bool:
    """Answered a call within ``ON_TIME`` of its opening, with any answer. An offer has no
    opening, and a late joiner missed the answers."""
    if event.kind is not EventKind.CALL or response.late or response.responded_at is None:
        return False
    # A re-run moves the opening to now and keeps the old answers; they were not quick to this opening.
    return _within(event.opens_at, response.responded_at)


def _within(opened: datetime, answered: datetime) -> bool:
    return opened <= answered <= opened + ON_TIME
