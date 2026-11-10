# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's pure rules (ADR 0009): settings, availability, prices, the reservation
bound, the month key, pseudonyms, conversation keys, and the history cap."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dinnerbell.domain import assistant
from dinnerbell.domain.assistant import (
    MODELS,
    OVERHEAD_TOKENS,
    PRICE_TABLE,
    UNKNOWN_MODEL_PRICES,
    AssistantSettings,
    Conversation,
    InvalidAssistantSettings,
    InvalidConversationKey,
    Prices,
    Pseudonyms,
    TokenUsage,
    Turn,
    assistant_settings,
    attempts_covered,
    check_assistant_settings,
    check_key,
    cost_micros,
    costlier,
    dollars,
    fallback_prices,
    format_dollars,
    month_key,
    recent_turns,
    reservation_micros,
    settings_to_json,
    slack_key,
    turns_from_json,
    web_key,
)
from dinnerbell.domain.members import Capabilities, Member, Role

CHICAGO = ZoneInfo("America/Chicago")
SAM = Member(1, 1, "Sam", "sam@example.com", Role.MEMBER, Capabilities(), active=True)


# Settings and availability ---------------------------------------------------------------------


def test_settings_default_to_off_five_dollars_and_a_day() -> None:
    assert assistant_settings({}) == AssistantSettings(enabled=False, cap_dollars=5, conversation_hours=24)


@pytest.mark.parametrize(
    "stored",
    [
        {"assistant_enabled": "yes", "assistant_monthly_cap_dollars": 0, "assistant_conversation_hours": 169},
        {"assistant_enabled": 1, "assistant_monthly_cap_dollars": True, "assistant_conversation_hours": "24"},
        {"assistant_monthly_cap_dollars": 101, "assistant_conversation_hours": -1},
    ],
)
def test_reading_settings_is_forgiving(stored: dict[str, object]) -> None:
    assert assistant_settings(stored) == AssistantSettings()


def test_settings_round_trip() -> None:
    values = AssistantSettings(enabled=True, cap_dollars=100, conversation_hours=0)
    assert assistant_settings(settings_to_json(values)) == values
    assert values.cap_micros == 100_000_000


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (AssistantSettings(cap_dollars=0), r"\$1 to \$100"),
        (AssistantSettings(cap_dollars=101), r"\$1 to \$100"),
        (AssistantSettings(conversation_hours=-1), "0 to 168"),
        (AssistantSettings(conversation_hours=169), "0 to 168"),
    ],
)
def test_checking_settings_is_strict(values: AssistantSettings, message: str) -> None:
    with pytest.raises(InvalidAssistantSettings, match=message):
        check_assistant_settings(values)


@pytest.mark.parametrize(
    ("configured", "switch_on", "member", "expected"),
    [
        (True, True, SAM, True),
        (True, True, replace(SAM, role=Role.ADMIN), True),
        (False, True, SAM, False),
        (True, False, SAM, False),
        (True, True, replace(SAM, active=False), False),
        (True, True, replace(SAM, role=Role.GUEST, email=None), False),
    ],
)
def test_availability_matrix(configured: bool, switch_on: bool, member: Member, expected: bool) -> None:
    assert assistant.available(configured=configured, switch_on=switch_on, member=member) is expected


# Prices and cost -------------------------------------------------------------------------------


def test_the_table_prices_three_models_and_their_fallbacks() -> None:
    assert set(MODELS) == {"claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"}
    assert set(PRICE_TABLE) == set(MODELS) | {"claude-sonnet-5", "claude-opus-5", "claude-opus-4-8"}
    for card in PRICE_TABLE.values():
        for prices in filter(None, (card.standard, card.long)):
            assert prices.cache_write * 4 == prices.input * 5
            assert prices.cache_read < prices.input
        assert set(card.falls_back_to) <= set(PRICE_TABLE)
    sonnet = PRICE_TABLE["claude-sonnet-5-5"].standard
    assert (sonnet.input, sonnet.output, sonnet.cache_read) == (2_000_000, 10_000_000, 200_000)
    assert PRICE_TABLE["claude-opus-5"].standard == Prices(5_000_000, 25_000_000, 500_000, 6_250_000)


def test_each_fallback_is_bounded_at_its_targets_highest_prices() -> None:
    assert fallback_prices(PRICE_TABLE["claude-sonnet-5-5"]) == PRICE_TABLE["claude-sonnet-5"].standard
    assert fallback_prices(PRICE_TABLE["claude-opus-5-5"]) == PRICE_TABLE["claude-opus-5"].standard
    assert fallback_prices(PRICE_TABLE["claude-haiku-5-5"]) is None


