# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guests (#38): someone who eats with the household when a member invites them, can pitch in,
and never influences what's chosen.

A guest is a member row with the role ``guest`` (``domain.members``). This module is the one
place that says what a guest may do, by action and by the call's state (``GUEST_RULES``). Every
use case a guest can reach asks ``check_guest`` first; members and admins pass through it
untouched. Anything a guest may not do raises ``EventRuleViolation`` with words to show them.

The rules, as David confirmed them on 2026-10-24:

1. A guest is in only by invitation, one call at a time (``invitation``), and only to a call.
2. Once in, a guest can answer In or Out, offer to cook, pick up, or order, and coordinate the
   logistics: close the window, mark it done, change the meal time or who does what, and hand
   coordinating to someone else. They can't vote, add a proposal, suggest, veto, lock a pick,
   or break a tie; with a guest coordinating, the tie goes to whoever invited them, then to an
   admin.
3. Nor can a guest start, schedule, post, call off, or re-run a call, make or join an offer, give
   feedback, invite anyone, or change anything but their own dietary needs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from dinnerbell.domain.events import Event, EventKind, EventRuleViolation, EventState, Status
from dinnerbell.domain.members import InvalidMemberInput, Member, normalize_display_name


class GuestAction(StrEnum):
    VIEW = "view"
    ANSWER = "answer"
    MAYBE = "maybe"
    OFFER_ROLE = "offer_role"
    DETAILS = "details"
    COORDINATE = "coordinate"
    HAND_OFF = "hand_off"
    CLOSE_WINDOW = "close_window"
    MANAGE_DECISION = "manage_decision"
    MARK_DONE = "mark_done"
    VOTE = "vote"
    PROPOSE = "propose"
    LOCK = "lock"
    CALL_OFF = "call_off"
    START = "start"
    POST_NOW = "post_now"
    RESTART = "restart"
    OFFER = "offer"
    JOIN_OFFER = "join_offer"
    FEEDBACK = "feedback"
    INVITE = "invite"


ANSWERING = frozenset({EventState.COLLECTING})
OPEN = frozenset({EventState.COLLECTING, EventState.VOTING, EventState.DECIDED})

GUEST_RULES: dict[GuestAction, frozenset[EventState]] = {
    GuestAction.VIEW: frozenset(EventState),
    GuestAction.ANSWER: ANSWERING,
    GuestAction.OFFER_ROLE: ANSWERING,
    GuestAction.COORDINATE: frozenset({EventState.COLLECTING, EventState.VOTING}),
    GuestAction.HAND_OFF: OPEN,
    GuestAction.CLOSE_WINDOW: frozenset({EventState.COLLECTING, EventState.VOTING}),
    GuestAction.MANAGE_DECISION: frozenset({EventState.DECIDED}),
    GuestAction.MARK_DONE: frozenset({EventState.DECIDED}),
}
"""What an invited guest may do, and in which states of the call. Every action not listed is
refused in every state: ``REFUSED`` says why. The use case still checks its own rules after
this, such as only the coordinator closing a window."""

REFUSED: dict[GuestAction, str] = {
    GuestAction.MAYBE: "As a guest, you're in or out.",
    GuestAction.DETAILS: "As a guest, ask whoever invited you to add details.",
    GuestAction.VOTE: "Guests don't vote, but thanks for joining!",
    GuestAction.PROPOSE: "Guests don't add picks. Ask whoever invited you.",
    GuestAction.LOCK: "Guests don't pick the plan. Whoever invited you, or an admin, can lock it.",
    GuestAction.CALL_OFF: "Only a household member can call it off.",
    GuestAction.START: "Only a household member can start a call.",
    GuestAction.POST_NOW: "Only a household member can post a call.",
    GuestAction.RESTART: "Only a household member can start a call again.",
    GuestAction.OFFER: "Only a household member can make an offer.",
    GuestAction.JOIN_OFFER: "Ask a household member to bring you along as their guest.",
    GuestAction.FEEDBACK: "Thanks! Feedback is for household members.",
    GuestAction.INVITE: "Only a household member can invite a guest.",
}
NOT_INVITED = "You haven't been invited to this one. Ask a household member to invite you."
NOT_NOW = "That isn't open to guests right now."
MAX_GUEST_DIETARY_NEEDS = 10


