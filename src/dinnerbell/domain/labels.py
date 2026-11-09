# SPDX-License-Identifier: AGPL-3.0-or-later
"""The words a member reads for the same thing on the web and in Slack (#149).

The web templates and the Slack messages both draw their answer, role, and confirmation texts
from here, so a button reads the same wherever it is. Plain words only: each surface adds its
own markup and escaping.
"""

from __future__ import annotations

from datetime import date

from dinnerbell.domain.away import Away
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.catalog import Status as ItemStatus
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

# Choices that stand for no selection (#214, #218, #239) -----------------------------------------
# Every menu choice or placeholder that picks nothing reads in square brackets, on the web and in
# Slack, so it can't be mistaken for a real pick. A real choice of nothing names what it is
# ("[Nobody]"); a menu that needs a pick asks for one ("[Choose someone]"). Square brackets mean
# nothing to Slack's mrkdwn, unlike angle brackets. tests/test_none_choices.py checks every menu.

NOTHING_FROM_CATALOG = "[Nothing from the catalog]"
NO_SAVED_GUEST = "[No saved guest]"
NO_ANSWER = "[No answer]"
NOBODY = "[Nobody]"
NO_CUISINE = "[No cuisine]"
NO_DIETARY_NEEDS = "[No dietary needs]"
WHICHEVER_WAY = "[Whichever the place allows]"
"""The way when changing the place: none chosen, so one the place allows (#219)."""
CHOOSE_FROM_CATALOG = "[Choose from the catalog]"
CHOOSE_CUISINE = "[Choose a cuisine]"
CHOOSE_WAY = "[Choose a way]"
CHOOSE_SOMEONE = "[Choose someone]"
CHOOSE_WHO_ATE = "[Choose who ate]"
CHOOSE_MEAL = "[Choose a meal]"
FIRST_WHO_OFFERED = "[First who offered]"
"""An optional role left empty: its first volunteer does it (FR-17, #178)."""
ITEM_STATUS_LABELS = {ItemStatus.ACTIVE: "Active", ItemStatus.INACTIVE: "Inactive", ItemStatus.ALL: "[All]"}
"""The catalog's **Show** filter, where **[All]** filters nothing."""


def keep_place(name: str) -> str:
    """The choice that keeps the place as it is, when changing it (#219)."""
    return f"[Keep {name}]"


EITHER_WAY = "[Either way]"
"""Clearing a takeout pick's way while voting, back to not chosen (#212)."""
EITHER_WAY_VALUE = "either"
"""The form value that clears a pick's way on the event page (#212), beside ``pickup`` and
``delivery``."""


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
DETAILS_HINT = "Times, a suggestion, or a note. To bring someone, invite them as a guest."
JOIN_LATE = "I'm in (late)"
COUNT_ME_IN = "Count me in"
CANT_MAKE_IT = "Can't make it"
CALL_OFF = "Call it off"
MARK_DONE = "Mark done"
ADJUST = "Adjust"
"""The coordinator's way to change the meal time, the place, and who does what (5.7, #157, #219)."""
LOCK = "Lock this"
BREAK_TIE = "Break the tie"
SHUFFLE = "Shuffle proposals"
VIEW_ON_WEB = "View on web"
"""Every link to an event's page, as a button or a line of text."""

LATE_VOTE_NOTE = {
    False: "Your vote shows, but counts only if the coordinator says so.",
    True: "You answered on time, so your vote counts.",
}
"""How joining late counts your vote (#76), by ``domain.events.late_vote_counts``."""

CLOSE_LABELS = {Window.RESPOND: "Start the vote now", Window.VOTE: "Decide now"}
"""Close now, named for what it does to the open window (FR-56)."""
CLOSE_SUBMIT = {Window.RESPOND: "Start the vote", Window.VOTE: "Decide now"}
CLOSE_EFFECT = {
    Window.RESPOND: (
        "Responses close for everyone now, and the vote on the proposals starts. "
        "Voting still closes at the usual time. If fewer than two people are in, everyone's on their own."
    ),
    Window.VOTE: "Voting closes for everyone now, and the votes so far decide the plan.",
}

MOVE_DECISION_TIME = "Move the decision time"
"""The coordinator's move of the decision time while voting (#198)."""
DECISION_TIME_SHIFTS = {"plus15": 15, "plus30": 30, "minus15": -15}
"""How far the coordinator moves the decision time, in minutes, by the web form's value: the
admin's moves of the vote window, without closing it now, which is **Decide now**."""
SHIFT_LABELS = {"plus15": "+15 min", "plus30": "+30 min", "minus15": "\u221215 min"}
SHIFT_WORDS = {"plus15": "15 minutes later", "plus30": "30 minutes later", "minus15": "15 minutes earlier"}
EARLIER_TITLE = "Move voting 15 minutes earlier?"
EARLIER_CLOSES = "If the new time has already passed, voting closes now, and the votes so far decide the plan."
"""What moving the decision time earlier can do, on the web's confirmation and Slack's modal."""


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


# Away (FR-63, #196) -------------------------------------------------------------------------------

AWAY_UNTIL = "Away until…"
"""Beside In, Maybe, and Out: pick the day you're back, and calls until then answer you Out."""
AWAY_BACK_DAY = "Back on"
"""The date field **Away until…** opens."""
AWAY_HINT = "Calls until then answer you Out. You can still say In to any of them."
AWAY_JOINS = (
    "If these days touch away days you already set, they're joined into one stretch; otherwise they replace them."
)
"""What **Away until…** does to the window a member has already (``away.away_from_call``)."""
AWAY_ENDED = "Welcome back! Calls ask you again."
AWAY_CLEARED = "Away days cleared."
IM_BACK = "I'm back"
"""Ends a window under way now."""
CLEAR_AWAY = "Clear"
"""Ends a window that hasn't started."""
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def short_day(day: date, *, weekday: bool = True) -> str:
    """Wed, Nov 12, or Nov 12. English names, whatever the server's locale."""
    month_day = f"{_MONTHS[day.month - 1]} {day.day}"
    return f"{_DAYS[day.weekday()]}, {month_day}" if weekday else month_day


def away_status(away: Away, today: date) -> str:
    """The one status line for a window, wherever it shows: "Away until Wed, Nov 12", or, before
    it starts, "Away from Mon, Nov 10 until Wed, Nov 12". "Until" names the day they're back."""
    if away.first > today:
        return f"Away from {short_day(away.first)} until {short_day(away.back)}"
    return f"Away until {short_day(away.back)}"


def away_end(away: Away, today: date) -> str:
    """The button that ends a window now: **I'm back** once it's started, **Clear** before."""
    return IM_BACK if away.first <= today else CLEAR_AWAY


def away_marker(back: date, *, capital: bool = False) -> str:
    """Beside an away member's Out on a roster: "away until Nov 12", or "Away until Nov 12"
    where it starts a label, such as the web's chip."""
    return f"{'Away' if capital else 'away'} until {short_day(back, weekday=False)}"
