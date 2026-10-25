# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offers: one person announcing a meal they're making or getting, with seats to spare (spec 4,
5.2, 5.3, FR-4, FR-5).

An offer is an event of kind ``offer``. It's posted as soon as it's made, takes people until
its time, and then it's on (``decided``) until it's done. "Full" isn't a state: it's an open
offer with no seats left, which opens again when someone drops out. Seats count everyone but
whoever made the offer, guests included, so "3 servings spare" means three more people.

FR-5 decides what posting an offer does to that slot's call on that day (``offer_path``): an
offer for a call that's open joins it as a pick, and a cooking offer posted before the call
replaces the call.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, date, datetime, time, tzinfo
from enum import StrEnum

from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import (
    LATEST_EAT_TIME,
    MAX_GUESTS,
    Event,
    EventRuleViolation,
    EventState,
    InvalidResponse,
    Response,
    Status,
    check_ad_hoc_day,
)
from dinnerbell.domain.members import Member

MAX_CAPACITY = 20
"""Spare servings an offer can have: the most guests one response can bring."""
DEFAULT_CAPACITY = 3
FULL = "Sorry, it's full."


class OfferPath(StrEnum):
    """What posting an offer does, given that slot's call on that day (FR-5)."""

    NEW = "new"
    """No call to join or replace: the offer is its own event."""
    ATTACH = "attach"
    """The call is open (collecting or voting): the offer becomes a pick on it."""
    REPLACE = "replace"
    """A cooking offer, and the call is scheduled but not posted: the call is called off and
    the offer takes its place."""


def offer_path(call: Event | None, mode: Mode) -> OfferPath:
    """FR-5. An offer for a slot whose call is open joins that call as a pick; a cooking offer
    posted before the call (it's scheduled, not posted) replaces it. Once a call is decided or
    over, an offer stands on its own. With no call at all, a cooking offer still stands in for
    it: the scheduler doesn't open a call for that slot and day while it's live
    (``replaces_call``)."""
    if call is None:
        return OfferPath.NEW
    if call.state in {EventState.COLLECTING, EventState.VOTING}:
        return OfferPath.ATTACH
    if call.state is EventState.SCHEDULED and mode is Mode.COOK:
        return OfferPath.REPLACE
    return OfferPath.NEW


def replaces_call(offer: Event) -> bool:
    """Whether ``offer`` keeps its slot's daily call from opening that day: a cooking offer
    that's still on. One that was called off or ended frees the slot again."""
    return (
        offer.is_offer
        and offer.decided_mode is Mode.COOK
        and offer.state in {EventState.COLLECTING, EventState.DECIDED}
    )


def check_capacity(capacity: int) -> int:
    if not 1 <= capacity <= MAX_CAPACITY:
        raise InvalidResponse(f"Spare servings run from 1 to {MAX_CAPACITY}.")
    return capacity


def offer_time(day: date, at: time, *, zone: tzinfo, today: date, now: datetime) -> datetime:
    """When an offer for ``day`` at the wall-clock ``at`` closes, in UTC: its meal time. Today
    or up to ``AD_HOC_DAYS_AHEAD`` days ahead, and not in the past."""
    check_ad_hoc_day(day, today)
    if at > LATEST_EAT_TIME:
        raise InvalidResponse("Pick a time by 11:45 PM.")
    moment = datetime.combine(day, at, tzinfo=zone).astimezone(UTC)
    if moment <= now:
        raise InvalidResponse("Pick a time that's still ahead.")
    return moment


def seats_taken(offer: Event, responses: Iterable[Response]) -> int:
    """Seats taken: everyone in, with their guests, except whoever made the offer, whose own
    seat was never spare. Their guests do take seats."""
    taken = sum(r.head_count for r in responses)
    offerer_in = any(r.member_id == offer.created_by and r.is_in for r in responses)
    return taken - (1 if offerer_in else 0)


def seats_left(offer: Event, responses: Iterable[Response]) -> int:
    return max((offer.capacity or 0) - seats_taken(offer, list(responses)), 0)


def check_can_join(offer: Event, actor: Member) -> None:
    """Joining, leaving, and bringing guests: active members, while the offer is open."""
    if not offer.is_offer:  # pragma: no cover - callers look up offers
        raise EventRuleViolation("That isn't an offer.")
    if not actor.active:
        raise EventRuleViolation("Only active members can join an offer.")
    if offer.state is not EventState.COLLECTING:
        raise EventRuleViolation("This offer isn't taking people anymore.")


def joined(
    offer: Event,
    responses: list[Response],
    member_id: int,
    *,
    guests: int,
    guest_needs: frozenset[int],
    dietary_tag_ids: Iterable[int],
    now: datetime,
) -> Response:
    """``member_id`` in on ``offer`` with ``guests``, if the seats allow. Seats they hold already
    count as theirs, so changing from two guests to one never needs a free seat."""
    if not 0 <= guests <= MAX_GUESTS:
        raise InvalidResponse(f"Guests run from 0 to {MAX_GUESTS}.")
    if not guest_needs <= set(dietary_tag_ids):
        raise InvalidResponse("Guest needs must be dietary tags. Reload the page and try again.")
    current = next((r for r in responses if r.member_id == member_id), None)
    offerer = member_id == offer.created_by
    wanted = guests + (0 if offerer else 1)
    held = 0 if current is None else current.head_count - (1 if offerer and current.is_in else 0)
    if wanted > held and wanted - held > seats_left(offer, responses):
        raise EventRuleViolation(FULL)
    base = current or Response(member_id=member_id, status=Status.IN, responded_at=now)
    return replace(base, status=Status.IN, guests=guests, guest_needs=guest_needs if guests else frozenset())


def check_can_leave(offer: Event, actor: Member) -> None:
    check_can_join(offer, actor)
    if offer.created_by == actor.id:
        raise EventRuleViolation("It's your offer. Call it off if you can't make it.")
