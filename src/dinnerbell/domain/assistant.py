# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optional assistant's rules (ADR 0009, spec 6.14): who may use it, its household
settings, what a model call costs and the most it can cost, the month a cost counts toward,
pseudonyms, conversation keys, and how much history a request carries.

Money is in whole micro-dollars (millionths of a US dollar), so sums are exact. Every rounding
rounds up, so a reservation is never below what the call can cost.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from functools import reduce
from typing import Any

from dinnerbell.domain.members import Member

ENABLED_KEY = "assistant_enabled"
CAP_KEY = "assistant_monthly_cap_dollars"
HOURS_KEY = "assistant_conversation_hours"

DEFAULT_CAP_DOLLARS = 5
MIN_CAP_DOLLARS = 1
MAX_CAP_DOLLARS = 100
DEFAULT_CONVERSATION_HOURS = 24
MIN_CONVERSATION_HOURS = 0
MAX_CONVERSATION_HOURS = 168

MICROS_PER_DOLLAR = 1_000_000
TOKENS_PER_PRICE_UNIT = 1_000_000
"""Prices are per million tokens."""

OVERHEAD_TOKENS = 2_000
"""Tokens the API adds to a request beyond its JSON, such as the tool-use instructions it
inserts when a request has tools. The reservation adds them to the payload's byte count."""


class InvalidAssistantSettings(ValueError):
    """An Assistant setting the admin entered that can't be saved. The message is safe to show."""


# Settings ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssistantSettings:
    """Admin's **Assistant** settings, kept in the household settings JSON."""

    enabled: bool = False
    cap_dollars: int = DEFAULT_CAP_DOLLARS
    """The most the household spends in a calendar month, in whole US dollars."""
    conversation_hours: int = DEFAULT_CONVERSATION_HOURS
    """How long Dinnerbell keeps a conversation after its last message. At 0 it keeps none."""

    @property
    def cap_micros(self) -> int:
        return self.cap_dollars * MICROS_PER_DOLLAR


