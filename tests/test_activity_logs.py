# SPDX-License-Identifier: AGPL-3.0-or-later
"""Logs that tell the story of a call (#75): one line per action and per refusal, with the same
fields everywhere, grouped by request ID and channel, and nothing members wrote."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dinnerbell.channels.slack import ids
from dinnerbell.config import Settings
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import NOT_IN_TO_ADD, AssignmentRole, EventRuleViolation, EventState, StaleRun
from dinnerbell.logs import JsonFormatter, start_unit, subject_var
from dinnerbell.services import activity, calls
from dinnerbell.services.database import Database, open_database
from tests.conftest import sign_in
from tests.support.calls import OPENS, RESPOND_BY, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.fake_slack import state
from tests.support.slack import Slack, wire
from tests.support.web import submit

CALL_FIELDS = ("event_id", "run", "slot", "day", "state_before", "state_after")
WHO = ("member_id", "member")
PLACE = ("item", "mode")
NOTE = "Pizza at Grandma's"


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
    assert "Grandma" not in everything  # a call-off's reason is logged by length (SEC-5)


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
