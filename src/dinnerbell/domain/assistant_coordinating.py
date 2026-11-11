# SPDX-License-Identifier: AGPL-3.0-or-later
"""What **Confirm** does on the assistant's coordinating drafts (#252, spec 6.14, **Drafts**):
deciding a pick (a lock or a tie broken), shuffling the picks, changing the plan as **Adjust**
does, handing off coordinating, marking the meal done, and calling off a call (FR-56).

The services work out what changes with their own rules; these say it in the words the call page
and Slack's **Adjust** use. A person goes in a line as ``person`` of their member ID, never a name:
the card shows its member the person's name (``with_people``), and the model reads the label the
conversation gives them, such as "Member 2". So a stored title or line holds neither.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import time

from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import AssignmentRole
from dinnerbell.domain.meals import Slot

ROLE_DOES = {AssignmentRole.COOK: "cooks it", AssignmentRole.ORDER: "orders it", AssignmentRole.PICKUP: "picks it up"}
"""What a role's holder does, as the decision words it."""
ROLES_TOLD = "Whoever you assign is told, and so is whoever no longer has the role."
AFTER_THE_MEAL = "It's past the meal time, so it corrects the record and messages nobody."
FIRST_VOLUNTEER = "A role you didn't name goes to whoever offered first, as the decision does."


_OPEN, _CLOSE = "\ue000", "\ue001"
"""Private-use characters that mark a person in a stored line: no name or label is stored."""
_PERSON = re.compile(f"{_OPEN}(\\d{{1,12}}){_CLOSE}")


def person(member_id: int) -> str:
    """A person in a stored title or line, by member ID, for ``with_people`` to write out."""
    return f"{_OPEN}{member_id}{_CLOSE}"


def with_people(text: str, name: Callable[[int], str]) -> str:
    """``text`` with each ``person`` written as ``name`` gives them: their name on their owner's
    card, or their label for the model."""
    return _PERSON.sub(lambda match: name(int(match.group(1))), text)


def plain(text: str) -> str:
    """Text that came from a member or the catalog, such as a call-off reason or a place's name,
    with the marker characters taken out, so it can never pose as a person."""
    return text.replace(_OPEN, "").replace(_CLOSE, "")


def clock(at: time) -> str:
    """A wall time as the call shows it, such as "6:30 PM"."""
    return f"{at:%I:%M %p}".lstrip("0")


def pick_words(name: str, mode: Mode) -> str:
    return f"{name} ({mode.label.lower()})"


def role_lines(roles: Mapping[AssignmentRole, str]) -> list[str]:
    """Who does each role the draft names, such as "Member 2 picks it up.", in the roles' order."""
    return [f"{who} {ROLE_DOES[role]}." for role in ROLE_DOES if (who := roles.get(role)) is not None]


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


# Deciding a pick (FR-15, FR-16) --------------------------------------------------------------


def decide_title(name: str, mode: Mode, *, tie: bool) -> str:
    pick = pick_words(name, mode)
    return f"Break the tie with {pick}" if tie else f"Lock {pick}"


def decide_lines(
    name: str,
    mode: Mode,
    *,
    tie: bool,
    slot: Slot,
    when: str,
    way: Mode | None = None,
    roles: Mapping[AssignmentRole, str] | None = None,
) -> tuple[str, ...]:
    """What deciding a pick does: ``calls.lock``, or ``coordinator.break_tie`` when ``tie``, which
    the decision says the coordinator chose. With ``way`` or ``roles``, the decision takes them in
    the same transaction (``lifecycle.decide``'s ``named``), switching to ``way`` and naming who
    does each role, so a refusal decides nothing."""
    pick = pick_words(name, mode)
    if tie:
        lines = [
            f"It breaks the tie for the most votes: {when}'s {slot.value} call is decided on {pick} now, and "
            "voting ends.",
            "The decision says you chose.",
        ]
    else:
        lines = [f"It locks {pick}: {when}'s {slot.value} call is decided on it now, and voting ends."]
    if way is not None or roles:
        lines.append("It assigns the roles with it, as Assign roles does, so nobody is asked to choose:")
        if way is not None:
            lines.append(f"It's {way.label.lower()} instead.")
        lines += role_lines(roles or {})
        lines.append(FIRST_VOLUNTEER)
        lines.append("If the roles can't be assigned, nothing is locked.")
    return tuple(lines)