def check_guest(event: Event | None, actor: Member, action: GuestAction, *, invited: bool) -> None:
    """Raise ``EventRuleViolation`` unless ``actor`` may take ``action`` on ``event``. Members
    and admins always pass: their own rules are the use case's. ``invited`` is whether ``actor``
    is invited to ``event``. ``event`` is None for an action on no call yet, such as starting one."""
    if not actor.is_guest:
        return
    # An event the guest isn't invited to reads the same whatever they tried, so no refusal
    # can tell them it exists or what it's doing.
    if event is not None and (event.kind is not EventKind.CALL or not invited):
        raise EventRuleViolation(NOT_INVITED)
    allowed = GUEST_RULES.get(action)
    if allowed is None:
        raise EventRuleViolation(REFUSED.get(action, NOT_NOW))
    if event is None:
        raise EventRuleViolation(NOT_INVITED)
    if event.state not in allowed:
        raise EventRuleViolation(NOT_NOW)


def answer_action(status: Status) -> GuestAction:
    """Guests answer In or Out; Maybe is a member's answer."""
    return GuestAction.MAYBE if status is Status.MAYBE else GuestAction.ANSWER


def check_can_invite(event: Event, actor: Member) -> None:
    """Rule 5 of #38: any active member invites guests to a call while answers are open, one
    call at a time. A guest can't invite anyone."""
    if not actor.is_member:
        raise EventRuleViolation(REFUSED[GuestAction.INVITE])
    if event.kind is not EventKind.CALL:
        raise EventRuleViolation("Guests come to calls. On an offer, tap +1 guest instead.")
    if event.state is not EventState.COLLECTING:
        raise EventRuleViolation("Invite guests while answers are open.")


def can_answer_for(guest: Member, actor: Member, inviter_id: int | None) -> bool:
    """Who answers for an unregistered guest: whoever invited them, or an admin."""
    return (
        guest.is_guest
        and not guest.is_registered_guest
        and actor.is_member
        and (actor.id == inviter_id or actor.is_admin)
    )


def tie_breaker(coordinator: Member | None, inviter_id: int | None) -> int | None:
    """Whose vote breaks a tie at the deadline (FR-16): the coordinator, or, when the
    coordinator is a guest, whoever invited them; None leaves it to the admins."""
    if coordinator is None:
        return None
    if coordinator.is_guest:
        return inviter_id
    return coordinator.id


@dataclass(frozen=True, slots=True)
class GuestInput:
    """A new guest, as an inviting member or an admin types them."""

    display_name: str
    dietary_needs: frozenset[int] = frozenset()


def check_guest_input(data: GuestInput, *, dietary_tag_ids: Iterable[int]) -> GuestInput:
    """Raises InvalidMemberInput for a name that can't be stored or needs that aren't dietary tags."""
    name = normalize_display_name(data.display_name)
    check_dietary_needs(data.dietary_needs, dietary_tag_ids=dietary_tag_ids)
    return GuestInput(name, data.dietary_needs)


def check_dietary_needs(needs: frozenset[int], *, dietary_tag_ids: Iterable[int]) -> frozenset[int]:
    if len(needs) > MAX_GUEST_DIETARY_NEEDS:
        raise InvalidMemberInput(f"Pick up to {MAX_GUEST_DIETARY_NEEDS} dietary needs.")
    if not needs <= set(dietary_tag_ids):
        raise InvalidMemberInput("Dietary needs must be dietary tags. Reload the page and try again.")
    return needs
