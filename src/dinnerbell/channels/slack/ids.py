# SPDX-License-Identifier: AGPL-3.0-or-later
"""Action IDs, callback IDs, and button values, and the strict parsers that read them back.

Slack sends ``action_id``, ``value``, and ``private_metadata`` back to the app. Dinnerbell
treats them as untrusted: a value must be exactly the shape this module writes, and the IDs
in it are only candidates. Services still scope every ID to the actor's household.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

from dinnerbell.domain.events import RoleOffer, Status, Window
from dinnerbell.domain.meals import Slot

RESPOND = {"respond_in": Status.IN, "respond_maybe": Status.MAYBE, "respond_out": Status.OUT}
ACTION_FOR_STATUS = {status: action for action, status in RESPOND.items()}
OFFER = {"offer_cook": RoleOffer.COOK, "offer_pickup": RoleOffer.PICKUP, "offer_order": RoleOffer.ORDER}
ACTION_FOR_OFFER = {role: action for action, role in OFFER.items()}
COORDINATE = "coordinate"
DETAILS = "details"
VOTE = "vote"
LOCK = "lock"
ADD_PROPOSAL = "add_proposal"
MARK_DONE = "mark_done"
START_CALL = {f"start_call_{slot.value}": slot for slot in Slot}
ACTION_FOR_START = {slot: action for action, slot in START_CALL.items()}
CALL_MENU = "call_menu"
"""The ⋯ menu on the call and decision messages. Its options' values are ``menu_value``s."""
CANCEL_CALL = "cancel_call"
"""A Call it off button, on the Home tab or on a decided call's messages. Its value is
``menu_value(MenuChoice.CANCEL, ...)``."""
CLOSE_WINDOW = "close_window"
"""Close now on the Home tab. Its value is a ``window_value``: the window the button closes."""
CARD_RESPOND = {"card_in": Status.IN, "card_maybe": Status.MAYBE, "card_out": Status.OUT}
"""In, Maybe, and Out on a message only one person sees: their Your answer card or their
reminder. Every card button's value is a ``card_value``, which names the run it was drawn for."""
CARD_ACTION_FOR_STATUS = {status: action for action, status in CARD_RESPOND.items()}
CARD_OFFER = {
    "card_offer_cook": RoleOffer.COOK,
    "card_offer_pickup": RoleOffer.PICKUP,
    "card_offer_order": RoleOffer.ORDER,
}
CARD_WITHDRAW = {
    "card_withdraw_cook": RoleOffer.COOK,
    "card_withdraw_pickup": RoleOffer.PICKUP,
    "card_withdraw_order": RoleOffer.ORDER,
}
"""A card shows the person's own offers, so its role buttons say which way they go: offer one
not offered yet, or withdraw one that is. A double tap can't undo itself, as a toggle would."""
CARD_ACTION_FOR_OFFER = {role: action for action, role in CARD_OFFER.items()}
CARD_ACTION_FOR_WITHDRAW = {role: action for action, role in CARD_WITHDRAW.items()}
CARD_COORDINATE = "card_coordinate"
CARD_DETAILS = "card_details"
CARD_ACTIONS = frozenset({*CARD_RESPOND, *CARD_OFFER, *CARD_WITHDRAW, CARD_COORDINATE, CARD_DETAILS})
ANSWER = "answer"
"""**Answer** on the call message while responses are open: posts the tapper's Your answer card.
Its value is a ``card_value``, naming the run the message showed."""
CALL_COORDINATE = "call_coordinate"
"""**I'll coordinate** on the call message while voting with nobody coordinating. Its value is a
``card_value``, so a message from before a restart can't claim the newer run (issue #54).
``COORDINATE``, without a run, stays for call messages drawn before it."""
JOIN_LATE = "join_late"
"""**I'm in (late)** (#76): on the call message while voting, on a Your answer card, and on Home.
Its value is a ``card_value``, naming the run it was drawn for."""
JOIN_LATE_VOTE = "join_late_vote"
"""**Join late and vote** (#76), on the private reply to a vote from someone who isn't in. Its
value is a ``proposal_value``."""
LATE_MENU = "late_menu"
"""The ⋯ menu beside a late joiner on the proposals message (#76): the coordinator's switches.
Each option's value is a ``late_value``."""
SHUFFLE = "shuffle"
"""**Shuffle picks** in the proposals message footer (#87): the coordinator's. Its value is a
``card_value``, naming the run the message showed."""
BREAK_TIE = "break_tie"
"""**Break the tie** beside a pick tied for the most votes (#87). Its value is a ``proposal_value``."""
ASSIGN_ROLES = "assign_roles"
"""**Assign roles** on the decision message and the coordinator's direct message (#87): opens the
modal. Its value is a ``card_value``."""
POST_NOW = "post_now"
"""**Post it now** on a scheduled call (issue #59): on the reply to starting a later day's call,
and on the Home tab. Its value is a ``card_value``."""
FEEDBACK = "feedback"
"""**How was it?** on a done call's decision message, the feedback reminder, and Home: opens the
feedback modal. Its value is a ``card_value``, naming the run the meal was decided in."""
JOIN_OFFER = "join_offer"
"""**Count me in** on an offer message (FR-4): join, or tap again to drop out. Its value is a
``card_value``, naming the offer and its run."""
ADD_GUEST = "add_guest"
"""**+1 guest** on an offer message: one more guest, counting the tapper in. A ``card_value``."""
LEAVE_OFFER = "leave_offer"
"""**Can't make it** on a full offer message, or on Home: drop out with any guests. A ``card_value``."""
OFFER_ACTIONS = frozenset({JOIN_OFFER, ADD_GUEST, LEAVE_OFFER})
MAKE_OFFER = "make_offer"
"""**Make an offer** on the Home tab: opens the offer modal. Its value is ``MAKE_OFFER``."""
INVITE_GUEST = "invite_guest"
"""**Invite a guest** on a member's Your answer card (#38): opens the invite modal. A ``card_value``."""
GUEST_RESPOND = {"guest_in": Status.IN, "guest_out": Status.OUT}
"""In and Out for a guest without Slack, on the card of the member who invited them. Their value
is a ``guest_value``, naming the call, its run, and the guest."""
GUEST_OFFER = {
    "guest_offer_cook": RoleOffer.COOK,
    "guest_offer_pickup": RoleOffer.PICKUP,
    "guest_offer_order": RoleOffer.ORDER,
}
GUEST_WITHDRAW = {
    "guest_withdraw_cook": RoleOffer.COOK,
    "guest_withdraw_pickup": RoleOffer.PICKUP,
    "guest_withdraw_order": RoleOffer.ORDER,
}
GUEST_ACTION_FOR_STATUS = {status: action for action, status in GUEST_RESPOND.items()}
GUEST_ACTION_FOR_OFFER = {role: action for action, role in GUEST_OFFER.items()}
GUEST_ACTION_FOR_WITHDRAW = {role: action for action, role in GUEST_WITHDRAW.items()}
GUEST_ACTIONS = frozenset({*GUEST_RESPOND, *GUEST_OFFER, *GUEST_WITHDRAW})
JOIN_DINNERBELL = "join_dinnerbell"
"""**Join Dinnerbell**, shown to someone in the channel Dinnerbell doesn't know (#38, option A).
Its value is ``JOIN_DINNERBELL``."""
MAKE_MEMBER = "make_member"
"""**Make member** on the admin's message about a guest who joined. Its value is the guest's ID."""
REMOVE_GUEST = "remove_guest"
"""**Remove** on the admin's message about a guest who joined. Its value is the guest's ID."""
LINK_PREFIX = "link_"
"""URL buttons. Slack still sends their clicks, which the app acknowledges and ignores."""
LINK_PATTERN = re.compile(r"^link_[a-z_]{1,40}$")