def test_a_reservation_covers_the_model_and_one_hop_per_fallback_model() -> None:
    assert attempts_covered("claude-sonnet-5-5") == 2
    assert attempts_covered("claude-opus-5-5") == 3
    assert attempts_covered("claude-haiku-5-5") == 1
    assert attempts_covered("claude-mystery") == 1


def test_the_ceiling_is_above_every_price_in_the_table() -> None:
    for card in PRICE_TABLE.values():
        assert card.highest.most(UNKNOWN_MODEL_PRICES) == UNKNOWN_MODEL_PRICES
    assert Prices(10_000_000, 50_000_000, 1_000_000, 12_500_000) == UNKNOWN_MODEL_PRICES


def test_an_attempt_without_a_model_is_priced_as_the_costlier() -> None:
    assert costlier("claude-opus-5-5", "claude-opus-5") == "claude-opus-5"
    assert costlier("claude-sonnet-5-5", "claude-sonnet-5") == "claude-sonnet-5-5"  # equal: the first
    assert costlier("claude-haiku-5-5", "claude-mystery") == "claude-mystery"


def test_dollars_are_exact() -> None:
    assert dollars("2.50") == 2_500_000
    assert dollars("0.125") == 125_000
    assert dollars("4") == 4_000_000


def test_cost_counts_every_kind_of_token_and_rounds_up() -> None:
    usage = TokenUsage("claude-sonnet-5-5", 1_000, 500, cache_read_tokens=10_000, cache_write_tokens=2_000)
    # 1,000 * $2 + 500 * $10 + 10,000 * $0.20 + 2,000 * $2.50, per million tokens
    assert cost_micros(usage) == 2_000 + 5_000 + 2_000 + 5_000
    assert cost_micros(TokenUsage("claude-sonnet-5-5", input_tokens=1)) == 2  # $0.000002
    assert cost_micros(TokenUsage("claude-haiku-5-5", input_tokens=1)) == 1  # $0.0000001, rounded up


def test_a_fallback_model_costs_its_own_prices_and_an_unknown_one_the_ceiling() -> None:
    assert cost_micros(TokenUsage("claude-sonnet-5", 1_000, 1_000)) == 2_000 + 10_000
    assert cost_micros(TokenUsage("claude-opus-4-8", 1_000, 1_000, cache_read_tokens=1_000)) == 5_000 + 25_000 + 500
    assert cost_micros(TokenUsage("claude-mystery", 1_000, 1_000)) == 10_000 + 50_000


def test_a_long_haiku_prompt_costs_its_long_rate() -> None:
    short = TokenUsage("claude-haiku-5-5", input_tokens=100_000, output_tokens=1_000)
    long = TokenUsage("claude-haiku-5-5", input_tokens=60_000, output_tokens=1_000, cache_read_tokens=40_001)
    assert cost_micros(short) == 10_000 + 500
    assert cost_micros(long) == 30_000 + 2_500 + 2_001


