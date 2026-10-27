# SPDX-License-Identifier: AGPL-3.0-or-later
"""Logs that tell the story of a call (#75): one line per action and per refusal, with the same
fields everywhere, grouped by request ID and channel, and nothing members wrote."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from dinnerbell.channels.base import NullChannel
from dinnerbell.channels.slack import ids
from dinnerbell.config import Settings
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import (
    NOT_IN_TO_ADD,
    AssignmentRole,
    EventRuleViolation,
    EventState,
    IllegalTransition,
    StaleRun,
    Status,
)
from dinnerbell.logs import JsonFormatter, channel_var, request_id_var, start_unit, subject_var
from dinnerbell.scheduler import runner
from dinnerbell.services import activity, calls
from dinnerbell.services.call_context import locked_event
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.health import SchedulerState
from tests.conftest import MEMBER_TEXT, leaks, sign_in
from tests.support.calls import OPENS, RESPOND_BY, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.fake_slack import state
from tests.support.slack import Slack, wire
from tests.support.web import submit

CALL_FIELDS = ("event_id", "run", "slot", "day", "state_before", "state_after")
WHO = ("member_id", "member")
PLACE = ("item", "mode")
NOTE = "Pizza at GrandmaZq7x's"


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


def lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Every activity line, as production writes it."""
    return [json.loads(JsonFormatter().format(r)) for r in caplog.records if r.name == activity.log.name]


def proposal_form(mode: str, name: str, cuisine_id: int) -> dict[str, Any]:
    return state(
        mode={"selected_option": {"value": mode}},
        free_text={"value": name},
        cuisine={"selected_option": {"value": str(cuisine_id)}},
    )


@pytest.mark.anyio
async def test_a_call_tells_its_story_one_line_per_action(
    slack: Slack, house: House, caplog: pytest.LogCaptureFixture
) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    await slack.click("Alex", "respond_in", str(event_id))
    await slack.click("Alex", "offer_pickup", str(event_id))
    await slack.click("Sam", "respond_in", str(event_id))
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    mexican = house.tags["Mexican"].id
    await slack.submit("Sam", ids.PROPOSAL_VIEW, proposal_form("pickup", "Burrito Barn", mexican), event_id)
    # #74: Kim isn't in, so her pickup pick is refused, and the line says why and for what.
    await slack.submit("Kim", ids.PROPOSAL_VIEW, proposal_form("pickup", "Noodle Hut", mexican), event_id)
    assert slack.ephemerals("Kim")[-1] == NOT_IN_TO_ADD
    ctx = await house.context(event_id)
    (burritos,) = [p for p in ctx.proposals if p.free_text == "Burrito Barn"]
    await slack.click("Sam", "vote", ids.proposal_value(event_id, burritos.id))
    await slack.click("Alex", "lock", ids.proposal_value(event_id, burritos.id))  # Alex is the admin
    async with house.sessionmaker() as db:
        now = house.clock()
        await calls.change_assignment(db, house["Alex"], event_id, AssignmentRole.PICKUP, house["Sam"].id, now=now)
        await calls.cancel_call(db, house["Alex"], event_id, note=NOTE, now=now)

    logged = lines(caplog)
    names = [line["msg"] for line in logged]
    assert names == [
        "opened",
        "answer",
        "offer_role",
        "answer",
        "voting",
        "propose",
        "refused",
        "vote",
        "lock",
        "assign",
        "call_off",
    ]
    by_name = {line["msg"]: line for line in logged}
    for name, line in by_name.items():
        if name == "refused":
            continue
        made = name == "opened"  # the scheduler made the call, so it had no state before
        assert all(line.get(key) is not None for key in CALL_FIELDS if not (made and key == "state_before")), line
        assert (line["event_id"], line["run"], line["slot"], line["day"]) == (event_id, 1, "dinner", "2026-10-22")
        if name not in {"opened", "voting"}:  # the scheduler's
            assert all(line.get(key) is not None for key in WHO), line
        if name in {"propose", "vote", "lock"}:
            assert (line["item"], line["mode"]) == ("Burrito Barn", "pickup"), line
    assert by_name["answer"]["answer"] == "in"
    assert (by_name["offer_role"]["role"], by_name["offer_role"]["offered"]) == ("pickup", True)
    assert (by_name["voting"]["state_before"], by_name["voting"]["state_after"]) == ("collecting", "voting")
    assert (by_name["lock"]["state_before"], by_name["lock"]["state_after"]) == ("voting", "decided")
    assert (by_name["assign"]["role"], by_name["assign"]["to_member"]) == ("pickup", "Sam")
    call_off = by_name["call_off"]
    assert (call_off["state_before"], call_off["state_after"], call_off["member"]) == ("decided", "cancelled", "Alex")
    assert call_off["note_length"] == len(NOTE)
    refused = by_name["refused"]
    assert refused["level"] == "INFO"
    assert (refused["reason"], refused["refusal"], refused["member"]) == ("not_in", NOT_IN_TO_ADD, "Kim")
    assert (refused["event_id"], refused["state"], refused["item"], refused["mode"]) == (
        event_id,
        "voting",
        "Noodle Hut",
        "pickup",
    )
    assert refused["channel"] == "slack"
    everything = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert "GrandmaZq7x" not in everything  # a call-off's reason is logged by length (SEC-5)