DETAILS_VIEW = "details_modal"
PROPOSAL_VIEW = "proposal_modal"
CANCEL_VIEW = "cancel_modal"
CLOSE_VIEW = "close_modal"
FEEDBACK_VIEW = "feedback_modal"
OFFER_VIEW = "offer_modal"
INVITE_VIEW = "invite_modal"
LOG_VIEW = "log_modal"
ASSIGN_VIEW = "assign_modal"

MAX_ID = 2**63 - 1
_DIGITS = re.compile(r"^[0-9]{1,19}$")
_PAIR = re.compile(r"^([0-9]{1,19}):([0-9]{1,19})$")
_MENU = re.compile(r"(cancel|close):([1-9][0-9]{0,18}):([1-9][0-9]{0,18})")
_WINDOW = re.compile(r"(respond|vote):([1-9][0-9]{0,18}):([1-9][0-9]{0,18})")
_CARD = re.compile(r"([1-9][0-9]{0,18}):([1-9][0-9]{0,18})")
_GUEST = re.compile(r"([1-9][0-9]{0,18}):([1-9][0-9]{0,18}):([1-9][0-9]{0,18})")
_LATE = re.compile(r"(part|vote):(on|off):([1-9][0-9]{0,18}):([1-9][0-9]{0,18}):([1-9][0-9]{0,18})")
_CHANNEL = re.compile(r"^[CDG][A-Z0-9]{2,31}$")
_MESSAGE_TS = re.compile(r"[0-9]{1,12}\.[0-9]{1,8}")
_RESPONSE_URL = re.compile(r"https://hooks\.slack(-gov)?\.com/actions/[A-Za-z0-9]+/[0-9]+/[A-Za-z0-9]+")
MAX_RESPONSE_URL = 500
MAX_METADATA = 3000


