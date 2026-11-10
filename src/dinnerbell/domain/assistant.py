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
    falls_back_to: tuple[str, ...] = ()
    """The models Anthropic's server-side refusal fallback (``fallbacks: "default"``) can run a
    declined request on. Empty for a model that has no server-side fallback."""

    def prices_for(self, prompt_tokens: int) -> Prices:
        if self.long is not None and prompt_tokens > self.long_over:
            return self.long
        return self.standard

    @property
    def highest(self) -> Prices:
        return self.standard if self.long is None else self.standard.most(self.long)


DEFAULT_MODEL = "claude-sonnet-5-5"


def _card(input_: str, output: str, cache_read: str, cache_write: str, *falls_back_to: str) -> PriceCard:
    prices = Prices(dollars(input_), dollars(output), dollars(cache_read), dollars(cache_write))
    return PriceCard(prices, falls_back_to=falls_back_to)


# Anthropic's first-party prices per million tokens: input, output, cache read, and 5-minute
# cache write (1.25 times input), from the claude-api skill and Anthropic's pricing page
# (checked 2026-11-10). The pricing page lists Sonnet 5.5's cache reads at $0.10; the table
# keeps the skill's $0.20, the higher of the two, so the cap never undercounts.
MODELS: Mapping[str, PriceCard] = {
    "claude-sonnet-5-5": _card("2", "10", "0.20", "2.50", "claude-sonnet-5"),
    "claude-opus-5-5": _card("4", "20", "0.20", "5", "claude-opus-5", "claude-opus-4-8"),
    # Two rate cards by prompt length; cache reads are 0.1 times input, writes 1.25 times.
    "claude-haiku-5-5": PriceCard(
        Prices(dollars("0.10"), dollars("0.50"), dollars("0.01"), dollars("0.125")),
        long=Prices(dollars("0.50"), dollars("2.50"), dollars("0.05"), dollars("0.625")),
        long_over=100_000,
    ),
}
"""The models ``DINNERBELL_ASSISTANT_MODEL`` may name."""

FALLBACK_MODELS: Mapping[str, PriceCard] = {
    "claude-sonnet-5": _card("2", "10", "0.20", "2.50"),
    "claude-opus-5": _card("5", "25", "0.50", "6.25"),
    "claude-opus-4-8": _card("5", "25", "0.50", "6.25"),
}
"""Models a server-side fallback can run on, priced for settling; never chosen to run."""

PRICE_TABLE: Mapping[str, PriceCard] = {**MODELS, **FALLBACK_MODELS}

UNKNOWN_MODEL_PRICES = Prices(dollars("10"), dollars("50"), dollars("1"), dollars("12.50"))
"""The ceiling for a model the table doesn't know: Claude Fable 5.1's prices, Anthropic's
highest for a current model, with Claude Fable 5's $1 cache reads, the highest of those."""


def known_model(model: str) -> bool:
    return model in PRICE_TABLE


def _rank(model: str) -> tuple[int, int, int, int]:
    card = PRICE_TABLE.get(model)
    prices = UNKNOWN_MODEL_PRICES if card is None else card.highest
    return (prices.output, prices.input, prices.cache_write, prices.cache_read)


def costlier(*models: str) -> str:
    """The model among ``models`` with the higher prices, for an attempt the API doesn't name."""
    return max(models, key=_rank)


def fallback_prices(card: PriceCard) -> Prices | None:
    """The highest prices among the models ``card``'s fallback can run on, or None without one."""
    found: Prices | None = None
    for target in card.falls_back_to:
        prices = PRICE_TABLE[target].highest
        found = prices if found is None else found.most(prices)
    return found


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
    know costs ``UNKNOWN_MODEL_PRICES``."""
    card = PRICE_TABLE.get(usage.model)
    prices = UNKNOWN_MODEL_PRICES if card is None else card.prices_for(usage.prompt_tokens)
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


def reservation_micros(model: str, *, payload_bytes: int, max_tokens: int, extra_input_tokens: int = 0) -> int:
    """What to reserve for one request, in micro-dollars: an upper bound on what it can cost,
    given the assumptions here. Dinnerbell still checks each reply against it (``converse``
    ends the message on an overrun), since the API's billing can change.

    A token is never shorter than a byte of the text it encodes, so the request's UTF-8 byte
    length, plus ``OVERHEAD_TOKENS`` for what the API adds, bounds its input tokens.
    ``extra_input_tokens`` covers input the bytes don't show: the thinking of this message's
    earlier calls, which goes back as signed blocks and bills as input. Every input token is
    priced at the higher of the input and cache-write prices, and ``max_tokens`` of output
    (thinking included) at the output price. A model with a server-side fallback can run the
    request again on each of its fallback models in turn, each one billing, or a sticky
    conversation can run it there instead (``attempts_covered``). Each hop is priced at the
    highest prices among those models, and its input grows by ``max_tokens`` per earlier hop,
    because after a decline mid-stream the next model reads the partial text as context."""
    input_tokens = payload_bytes + OVERHEAD_TOKENS + extra_input_tokens
    card = PRICE_TABLE.get(model)
    if card is None:
        return _attempt_bound(UNKNOWN_MODEL_PRICES, input_tokens, max_tokens)
    bound = _attempt_bound(card.prices_for(input_tokens), input_tokens, max_tokens)
    fallback = fallback_prices(card)
    if fallback is not None:
        for hop in range(1, len(card.falls_back_to) + 1):
            bound += _attempt_bound(fallback, input_tokens + hop * max_tokens, max_tokens)
    return bound


def attempts_covered(model: str) -> int:
    """How many billed attempts one reservation for ``model`` covers: the model itself, and one
    hop to each model its fallback can run on."""
    card = PRICE_TABLE.get(model)
    return 1 + (len(card.falls_back_to) if card is not None else 0)


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

    def onto(self, base: Pseudonyms) -> Pseudonyms:
        """``base`` with every member this map has added, in this map's order. Someone ``base``
        already labels keeps their label, and anyone else gets the next number in ``base``: for
        saving an exchange onto a conversation another message saved meanwhile."""
        merged = base
        for member_id, _ in sorted(self.numbers.items(), key=lambda item: item[1]):
            merged = merged.with_member(member_id)
        return merged

    def relabel(self, text: str, target: Pseudonyms) -> str:
        """``text`` written with this map's labels, rewritten with ``target``'s, in one pass, so
        a label that moved can't be rewritten twice. A label this map doesn't hold, such as one
        the model invented or quoted, becomes "a member", so it can never later show a real
        person's name."""
        by_number = {number: member_id for member_id, number in self.numbers.items()}

        def swap(match: re.Match[str]) -> str:
            member_id = by_number.get(int(match.group(1)))
            label = target.label(member_id) if member_id is not None else None
            return label or "a member"

        return _LABEL.sub(swap, text)

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
