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

from dinnerbell.domain.events import RoleOffer, Status
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
LINK_PREFIX = "link_"
"""URL buttons. Slack still sends their clicks, which the app acknowledges and ignores."""
LINK_PATTERN = re.compile(r"^link_[a-z_]{1,40}$")

DETAILS_VIEW = "details_modal"
PROPOSAL_VIEW = "proposal_modal"

MAX_ID = 2**63 - 1
_DIGITS = re.compile(r"^[0-9]{1,19}$")
_PAIR = re.compile(r"^([0-9]{1,19}):([0-9]{1,19})$")
_CHANNEL = re.compile(r"^[CDG][A-Z0-9]{2,31}$")
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
    if not isinstance(value, str) or not _DIGITS.match(value):
        raise BadValue("not an event id")
    return _row_id(value)


def proposal_value(event_id: int, proposal_id: int) -> str:
    return f"{event_id}:{proposal_id}"


def parse_proposal(value: object) -> tuple[int, int]:
    match = _PAIR.match(value) if isinstance(value, str) else None
    if match is None:
        raise BadValue("not a proposal value")
    return _row_id(match.group(1)), _row_id(match.group(2))


def parse_slot(value: object) -> Slot:
    if value not in {s.value for s in Slot}:
        raise BadValue("not a slot")
    return Slot(str(value))


def parse_option_id(value: object) -> int:
    """A catalog item or tag ID from a select option's value."""
    return parse_event(value)


def is_channel_id(value: object) -> bool:
    return isinstance(value, str) and bool(_CHANNEL.match(value))


@dataclass(frozen=True, slots=True)
class ViewOrigin:
    """What a modal is about and where it was opened: the event, and the channel (or DM) to
    answer in when something goes wrong after the modal closes. Kept in private_metadata."""

    event_id: int
    channel_id: str | None = None
    home: bool = False
    """Opened from the Home tab: answer there, not in a direct message (spec 10)."""

    def encode(self) -> str:
        data = {"event": self.event_id, "channel": self.channel_id, "home": self.home}
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def decode(cls, text: object) -> ViewOrigin:
        if not isinstance(text, str) or len(text) > MAX_METADATA:
            raise BadValue("bad metadata")
        try:
            data = json.loads(text)
        except ValueError:
            raise BadValue("bad metadata") from None
        if not isinstance(data, dict) or set(data) != {"event", "channel", "home"}:
            raise BadValue("bad metadata")
        event, channel, home = data["event"], data["channel"], data["home"]
        if not isinstance(home, bool):
            raise BadValue("bad metadata")
        if not isinstance(event, int) or isinstance(event, bool) or not 1 <= event <= MAX_ID:
            raise BadValue("bad metadata")
        if channel is not None and not is_channel_id(channel):
            raise BadValue("bad metadata")
        return cls(event, channel, home)