class BadValue(ValueError):
    """A value that this app didn't write. Never shown to anyone in detail."""


def _row_id(text: str) -> int:
    number = int(text)
    if not 1 <= number <= MAX_ID:
        raise BadValue("id out of range")
    return number


def event_value(event_id: int) -> str:
    return str(event_id)


def parse_event(value: object) -> int:
    if not isinstance(value, str) or not _DIGITS.fullmatch(value):
        raise BadValue("not an event id")
    return _row_id(value)


def proposal_value(event_id: int, proposal_id: int) -> str:
    return f"{event_id}:{proposal_id}"


def parse_proposal(value: object) -> tuple[int, int]:
    match = _PAIR.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise BadValue("not a proposal value")
    return _row_id(match.group(1)), _row_id(match.group(2))


class MenuChoice(StrEnum):
    CANCEL = "cancel"
    CLOSE = "close"


def menu_value(choice: MenuChoice, event_id: int, run: int) -> str:
    """``cancel:<id>:<run>`` or ``close:<id>:<run>``. The run is the one the message shows, so
    a message left over from before a re-run or a restart can't act on the newer run."""
    return f"{choice.value}:{event_id}:{run}"


def parse_menu(value: object) -> tuple[MenuChoice, int, int]:
    """``cancel:<id>:<run>`` or ``close:<id>:<run>``, exactly as ``menu_value`` writes them:
    the choice, the event ID, and the run."""
    match = _MENU.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise BadValue("not a menu value")
    return MenuChoice(match.group(1)), _row_id(match.group(2)), _row_id(match.group(3))


def window_value(window: Window, event_id: int, run: int) -> str:
    return f"{window.value}:{event_id}:{run}"


def parse_window(value: object) -> tuple[Window, int, int]:
    """``respond:<id>:<run>`` or ``vote:<id>:<run>``, exactly as ``window_value`` writes them."""
    match = _WINDOW.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise BadValue("not a window value")
    return Window(match.group(1)), _row_id(match.group(2)), _row_id(match.group(3))


def card_value(event_id: int, run: int) -> str:
    """``<id>:<run>``: the call and the run a card button was drawn for."""
    return f"{event_id}:{run}"


def parse_card(value: object) -> tuple[int, int]:
    """``<id>:<run>``, exactly as ``card_value`` writes it: the event ID and the run."""
    match = _CARD.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise BadValue("not a card value")
    return _row_id(match.group(1)), _row_id(match.group(2))


class LateSwitch(StrEnum):
    """A late joiner's switches (#76): taking part, and whether their vote counts."""

    TAKING_PART = "part"
    VOTE_COUNTS = "vote"


def late_value(switch: LateSwitch, on: bool, event_id: int, run: int, member_id: int) -> str:
    """``<switch>:<on|off>:<id>:<run>:<member>``: turn ``switch`` on or off for a late joiner, on
    the run the proposals message showed."""
    return f"{switch.value}:{'on' if on else 'off'}:{event_id}:{run}:{member_id}"


def parse_late(value: object) -> tuple[LateSwitch, bool, int, int, int]:
    """``<switch>:<on|off>:<id>:<run>:<member>``, exactly as ``late_value`` writes it."""
    match = _LATE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise BadValue("not a late joiner value")
    switch, on = LateSwitch(match.group(1)), match.group(2) == "on"
    return switch, on, _row_id(match.group(3)), _row_id(match.group(4)), _row_id(match.group(5))


def guest_value(event_id: int, run: int, guest_id: int) -> str:
    """``<id>:<run>:<guest>``: a guest button on a member's card, for that call's run."""
    return f"{event_id}:{run}:{guest_id}"


