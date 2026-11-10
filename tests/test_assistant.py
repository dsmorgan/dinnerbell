# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's core (ADR 0009, spec 6.14): availability, the cost cap's reservations and
settlement, conversations and their pseudonyms, what leaves the house, the tool loop, the actor
every tool runs as, retention, and scrub-test. Every model call goes to ``FakeModel``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.models import AssistantConversationRow, AssistantUsageRow
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.assistant import (
    AssistantSettings,
    InvalidConversationKey,
    TokenUsage,
    cost_micros,
    reservation_micros,
    slack_key,
    web_key,
)
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.feedback import FeedbackInput, PlaceIssue, Rating
from dinnerbell.domain.meals import MealInput, Slot
from dinnerbell.domain.members import Member, Role
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.services import feedback, meals, scrub
from dinnerbell.services import schedule as schedule_service
from dinnerbell.services.assistant import admin, conversation, retention, tools
from dinnerbell.services.assistant.client import ModelCallFailed
from dinnerbell.services.assistant.conversation import (
    AT_CAP,
    FAILED,
    MAX_MODEL_CALLS,
    MESSAGE_SECONDS,
    OUT_OF_TIME,
    REFUSED,
    TOO_LONG_ANSWER,
    TOO_LONG_MESSAGE,
    TOO_MANY_STEPS,
    TURNED_OFF,
    Assistant,
    Reply,
    build_request,
    converse,
    request_bytes,
)
from dinnerbell.services.assistant.runtime import AssistantConfig, available
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.preferences import set_my_preference
from dinnerbell.web.routes.assistant_page import SEND_TIMEOUT_MS
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, failure, stopped, tool_call, usage

pytestmark = pytest.mark.anyio

KEY = "sk-ant-test-not-a-real-key"
LOOKUP = "look_up_catalog_items"


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"anthropic_api_key": KEY}


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def helper(settings: Settings, model: FakeModel, house: House) -> Assistant:
    return Assistant(AssistantConfig.from_settings(settings), model, house.clock)


async def switch(house: House, helper: Assistant, **values: Any) -> None:
    async with house.sessionmaker() as db:
        await admin.update_assistant(
            db, helper.config, house["Alex"], AssistantSettings(**({"enabled": True} | values)), now=house.clock()
        )


async def ask(
    house: House, helper: Assistant, name: str, text: str, *, key: str | None = None, private: bool = True
) -> Reply | None:
    member = house[name]
    async with house.sessionmaker() as db:
        return await converse(db, helper, member, key or web_key(member.id), private=private, text=text)


async def usage_rows(house: House) -> list[AssistantUsageRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AssistantUsageRow).order_by(AssistantUsageRow.id)))


async def conversations(house: House) -> list[AssistantConversationRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AssistantConversationRow).order_by(AssistantConversationRow.id)))


def user_texts(request: dict[str, Any]) -> list[str]:
    return [m["content"] for m in request["messages"] if m["role"] == "user" and isinstance(m["content"], str)]


@pytest.mark.flag("assistant", on=False)
async def test_with_the_flag_off_nothing_answers_or_shows(
    settings: Settings, house: House, helper: Assistant, model: FakeModel
) -> None:
    assert settings.anthropic_api_key is not None
    assert not helper.config.configured
    async with house.sessionmaker() as db:
        assert not await available(db, helper.config, house["Sam"])
        with pytest.raises(NotFound):
            await admin.assistant_admin(db, helper.config, house["Alex"], now=house.clock())
        with pytest.raises(NotFound):
            await admin.update_assistant(
                db, helper.config, house["Alex"], AssistantSettings(enabled=True), now=house.clock()
            )
    assert await ask(house, helper, "Sam", "What Thai places do we have?") is None
    assert model.requests == [] and await usage_rows(house) == [] and await conversations(house) == []


async def test_a_private_only_tool_is_offered_only_in_private(monkeypatch: pytest.MonkeyPatch) -> None:
    private = replace(tools.LOOK_UP_CATALOG_ITEMS, name="my_preferences", private_only=True)
    monkeypatch.setattr(tools, "TOOLS", (tools.LOOK_UP_CATALOG_ITEMS, private))
    assert [t.name for t in tools.tools_for(True)] == [LOOKUP, "my_preferences"]
    assert [t.name for t in tools.tools_for(False)] == [LOOKUP]


def test_the_deadline_comes_before_the_web_page_gives_up() -> None:
    assert MESSAGE_SECONDS * 1000 < SEND_TIMEOUT_MS


