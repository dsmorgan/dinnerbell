# SPDX-License-Identifier: AGPL-3.0-or-later
"""Slack security (spec 13.5): only active members of this household, from this workspace and
channel, can act; tampered values reach nothing outside the household; tokens never reach
logs, exceptions, or the outbox."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import timedelta

import aiohttp
import pytest
from sqlalchemy import text

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.interactions import INACTIVE, OUT_OF_DATE, UNKNOWN_USER, WRONG_PLACE, WRONG_WORKSPACE
from dinnerbell.config import Settings
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.events import EventState
from dinnerbell.services.database import Database, open_database
from tests.support.calls import OPENS, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import APP_TOKEN, BOT_TOKEN, OTHER_TEAM, Failure, FakeSlack, state
from tests.support.households import other_call, other_household
from tests.support.slack import Slack, connection, wire

pytestmark = pytest.mark.anyio


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
    wired = await wire(house, linked=("Alex", "Sam", "Kim"))
    yield wired
    await wired.conn.stop()


async def open_call(house: House) -> int:
    house.clock.now = OPENS
    await house.tick()
    return await dinner_id(house)


async def responses(house: House, event_id: int) -> int:
    return len((await house.context(event_id)).responses)


async def test_an_unlinked_slack_user_is_refused(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("U04LEE", "respond_in", str(event_id))  # Lee has no Slack link
    await slack.click("U9STRANGER", "respond_in", str(event_id))
    assert await responses(house, event_id) == 0
    assert slack.ephemerals("U04LEE") == [UNKNOWN_USER]
    assert slack.ephemerals("U9STRANGER") == [UNKNOWN_USER]


async def test_an_inactive_member_is_refused(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    async with house.sessionmaker() as db:
        await MemberRepository(db).update(house.household.id, house["Kim"].id, house.clock(), active=False)
        await db.commit()
    await slack.click("Kim", "respond_in", str(event_id))
    await slack.command("Kim", "call lunch")
    assert await responses(house, event_id) == 0
    assert slack.ephemerals("Kim") == [INACTIVE, INACTIVE]
    await slack.open_home("Kim")
    assert INACTIVE in str(slack.last_home("Kim"))


@pytest.mark.parametrize("team", [OTHER_TEAM, None])
async def test_payloads_from_another_workspace_are_ignored(slack: Slack, house: House, team: str | None) -> None:
    event_id = await open_call(house)
    slack.fake.clear()
    response = await slack.click("Sam", "respond_in", str(event_id), team=team)
    assert response.status == 200  # acknowledged, then dropped
    values = state(note={"value": "hi"})
    await slack.submit("Sam", ids.DETAILS_VIEW, values, event_id, team=team)
    assert await responses(house, event_id) == 0
    assert slack.fake.calls == []


async def test_other_workspaces_get_no_command_or_home(slack: Slack, house: House) -> None:
    response = await slack.command("Sam", "call", team=OTHER_TEAM)
    assert '"text": "' + WRONG_WORKSPACE in response.body.replace('":"', '": "')
    await slack.open_home("Sam", team=OTHER_TEAM)
    assert slack.fake.calls == []
    house.clock.now = OPENS
    await house.tick()
    async with house.sessionmaker() as db:
        assert (await db.execute(text("SELECT count(*) FROM event"))).scalar_one() == 1  # only the scheduled one


async def test_nothing_is_accepted_before_the_workspace_is_known(house: House) -> None:
    fake = FakeSlack()
    conn = connection(house, fake)  # checks haven't run yet
    from tests.support.fake_slack import block_action, send

    event_id = await open_call(house)
    await send(conn.app, "interactive", block_action("U01ALEX", "respond_in", str(event_id)))
    await conn.interactions.settle()
    assert await responses(house, event_id) == 0
    assert fake.calls == []


async def test_buttons_from_other_channels_are_refused(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    for channel in ("C0OTHER", "G0PRIVATE", None):
        await slack.click("Sam", "respond_in", str(event_id), channel=channel)
    assert await responses(house, event_id) == 0
    assert [c.args["text"] for c in slack.fake.of("chat.postMessage")] == [WRONG_PLACE] * 3
    assert slack.fake.of("conversations.open") == []  # refused on sight, before asking Slack about DMs


async def test_a_modal_payload_without_a_view_type_is_dropped(slack: Slack, house: House) -> None:
    from tests.support.fake_slack import block_action

    event_id = await open_call(house)
    payload = block_action("U02SAM", "respond_in", str(event_id), home=True)
    payload["view"]["type"] = "modal"
    await slack._send("interactive", payload)
    assert await responses(house, event_id) == 0
    assert slack.fake.calls == []


@pytest.mark.parametrize(
    ("action", "value"),
    [
        ("respond_in", "abc"),
        ("respond_in", "0"),
        ("respond_in", "99999999999999999999"),
        ("respond_in", "-3"),
        ("respond_in", None),
        ("vote", "7"),
        ("vote", "1:2:3"),
        ("start_call_dinner", "breakfast"),
        ("unknown_action", "7"),
        ("details", "7"),
    ],
)
async def test_values_this_app_did_not_write_are_refused(
    slack: Slack, house: House, action: str, value: str | None
) -> None:
    event_id = await open_call(house)
    payload_value = value
    if action == "details":  # a valid event, but no trigger to open a modal with
        from tests.support.fake_slack import block_action

        payload = block_action("U02SAM", action, str(event_id))
        del payload["trigger_id"]
        await slack._send("interactive", payload)
    else:
        await slack.click("Sam", action, payload_value)
    assert await responses(house, event_id) == 0
    assert slack.ephemerals("Sam") == [OUT_OF_DATE]


async def test_another_households_ids_are_not_found(slack: Slack, house: House, migrated: Settings) -> None:
    theirs = other_call(migrated, other_household(migrated))
    event, proposal = theirs["event"], theirs["proposal"]
    await slack.click("Alex", "respond_in", str(event))
    await slack.click("Alex", "vote", ids.proposal_value(event, proposal))
    await slack.click("Alex", "lock", ids.proposal_value(event, proposal))
    await slack.click("Alex", "mark_done", str(event))
    await slack.click("Alex", "details", str(event))
    assert slack.ephemerals("Alex") == ["No such call."] * 5
    assert slack.fake.of("views.open") == []

    # Our own event with their proposal.
    ours = await open_call(house)
    await slack.click("Alex", "respond_in", str(ours))
    await slack.click("Alex", "offer_pickup", str(ours))
    await slack.click("Sam", "respond_in", str(ours))
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(ours)).state is EventState.VOTING
    await slack.click("Alex", "lock", ids.proposal_value(ours, proposal))
    await slack.click("Alex", "vote", ids.proposal_value(ours, proposal))
    assert slack.ephemerals("Alex")[-2:] == ["No such proposal."] * 2
    assert (await house.event(ours)).state is EventState.VOTING

    # And a modal whose metadata names their event.
    values = state(note={"value": "hijack"})
    await slack.submit("Sam", ids.DETAILS_VIEW, values, event)
    assert slack.ephemerals("Sam")[-1] == "No such call."
    async with house.sessionmaker() as db:
        rows = (await db.execute(text("SELECT count(*) FROM response WHERE event_id = :e"), {"e": event})).scalar_one()
    assert rows == 0


@pytest.mark.parametrize(
    "metadata",
    ['{"event": 1}', "not json", '{"event": "1", "channel": null}', '{"event": 1, "channel": "#general"}', "[]"],
)
async def test_tampered_modal_metadata_is_dropped(slack: Slack, house: House, metadata: str) -> None:
    from tests.support.fake_slack import view_submission

    event_id = await open_call(house)
    payload = view_submission("U02SAM", ids.DETAILS_VIEW, state(note={"value": "x"}), metadata)
    response = await slack._send("interactive", payload)
    assert response.status == 200
    assert await responses(house, event_id) == 0


@pytest.mark.parametrize(
    "values",
    [
        state(guest_needs={"selected_options": [{"value": "x"}]}),
        state(guest_needs={"selected_options": "nope"}),
        state(eat_from={"selected_time": "25:99"}),
        state(eat_from={"selected_time": "1830"}),
        state(suggest_item={"selected_option": {"value": "1; DROP TABLE"}}),
    ],
)
async def test_tampered_modal_values_are_dropped(slack: Slack, house: House, values: dict[str, object]) -> None:
    event_id = await open_call(house)
    await slack.submit("Sam", ids.DETAILS_VIEW, values, event_id)
    assert await responses(house, event_id) == 0


async def test_slash_call_by_a_stranger_is_refused(slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await slack.command("U9STRANGER", "call lunch")
    assert slack.ephemerals("U9STRANGER") == [UNKNOWN_USER]


async def test_tokens_never_reach_logs_errors_or_the_outbox(house: House, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    slack = await wire(house)
    fake = slack.fake
    # Every kind of failure, including exceptions whose own text carries a token.
    fake.fail("chat.postMessage", Failure(raises=aiohttp.ClientConnectionError(f"bad {BOT_TOKEN} {APP_TOKEN}")))
    fake.fail("chat.postMessage", Failure("ratelimited", status=429, headers={"Retry-After": "1"}))
    fake.fail("chat.postMessage", Failure("invalid_auth"))
    fake.fail("chat.postMessage", Failure(raises=TimeoutError(BOT_TOKEN)))
    house.clock.now = OPENS
    await house.tick()
    for _ in range(4):
        await slack.drain()
        house.clock.advance(timedelta(hours=1))
    event_id = await dinner_id(house)
    fake.fail("chat.postEphemeral", Failure(raises=OSError(BOT_TOKEN)))
    fake.fail("chat.postMessage", Failure(raises=OSError(BOT_TOKEN)))
    await slack.click("U9STRANGER", "respond_in", str(event_id))

    broken = FakeSlack()
    broken.fail("auth.test", Failure(raises=OSError(BOT_TOKEN)), Failure("invalid_auth"))
    broken.fail("apps.connections.open", Failure("invalid_auth"))
    checks = connection(house, broken)
    for _ in range(3):
        assert not await checks.run_once()

    async with house.sessionmaker() as db:
        errors = [e for (e,) in (await db.execute(text("SELECT last_error FROM outbox"))).all() if e]
    assert errors
    everything = "\n".join([*errors, caplog.text, *(str(r.__dict__) for r in caplog.records)])
    # The texts that could carry a token were all captured: Slack's refusals, and the errors.
    assert "Slack refused DINNERBELL_SLACK_BOT_TOKEN" in caplog.text
    assert any(e.startswith(("SlackUnavailable", "HeldDelivery")) for e in errors)
    for secret in (BOT_TOKEN, APP_TOKEN, BOT_TOKEN.split("-", 1)[1], APP_TOKEN.split("-", 1)[1]):
        assert secret not in everything
    await slack.conn.stop()


async def test_strangers_on_the_home_tab_and_in_modals(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("U9STRANGER", "respond_in", str(event_id), home=True)
    assert UNKNOWN_USER in str(slack.last_home("U9STRANGER"))
    await slack.submit("U9STRANGER", ids.DETAILS_VIEW, state(note={"value": "x"}), event_id)
    assert slack.ephemerals("U9STRANGER") == [UNKNOWN_USER]
    assert await responses(house, event_id) == 0


async def test_payloads_without_a_user_are_dropped(slack: Slack, house: House) -> None:
    from tests.support.fake_slack import block_action, slash, view_submission

    event_id = await open_call(house)
    action = block_action("U02SAM", "respond_in", str(event_id))
    action["user"] = None
    await slack._send("interactive", action)
    submission = view_submission("U02SAM", ids.DETAILS_VIEW, {}, ids.ViewOrigin(event_id).encode())
    submission["user"] = {}
    await slack._send("interactive", submission)
    command = slash("U02SAM", "call")
    del command["user_id"]
    await slack._send("slash_commands", command)
    assert slack.fake.calls == []
    assert await responses(house, event_id) == 0
    assert ids.ViewOrigin.decode('{"event": 1, "channel": null, "home": true}') == ids.ViewOrigin(1, home=True)
    with pytest.raises(ids.BadValue):
        ids.ViewOrigin.decode(None)


async def test_a_dm_button_only_works_in_that_members_own_dm(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Sam", "respond_in", str(event_id), channel="D04LEE")  # Lee's DM, not Sam's
    assert await responses(house, event_id) == 0
    assert [c.args["text"] for c in slack.fake.of("chat.postMessage")] == [WRONG_PLACE]
    opened = slack.fake.of("conversations.open")
    assert [c.args for c in opened] == [{"users": "U02SAM", "prevent_creation": "1"}]

    await slack.click("Sam", "respond_in", str(event_id), channel="D02SAM")  # Sam's own DM
    assert await responses(house, event_id) == 1
    assert len(slack.fake.of("conversations.open")) == 1  # remembered


@pytest.mark.parametrize("failure", [Failure("internal_error", status=500), Failure(raises=TimeoutError())])
async def test_a_dm_that_cant_be_checked_is_refused(slack: Slack, house: House, failure: Failure) -> None:
    event_id = await open_call(house)
    slack.fake.fail("conversations.open", failure)
    await slack.click("Lee", "respond_in", str(event_id), channel="D04LEE")
    assert await responses(house, event_id) == 0


async def test_a_dm_slack_says_doesnt_exist_is_refused(
    slack: Slack, house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await open_call(house)

    async def none(user: str) -> None:
        return None

    monkeypatch.setattr(slack.conn.api, "dm_channel", none)
    await slack.click("Lee", "respond_in", str(event_id), channel="D04LEE")
    assert await responses(house, event_id) == 0