@pytest.mark.anyio
async def test_one_taps_lines_share_its_request_id_and_channel(
    slack: Slack, house: House, caplog: pytest.LogCaptureFixture
) -> None:
    house.clock.now = OPENS
    await house.tick()
    await slack.click("Sam", "respond_in", "1")
    await slack.click("Kim", "respond_in", "1")
    records = [json.loads(JsonFormatter().format(r)) for r in caplog.records]
    taps = [r for r in records if r["msg"] == "slack action"]
    answers = [r for r in records if r["msg"] == "answer"]
    assert [a["member"] for a in answers] == ["Sam", "Kim"]
    for tap, answer in zip(taps, answers, strict=True):
        assert tap["request_id"] == answer["request_id"]
        assert tap["channel"] == answer["channel"] == "slack"
        assert answer["member_id"] == tap["member_id"]
    assert taps[0]["request_id"] != taps[1]["request_id"]


def test_a_web_requests_lines_share_its_request_id_and_channel(
    client: TestClient, provider: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    sign_in(client, provider)
    started = submit(client, "/events", {"slot": "dinner", "day": "2026-10-22"})
    event_id = int(started.headers["location"].rsplit("/", 1)[1])
    answered = submit(client, f"/events/{event_id}/respond", {"status": "in"})
    refused = submit(client, f"/events/{event_id}/proposals", {"mode": "pickup", "free_text": "Noodle Hut"})
    assert refused.status_code == 409
    logged = lines(caplog)
    by_name = {line["msg"]: line for line in logged}
    assert by_name["start"]["request_id"] == started.headers["X-Request-ID"]
    assert by_name["answer"]["request_id"] == answered.headers["X-Request-ID"]
    assert by_name["refused"]["request_id"] == refused.headers["X-Request-ID"]
    assert {line["channel"] for line in logged} == {"web"}
    assert by_name["start"]["state_after"] == "collecting" and "state_before" not in by_name["start"]
    assert (by_name["refused"]["reason"], by_name["refused"]["path"]) == (
        "voting_closed",
        f"/events/{event_id}/proposals",
    )
    assert by_name["refused"]["state"] == "collecting"


@pytest.mark.anyio
async def test_a_rolled_back_action_is_never_logged_as_done(house: House, caplog: pytest.LogCaptureFixture) -> None:
    house.clock.now = OPENS
    await house.tick()
    caplog.clear()
    async with house.sessionmaker() as db:
        with pytest.raises(StaleRun):
            await calls.cancel_call(db, house["Alex"], 1, run=2, now=house.clock())
    assert lines(caplog) == []


@pytest.mark.anyio
async def test_scheduler_transitions_say_why_a_call_ended(house: House, caplog: pytest.LogCaptureFixture) -> None:
    house.clock.now = OPENS
    await house.tick()
    house.clock.now = RESPOND_BY + timedelta(minutes=1)
    await house.tick()  # nobody answered
    (ended,) = [line for line in lines(caplog) if line["msg"] == "abandoned"]
    assert (ended["state_before"], ended["state_after"], ended["end_reason"]) == ("collecting", "abandoned", "too_few")
    assert ended["step"] == "close_responses" and "member" not in ended


def test_every_refusal_has_a_stable_reason_code() -> None:
    assert activity.reason(EventRuleViolation("Nope.")) == "rule"
    assert activity.reason(EventRuleViolation("Nope.", code="not_in")) == "not_in"
    assert activity.reason(StaleRun("Moved on.")) == "stale_run"
    assert activity.reason(ValueError("x")) == "value_error"


def test_a_unit_starts_fresh_and_restores_what_was_before() -> None:
    end = start_unit("slack")
    activity.about(item="Noodle Hut", mode=Mode.PICKUP)
    inner = start_unit("scheduler")
    assert subject_var.get() is None
    inner()
    assert subject_var.get() == {"item": "Noodle Hut", "mode": "pickup"}
    end()
    assert subject_var.get() is None


@pytest.mark.anyio
async def test_a_delivery_line_names_the_call_and_the_posted_message(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    house.clock.now = OPENS
    await house.tick()
    channel = RecordingChannel()
    await house.drain(channel)
    records = [json.loads(JsonFormatter().format(r)) for r in caplog.records]
    (posted,) = [r for r in records if r["msg"] == "notification delivered" and r["kind"] == "call_opened"]
    assert (posted["event_id"], posted["channel_id"], posted["message_ts"]) == (1, "C1", "1.0")
    assert posted["level"] == "INFO"


@pytest.mark.anyio
async def test_a_misnamed_field_is_refused_where_its_written(house: House) -> None:
    """A field named like a LogRecord attribute, or one of the line's own, would make ``logging``
    raise in the commit hook, or replace the line's timestamp."""
    house.clock.now = OPENS
    await house.tick()
    event = await house.event(1)
    async with house.sessionmaker() as db:
        for name in ("created", "module", "args", "ts", "msg", "channel"):
            with pytest.raises(ValueError, match=name):
                activity.action(db, "answer", event, None, **{name: 1})
        with pytest.raises(ValueError, match="created"):
            activity.refused(EventRuleViolation("No."), "No.", None, created=1)


@pytest.mark.anyio
async def test_a_line_that_cant_be_written_never_breaks_a_committed_action(
    house: House, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    house.clock.now = OPENS
    await house.tick()
    monkeypatch.setattr(activity, "_checked", lambda fields: fields)  # let a bad name through
    async with house.sessionmaker() as db:
        async with locked_event(db, house.household.id, 1, house.clock()) as event:
            activity.action(db, "answer", event, house["Kim"], module="oops")
        await calls.respond(db, house["Sam"], 1, Status.IN, now=house.clock())  # the session still works
    (failed,) = [r for r in caplog.records if r.getMessage() == "activity line failed"]
    assert failed.levelname == "ERROR" and failed.line == "answer"  # type: ignore[attr-defined]
    assert [line["member"] for line in lines(caplog) if line["msg"] == "answer"] == ["Sam"]


@pytest.mark.anyio
async def test_a_rolled_back_line_never_rides_on_the_next_commit(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    """A use case that fails after noting its line leaves nothing for the next one on the same
    session to write."""
    house.clock.now = OPENS
    await house.tick()
    caplog.clear()
    async with house.sessionmaker() as db:

        async def doomed() -> None:
            async with locked_event(db, house.household.id, 1, house.clock()) as event:
                activity.action(db, "doomed", event, house["Kim"])
                raise RuntimeError("after the line was noted")

        with pytest.raises(RuntimeError):
            await doomed()
        await calls.respond(db, house["Sam"], 1, Status.IN, now=house.clock())
    assert [line["msg"] for line in lines(caplog)] == ["answer"]


@pytest.mark.anyio
async def test_a_line_keeps_the_context_it_was_noted_in(house: House, caplog: pytest.LogCaptureFixture) -> None:
    house.clock.now = OPENS
    await house.tick()
    caplog.clear()
    async with house.sessionmaker() as db:
        async with locked_event(db, house.household.id, 1, house.clock()) as event:
            end = start_unit("web", "req-noted")
            activity.action(db, "answer", event, house["Kim"])
            end()
        # Committed in the test's own context, which has no request ID or channel.
        assert request_id_var.get() != "req-noted" and channel_var.get() is None
    (line,) = lines(caplog)
    assert (line["request_id"], line["channel"]) == ("req-noted", "web")


@pytest.mark.anyio
async def test_a_scheduler_job_run_has_its_own_request_id_and_channel(
    house: House, migrated: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    house.clock.now = OPENS
    context = runner.JobContext(
        state=SchedulerState(),
        clock=house.clock,
        zone=ZoneInfo(migrated.timezone),
        database_path=migrated.database_path,
        backup_dir=migrated.backup_dir,
        sessionmaker=house.sessionmaker,
        channel=NullChannel(),
    )
    job = runner._tracked(runner.call_tick)
    await job(context)
    house.clock.now = RESPOND_BY + timedelta(minutes=1)
    await job(context)
    opened, ended = [line for line in lines(caplog) if line["msg"] in {"opened", "abandoned"}]
    assert opened["channel"] == ended["channel"] == "scheduler"
    assert opened["request_id"] and ended["request_id"] and opened["request_id"] != ended["request_id"]
    delivered = [r for r in caplog.records if r.getMessage() == "notification delivered"]
    assert delivered and {getattr(r, "_request_id", None) for r in delivered} <= {
        opened["request_id"],
        ended["request_id"],
    }
    assert channel_var.get() is None and request_id_var.get() is None  # the run's unit ended with it


@pytest.mark.anyio
async def test_a_lost_race_is_logged_with_the_reason_its_reply_gives(
    slack: Slack, house: House, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    house.clock.now = OPENS
    await house.tick()

    async def finished(*_args: Any, **_kwargs: Any) -> None:
        raise IllegalTransition(EventState.DONE, EventState.CANCELLED)

    monkeypatch.setattr(calls, "respond", finished)
    await slack.click("Sam", "respond_in", "1")
    (refused,) = [line for line in lines(caplog) if line["msg"] == "refused"]
    assert (refused["reason"], refused["refusal"]) == ("call_finished", "This call already finished.")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("cancel", "no_call"),
        ("cancel lunch", "no_call"),
        ("call someday", "usage"),
        ("cancel dinner 2026-13-45", "usage"),
    ],
)
async def test_slash_command_refusals_have_codes(
    slack: Slack, caplog: pytest.LogCaptureFixture, text: str, reason: str
) -> None:
    await slack.command("Alex", text)
    (refused,) = [line for line in lines(caplog) if line["msg"] == "refused"]
    assert (refused["reason"], refused["member"], refused["channel"]) == (reason, "Alex", "slack")


def test_the_privacy_guard_reads_lines_as_they_print() -> None:
    assert leaks('{"msg": "x", "note": "a\\nkim@example.com"}')  # an email after a newline
    assert not leaks('{"msg": "Suggestion:\\n\\n@app.command(\\"/dinnerbell\\")"}')  # a decorator
    for secret in (MEMBER_TEXT, "subject-admin", "1234.5678.trigger", "xoxb-1-2", "?code=abc"):
        assert leaks(f'{{"msg": "{secret}"}}'), secret
    assert not leaks('{"msg": "Copy the Bot User OAuth Token (xoxb-) again"}')