@pytest.mark.flag("assistant", on=True)
class TestAssistantOn:
    async def test_availability_needs_the_key_the_flag_the_switch_and_a_member(
        self, settings: Settings, house: House, helper: Assistant
    ) -> None:
        assert helper.config.configured
        sam = house["Sam"]
        async with house.sessionmaker() as db:
            assert not await available(db, helper.config, sam)  # the switch starts off
        await switch(house, helper)
        no_key = replace(helper.config, key_set=False)
        no_flag = replace(helper.config, flag_on=False)
        async with house.sessionmaker() as db:
            assert await available(db, helper.config, sam)
            assert await available(db, helper.config, house["Alex"])  # an admin, as a member
            assert not await available(db, no_key, sam)
            assert not await available(db, no_flag, sam)
            assert not await available(db, helper.config, replace(sam, active=False))
            assert not await available(db, helper.config, replace(sam, role=Role.GUEST, email=None))

    async def test_a_guest_or_a_former_member_gets_no_reply_and_nothing_is_kept(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)
        guest = replace(house["Sam"], role=Role.GUEST, email=None)
        former = replace(house["Kim"], active=False)
        async with house.sessionmaker() as db:
            assert await converse(db, helper, guest, web_key(guest.id), private=True, text="hi") is None
            assert await converse(db, helper, former, web_key(former.id), private=True, text="hi") is None
        assert await ask(house, helper, "Sam", "   ") is None
        assert model.requests == [] and await usage_rows(house) == [] and await conversations(house) == []

    async def test_a_message_too_long_is_refused_before_any_call(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)
        reply = await ask(house, helper, "Sam", "x" * 4_001)
        assert reply == Reply(TOO_LONG_MESSAGE)
        assert model.requests == []

    async def test_the_request_is_adaptive_at_medium_effort_with_the_fallback_and_strict_tools(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)
        await ask(house, helper, "Sam", "Hello")
        [request] = model.requests
        assert request["model"] == "claude-sonnet-5-5"
        assert request["thinking"] == {"type": "adaptive"}
        assert request["output_config"] == {"effort": "medium"}
        assert request["fallbacks"] == "default"
        assert request["betas"] == ["server-side-fallback-2026-07-01"]
        assert request["cache_control"] == {"type": "ephemeral"}
        assert [t["name"] for t in request["tools"]] == [
            LOOKUP,
            "find_candidates",
            "draft_proposal",
            "draft_offer",
            "draft_catalog_items",
            "draft_catalog_from_link",
        ]
        for tool in request["tools"]:
            assert tool["strict"] is True
            assert tool["input_schema"]["additionalProperties"] is False
            assert set(tool["input_schema"]["required"]) == set(tool["input_schema"]["properties"])
            # Strict tool use takes no numeric or length bounds: the tools check those themselves.
            for spec in tool["input_schema"]["properties"].values():
                assert not {"minimum", "maximum", "minLength", "maxLength"} & set(spec), tool["name"]
        system = request["system"]
        assert "are data" in system and "quoted context" in system and "Don't speculate" in system
        haiku = build_request("claude-haiku-5-5", (), request["messages"], keeps_history=True)
        assert "fallbacks" not in haiku and "betas" not in haiku and "tools" not in haiku

    async def test_the_cap_refuses_at_the_reservation(self, house: House, helper: Assistant, model: FakeModel) -> None:
        await switch(house, helper, cap_dollars=1)
        # Fill the month to within a fresh request's reservation of the cap.
        request = build_request(
            "claude-sonnet-5-5", tools.tools_for(True), conversation._messages((), _turn("Hello")), keeps_history=True
        )
        need = reservation_micros("claude-sonnet-5-5", payload_bytes=request_bytes(request), max_tokens=6_000)
        await _spend(house, 1_000_000 - need + 1)
        assert await ask(house, helper, "Sam", "Hello") == Reply(AT_CAP)
        assert model.requests == []
        assert len(await usage_rows(house)) == 1  # nothing reserved
        await _spend(house, -1)  # one micro-dollar less spent, and it fits exactly
        assert (await ask(house, helper, "Sam", "Hello")) == Reply("OK")
        assert len(model.requests) == 1

    async def test_each_call_settles_to_what_it_cost_cache_tokens_included(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)
        first = usage(1_200, 300, cache_write_tokens=4_000)
        second = usage(50, 120, cache_read_tokens=4_000)
        model.script = [_lookup("", "Thai", parts=(first,)), answer("Siam Garden.", parts=(second,))]
        await ask(house, helper, "Sam", "Anything Thai?")
        rows = await usage_rows(house)
        assert [r.state for r in rows] == ["settled", "settled"]
        assert [(r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens) for r in rows] == [
            (1_200, 300, 0, 4_000),
            (50, 120, 4_000, 0),
        ]
        assert [r.cost_micros for r in rows] == [cost_micros(first), cost_micros(second)]
        assert {r.member_id for r in rows} == {house["Sam"].id} and {r.month for r in rows} == {"2026-10"}

    async def test_a_fallback_is_settled_at_every_attempt_at_each_models_prices(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)
        declined = usage(900, 40)
        rescued = TokenUsage("claude-sonnet-5", 900, 100)
        model.script = [answer("Taqueria.", parts=(declined, rescued), model="claude-sonnet-5")]
        await ask(house, helper, "Sam", "Tacos?")
        [row] = await usage_rows(house)
        assert row.model == "claude-sonnet-5" and (row.input_tokens, row.output_tokens) == (1_800, 140)
        assert row.cost_micros == cost_micros(declined) + 900 * 2 + 100 * 10

    async def test_an_unknown_model_costs_the_ceiling_and_is_logged_as_an_error(
        self, house: House, helper: Assistant, model: FakeModel, caplog: pytest.LogCaptureFixture
    ) -> None:
        await switch(house, helper)
        mystery = TokenUsage("claude-mystery-9", 1_000, 100)
        model.script = [answer("Taqueria.", parts=(mystery,), model="claude-mystery-9")]
        await ask(house, helper, "Sam", "Tacos?")
        [row] = await usage_rows(house)
        assert row.cost_micros == 1_000 * 10 + 100 * 50
        [error] = [r for r in caplog.records if r.levelname == "ERROR"]
        assert error.getMessage() == "assistant model not in the price table"
        assert error.model == "claude-mystery-9"

    async def test_each_call_reserves_for_the_thinking_earlier_calls_sent_back(
        self, house: House, helper: Assistant, model: FakeModel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await switch(house, helper)
        extras: list[int] = []
        real = conversation.reservation_micros

        def spy(model_id: str, **kwargs: Any) -> int:
            extras.append(kwargs["extra_input_tokens"])
            return real(model_id, **kwargs)

        monkeypatch.setattr(conversation, "reservation_micros", spy)
        model.script = [
            replace(_lookup("", ""), usage=(usage(output_tokens=700),)),
            replace(_lookup("", "Thai"), usage=(usage(output_tokens=300), usage(output_tokens=50))),
            answer("Siam Garden."),
        ]
        await ask(house, helper, "Sam", "Thai?")
        assert extras == [0, 700, 1_050]

    async def test_the_month_rolls_over_in_the_households_time_zone(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper, cap_dollars=1)
        house.clock.now = datetime(2026, 11, 1, 4, 30, tzinfo=UTC)  # 23:30 on October 31 in Chicago
        await _spend(house, 1_000_000, month="2026-10")
        assert await ask(house, helper, "Sam", "Hello") == Reply(AT_CAP)
        house.clock.now = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 00:30 on November 1
        assert await ask(house, helper, "Sam", "Hello") == Reply("OK")
        assert [r.month for r in await usage_rows(house)] == ["2026-10", "2026-11"]
        async with house.sessionmaker() as db:
            view = await admin.assistant_admin(db, helper.config, house["Alex"], now=house.clock())
        assert view.month == "2026-11" and view.spent_micros == (await usage_rows(house))[1].cost_micros

    async def test_turning_the_switch_off_mid_message_stops_the_next_call(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)

        class TurnsOff(FakeModel):
            async def create(self, request: Any, *, time_left: float) -> Any:
                await switch(house, helper, enabled=False)
                return await super().create(request, time_left=time_left)

        off = TurnsOff(script=[_lookup("", "")])
        reply = await ask(house, replace(helper, client=off), "Sam", "Everything?")
        assert reply == Reply(TURNED_OFF)
        assert len(off.requests) == 1

    @pytest.mark.parametrize(("billed", "state"), [(True, ["settled"]), (False, [])])
    async def test_a_failed_call_keeps_its_reservation_only_when_it_may_have_run(
        self, house: House, helper: Assistant, model: FakeModel, billed: bool, state: list[str]
    ) -> None:
        await switch(house, helper)
        model.script = [failure(maybe_billed=billed)]
        assert await ask(house, helper, "Sam", "Hello") == Reply(FAILED)
        rows = await usage_rows(house)
        assert [r.state for r in rows] == state
        if billed:
            [request] = model.requests
            assert rows[0].cost_micros == reservation_micros(
                "claude-sonnet-5-5", payload_bytes=request_bytes(request), max_tokens=6_000
            )
        assert await conversations(house) == []

    async def test_more_billed_attempts_than_reserved_are_recorded_logged_and_stop_the_message(
        self, house: House, helper: Assistant, model: FakeModel, caplog: pytest.LogCaptureFixture
    ) -> None:
        await switch(house, helper)
        hops = (usage(), TokenUsage("claude-sonnet-5", 1_000, 200), TokenUsage("claude-sonnet-5", 1_000, 200))
        model.script = [replace(_lookup("", ""), usage=hops), answer("Never sent.")]
        assert await ask(house, helper, "Sam", "Everything?") == Reply(FAILED)
        assert len(model.requests) == 1  # no further call in this message
        [row] = await usage_rows(house)
        assert row.cost_micros == sum(cost_micros(part) for part in hops)
        [error] = [r for r in caplog.records if r.getMessage() == "assistant call billed more attempts than reserved"]
        assert (error.levelname, error.attempts, error.covered) == ("ERROR", 3, 2)
        model.script = [answer("Taqueria.", parts=hops)]  # a final answer still reaches the member
        assert await ask(house, helper, "Sam", "Tacos?") == Reply("Taqueria.")

    async def test_an_exchange_isnt_kept_in_a_conversation_that_changed_hands(
        self, house: House, helper: Assistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        await switch(house, helper)
        thread = slack_key("C0HOUSE", "1730000000.000500")
        kim = house["Kim"]

        class Swaps(FakeModel):
            async def create(self, request: Any, *, time_left: float) -> Any:
                # While Sam waits, the key becomes a private conversation of Kim's.
                async with house.sessionmaker() as db:
                    from dinnerbell.db.assistant import AssistantRepository
                    from dinnerbell.domain.assistant import Conversation, Pseudonyms, Turn

                    taken = Conversation(
                        thread,
                        True,
                        kim.id,
                        Pseudonyms({kim.id: 1}),
                        (Turn("user", "Mine", "Member 1"),),
                        house.clock(),
                    )
                    await AssistantRepository(db).save_conversation(house.household.id, taken, house.clock())
                    await db.commit()
                return await super().create(request, time_left=time_left)

        reply = await ask(house, replace(helper, client=Swaps()), "Sam", "Sam's question", key=thread, private=False)
        assert reply == Reply("OK")
        [row] = await conversations(house)
        assert (row.private, row.owner_member_id) == (True, kim.id)
        assert row.transcript == [{"role": "user", "text": "Mine", "author": "Member 1"}]
        assert row.pseudonyms == {str(kim.id): 1}
        assert any(r.getMessage().startswith("assistant exchange not kept") for r in caplog.records)

    async def test_a_real_reply_goes_back_unchanged_and_answers_once(self, house: House, helper: Assistant) -> None:
        """``to_reply`` and ``echoed_content`` on SDK messages, not the fake's dicts."""
        from anthropic.types.beta import BetaMessage

        from dinnerbell.services.assistant.anthropic_client import to_reply

        tokens = {"input_tokens": 900, "output_tokens": 60}
        blocks = [
            {"type": "thinking", "thinking": "", "signature": "sig-1"},
            {"type": "text", "text": "Let me look."},
            {"type": "tool_use", "id": "toolu_9", "name": LOOKUP, "input": {"name": "siam", "tag": ""}},
        ]
        messages = [
            BetaMessage.model_validate(
                {
                    "id": f"msg_{n}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-5-5",
                    "content": content,
                    "stop_reason": stop,
                    "stop_sequence": None,
                    "usage": tokens,
                }
            )
            for n, (content, stop) in enumerate(
                [(blocks, "tool_use"), ([blocks[0], {"type": "text", "text": "Siam Garden is there."}], "end_turn")]
            )
        ]

        class Real(FakeModel):
            async def create(self, request: Any, *, time_left: float) -> Any:
                self.requests.append(json.loads(json.dumps(request)))
                return to_reply(messages[len(self.requests) - 1], requested=request["model"])

        await switch(house, helper)
        real = Real()
        reply = await ask(house, replace(helper, client=real), "Sam", "Is Siam Garden there?")
        assert reply == Reply("Siam Garden is there.")
        echoed = real.requests[1]["messages"][-2]
        assert echoed == {"role": "assistant", "content": blocks}  # exactly what came, nothing doubled

    @pytest.mark.parametrize(
        "parts",
        [(usage(10_000_000, 1_000),), (TokenUsage("claude-mystery-9", 100, 10),)],
        ids=["cost-over-reservation", "unknown-model"],
    )
    async def test_any_overrun_is_recorded_and_ends_the_message(
        self, house: House, helper: Assistant, model: FakeModel, caplog: pytest.LogCaptureFixture, parts: Any
    ) -> None:
        await switch(house, helper, cap_dollars=100)
        model.script = [replace(_lookup("", ""), usage=parts), answer("Never sent.")]
        assert await ask(house, helper, "Sam", "Everything?") == Reply(FAILED)
        assert len(model.requests) == 1
        [row] = await usage_rows(house)
        assert row.cost_micros == sum(cost_micros(part) for part in parts)
        assert [r.levelname for r in caplog.records if r.levelname == "ERROR"] == ["ERROR"]

    async def test_the_save_uses_the_settings_as_they_are_then(self, house: House, helper: Assistant) -> None:
        await switch(house, helper)
        changes = iter([{"enabled": False}, {"conversation_hours": 0}])

        class Changes(FakeModel):
            async def create(self, request: Any, *, time_left: float) -> Any:
                await switch(house, helper, **next(changes))  # the admin saves while the model answers
                return await super().create(request, time_left=time_left)

        changing = replace(helper, client=Changes())
        await ask(house, changing, "Sam", "First")  # switched off meanwhile: nothing kept
        assert await conversations(house) == []
        await switch(house, helper)
        await ask(house, helper, "Sam", "Second")
        assert len(await conversations(house)) == 1
        await ask(house, changing, "Sam", "Third")  # hours set to 0 meanwhile: the conversation goes
        assert await conversations(house) == []

    async def test_a_conversation_that_changed_hands_isnt_deleted_at_zero_hours(
        self, house: House, helper: Assistant
    ) -> None:
        await switch(house, helper, conversation_hours=0)
        thread = slack_key("C0HOUSE", "1730000000.000600")
        kim = house["Kim"]

        class Swaps(FakeModel):
            async def create(self, request: Any, *, time_left: float) -> Any:
                async with house.sessionmaker() as db:
                    from dinnerbell.db.assistant import AssistantRepository
                    from dinnerbell.domain.assistant import Conversation, Pseudonyms

                    taken = Conversation(thread, True, kim.id, Pseudonyms({kim.id: 1}), (), house.clock())
                    await AssistantRepository(db).save_conversation(house.household.id, taken, house.clock())
                    await db.commit()
                return await super().create(request, time_left=time_left)

        await ask(house, replace(helper, client=Swaps()), "Sam", "Hi", key=thread, private=False)
        [row] = await conversations(house)
        assert row.owner_member_id == kim.id  # someone else's: left alone, even at 0 hours

    async def test_a_failure_logs_what_anthropic_said(
        self, house: House, helper: Assistant, model: FakeModel, caplog: pytest.LogCaptureFixture
    ) -> None:
        await switch(house, helper)
        model.script = [
            ModelCallFailed(maybe_billed=False, status=529, error_type="OverloadedError", request_id="req_1")
        ]
        assert await ask(house, helper, "Sam", "Hello") == Reply(FAILED)
        [line] = [r for r in caplog.records if r.getMessage() == "assistant model call failed"]
        assert (line.status, line.error_type, line.anthropic_request_id) == (
            529,
            "OverloadedError",
            "req_1",
        )

    async def test_any_other_error_keeps_the_reservation_and_fails_quietly(
        self, house: House, helper: Assistant, model: FakeModel, caplog: pytest.LogCaptureFixture
    ) -> None:
        await switch(house, helper)
        model.script = [RuntimeError("bug in the client")]
        assert await ask(house, helper, "Sam", "Hello") == Reply(FAILED)
        [row] = await usage_rows(house)
        assert row.state == "settled" and row.cost_micros > 0
        assert any(r.levelname == "ERROR" and r.getMessage() == "assistant model call broke" for r in caplog.records)

    async def test_a_cancelled_call_keeps_its_reservation(
        self, house: House, helper: Assistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        await switch(house, helper)
        started = asyncio.Event()

        class Hangs(FakeModel):
            async def create(self, request: Any, *, time_left: float) -> Any:
                started.set()
                await asyncio.Event().wait()  # until cancelled

        task = asyncio.create_task(ask(house, replace(helper, client=Hangs()), "Sam", "Hello"))
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        [row] = await usage_rows(house)
        assert row.state == "settled" and row.cost_micros > 0
        assert any(
            r.levelname == "ERROR" and r.getMessage() == "assistant model call cancelled" for r in caplog.records
        )

    async def test_each_call_gets_what_is_left_of_the_message_and_none_starts_after_it(
        self, house: House, helper: Assistant
    ) -> None:
        """A message has ``MESSAGE_SECONDS`` in all, not six calls' worth: each call is given
        what's left, and once it's gone the next call is never reserved or made. Nothing of the
        message is kept, and every call made stays settled."""
        await switch(house, helper)

        class Slow(FakeModel):
            async def create(self, request: Any, *, time_left: float) -> Any:
                reply = await super().create(request, time_left=time_left)
                house.clock.advance(timedelta(seconds=30))  # each call takes half a minute
                return reply

        slow = Slow(script=[_lookup("", "")] * MAX_MODEL_CALLS)
        assert await ask(house, replace(helper, client=slow), "Sam", "Hello") == Reply(OUT_OF_TIME)
        assert slow.time_left == [100.0, 70.0, 40.0, 10.0]
        rows = await usage_rows(house)
        assert len(rows) == 4 and {r.state for r in rows} == {"settled"}
        assert await conversations(house) == []

    async def test_a_call_still_running_at_the_deadline_is_cut_off_and_stays_reserved(
        self, house: House, helper: Assistant, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await switch(house, helper)
        started = asyncio.Event()
        deadline = house.clock() + timedelta(seconds=MESSAGE_SECONDS)
        real = AssistantRepository.reserve
        reserved = 0

        async def late(self: AssistantRepository, *args: Any, **kwargs: Any) -> int:
            nonlocal reserved
            reserved += 1
            if reserved == 2:  # the second call starts with a moment of the message left
                house.clock.now = deadline - timedelta(microseconds=1)
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(AssistantRepository, "reserve", late)

        class Hangs(FakeModel):
            async def create(self, request: Any, *, time_left: float) -> Any:
                if not self.requests:
                    return await super().create(request, time_left=time_left)
                self.time_left.append(time_left)
                started.set()
                await asyncio.Event().wait()  # never answers: only the deadline ends it
                raise AssertionError("unreachable")

        hangs = Hangs(script=[_lookup("", "")])
        reply = await asyncio.wait_for(ask(house, replace(helper, client=hangs), "Sam", "Hello"), 10)
        assert started.is_set() and hangs.time_left == [100.0, 0.000001]
        assert reply == Reply(OUT_OF_TIME)
        first, cut = await usage_rows(house)
        assert first.state == cut.state == "settled" and cut.cost_micros > 0 and cut.input_tokens == 0
        assert await conversations(house) == []
        assert any(r.getMessage() == "assistant model call ran out of time" for r in caplog.records)

    async def test_a_reservation_that_waited_past_the_deadline_is_released_uncalled(
        self, house: House, helper: Assistant, model: FakeModel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await switch(house, helper)
        real = AssistantRepository.reserve

        async def slow_lock(self: AssistantRepository, *args: Any, **kwargs: Any) -> int:
            house.clock.advance(timedelta(seconds=MESSAGE_SECONDS))  # as if it waited for the write lock
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(AssistantRepository, "reserve", slow_lock)
        assert await ask(house, helper, "Sam", "Hello") == Reply(OUT_OF_TIME)
        assert model.requests == [] and await usage_rows(house) == []

    async def test_two_messages_at_once_with_room_for_one_reservation(self, house: House, helper: Assistant) -> None:
        await switch(house, helper, cap_dollars=1)
        request = build_request(
            "claude-sonnet-5-5", tools.tools_for(True), conversation._messages((), _turn("Hello")), keeps_history=True
        )
        need = reservation_micros("claude-sonnet-5-5", payload_bytes=request_bytes(request), max_tokens=6_000)
        await _spend(house, 1_000_000 - need - need // 2)  # room for one reservation, not two
        release = asyncio.Event()

        class Waits(FakeModel):
            async def create(self, request: Any, *, time_left: float) -> Any:
                await release.wait()  # holds its reservation until the other message is done
                return await super().create(request, time_left=time_left)

        held = replace(helper, client=Waits())

        async def go(name: str) -> Reply | None:
            try:
                return await ask(house, held, name, "Hello")
            finally:
                release.set()

        replies = await asyncio.wait_for(asyncio.gather(go("Sam"), go("Alex")), 10)
        assert sorted(r.text for r in replies if r is not None) == sorted([AT_CAP, "OK"])
        assert len(held.client.requests) == 1  # type: ignore[attr-defined]

    async def test_two_members_at_once_in_one_thread_both_keep_their_exchange(
        self, house: House, helper: Assistant
    ) -> None:
        await switch(house, helper)
        both = asyncio.Event()

        class Together(FakeModel):
            async def create(self, request: Any, *, time_left: float) -> Any:
                self.requests.append(request)
                if len(self.requests) == 2:
                    both.set()
                await both.wait()  # neither answers until both have read the empty thread
                return answer("Member 1 asked first.")

        client = Together()
        thread = slack_key("C0HOUSE", "1730000000.000400")
        together = replace(helper, client=client)
        await asyncio.wait_for(
            asyncio.gather(
                ask(house, together, "Sam", "Sam's question", key=thread, private=False),
                ask(house, together, "Kim", "Kim's question", key=thread, private=False),
            ),
            10,
        )
        [row] = await conversations(house)
        sam, kim = str(house["Sam"].id), str(house["Kim"].id)
        assert sorted(row.pseudonyms.values()) == [1, 2] and set(row.pseudonyms) == {sam, kim}
        turns = row.transcript
        assert len(turns) == 4 and {t["text"] for t in turns[::2]} == {"Sam's question", "Kim's question"}
        for asked, answered in zip(turns[::2], turns[1::2], strict=True):
            assert answered["text"] == f"{asked['author']} asked first."  # each answer names its own asker

    async def test_the_purge_takes_no_write_lock_when_nothing_is_idle(
        self, house: House, helper: Assistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await switch(house, helper)
        await ask(house, helper, "Sam", "Hello")
        locks: list[object] = []
        real = retention.write_transaction

        def counting(db: Any) -> Any:
            locks.append(db)
            return real(db)

        monkeypatch.setattr(retention, "write_transaction", counting)
        assert await retention.purge_idle_conversations(house.sessionmaker, house.clock()) == 0
        assert locks == []
        house.clock.advance(timedelta(hours=24))
        assert await retention.purge_idle_conversations(house.sessionmaker, house.clock()) == 1
        assert len(locks) == 1

    @pytest.mark.parametrize(("reason", "reply"), [("refusal", REFUSED), ("max_tokens", TOO_LONG_ANSWER)])
    async def test_a_refusal_or_a_cut_off_runs_no_tool(
        self,
        house: House,
        helper: Assistant,
        model: FakeModel,
        monkeypatch: pytest.MonkeyPatch,
        reason: str,
        reply: str,
    ) -> None:
        await switch(house, helper)
        ran: list[str] = []

        async def spy(name: str, *args: Any, **kwargs: Any) -> tools.ToolResult:
            ran.append(name)
            return tools.ToolResult("[]")

        monkeypatch.setattr(conversation, "run_tool", spy)
        half = {"type": "tool_use", "id": "toolu_1", "name": LOOKUP, "input": {"name": ""}}
        model.script = [stopped(reason, half)]
        assert await ask(house, helper, "Sam", "Hello") == Reply(reply)
        assert ran == [] and await conversations(house) == []

    async def test_an_unexpected_stop_or_a_tool_turn_without_a_call_fails_quietly(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)
        model.script = [stopped("pause_turn"), stopped("tool_use", {"type": "text", "text": "Let me look."})]
        assert await ask(house, helper, "Sam", "Hello") == Reply(FAILED)
        assert await ask(house, helper, "Sam", "Hello") == Reply(FAILED)

    async def test_the_loop_stops_after_six_model_calls(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)
        model.script = [_lookup("", "") for _ in range(MAX_MODEL_CALLS + 2)]
        assert await ask(house, helper, "Sam", "Keep looking") == Reply(TOO_MANY_STEPS)
        assert len(model.requests) == MAX_MODEL_CALLS == len(await usage_rows(house))

    async def test_a_tool_round_sends_back_every_result_in_one_message(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)
        model.script = [
            tool_call(
                LOOKUP,
                {"name": "siam", "tag": ""},
                toolu_2=(LOOKUP, {"name": 1, "tag": ""}),
                toolu_3=("drop_table", {}),
            ),
            answer("Siam Garden is there."),
        ]
        reply = await ask(house, helper, "Sam", "Is Siam Garden in the catalog?")
        assert reply == Reply("Siam Garden is there.")
        second = model.requests[1]
        echoed, results = second["messages"][-2:]
        assert echoed["role"] == "assistant"
        assert echoed["content"][0]["type"] == "thinking"  # thinking blocks go back unchanged
        assert [r["tool_use_id"] for r in results["content"]] == ["toolu_1", "toolu_2", "toolu_3"]
        found = json.loads(results["content"][0]["content"])
        assert found == {
            "items": [
                {
                    "name": "Siam Garden",
                    "kind": "restaurant",
                    "tags": ["Thai"],
                    "modes": ["pickup", "delivery"],
                    "meals": ["lunch", "dinner"],
                }
            ],
            "more": False,
        }
        assert results["content"][1]["is_error"] is True and "string" in results["content"][1]["content"]
        assert results["content"][2]["is_error"] is True and "no tool" in results["content"][2]["content"]

    async def test_the_lookup_finds_by_tag_skips_inactive_items_and_caps_its_list(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)
        for n in range(30):
            await house.add_item(f"Noodle Bar {n:02d}", Mode.PICKUP, cuisine="Noodles")
        async with house.sessionmaker() as db:
            from dinnerbell.services import catalog as catalog_service

            await catalog_service.set_item_active(
                db, house["Sam"], house.items["Taqueria"].id, False, now=house.clock()
            )
        model.script = [_lookup("", "mex"), _lookup("bar", "noodle"), answer("Done.")]
        await ask(house, helper, "Sam", "Mexican?")
        mexican = _result(model.requests[1])
        assert [i["name"] for i in mexican["items"]] == ["Sam's Tacos"]  # Taqueria is inactive
        noodles = _result(model.requests[2])
        assert len(noodles["items"]) == 25 and noodles["more"] is True
        assert noodles["items"][0]["name"] == "Noodle Bar 00"

    async def test_a_tool_input_with_extra_fields_or_a_long_query_is_an_error_for_the_model(
        self, house: House, helper: Assistant
    ) -> None:
        async with house.sessionmaker() as db:
            extra = await tools.run_tool(
                LOOKUP, {"name": "", "tag": "", "x": 1}, db=db, member=house["Sam"], private=True, now=house.clock()
            )
            long = await tools.run_tool(
                LOOKUP, {"name": "x" * 81, "tag": ""}, db=db, member=house["Sam"], private=True, now=house.clock()
            )
            not_object = await tools.run_tool(
                LOOKUP, ["x"], db=db, member=house["Sam"], private=True, now=house.clock()
            )
        assert extra.is_error and long.is_error and not_object.is_error

    async def test_a_tool_that_breaks_or_is_refused_tells_the_model(
        self, house: House, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def refused(*args: Any) -> Any:
            raise Forbidden("No.")

        async def broken(*args: Any) -> Any:
            raise RuntimeError("bug")

        for run, expected in ((refused, "No."), (broken, "didn't work")):
            monkeypatch.setattr(tools, "TOOLS", (replace(tools.LOOK_UP_CATALOG_ITEMS, run=run),))
            async with house.sessionmaker() as db:
                result = await tools.run_tool(
                    LOOKUP, {"name": "", "tag": ""}, db=db, member=house["Sam"], private=True, now=house.clock()
                )
            assert result.is_error and expected in result.content

    async def test_an_admin_reaches_every_tool_as_a_member(
        self, house: House, helper: Assistant, model: FakeModel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await switch(house, helper)
        seen: list[Member] = []
        real = tools.catalog_service.catalog

        async def spy(db: Any, actor: Member) -> Any:
            seen.append(actor)
            return await real(db, actor)

        monkeypatch.setattr(tools.catalog_service, "catalog", spy)
        model.script = [_lookup("", ""), answer("Three.")]
        alex = house["Alex"]
        assert alex.is_admin
        await ask(house, helper, "Alex", "How many places?")
        assert [(m.id, m.role, m.is_admin) for m in seen] == [(alex.id, Role.MEMBER, False)]
        # And an admin-only use case refuses that actor.
        async with house.sessionmaker() as db:
            with pytest.raises(Forbidden):
                await schedule_service.ranking_settings(db, tools.tool_actor(alex))
            with pytest.raises(Forbidden):
                await admin.assistant_admin(db, helper.config, tools.tool_actor(alex), now=house.clock())

    async def test_names_come_back_only_in_the_reply(self, house: House, helper: Assistant, model: FakeModel) -> None:
        await switch(house, helper)
        model.script = [answer("Member 1, try Siam Garden.")]
        assert await ask(house, helper, "Sam", "Where should I eat?") == Reply("Sam, try Siam Garden.")
        [row] = await conversations(house)
        assert row.transcript[-1] == {"role": "assistant", "text": "Member 1, try Siam Garden."}
        assert "Sam" not in model.sent

    async def test_nothing_personal_leaves_the_house(self, house: House, helper: Assistant, model: FakeModel) -> None:
        """No emails, Slack IDs, preferences, vetoes, ratings, notes, household name, or names,
        except what the member wrote themselves."""
        await switch(house, helper)
        sam, kim = house["Sam"], house["Kim"]
        async with house.sessionmaker() as db:
            for person, slack in ((sam, "U0SAM1"), (kim, "U0KIM2")):
                await MemberRepository(db).update(house.household.id, person.id, house.clock(), slack_user_id=slack)
            await db.commit()
            await set_my_preference(
                db, kim, TargetType.ITEM, house.items["Taqueria"].id, Stance(veto=True), now=house.clock()
            )
            await set_my_preference(
                db, sam, TargetType.ITEM, house.items["Siam Garden"].id, Stance(score=2), now=house.clock()
            )
            meal = await meals.log_meal(
                db,
                sam,
                MealInput(date(2026, 10, 20), Slot.DINNER, house.items["Siam Garden"].id, frozenset({sam.id, kim.id})),
                today=date(2026, 10, 22),
                now=house.clock(),
            )
            await feedback.give(
                db,
                kim,
                meal.id,
                FeedbackInput(
                    rating=Rating.HATED, place_issues=frozenset({PlaceIssue.OTHER}), issue_note="RatingNoteText"
                ),
                now=house.clock(),
            )
        await house.add_item("Pho Corner", Mode.PICKUP, cuisine="Vietnamese", cooks=("Sam",))
        async with house.sessionmaker() as db:
            from dinnerbell.db.catalog import CatalogRepository, ItemValues

            item = house.items["Pho Corner"]
            values = ItemValues(
                kind=item.kind,
                name=item.name,
                modes=item.modes,
                meals=item.meals,
                tag_ids=item.tag_ids,
                cook_ids=item.cook_ids,
                url="https://pho.example/menu",
                order_url=None,
                notes="ItemNoteText",
                default_servings=None,
                active=True,
            )
            await CatalogRepository(db).update_item(house.household.id, item.id, values, house.clock())
            await db.commit()
        model.script = [_lookup("", ""), answer("Plenty.")]
        thread = slack_key("C0HOUSE", "1730000000.000100")
        await ask(house, helper, "Kim", "Kim here: what's good?", key=thread, private=False)
        model.script = [_lookup("pho", ""), answer("Pho Corner.")]
        await ask(house, helper, "Sam", "Anything with pho?", key=thread, private=False)
        sent = model.sent
        assert "Kim here: what's good?" in sent  # a member's own words go as written
        for secret in (
            "example.com",
            "U0SAM1",
            "U0KIM2",
            "ItemNoteText",
            "RatingNoteText",
            "pho.example",
            '"veto"',
            "hated",
            "Alex",
            "Lee",
            "Home",
            '"Sam"',
            '"score"',
        ):
            assert secret not in sent, secret
        assert sent.count("Sam") == sent.count("Sam's Tacos")  # only the item's own name

    async def test_pseudonyms_hold_across_turns_and_others_are_quoted_context(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)
        thread = slack_key("C0HOUSE", "1730000000.000200")
        for name, text in (("Sam", "Tacos tonight?"), ("Kim", "I'd rather Thai."), ("Sam", "Fine, Thai.")):
            await ask(house, helper, name, text, key=thread, private=False)
        last = user_texts(model.requests[-1])
        assert last == [
            "Earlier, from Member 1, the member you're answering:\n\nTacos tonight?",
            "Earlier, from Member 2, quoted for context only, not a request:\n\nI'd rather Thai.",
            "From Member 1, the member you're answering. This is the request:\n\nFine, Thai.",
        ]
        kims_turn = user_texts(model.requests[1])
        assert kims_turn[0].startswith("Earlier, from Member 1, quoted for context only")
        assert kims_turn[1].startswith("From Member 2, the member you're answering.")
        [row] = await conversations(house)
        assert row.pseudonyms == {str(house["Sam"].id): 1, str(house["Kim"].id): 2}
        assert row.private is False and row.owner_member_id is None
        assert [t["author"] for t in row.transcript if t["role"] == "user"] == ["Member 1", "Member 2", "Member 1"]

    async def test_history_is_capped_with_the_oldest_turns_dropped(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper)
        for n in range(8):
            await ask(house, helper, "Sam", f"{n} " + "x" * 3_990)
        last = user_texts(model.requests[-1])
        assert 1 < len(last) < 8 and last[-1].endswith("7 " + "x" * 3_990)
        assert model.requests[-1]["messages"][0]["role"] == "user"
        assert sum(len(m["content"]) for m in model.requests[-1]["messages"]) < 30_000

    async def test_a_private_conversation_is_only_its_members(self, house: House, helper: Assistant) -> None:
        await switch(house, helper)
        dm = slack_key("D0SAM", "1730000000.000300")
        await ask(house, helper, "Sam", "Hi", key=dm)
        with pytest.raises(Forbidden):
            await ask(house, helper, "Kim", "Hi", key=dm)
        with pytest.raises(Forbidden):
            await ask(house, helper, "Sam", "Hi", key=dm, private=False)
        with pytest.raises(InvalidConversationKey):
            await ask(house, helper, "Kim", "Hi", key=web_key(house["Sam"].id))

    async def test_conversations_expire_and_the_purge_deletes_them(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper, conversation_hours=2)
        await ask(house, helper, "Sam", "First question")
        await ask(house, helper, "Kim", "Kim's question")
        house.clock.advance(timedelta(hours=1))
        await ask(house, helper, "Sam", "Second question")
        assert len(user_texts(model.requests[-1])) == 2  # the first is history
        house.clock.advance(timedelta(hours=1, minutes=30))
        assert await retention.purge_idle_conversations(house.sessionmaker, house.clock()) == 1  # Kim's
        house.clock.advance(timedelta(hours=1))
        await ask(house, helper, "Sam", "Third question")  # past 2 hours: a fresh conversation
        assert user_texts(model.requests[-1]) == [
            "From Member 1, the member you're answering. This is the request:\n\nThird question"
        ]
        [row] = await conversations(house)
        assert [t["text"] for t in row.transcript] == ["Third question", "OK"]
        assert await retention.purge_idle_conversations(house.sessionmaker, house.clock()) == 0

    async def test_lowering_conversation_hours_applies_at_once(self, house: House, helper: Assistant) -> None:
        await switch(house, helper, conversation_hours=48)
        await ask(house, helper, "Sam", "Old question")
        house.clock.advance(timedelta(hours=5))
        await ask(house, helper, "Kim", "New question")
        await switch(house, helper, conversation_hours=4)
        assert [r.key for r in await conversations(house)] == [web_key(house["Kim"].id)]
        await switch(house, helper, conversation_hours=0)
        assert await conversations(house) == []

    async def test_at_zero_hours_nothing_is_kept_and_the_model_is_told(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper, conversation_hours=0)
        await ask(house, helper, "Sam", "First")
        await ask(house, helper, "Sam", "And the second?")
        assert await conversations(house) == []
        assert len(user_texts(model.requests[-1])) == 1
        assert "can't see earlier messages" in model.requests[-1]["system"]

    async def test_admin_shows_this_months_usage_by_member_and_audits_changes(
        self, house: House, helper: Assistant, model: FakeModel
    ) -> None:
        await switch(house, helper, cap_dollars=10)
        await ask(house, helper, "Sam", "One")
        await ask(house, helper, "Sam", "Two")
        await ask(house, helper, "Kim", "Three")
        async with house.sessionmaker() as db:
            view = await admin.assistant_admin(db, helper.config, house["Alex"], now=house.clock())
            with pytest.raises(Forbidden):
                await admin.assistant_admin(db, helper.config, house["Sam"], now=house.clock())
            with pytest.raises(Forbidden):
                await admin.update_assistant(db, helper.config, house["Sam"], AssistantSettings(), now=house.clock())
        rows = await usage_rows(house)
        assert view.settings == AssistantSettings(enabled=True, cap_dollars=10)
        assert view.spent_micros == sum(r.cost_micros for r in rows)
        assert [(u.name, u.micros) for u in view.by_member] == [
            ("Sam", rows[0].cost_micros + rows[1].cost_micros),
            ("Kim", rows[2].cost_micros),
        ]
        audits = await house.audits("household.assistant")
        assert [a.payload for a in audits] == [
            {"assistant_enabled": {"from": False, "to": True}, "assistant_monthly_cap_dollars": {"from": 5, "to": 10}}
        ]
        async with house.sessionmaker() as db:  # saving the same values changes and audits nothing
            assert not await admin.update_assistant(db, helper.config, house["Alex"], view.settings, now=house.clock())
        assert len(await house.audits("household.assistant")) == 1

    async def test_scrub_test_turns_the_assistant_off_and_deletes_conversations(
        self, house: House, helper: Assistant
    ) -> None:
        await switch(house, helper)
        await ask(house, helper, "Sam", "Hello")
        async with house.sessionmaker() as db:
            summary = await scrub.scrub_test(
                db, instance_label="TEST", keep=[house["Alex"].email or ""], now=house.clock()
            )
        assert summary.assistant_turned_off and summary.conversations_deleted == 1
        assert "Assistant conversations deleted: 1" in summary.lines()
        assert await conversations(house) == [] and len(await usage_rows(house)) == 1
        async with house.sessionmaker() as db:
            assert not await available(db, helper.config, house["Alex"])
            again = await scrub.scrub_test(
                db, instance_label="TEST", keep=[house["Alex"].email or ""], now=house.clock()
            )
        assert not again.assistant_turned_off and again.conversations_deleted == 0
        assert not any("Assistant" in line for line in again.lines())  # nothing to say


def _turn(text: str) -> Any:
    from dinnerbell.domain.assistant import Turn

    return Turn("user", text, "Member 1")


def _lookup(name: str, tag: str, *, parts: tuple[TokenUsage, ...] | None = None) -> Any:
    reply = tool_call(LOOKUP, {"name": name, "tag": tag})
    return reply if parts is None else replace(reply, usage=parts)


def _result(request: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = json.loads(request["messages"][-1]["content"][0]["content"])
    return found


async def _spend(house: House, micros: int, *, month: str = "2026-10") -> None:
    """Add to what the month has spent, as one settled row (a negative amount takes some back)."""
    async with house.sessionmaker() as db:
        row = await db.scalar(
            select(AssistantUsageRow).where(AssistantUsageRow.model == "earlier", AssistantUsageRow.month == month)
        )
        if row is None:
            now = house.clock()
            db.add(
                AssistantUsageRow(
                    household_id=house.household.id,
                    member_id=None,
                    model="earlier",
                    month=month,
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_micros=micros,
                    state="settled",
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.cost_micros += micros
        await db.commit()
