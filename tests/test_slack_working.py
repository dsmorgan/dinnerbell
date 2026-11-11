# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's working indicator in Slack (#265, spec 6.14): a status in the thread, or a
reaction where Slack refuses one, shown while a message is answered and cleared after its reply,
error, or deadline line. Slack is ``FakeSlack`` and the model is ``FakeModel``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from typing import Any

import aiohttp
import pytest

from dinnerbell.channels.slack import assistant as slack_assistant
from dinnerbell.channels.slack import working
from dinnerbell.channels.slack.assistant import STILL_ON_IT, TEXT_ONLY
from dinnerbell.channels.slack.working import LOADING_MESSAGES, REACTION, REFUSAL_TTL, STATUS
from dinnerbell.config import Settings
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.assistant import AssistantSettings
from dinnerbell.domain.members import Role
from dinnerbell.services.assistant import admin
from dinnerbell.services.assistant.client import ModelReply
from dinnerbell.services.assistant.conversation import FAILED, OUT_OF_TIME, Assistant, Reply
from dinnerbell.services.assistant.drafts import DraftView
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.database import Database, open_database
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer
from tests.support.fake_slack import BOT_USER, CHANNEL, Failure, message_event, next_ts, send
from tests.support.slack import SLACK_IDS, Slack, link, wire

pytestmark = pytest.mark.anyio

KEY = "sk-ant-test-not-a-real-key"
SAM_DM = "D02SAM"
SET_STATUS = "assistant.threads.setStatus"
INDICATOR = (SET_STATUS, "reactions.add", "reactions.remove")
SECRET = "Zq7x private words"


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"anthropic_api_key": KEY}


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> AsyncIterator[House]:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    yield house
    await house.close()


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def helper(settings: Settings, model: FakeModel, house: House) -> Assistant:
    return Assistant(AssistantConfig.from_settings(settings), model, house.clock)


@pytest.fixture
async def slack(house: House, helper: Assistant) -> Slack:
    slack = await wire(house, assistant=helper)
    async with house.sessionmaker() as db:
        settings = AssistantSettings(enabled=True)
        await admin.update_assistant(db, helper.config, house["Alex"], settings, now=house.clock())
    return slack


def steps(slack: Slack) -> list[tuple[str, dict[str, Any]]]:
    """The indicator's calls and the replies, in order, each as its method and arguments."""
    shown = (*INDICATOR, "chat.postMessage", "chat.postEphemeral")
    return [(c.method, c.args) for c in slack.fake.calls if c.method in shown]


def methods(slack: Slack) -> list[str]:
    return [method for method, _ in steps(slack)]


def status_set(channel: str, root: str) -> tuple[str, dict[str, Any]]:
    return SET_STATUS, {
        "channel_id": channel,
        "thread_ts": root,
        "status": STATUS,
        "loading_messages": LOADING_MESSAGES,
    }


def status_cleared(channel: str, root: str) -> tuple[str, dict[str, Any]]:
    return SET_STATUS, {"channel_id": channel, "thread_ts": root, "status": ""}


def reacted(method: str, channel: str, ts: str) -> tuple[str, dict[str, Any]]:
    return method, {"channel": channel, "timestamp": ts, "name": REACTION}


