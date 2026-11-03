# SPDX-License-Identifier: AGPL-3.0-or-later
"""The words a member reads for the same thing on the web and in Slack (#149).

The web templates and the Slack messages both draw their answer, role, and confirmation texts
from here, so a button reads the same wherever it is. Plain words only: each surface adds its
own markup and escaping.
"""

from __future__ import annotations

from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import AssignmentRole, RoleOffer, Status, Window

ANSWER_ORDER = (Status.IN, Status.MAYBE, Status.OUT)
"""In, Maybe, Out: the one order answers are shown in, everywhere."""
GUEST_ANSWER_ORDER = (Status.IN, Status.OUT)
"""A guest is in or out (#38)."""
ANSWER_LABELS = {Status.IN: "In", Status.MAYBE: "Maybe", Status.OUT: "Out"}

OFFER_LABELS = {RoleOffer.COOK: "I can cook", RoleOffer.PICKUP: "I can pick up", RoleOffer.ORDER: "I'll order"}
"""Your own role offers, on your answer buttons."""
OFFER_WORDS = {RoleOffer.COOK: "cook", RoleOffer.PICKUP: "pick up", RoleOffer.ORDER: "order"}
OFFERED_LABELS = {role: f"Can {word}" for role, word in OFFER_WORDS.items()}
"""Someone else's role offers: on a roster, and on the buttons for a guest you answer for. They
say what a person can do, never what they're assigned to."""
ROLE_TITLES = {
    AssignmentRole.COOK: "Cooking",
    AssignmentRole.PICKUP: "Picking up",
    AssignmentRole.ORDER: "Ordering",
    AssignmentRole.COORDINATOR: "Coordinating",
}
"""Who does each role once it's decided."""
ROLE_WHO = {AssignmentRole.COOK: "Who cooks", AssignmentRole.ORDER: "Who orders", AssignmentRole.PICKUP: "Who picks up"}
"""The field that picks someone for each role, in **Assign roles** and **Adjust** (#178)."""
PICKUP_ROLE = AssignmentRole.PICKUP
PICKUP_MODE = Mode.PICKUP
"""For the templates, which can't name an enum member themselves."""
WAY_LABELS = {Mode.PICKUP: "Make it pickup", Mode.DELIVERY: "Make it delivery"}
"""Setting a takeout pick's way while voting (#178)."""


def roles_to_choose(mode: Mode, *, way_open: bool) -> str:
    """What the coordinator chooses at the decision (#178), to follow "choose": "who cooks", "who
    orders and who picks it up", or, with the way still open, "pickup or delivery, who orders,
    and who picks it up"."""
    if way_open:
        return "pickup or delivery, who orders, and who picks it up"
    return {Mode.COOK: "who cooks", Mode.PICKUP: "who orders and who picks it up", Mode.DELIVERY: "who orders"}[mode]


def roles_fallback(mode: Mode, *, way_open: bool) -> str:
    """What happens if the coordinator doesn't choose in time (#178), as a sentence's end:
    "the first person who offered does it", or, for the two roles of a pickup, "the first
    people who offered do them"."""
    if way_open or mode is Mode.PICKUP:
        return "the first people who offered do them"
    return "the first person who offered does it"


COORDINATE = "I'll coordinate"
DETAILS = "Details"
DETAILS_HINT = "Guests, times, a suggestion, or a note."
JOIN_LATE = "I'm in (late)"
COUNT_ME_IN = "Count me in"
CANT_MAKE_IT = "Can't make it"
CALL_OFF = "Call it off"
MARK_DONE = "Mark done"
ADJUST = "Adjust"
"""The coordinator's way to change the meal time and who does what (5.7, #157)."""
LOCK = "Lock this"
BREAK_TIE = "Break the tie"
SHUFFLE = "Shuffle proposals"
VIEW_ON_WEB = "View on web"
"""Every link to an event's page, as a button or a line of text."""

CLOSE_LABELS = {Window.RESPOND: "Start the vote now", Window.VOTE: "Decide now"}
"""Close now, named for what it does to the open window (FR-56)."""
CLOSE_SUBMIT = {Window.RESPOND: "Start the vote", Window.VOTE: "Decide now"}
CLOSE_EFFECT = {
    Window.RESPOND: (
        "Responses close for everyone now, and the vote on the proposals starts. "
        "If fewer than two people are in, everyone's on their own."
    ),
    Window.VOTE: "Voting closes for everyone now, and the votes so far decide the plan.",
}


def decides_now(item: str) -> str:
    """What locking a proposal, or choosing it to break a tie, does."""
    return f"{item} becomes the plan right away, and voting closes."


LOCK_TITLE = "Lock this in?"
LOCK_SUBMIT = "Lock it in"
BREAK_TIE_TITLE = "Break the tie?"
BREAK_TIE_SUBMIT = "Choose it"
SHUFFLE_TITLE = "Shuffle the proposals?"
SHUFFLE_EFFECT = "Proposals nobody has voted for are replaced. Proposals with votes, and ones added or suggested, stay."
SHUFFLE_SUBMIT = "Shuffle"
MARK_DONE_EFFECT = "This ends the meal and adds it to the history."


def mark_done_title(slot: str) -> str:
    return f"Mark {slot} done?"


def web_confirm(title: str, effect: str) -> str:
    """A confirmation as one line, for the web's browser dialog."""
    return f"{title} {effect}"