def _whole(value: object, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def assistant_settings(settings: Mapping[str, Any]) -> AssistantSettings:
    """The stored settings. Forgiving, like the other settings readers: a missing or malformed
    value reads as its default, and the switch reads as on only when it's exactly ``true``."""
    cap = _whole(settings.get(CAP_KEY), MIN_CAP_DOLLARS, MAX_CAP_DOLLARS)
    hours = _whole(settings.get(HOURS_KEY), MIN_CONVERSATION_HOURS, MAX_CONVERSATION_HOURS)
    return AssistantSettings(
        enabled=settings.get(ENABLED_KEY) is True,
        cap_dollars=DEFAULT_CAP_DOLLARS if cap is None else cap,
        conversation_hours=DEFAULT_CONVERSATION_HOURS if hours is None else hours,
    )


def check_assistant_settings(values: AssistantSettings) -> AssistantSettings:
    if not MIN_CAP_DOLLARS <= values.cap_dollars <= MAX_CAP_DOLLARS:
        raise InvalidAssistantSettings(f"The monthly cap is ${MIN_CAP_DOLLARS} to ${MAX_CAP_DOLLARS}.")
    if not MIN_CONVERSATION_HOURS <= values.conversation_hours <= MAX_CONVERSATION_HOURS:
        raise InvalidAssistantSettings(
            f"Conversation hours run {MIN_CONVERSATION_HOURS} to {MAX_CONVERSATION_HOURS} (a week)."
        )
    return values


def settings_to_json(values: AssistantSettings) -> dict[str, Any]:
    return {ENABLED_KEY: values.enabled, CAP_KEY: values.cap_dollars, HOURS_KEY: values.conversation_hours}


def available(*, configured: bool, switch_on: bool, member: Member) -> bool:
    """Whether the assistant answers ``member``: the operator set the key and turned on the flag
    (``configured``), the admin turned on Admin's **Assistant**, and ``member`` is an active
    member or admin, not a guest. Anyone else gets no reply (spec 6.14, **Who**)."""
    return configured and switch_on and member.is_member


# Prices and cost -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Prices:
    """Micro-dollars per million tokens."""

    input: int
    output: int
    cache_read: int
    cache_write: int
    """A 5-minute cache write, 1.25 times the input price."""

    def most(self, other: Prices) -> Prices:
        return Prices(
            input=max(self.input, other.input),
            output=max(self.output, other.output),
            cache_read=max(self.cache_read, other.cache_read),
            cache_write=max(self.cache_write, other.cache_write),
        )


def dollars(amount: str) -> int:
    """``"2.50"`` dollars as micro-dollars, exactly."""
    whole, _, cents = amount.partition(".")
    return int(whole) * MICROS_PER_DOLLAR + int((cents + "000000")[:6])


@dataclass(frozen=True, slots=True)
class PriceCard:
    """One model's prices. ``long`` applies instead of ``standard`` to a request whose prompt
    (input, cache read, and cache write tokens) is over ``long_over`` tokens."""

    standard: Prices
    long: Prices | None = None
    long_over: int = 0
    server_fallback: bool = False
    """The model takes Anthropic's server-side refusal fallback (``fallbacks: "default"``), so a
    request can run twice: on this model, then on the fallback model."""

    def prices_for(self, prompt_tokens: int) -> Prices:
        if self.long is not None and prompt_tokens > self.long_over:
            return self.long
        return self.standard

    @property
    def highest(self) -> Prices:
        return self.standard if self.long is None else self.standard.most(self.long)


DEFAULT_MODEL = "claude-sonnet-5-5"
PRICE_TABLE: Mapping[str, PriceCard] = {
    # Anthropic's first-party prices per million tokens, from the claude-api skill (cached
    # 2026-10-06). Cache writes are 5-minute writes, 1.25 times the input price.
    "claude-sonnet-5-5": PriceCard(
        Prices(dollars("2"), dollars("10"), dollars("0.20"), dollars("2.50")), server_fallback=True
    ),
    "claude-opus-5-5": PriceCard(
        Prices(dollars("4"), dollars("20"), dollars("0.20"), dollars("5")), server_fallback=True
    ),
    # Two rate cards by prompt length; cache reads are 0.1 times input, writes 1.25 times.
    "claude-haiku-5-5": PriceCard(
        Prices(dollars("0.10"), dollars("0.50"), dollars("0.01"), dollars("0.125")),
        long=Prices(dollars("0.50"), dollars("2.50"), dollars("0.05"), dollars("0.625")),
        long_over=100_000,
    ),
}


def _highest_prices(cards: Iterable[PriceCard]) -> Prices:
    first, *rest = cards
    return reduce(lambda found, card: found.most(card.highest), rest, first.highest)


MOST_EXPENSIVE = _highest_prices(PRICE_TABLE.values())
"""Each price at the table's highest, for a model the table doesn't know, such as the model a
fallback ran on."""


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What one model attempt used, as the API reports it."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


def _cost(tokens: int, price_per_million: int) -> int:
    return -(-tokens * price_per_million // TOKENS_PER_PRICE_UNIT)  # rounds up


def cost_micros(usage: TokenUsage) -> int:
    """What ``usage`` cost, in micro-dollars, at its model's prices. A model the table doesn't
    know, such as a fallback's, costs the table's highest prices."""
    card = PRICE_TABLE.get(usage.model)
    prices = MOST_EXPENSIVE if card is None else card.prices_for(usage.prompt_tokens)
    return (
        _cost(usage.input_tokens, prices.input)
        + _cost(usage.output_tokens, prices.output)
        + _cost(usage.cache_read_tokens, prices.cache_read)
        + _cost(usage.cache_write_tokens, prices.cache_write)
    )


def total_cost_micros(parts: Iterable[TokenUsage]) -> int:
    return sum(cost_micros(part) for part in parts)


def _attempt_bound(prices: Prices, input_tokens: int, max_tokens: int) -> int:
    worst_input = max(prices.input, prices.cache_write)  # cache reads cost less than either
    return _cost(input_tokens, worst_input) + _cost(max_tokens, prices.output)


def reservation_micros(model: str, *, payload_bytes: int, max_tokens: int) -> int:
    """The most one request can cost, in micro-dollars, to reserve before sending it.

    A token is never shorter than a byte of the text it encodes, so the request's UTF-8 byte
    length, plus ``OVERHEAD_TOKENS`` for what the API adds, bounds its input tokens. Every input
    token is priced at the higher of the input and cache-write prices, and ``max_tokens`` of
    output (thinking included) at the output price. A model with a server-side fallback can run
    the request a second time on a model the table may not know, so that attempt is bounded
    again at the table's highest prices."""
    input_tokens = payload_bytes + OVERHEAD_TOKENS
    card = PRICE_TABLE.get(model)
    if card is None:
        return _attempt_bound(MOST_EXPENSIVE, input_tokens, max_tokens)
    bound = _attempt_bound(card.prices_for(input_tokens), input_tokens, max_tokens)
    if card.server_fallback:
        bound += _attempt_bound(MOST_EXPENSIVE, input_tokens, max_tokens)
    return bound


def month_key(now: datetime, zone: tzinfo) -> str:
    """The calendar month ``now`` falls in, in the household's time zone, such as ``2026-11``.
    The cap counts spending per month (spec 6.14, **Cost cap**)."""
    return f"{now.astimezone(zone):%Y-%m}"


def format_dollars(micros: int) -> str:
    """Micro-dollars as dollars to the cent, rounded up, such as ``$0.04``."""
    cents = -(-micros // 10_000)
    return f"${cents // 100}.{cents % 100:02d}"


# Pseudonyms ------------------------------------------------------------------------------------

_LABEL = re.compile(r"\bMember (\d{1,4})\b")


@dataclass(frozen=True, slots=True)
class Pseudonyms:
    """One conversation's names for its members: "Member 1", "Member 2", and so on, in the order
    they first appear. Fixed for the conversation and only ever added to, so a member keeps their
    label across turns."""

    numbers: Mapping[int, int] = field(default_factory=dict)
    """Member ID to number."""

    def label(self, member_id: int) -> str | None:
        number = self.numbers.get(member_id)
        return None if number is None else f"Member {number}"

    def with_member(self, member_id: int) -> Pseudonyms:
        if member_id in self.numbers:
            return self
        return Pseudonyms({**self.numbers, member_id: max(self.numbers.values(), default=0) + 1})

    def to_json(self) -> dict[str, int]:
        return {str(member_id): number for member_id, number in sorted(self.numbers.items())}

    @classmethod
    def from_json(cls, raw: object) -> Pseudonyms:
        if not isinstance(raw, Mapping):
            return cls()
        numbers: dict[int, int] = {}
        for key, value in raw.items():
            if isinstance(key, str) and key.isdigit() and isinstance(value, int) and not isinstance(value, bool):
                numbers[int(key)] = value
        return cls(numbers)

    def restore_names(self, text: str, names: Mapping[int, str]) -> str:
        """``text`` with each label this conversation gave a member replaced by their name in
        ``names``. A label nobody holds, or one whose member has no name there, stays as it is.
        Only a reply shown to a member goes through this; what Dinnerbell sends and stores keeps
        the labels."""
        by_number = {number: member_id for member_id, number in self.numbers.items()}

        def swap(match: re.Match[str]) -> str:
            member_id = by_number.get(int(match.group(1)))
            name = names.get(member_id) if member_id is not None else None
            return name if name else match.group(0)

        return _LABEL.sub(swap, text)


# Conversations ---------------------------------------------------------------------------------

MAX_KEY_LENGTH = 80
_SLACK_ID = re.compile(r"[A-Z0-9]{1,20}")
_SLACK_TS = re.compile(r"\d{1,12}\.\d{1,8}")


class InvalidConversationKey(ValueError):
    """A conversation key Dinnerbell never makes."""


def web_key(member_id: int) -> str:
    """The key of a member's one conversation on the web's **Assistant** page."""
    return f"web:{member_id}"


def slack_key(channel: str, thread_ts: str) -> str:
    """The key of a Slack thread's conversation: its channel (a DM or the household channel)
    and its root message's timestamp."""
    if not _SLACK_ID.fullmatch(channel) or not _SLACK_TS.fullmatch(thread_ts):
        raise InvalidConversationKey("not a Slack channel and thread timestamp")
    return f"slack:{channel}:{thread_ts}"


def check_key(key: str, *, private: bool, member_id: int) -> None:
    """Raise ``InvalidConversationKey`` unless ``key`` is one Dinnerbell makes and fits: a web
    conversation is always private, and only its own member's."""
    if key.startswith("web:"):
        if not private or key != web_key(member_id):
            raise InvalidConversationKey("a web conversation belongs to one member and is private")
        return
    if key.startswith("slack:"):
        _, channel, thread_ts = [*key.split(":"), "", ""][:3]
        if _SLACK_ID.fullmatch(channel) and _SLACK_TS.fullmatch(thread_ts) and key == slack_key(channel, thread_ts):
            return
    raise InvalidConversationKey("not a conversation key")


@dataclass(frozen=True, slots=True)
class Turn:
    """One message in a stored conversation, in pseudonym form. ``author`` is the label of the
    member who wrote a user turn, and None for the assistant's."""

    role: str
    text: str
    author: str | None = None

    def to_json(self) -> dict[str, str]:
        found = {"role": self.role, "text": self.text}
        return found | ({"author": self.author} if self.author is not None else {})

    @classmethod
    def from_json(cls, raw: object) -> Turn | None:
        if not isinstance(raw, Mapping):
            return None
        role, text, author = raw.get("role"), raw.get("text"), raw.get("author")
        if role not in ("user", "assistant") or not isinstance(text, str):
            return None
        return cls(str(role), text, author if isinstance(author, str) else None)

    @property
    def size(self) -> int:
        return len(self.text.encode()) + len((self.author or "").encode())


def turns_from_json(raw: object) -> tuple[Turn, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(turn for turn in (Turn.from_json(item) for item in raw) if turn is not None)


@dataclass(frozen=True, slots=True)
class Conversation:
    """A stored conversation (spec 6.14, **Conversations** and **History**)."""

    key: str
    private: bool
    owner_member_id: int | None
    """The member a private conversation belongs to; None for a shared Slack thread."""
    pseudonyms: Pseudonyms
    turns: tuple[Turn, ...]
    last_message_at: datetime

    def expired(self, now: datetime, hours: int) -> bool:
        """Whether Admin's **Conversation hours** have passed since the last message. At 0,
        every conversation has."""
        return hours == 0 or (now - self.last_message_at).total_seconds() >= hours * 3600


def recent_turns(turns: Sequence[Turn], max_bytes: int) -> tuple[Turn, ...]:
    """The newest of ``turns`` that fit in ``max_bytes``, dropping the oldest first. The kept
    history starts with a user turn, as a request must."""
    kept: list[Turn] = []
    used = 0
    for turn in reversed(turns):
        if used + turn.size > max_bytes:
            break
        kept.append(turn)
        used += turn.size
    kept.reverse()
    while kept and kept[0].role != "user":
        kept.pop(0)
    return tuple(kept)