# Shuffling the picks (FR-61) -------------------------------------------------------------------


def shuffle_lines(*, anyone_voted: bool, left_after: int) -> tuple[str, ...]:
    """What ``coordinator.shuffle`` does: redraws the ranked picks, or only those with no votes
    once anyone has voted, under every filter ranking holds to."""
    redrawn = (
        "the ranked picks nobody has voted for" if anyone_voted else "every ranked pick, since nobody has voted yet"
    )
    shuffles = "1 shuffle" if left_after == 1 else f"{left_after} shuffles"
    return (
        f"It redraws {redrawn}, holding to every filter ranking does.",
        "Picks someone added, offered, or suggested stay.",
        f"You'll have {shuffles} left.",
    )


# Changing the plan, as Adjust does (spec 5.7) ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanChange:
    """What a plan draft changes, as the card says it: each part None when it stays."""

    meal_time: tuple[time, time] | None = None
    """From, to."""
    place: tuple[str, str] | None = None
    """From, to: catalog names."""
    way: tuple[Mode, Mode] | None = None
    """From, to."""
    roles: Mapping[AssignmentRole, str] | None = None
    """Who the member named for a role, by label."""


def plan_title(*, slot: Slot, when: str) -> str:
    return f"Change the plan for {when}'s {slot.value}"


def plan_lines(change: PlanChange, *, ahead: bool) -> tuple[str, ...]:
    """What ``coordinator.adjust`` does with the parts the member changed. ``ahead``: it's before
    the meal time, so whoever gains or loses a role is told."""
    lines: list[str] = []
    if change.meal_time is not None:
        was, now = change.meal_time
        lines.append(f"The meal time moves from {clock(was)} to {clock(now)}.")
    if change.place is not None:
        old, new = change.place
        lines.append(f"The place changes from {old} to {new}. People keep their roles if the new way has them.")
    if change.way is not None:
        was_way, now_way = change.way
        lines.append(f"It switches from {was_way.label.lower()} to {now_way.label.lower()}.")
    lines += role_lines(change.roles or {})
    if change.roles or change.way is not None or change.place is not None:
        lines.append(FIRST_VOLUNTEER)
    lines.append(ROLES_TOLD if ahead else AFTER_THE_MEAL)
    return tuple(lines)


# Handing off, marking done, and calling off ----------------------------------------------------


def hand_off_lines(to: str, *, slot: Slot, when: str, decided: bool) -> tuple[str, ...]:
    """What ``calls.hand_off`` does: coordinating puts the new coordinator in, which joins them
    late while voting, and they're told (#154). Once it's decided they must be in already."""
    lines = [f"{to} coordinates {when}'s {slot.value} call from then on, and you no longer do."]
    if not decided:
        lines.append("Coordinating puts them in, and they get a message if that changes their answer.")
    lines.append("They get a message saying so.")
    return tuple(lines)


def done_lines(name: str, *, slot: Slot, when: str) -> tuple[str, ...]:
    """What ``calls.mark_done`` does: the meal goes in History, as at two hours past the meal."""
    return (
        f"It marks {when}'s {slot.value} of {name} done, and it goes in History.",
        "Feedback on it opens, and contributions and badges count who did what.",
    )


def call_off_lines(*, slot: Slot, when: str, posted: bool, decided: bool, note: str | None) -> tuple[str, ...]:
    """What ``calls.cancel_call`` does (FR-56): the call ends as cancelled, nothing goes in the
    meal log, and those in it are told, with the member's reason."""
    lines = [f"It calls off {when}'s {slot.value} call. Nothing goes in the meal log."]
    if posted:
        told = "Everyone who was in or maybe, and the coordinator, is told in the call's thread"
        lines.append(f'{told}, with your reason: "{note}".' if note else f"{told}.")
    if decided:
        lines.append("Anyone assigned to cook, pick up, or order gets a direct message.")
    lines.append("Anyone can start it again later the same way as a new call.")
    return tuple(_capital(line) for line in lines)