class Slow(FakeModel):
    """Holds every request until ``gate`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.started = 0

    async def create(self, request: Mapping[str, Any], *, time_left: float) -> ModelReply:
        self.started += 1
        await self.gate.wait()
        return await super().create(request, time_left=time_left)


def slow_model(slack: Slack) -> Slow:
    slow = Slow()
    assert slack.conn.assistant is not None
    object.__setattr__(slack.conn.assistant.assistant, "client", slow)
    return slow


async def until(check: Any) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while not check():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0)


def mention_event(user: str, text: str, **options: Any) -> dict[str, Any]:
    return message_event(user, text, kind="app_mention", channel=CHANNEL, channel_type=None, **options)


def replying(monkeypatch: pytest.MonkeyPatch, reply: Reply) -> None:
    async def converse(*args: Any, **kwargs: Any) -> Reply:
        return reply

    monkeypatch.setattr(slack_assistant, "converse", converse)


# The status ---------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_a_dm_shows_the_status_while_its_answered_and_clears_it_after_the_reply(
    slack: Slack, model: FakeModel
) -> None:
    model.script = [answer("There are two Thai places.")]
    root = next_ts()
    await slack.dm("Sam", "What Thai places do we have?", ts=root)
    assert methods(slack) == [SET_STATUS, "chat.postMessage", SET_STATUS]
    first, reply, last = steps(slack)
    assert first == status_set(SAM_DM, root)
    assert reply[1]["text"] == "There are two Thai places."
    assert last == status_cleared(SAM_DM, root)


@pytest.mark.flag("assistant", on=True)
async def test_a_mention_shows_the_status_in_its_thread(slack: Slack) -> None:
    root = next_ts()
    await slack.mention("Sam", f"<@{BOT_USER}> any ideas?", ts=root)
    reply = next_ts()
    await slack.mention("Kim", f"<@{BOT_USER}> something cheaper", ts=reply, thread_ts=root)
    calls = steps(slack)
    assert [call for call in calls if call[0] == SET_STATUS] == [
        status_set(CHANNEL, root),
        status_cleared(CHANNEL, root),
        status_set(CHANNEL, root),  # the thread's root, not the reply's own ts
        status_cleared(CHANNEL, root),
    ]


@pytest.mark.flag("assistant", on=True)
async def test_the_status_shows_before_the_model_answers(slack: Slack) -> None:
    slow = slow_model(slack)
    root = next_ts()
    await send(slack.conn.app, "events_api", message_event("U02SAM", "Hi", channel=SAM_DM, ts=root))
    await until(lambda: slow.started == 1 and bool(slack.fake.of(SET_STATUS)))
    assert steps(slack) == [status_set(SAM_DM, root)]
    slow.gate.set()
    await slack.conn.interactions.settle()
    assert steps(slack)[-1] == status_cleared(SAM_DM, root)


@pytest.mark.flag("assistant", on=True)
async def test_the_status_clears_after_an_ephemeral_reply(slack: Slack, monkeypatch: pytest.MonkeyPatch) -> None:
    replying(monkeypatch, Reply("Here's a draft.", (DraftView(1, "proposal", "A noodle place", ("Adds it.",)),)))
    root = next_ts()
    await slack.mention("Sam", f"<@{BOT_USER}> propose noodles", ts=root)
    assert methods(slack) == [SET_STATUS, "chat.postEphemeral", "chat.postEphemeral", SET_STATUS]
    assert steps(slack)[-1] == status_cleared(CHANNEL, root)


@pytest.mark.parametrize("reply", [FAILED, OUT_OF_TIME], ids=["error", "deadline"])
@pytest.mark.flag("assistant", on=True)
async def test_the_status_clears_after_an_error_or_the_deadline_line(
    slack: Slack, monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    replying(monkeypatch, Reply(reply))
    root = next_ts()
    await slack.dm("Sam", "Hi", ts=root)
    assert methods(slack) == [SET_STATUS, "chat.postMessage", SET_STATUS]
    assert steps(slack)[1][1]["text"] == reply
    assert steps(slack)[-1] == status_cleared(SAM_DM, root)


@pytest.mark.flag("assistant", on=True)
async def test_the_status_clears_when_the_reply_is_refused(slack: Slack) -> None:
    slack.fake.fail("chat.postMessage", Failure("not_in_channel"))
    root = next_ts()
    await slack.dm("Sam", "Hi", ts=root)
    assert steps(slack)[-1] == status_cleared(SAM_DM, root)


@pytest.mark.flag("assistant", on=True)
async def test_the_status_clears_when_answering_breaks_after_converse(
    slack: Slack, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(reply: Reply) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(slack_assistant, "reply_text", broken)
    root = next_ts()
    await slack.dm("Sam", "Hi", ts=root)
    assert steps(slack) == [status_set(SAM_DM, root), status_cleared(SAM_DM, root)]
    assert "the assistant's slack message failed" in caplog.text


def cancel_answers(slack: Slack) -> None:
    for task in list(slack.conn.interactions.tasks):
        task.cancel()


@pytest.mark.flag("assistant", on=True)
async def test_a_cancelled_answer_still_clears_the_status(slack: Slack) -> None:
    slow = slow_model(slack)
    root = next_ts()
    await send(slack.conn.app, "events_api", message_event("U02SAM", "First", channel=SAM_DM, ts=root))
    await until(lambda: slow.started == 1 and bool(slack.fake.of(SET_STATUS)))
    cancel_answers(slack)
    await slack.conn.interactions.settle()
    assert steps(slack) == [status_set(SAM_DM, root), status_cleared(SAM_DM, root)]


@pytest.mark.flag("assistant", on=True)
async def test_a_cancelled_answer_still_takes_its_reaction_back(slack: Slack) -> None:
    slack.fake.fail(SET_STATUS, Failure("method_not_supported_for_channel_type"))
    slow = slow_model(slack)
    ts = next_ts()
    mention = mention_event("U02SAM", f"<@{BOT_USER}> hi", ts=ts)
    await send(slack.conn.app, "events_api", mention)
    await until(lambda: slow.started == 1 and bool(slack.fake.of("reactions.add")))
    cancel_answers(slack)
    await slack.conn.interactions.settle()
    assert steps(slack)[1:] == [reacted("reactions.add", CHANNEL, ts), reacted("reactions.remove", CHANNEL, ts)]


@pytest.mark.flag("assistant", on=True)
async def test_a_cancel_while_clearing_still_clears(slack: Slack) -> None:
    slow = slow_model(slack)
    root = next_ts()
    await send(slack.conn.app, "events_api", message_event("U02SAM", "First", channel=SAM_DM, ts=root))
    await until(lambda: slow.started == 1 and bool(slack.fake.of(SET_STATUS)))
    slack.fake.gates[SET_STATUS] = asyncio.Event()  # the clear after the reply hangs
    slow.gate.set()
    await until(lambda: len(slack.fake.of(SET_STATUS)) == 2)
    cancel_answers(slack)
    await slack.conn.interactions.settle()
    assert methods(slack) == [SET_STATUS, "chat.postMessage", SET_STATUS, SET_STATUS]
    assert steps(slack)[2:] == [status_cleared(SAM_DM, root)] * 2  # tried again, briefly, after the cancel


# No indicator ------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["guest", "stranger"])
@pytest.mark.flag("assistant", on=True)
async def test_messages_dinnerbell_ignores_get_no_indicator(house: House, slack: Slack, who: str) -> None:
    if who == "guest":
        async with house.sessionmaker() as db:
            await MemberRepository(db).update(
                house.household.id, house["Kim"].id, house.clock(), role=Role.GUEST, email=None
            )
            await db.commit()
    sender = "U09NOBODY" if who == "stranger" else "Kim"
    await slack.dm(sender, "Hi")
    await slack.mention(sender, f"<@{BOT_USER}> hi")
    assert steps(slack) == []


@pytest.mark.flag("assistant", on=True)
async def test_with_the_switch_off_there_is_no_indicator(house: House, helper: Assistant, slack: Slack) -> None:
    async with house.sessionmaker() as db:
        await admin.update_assistant(
            db, helper.config, house["Alex"], AssistantSettings(enabled=False), now=house.clock()
        )
    await slack.dm("Sam", "Hi")
    assert steps(slack) == []


@pytest.mark.flag("assistant", on=True)
async def test_a_file_gets_the_text_only_line_and_no_indicator(slack: Slack) -> None:
    await slack.dm("Sam", "", subtype="file_share", files=[{"id": "F1"}])
    assert methods(slack) == ["chat.postMessage"]
    assert steps(slack)[0][1]["text"] == TEXT_ONLY


@pytest.mark.flag("assistant", on=True)
async def test_a_message_while_one_is_answered_gets_no_indicator(slack: Slack) -> None:
    slow = slow_model(slack)
    first = next_ts()
    await send(slack.conn.app, "events_api", message_event("U02SAM", "First", channel=SAM_DM, ts=first))
    await until(lambda: slow.started == 1 and bool(slack.fake.of(SET_STATUS)))
    await send(slack.conn.app, "events_api", message_event("U02SAM", "Second", channel=SAM_DM))
    await until(lambda: bool(slack.fake.of("chat.postMessage")))
    slow.gate.set()
    await slack.conn.interactions.settle()
    calls = steps(slack)
    assert calls[1][1]["text"] == STILL_ON_IT
    statuses = [call for call in calls if call[0] == SET_STATUS]
    assert statuses == [status_set(SAM_DM, first), status_cleared(SAM_DM, first)]


# The reaction ------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_where_slack_refuses_the_status_the_message_gets_a_reaction(slack: Slack) -> None:
    slack.fake.fail(SET_STATUS, Failure("method_not_supported_for_channel_type"))
    root = next_ts()
    await slack.mention("Sam", f"<@{BOT_USER}> any ideas?", ts=root)
    assert steps(slack)[0] == status_set(CHANNEL, root)
    assert methods(slack) == [SET_STATUS, "reactions.add", "chat.postMessage", "reactions.remove"]
    assert steps(slack)[1] == reacted("reactions.add", CHANNEL, root)
    assert steps(slack)[-1] == reacted("reactions.remove", CHANNEL, root)


@pytest.mark.flag("assistant", on=True)
async def test_a_refusal_is_remembered_for_its_kind_of_conversation(slack: Slack) -> None:
    slack.fake.fail(SET_STATUS, Failure("method_not_supported_for_channel_type"))
    await slack.mention("Sam", f"<@{BOT_USER}> any ideas?")
    slack.fake.clear()
    reply = next_ts()
    await slack.mention("Kim", f"<@{BOT_USER}> anything else?", ts=reply)
    assert methods(slack) == ["reactions.add", "chat.postMessage", "reactions.remove"]  # no status tried
    assert steps(slack)[0] == reacted("reactions.add", CHANNEL, reply)
    slack.fake.clear()
    await slack.dm("Sam", "Hi")  # a DM still tries the status
    assert methods(slack) == [SET_STATUS, "chat.postMessage", SET_STATUS]


@pytest.mark.parametrize("error", ["ratelimited", "internal_error"])
@pytest.mark.flag("assistant", on=True)
async def test_a_passing_failure_falls_back_once_and_tries_the_status_again_next_time(slack: Slack, error: str) -> None:
    slack.fake.fail(SET_STATUS, Failure(error))
    await slack.dm("Sam", "Hi")
    assert methods(slack) == [SET_STATUS, "reactions.add", "chat.postMessage", "reactions.remove"]
    slack.fake.clear()
    await slack.dm("Sam", "Again")
    assert methods(slack) == [SET_STATUS, "chat.postMessage", SET_STATUS]


@pytest.mark.flag("assistant", on=True)
async def test_without_reactions_write_the_reply_still_posts_and_neither_is_tried_again(slack: Slack) -> None:
    slack.fake.fail(SET_STATUS, Failure("method_not_supported_for_channel_type"))
    slack.fake.fail("reactions.add", Failure("missing_scope"))
    await slack.mention("Sam", f"<@{BOT_USER}> any ideas?")
    assert methods(slack) == [SET_STATUS, "reactions.add", "chat.postMessage"]
    slack.fake.clear()
    await slack.mention("Sam", f"<@{BOT_USER}> and now?")
    assert methods(slack) == ["chat.postMessage"]


@pytest.mark.flag("assistant", on=True)
async def test_a_reaction_already_there_or_gone_counts_as_done(slack: Slack) -> None:
    slack.fake.fail(SET_STATUS, Failure("internal_error"))
    slack.fake.fail("reactions.add", Failure("already_reacted"))
    slack.fake.fail("reactions.remove", Failure("no_reaction"))
    await slack.dm("Sam", "Hi")
    assert methods(slack) == [SET_STATUS, "reactions.add", "chat.postMessage", "reactions.remove"]


@pytest.mark.flag("assistant", on=True)
async def test_a_slow_status_never_holds_up_the_reply(slack: Slack) -> None:
    held = slack.fake.gates[SET_STATUS] = asyncio.Event()  # Slack hasn't answered yet
    root = next_ts()
    await send(slack.conn.app, "events_api", message_event("U02SAM", "Hi", channel=SAM_DM, ts=root))
    await until(lambda: bool(slack.fake.of("chat.postMessage")))  # posted while setStatus waits
    assert methods(slack) == [SET_STATUS, "chat.postMessage"]
    held.set()
    await slack.conn.interactions.settle()
    assert steps(slack)[-1] == status_cleared(SAM_DM, root)  # cleared once Slack answered


@pytest.mark.flag("assistant", on=True)
async def test_a_status_that_times_out_falls_back_and_is_cleared_too(
    slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(working, "STEP_SECONDS", 0.01)
    slack.fake.gates[SET_STATUS] = asyncio.Event()  # never set: Slack doesn't answer
    root = next_ts()
    await slack.dm("Sam", "Hi", ts=root)
    calls = steps(slack)
    assert sorted(methods(slack)[1:3]) == ["chat.postMessage", "reactions.add"]  # in either order
    assert calls[3:] == [status_cleared(SAM_DM, root), reacted("reactions.remove", SAM_DM, root)]  # it may have landed
    slack.fake.clear()
    await slack.dm("Sam", "Again")  # a timeout isn't a refusal, so the status is tried again
    assert methods(slack) == [SET_STATUS, "chat.postMessage", SET_STATUS]


@pytest.mark.flag("assistant", on=True)
async def test_a_status_lost_in_transit_falls_back_and_is_cleared_too(slack: Slack) -> None:
    slack.fake.fail(SET_STATUS, Failure(raises=aiohttp.ClientConnectionError("reset")))
    root = next_ts()
    await slack.dm("Sam", "Hi", ts=root)
    assert methods(slack) == [SET_STATUS, "reactions.add", "chat.postMessage", SET_STATUS, "reactions.remove"]
    assert steps(slack)[3] == status_cleared(SAM_DM, root)  # Slack may have set it before the error


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("failures", [1, 2])
async def test_a_failed_reaction_removal_is_tried_once_more(slack: Slack, failures: int) -> None:
    slack.fake.fail(SET_STATUS, Failure("method_not_supported_for_channel_type"))
    slack.fake.fail("reactions.remove", *[Failure("internal_error")] * failures)
    await slack.mention("Sam", f"<@{BOT_USER}> hi")
    assert methods(slack)[-2:] == ["reactions.remove", "reactions.remove"]
    assert methods(slack).count("reactions.remove") == 2  # and no more


@pytest.mark.flag("assistant", on=True)
async def test_a_refusal_is_forgotten_after_an_hour(house: House, slack: Slack) -> None:
    slack.fake.fail(SET_STATUS, Failure("method_not_supported_for_channel_type"))
    slack.fake.fail("reactions.add", Failure("missing_scope"))  # reactions:write not added yet
    await slack.mention("Sam", f"<@{BOT_USER}> any ideas?")
    house.clock.advance(REFUSAL_TTL - timedelta(minutes=1))
    slack.fake.clear()
    await slack.mention("Sam", f"<@{BOT_USER}> and now?")
    assert methods(slack) == ["chat.postMessage"]
    house.clock.advance(timedelta(minutes=1))  # the operator has reinstalled with reactions:write
    slack.fake.fail(SET_STATUS, Failure("method_not_supported_for_channel_type"))
    slack.fake.clear()
    await slack.mention("Sam", f"<@{BOT_USER}> and now?")
    assert methods(slack) == [SET_STATUS, "reactions.add", "chat.postMessage", "reactions.remove"]


class Held(FakeModel):
    """Holds each request until its own event, in ``held``, is set."""

    def __init__(self) -> None:
        super().__init__()
        self.held: list[asyncio.Event] = []

    async def create(self, request: Mapping[str, Any], *, time_left: float) -> ModelReply:
        gate = asyncio.Event()
        self.held.append(gate)
        await gate.wait()
        return await super().create(request, time_left=time_left)


@pytest.mark.flag("assistant", on=True)
async def test_two_answers_in_one_thread_clear_the_status_only_after_the_last(slack: Slack) -> None:
    held = Held()
    assert slack.conn.assistant is not None
    object.__setattr__(slack.conn.assistant.assistant, "client", held)
    root = next_ts()
    app = slack.conn.app
    first = mention_event("U02SAM", f"<@{BOT_USER}> hi", ts=root)
    await send(app, "events_api", first)
    await until(lambda: len(held.held) == 1)
    second = mention_event("U03KIM", f"<@{BOT_USER}> me too", thread_ts=root)
    await send(app, "events_api", second)
    await until(lambda: len(held.held) == 2 and len(slack.fake.of(SET_STATUS)) == 2)
    held.held[0].set()
    await until(lambda: len(slack.fake.of("chat.postMessage")) == 1 and len(slack.fake.of(SET_STATUS)) == 3)
    held.held[1].set()
    await slack.conn.interactions.settle()
    assert methods(slack) == [SET_STATUS, SET_STATUS, "chat.postMessage", SET_STATUS, "chat.postMessage", SET_STATUS]
    statuses = [call for call in steps(slack) if call[0] == SET_STATUS]
    # Sam's reply cleared the status, so it's set again for Kim's; only Kim's clears it.
    assert statuses == [status_set(CHANNEL, root)] * 3 + [status_cleared(CHANNEL, root)]
    assert slack.conn.assistant.indicators.threads == {}


@pytest.mark.flag("assistant", on=True)
async def test_a_message_in_someone_elses_conversation_gets_no_indicator(house: House, slack: Slack) -> None:
    root = next_ts()
    await slack.dm("Sam", "Hi", ts=root)
    await link(house, "Sam", None)  # the admin moves Sam's Slack user to Kim
    await link(house, "Kim", SLACK_IDS["Sam"])
    slack.fake.clear()
    await slack.dm("Sam", "Still me?", thread_ts=root)  # now Kim, in Sam's old DM thread
    assert steps(slack) == []


# Logs --------------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_failures_are_logged_by_type_without_the_message(
    slack: Slack, caplog: pytest.LogCaptureFixture, house: House
) -> None:
    caplog.set_level(logging.INFO, logger="dinnerbell.channels.slack.working")
    slack.fake.fail(SET_STATUS, Failure("method_not_supported_for_channel_type"))
    slack.fake.fail("reactions.add", Failure("missing_scope"))
    await slack.dm("Sam", SECRET)
    failed = [r for r in caplog.records if r.getMessage() == "slack working indicator call failed"]
    assert [(r.slack_method, r.error, r.slack_error) for r in failed] == [  # type: ignore[attr-defined]
        (SET_STATUS, "SlackUnavailable", "method_not_supported_for_channel_type"),
        ("reactions.add", "SlackAuthError", "missing_scope"),
    ]
    assert all(r._member_id == house["Sam"].id for r in failed)  # type: ignore[attr-defined]
    assert "Zq7x" not in caplog.text
    assert "not trying it there for now" in caplog.text