def parse_guest(value: object) -> tuple[int, int, int]:
    """``<id>:<run>:<guest>``, exactly as ``guest_value`` writes it."""
    match = _GUEST.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise BadValue("not a guest value")
    return _row_id(match.group(1)), _row_id(match.group(2)), _row_id(match.group(3))


def message_ts(value: object) -> str | None:
    """A message timestamp from a payload's container, such as ``1712345678.000200``, or None."""
    return value if isinstance(value, str) and _MESSAGE_TS.fullmatch(value) else None


def response_url(value: object) -> str | None:
    """An interaction's ``response_url``, or None unless the whole string is the shape Slack
    sends: ``https://hooks.slack.com/actions/<team>/<number>/<token>`` (or ``hooks.slack-gov.com``).
    Matching the whole string, rather than parsing it, means the string checked is the string
    sent. It's a bearer capability for the message tapped: never log it, and never send it
    anywhere but back to Slack."""
    if not isinstance(value, str) or len(value) > MAX_RESPONSE_URL:
        return None
    return value if _RESPONSE_URL.fullmatch(value) else None


def parse_slot(value: object) -> Slot:
    if value not in {s.value for s in Slot}:
        raise BadValue("not a slot")
    return Slot(str(value))


def parse_option_id(value: object) -> int:
    """A catalog item or tag ID from a select option's value."""
    return parse_event(value)


def is_channel_id(value: object) -> bool:
    return isinstance(value, str) and bool(_CHANNEL.fullmatch(value))


@dataclass(frozen=True, slots=True)
class ViewOrigin:
    """What a modal is about and where it was opened: the event, and the channel (or DM) to
    answer in when something goes wrong after the modal closes. Kept in private_metadata."""

    event_id: int
    channel_id: str | None = None
    home: bool = False
    """Opened from the Home tab: answer there, not in a direct message (spec 10)."""
    window: Window | None = None
    """The window a Close now modal confirms closing. Only that modal sets it."""
    run: int | None = None
    """The run of the call the modal was opened for. Call it off, Close now, and feedback set it,
    and so does Details opened from a card, so a modal left open across a re-run or a restart
    can't act on the newer run."""

    def encode(self) -> str:
        data: dict[str, object] = {"event": self.event_id, "channel": self.channel_id, "home": self.home}
        if self.window is not None:
            data["window"] = self.window.value
        if self.run is not None:
            data["run"] = self.run
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def decode(cls, text: object) -> ViewOrigin:
        if not isinstance(text, str) or len(text) > MAX_METADATA:
            raise BadValue("bad metadata")
        try:
            data = json.loads(text)
        except ValueError:
            raise BadValue("bad metadata") from None
        if not isinstance(data, dict) or set(data) - {"window", "run"} != {"event", "channel", "home"}:
            raise BadValue("bad metadata")
        event, channel, home = data["event"], data["channel"], data["home"]
        window, run = data.get("window"), data.get("run")
        if "window" in data and not (isinstance(window, str) and window in {w.value for w in Window}):
            raise BadValue("bad metadata")
        if "run" in data and not (isinstance(run, int) and not isinstance(run, bool) and 1 <= run <= MAX_ID):
            raise BadValue("bad metadata")
        if not isinstance(home, bool):
            raise BadValue("bad metadata")
        if not isinstance(event, int) or isinstance(event, bool) or not 1 <= event <= MAX_ID:
            raise BadValue("bad metadata")
        if channel is not None and not is_channel_id(channel):
            raise BadValue("bad metadata")
        return cls(event, channel, home, Window(window) if window is not None else None, run)


@dataclass(frozen=True, slots=True)
class FormOrigin:
    """Where a modal that isn't about an event was opened: the offer and log modals. The
    channel (or DM) to answer in, or the Home tab. Kept in private_metadata."""

    channel_id: str | None = None
    home: bool = False

    def encode(self) -> str:
        return json.dumps({"form": True, "channel": self.channel_id, "home": self.home}, separators=(",", ":"))

    @classmethod
    def decode(cls, text: object) -> FormOrigin:
        if not isinstance(text, str) or len(text) > MAX_METADATA:
            raise BadValue("bad metadata")
        try:
            data = json.loads(text)
        except ValueError:
            raise BadValue("bad metadata") from None
        if not isinstance(data, dict) or set(data) != {"form", "channel", "home"} or data["form"] is not True:
            raise BadValue("bad metadata")
        channel, home = data["channel"], data["home"]
        if not isinstance(home, bool) or (channel is not None and not is_channel_id(channel)):
            raise BadValue("bad metadata")
        return cls(channel, home)