@pytest.mark.parametrize("model", sorted(MODELS))
@pytest.mark.parametrize("payload_bytes", [0, 5_000, 99_000, 250_000])
@pytest.mark.parametrize("extra", [0, 3_000])
def test_the_reservation_covers_the_worst_usage(model: str, payload_bytes: int, extra: int) -> None:
    max_tokens = 6_000
    bound = reservation_micros(model, payload_bytes=payload_bytes, max_tokens=max_tokens, extra_input_tokens=extra)
    most = payload_bytes + OVERHEAD_TOKENS + extra
    targets = PRICE_TABLE[model].falls_back_to
    for split in (0, most // 3, most):  # input, cache read, and cache write in any mix
        worst = [
            TokenUsage(model, input_tokens=split, output_tokens=max_tokens, cache_write_tokens=most - split),
            TokenUsage(model, cache_read_tokens=split, output_tokens=max_tokens, cache_write_tokens=most - split),
        ]
        for usage in worst:
            # A declined attempt, then the dearest of its fallback models; or the fallback alone (sticky).
            # One hop per fallback model, each one billing, at the dearest of them.
            hop = max((cost_micros(replace(usage, model=t)) for t in targets), default=0)
            assert bound >= cost_micros(usage) + len(targets) * hop


def test_a_fallback_adds_an_attempt_at_its_targets_prices() -> None:
    def bound(model: str, extra: int = 0) -> int:
        return reservation_micros(model, payload_bytes=8_000, max_tokens=6_000, extra_input_tokens=extra)

    # 10,000 input tokens at the cache-write price and 6,000 output, then again on the fallback.
    assert bound("claude-sonnet-5-5") == (25_000 + 60_000) * 2
    assert bound("claude-opus-5-5") == (50_000 + 120_000) + 2 * (62_500 + 150_000)  # two possible hops
    assert bound("claude-haiku-5-5") == 1_250 + 3_000  # no server-side fallback
    assert bound("claude-mystery") == 125_000 + 300_000
    assert bound("claude-sonnet-5-5", extra=2_000) == (30_000 + 60_000) * 2


def test_format_dollars_rounds_up_to_the_cent() -> None:
    assert format_dollars(0) == "$0.00"
    assert format_dollars(1) == "$0.01"
    assert format_dollars(5_000_000) == "$5.00"
    assert format_dollars(1_234_567) == "$1.24"


def test_the_month_is_the_households() -> None:
    # 23:30 on October 31 in Chicago is already November in UTC.
    late = datetime(2026, 11, 1, 4, 30, tzinfo=UTC)
    assert month_key(late, CHICAGO) == "2026-10"
    assert month_key(late + timedelta(hours=1), CHICAGO) == "2026-11"
    assert month_key(late, UTC) == "2026-11"


# Pseudonyms ------------------------------------------------------------------------------------


def test_pseudonyms_are_fixed_and_only_added_to() -> None:
    names = Pseudonyms().with_member(7).with_member(3).with_member(7)
    assert (names.label(7), names.label(3), names.label(9)) == ("Member 1", "Member 2", None)
    again = Pseudonyms.from_json(names.to_json()).with_member(9)
    assert (again.label(7), again.label(3), again.label(9)) == ("Member 1", "Member 2", "Member 3")
    assert names.with_member(7) is names


def test_reading_pseudonyms_skips_what_isnt_one() -> None:
    assert Pseudonyms.from_json(["x"]) == Pseudonyms()
    assert Pseudonyms.from_json({"7": 1, "x": 2, "8": True, "9": "3"}).numbers == {7: 1}


def test_an_exchange_saved_onto_a_newer_map_keeps_labels_and_relabels_its_text() -> None:
    sent = Pseudonyms({7: 1, 5: 2})  # Sam, then Kim, as this message saw it
    saved_meanwhile = Pseudonyms({7: 1, 3: 2})  # Sam, then Lee, saved by another message
    merged = sent.onto(saved_meanwhile)
    assert merged.numbers == {7: 1, 3: 2, 5: 3}
    assert sent.relabel("Member 2 asked Member 1; Member 9 isn't anyone.", merged) == (
        "Member 3 asked Member 1; Member 9 isn't anyone."
    )
    assert sent.onto(Pseudonyms()) == sent


def test_names_come_back_only_for_labels_the_conversation_gave() -> None:
    names = Pseudonyms({7: 1, 3: 2})
    text = "Member 1 and Member 2 can go; Member 3 can't, and Member 10 isn't here."
    assert names.restore_names(text, {7: "Sam", 3: ""}) == (
        "Sam and Member 2 can go; Member 3 can't, and Member 10 isn't here."
    )


# Conversations ---------------------------------------------------------------------------------


def test_conversation_keys() -> None:
    assert web_key(4) == "web:4"
    assert slack_key("D012AB", "1730000000.000100") == "slack:D012AB:1730000000.000100"
    with pytest.raises(InvalidConversationKey):
        slack_key("d012ab", "1730000000.000100")
    with pytest.raises(InvalidConversationKey):
        slack_key("C1", "now")
    check_key("web:4", private=True, member_id=4)
    check_key("slack:C1:1.2", private=False, member_id=4)
    for key, private in [("web:4", False), ("web:5", True), ("slack:C1", False), ("slack:c1:1.2", True), ("x", True)]:
        with pytest.raises(InvalidConversationKey):
            check_key(key, private=private, member_id=4)


def test_turns_round_trip_and_skip_what_isnt_one() -> None:
    turns = (Turn("user", "Tacos?", "Member 1"), Turn("assistant", "Taco Spot."))
    assert turns_from_json([t.to_json() for t in turns]) == turns
    assert turns_from_json([{"role": "system", "text": "x"}, {"role": "user"}, "x"]) == ()
    assert turns_from_json({"role": "user"}) == ()


def test_history_drops_the_oldest_turns_first_and_starts_with_a_user() -> None:
    turns = [Turn("user", "a" * 10, "Member 1"), Turn("assistant", "b" * 10), Turn("user", "c" * 10, "Member 2")]
    assert recent_turns(turns, 1_000) == tuple(turns)
    assert recent_turns(turns, 37) == (turns[2],)  # the assistant turn fits, but can't come first
    assert recent_turns(turns, 5) == ()


def test_a_conversation_expires_after_its_hours() -> None:
    last = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
    talk = Conversation("web:1", True, 1, Pseudonyms(), (), last)
    assert not talk.expired(last + timedelta(hours=23, minutes=59), 24)
    assert talk.expired(last + timedelta(hours=24), 24)
    assert talk.expired(last, 0)
