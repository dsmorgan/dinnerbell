# SPDX-License-Identifier: AGPL-3.0-or-later
"""Call it off and Close now in Slack (FR-56, issue #11): the ⋯ menu on the call and decision
messages, the Call it off modal, Close now's confirmation, the App Home buttons, and
``/dinnerbell cancel``. The services check every rule; these tests check that Slack shows each
person only what applies, refuses the rest with the rule's own words, and never trusts a value
it didn't write."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.interactions import (
    CALLED_OFF,
    CLOSED,
    HELP,
    NO_OPEN_CALL,
    OUT_OF_DATE,
    WHICH_CALL,
    parse_cancel_args,
)
from dinnerbell.channels.slack.views import FormError, parse_cancel
from dinnerbell.config import Settings
from dinnerbell.domain.events import STALE_CALL, WINDOW_MOVED, EventRuleViolation, EventState, Window, WindowMoved
from dinnerbell.domain.meals import Slot
from dinnerbell.services import call_admin, calls, event_views
from dinnerbell.services.database import Database, open_database
from tests.support.calls import DONE_AT, OPENS, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import CHANNEL, block_action, send, slash, state, view_submission
from tests.support.households import other_call, other_household
from tests.support.slack import Slack, wire

pytestmark = pytest.mark.anyio

ONLY_SOME = "Only the coordinator, whoever started this call, or an admin can call it off."
DECIDED_NOW = "It's decided now, so only the coordinator or an admin can call it off."
NOT_CLOSER = "Only the coordinator or an admin can close it now."
FINISHED = "This call already finished."


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


async def daily_call(house: House) -> int:
    """Today's dinner, opened by the scheduler: nobody started it."""
    house.clock.now = OPENS
    await house.tick()
    return await dinner_id(house)


async def kims_call(slack: Slack) -> int:
    """Today's dinner, started by Kim, a plain member."""
    slack.house.clock.now = OPENS
    await slack.command("Kim", "call")
    return await dinner_id(slack.house)


async def to_voting(slack: Slack, event_id: int) -> None:
    await slack.click("Alex", "respond_in", str(event_id))
    await slack.click("Alex", "offer_pickup", str(event_id))
    await slack.click("Sam", "respond_in", str(event_id))
    slack.house.clock.now = RESPOND_BY
    await slack.house.tick()
    assert (await slack.house.event(event_id)).state is EventState.VOTING


async def to_decided(slack: Slack, event_id: int) -> None:
    await to_voting(slack, event_id)
    first = (await slack.house.context(event_id)).proposals[0].id
    await slack.click("Alex", "lock", ids.proposal_value(event_id, first))
    assert (await slack.house.event(event_id)).state is EventState.DECIDED


def cancel(event_id: int, run: int = 1) -> str:
    return ids.menu_value(ids.MenuChoice.CANCEL, event_id, run)


def close(event_id: int, run: int = 1) -> str:
    return ids.menu_value(ids.MenuChoice.CLOSE, event_id, run)


def closes(window: Window, event_id: int, run: int = 1) -> str:
    return ids.window_value(window, event_id, run)


async def call_off(slack: Slack, who: str, values: dict[str, Any], event_id: int, run: int = 1) -> Any:
    """Submit the Call it off modal as opened in the channel for ``run``."""
    origin = ids.ViewOrigin(event_id, CHANNEL, run=run)
    return await slack.submit(who, ids.CANCEL_VIEW, values, event_id, origin=origin)


def opened(slack: Slack) -> list[dict[str, Any]]:
    return [c.args["view"] for c in slack.fake.of("views.open")]


def body(response: object) -> dict[str, object]:
    raw = getattr(response, "body", "")
    return json.loads(raw) if raw else {}


def reason(text: str | None) -> dict[str, Any]:
    return state(reason={"type": "plain_text_input", "value": text})


def home_actions(slack: Slack, who: str) -> list[str]:
    home = slack.last_home(who)
    return [e["action_id"] for b in home["blocks"] if b["type"] == "actions" for e in b["elements"]]


# Values ------------------------------------------------------------------------------------------


def test_menu_and_window_values_round_trip() -> None:
    assert cancel(7, 2) == "cancel:7:2"
    assert ids.parse_menu(cancel(7)) == (ids.MenuChoice.CANCEL, 7, 1)
    assert ids.parse_menu(close(2**63 - 1, 3)) == (ids.MenuChoice.CLOSE, 2**63 - 1, 3)
    assert ids.parse_window(closes(Window.RESPOND, 7)) == (Window.RESPOND, 7, 1)
    assert ids.parse_window(closes(Window.VOTE, 8, 2)) == (Window.VOTE, 8, 2)


@pytest.mark.parametrize(
    "value",
    [
        None,
        7,
        "",
        "7",
        "cancel",
        "cancel:",
        "cancel:0",
        "cancel:007",
        "cancel:-7",
        "cancel:7",  # the two-part form, from before values named the run
        "close:7",
        "cancel:7:1 ",
        " cancel:7:1",
        "cancel:7:0",
        "cancel:7:01",
        "cancel:7:1:2",
        "cancel::1",
        "CANCEL:7:1",
        "drop:7:1",
        "respond:7:1",
        "cancel:99999999999999999999:1",
        "cancel:9223372036854775808:1",
        "cancel:7:9223372036854775808",
        "cancel:7:1\n",
    ],
)
def test_menu_values_this_app_did_not_write_are_refused(value: object) -> None:
    with pytest.raises(ids.BadValue):
        ids.parse_menu(value)


@pytest.mark.parametrize(
    "value",
    [None, "7", "respond:7", "vote:7", "cancel:7:1", "close:7:1", "respond:0:1", "vote:x:1", "Vote:7:1", "respond:7:0"],
)
def test_window_values_this_app_did_not_write_are_refused(value: object) -> None:
    with pytest.raises(ids.BadValue):
        ids.parse_window(value)


def test_a_trailing_newline_is_not_the_value_this_app_wrote() -> None:
    """``$`` matches before a final newline; every parser matches the whole value instead."""
    for parse, value in (
        (ids.parse_event, "7\n"),
        (ids.parse_proposal, "7:8\n"),
        (ids.parse_window, "respond:7:1\n"),
    ):
        with pytest.raises(ids.BadValue):
            parse(value)
    assert not ids.is_channel_id("C0DINNER\n")


def test_call_off_metadata_carries_its_run_and_window_and_nothing_else() -> None:
    origin = ids.ViewOrigin(7, "C0DINNER", window=Window.VOTE, run=3)
    assert ids.ViewOrigin.decode(origin.encode()) == origin
    assert ids.ViewOrigin.decode(ids.ViewOrigin(7, run=2).encode()) == ids.ViewOrigin(7, run=2)
    plain = ids.ViewOrigin(7, "C0DINNER").encode()
    assert "window" not in plain and "run" not in plain  # other modals' metadata unchanged
    for bad in (
        '{"event":7,"channel":null,"home":false,"run":0}',
        '{"event":7,"channel":null,"home":false,"run":"1"}',
        '{"event":7,"channel":null,"home":false,"run":true}',
        '{"event":7,"channel":null,"home":false,"run":null}',
        '{"event":7,"channel":null,"home":false,"run":9223372036854775808}',
        '{"event":7,"channel":null,"home":false,"window":"later"}',
        '{"event":7,"channel":null,"home":false,"window":null}',
        '{"event":7,"channel":null,"home":false,"window":["vote"]}',
        '{"event":7,"channel":null,"home":false,"window":"vote","extra":1}',
    ):
        with pytest.raises(ids.BadValue):
            ids.ViewOrigin.decode(bad)


def test_the_reason_field() -> None:
    assert parse_cancel(reason("Pizza at Grandma's")) == "Pizza at Grandma's"
    assert parse_cancel(reason(None)) is None
    assert parse_cancel(reason("")) is None
    assert parse_cancel({}) is None
    assert parse_cancel(reason("x" * 140)) == "x" * 140
    with pytest.raises(FormError) as raised:
        parse_cancel(reason("x" * 141))  # Slack enforces max_length, so only a tampered payload
    assert raised.value.block_id == "reason"


def test_parse_cancel_args() -> None:
    today = date(2026, 10, 22)
    assert parse_cancel_args([], today) == (None, None, None)
    assert parse_cancel_args(["Lunch"], today) == (Slot.LUNCH, None, None)
    assert parse_cancel_args(["tomorrow", "dinner"], today) == (Slot.DINNER, date(2026, 10, 23), None)
    assert parse_cancel_args(["dinner", "2026-10-24", "Pizza", "at", "Grandma's"], today) == (
        Slot.DINNER,
        date(2026, 10, 24),
        "Pizza at Grandma's",
    )
    # Only the first slot and day count, so a reason can start with one; it keeps its case.
    assert parse_cancel_args(["dinner", "Lunch", "ran", "LATE"], today) == (Slot.DINNER, None, "Lunch ran LATE")
    assert parse_cancel_args(["today", "Tomorrow", "instead"], today) == (None, today, "Tomorrow instead")
    assert parse_cancel_args(["Sick", "today"], today) == (None, None, "Sick today")
    # Slack escapes these three in a command's text; the reason gets them back.
    assert parse_cancel_args(["R&amp;D", "&lt;3", "&gt;_&lt;"], today) == (None, None, "R&D <3 >_<")
    with pytest.raises(EventRuleViolation) as raised:
        parse_cancel_args(["2026-13-01"], today, command="/dinnerbell-test")
    assert str(raised.value).startswith("Try `/dinnerbell-test cancel`")


# The ⋯ menu --------------------------------------------------------------------------------------


async def test_a_plain_member_is_told_who_can(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await slack.choose("Kim", cancel(event_id))
    await slack.choose("Kim", close(event_id))
    assert opened(slack) == []
    assert slack.ephemerals("Kim") == [ONLY_SOME, NOT_CLOSER]
    assert slack.fake.of("chat.postEphemeral")[0].args["channel"] == CHANNEL
    assert (await house.event(event_id)).state is EventState.COLLECTING


async def test_the_coordinator_gets_both(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await slack.click("Sam", "respond_in", str(event_id))
    await slack.click("Sam", "coordinate", str(event_id))
    slack.fake.clear()
    await slack.choose("Sam", cancel(event_id))
    await slack.choose("Sam", close(event_id))
    modal, confirm = opened(slack)
    assert modal["callback_id"] == ids.CANCEL_VIEW and confirm["callback_id"] == ids.CLOSE_VIEW
    assert ids.ViewOrigin.decode(modal["private_metadata"]) == ids.ViewOrigin(event_id, CHANNEL, run=1)
    assert ids.ViewOrigin.decode(confirm["private_metadata"]) == ids.ViewOrigin(
        event_id, CHANNEL, window=Window.RESPOND, run=1
    )
    assert confirm["title"]["text"] == "Start the vote now" and confirm["submit"]["text"] == "Start the vote"
    assert slack.ephemerals("Sam") == []


async def test_an_admin_gets_both_on_any_call(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await to_voting(slack, event_id)
    slack.fake.clear()
    await slack.choose("Alex", cancel(event_id))
    await slack.choose("Alex", close(event_id))
    assert [v["callback_id"] for v in opened(slack)] == [ids.CANCEL_VIEW, ids.CLOSE_VIEW]
    assert opened(slack)[1]["title"]["text"] == "Decide now"


async def test_the_starter_can_call_it_off_until_its_decided(slack: Slack, house: House) -> None:
    event_id = await kims_call(slack)
    slack.fake.clear()
    await slack.choose("Kim", cancel(event_id))
    await slack.choose("Kim", close(event_id))  # starting it isn't coordinating it
    assert [v["callback_id"] for v in opened(slack)] == [ids.CANCEL_VIEW]
    assert slack.ephemerals("Kim")[-1] == NOT_CLOSER

    await to_decided(slack, event_id)
    slack.fake.clear()
    await slack.choose("Kim", cancel(event_id))
    assert opened(slack) == []
    assert slack.ephemerals("Kim") == [DECIDED_NOW]
    await slack.choose("Alex", cancel(event_id))
    assert [v["callback_id"] for v in opened(slack)] == [ids.CANCEL_VIEW]
    decided = opened(slack)[0]["blocks"][1]["text"]["text"]
    assert "whoever was cooking, picking up, or ordering gets a message" in decided


@pytest.mark.parametrize(
    "value", ["cancel", "cancel:{id}", "close:{id}", "cancel:0:1", "cancel:{id}:0", "close:{id}:1:1", "drop:{id}:1", ""]
)
async def test_menu_values_this_app_did_not_write_get_out_of_date(slack: Slack, house: House, value: str) -> None:
    event_id = await daily_call(house)
    await slack.choose("Alex", value.format(id=event_id))
    assert opened(slack) == []
    assert slack.ephemerals("Alex") == [OUT_OF_DATE]
    assert (await house.event(event_id)).state is EventState.COLLECTING


async def test_a_menu_pick_without_an_option_or_a_trigger_is_refused(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    payload = block_action("U01ALEX", ids.CALL_MENU, cancel(event_id))  # a value, not a selected option
    await slack._send("interactive", payload)
    payload = block_action("U01ALEX", ids.CALL_MENU, None, selected=cancel(event_id))
    del payload["trigger_id"]
    await slack._send("interactive", payload)
    assert opened(slack) == []
    assert slack.ephemerals("Alex") == [OUT_OF_DATE, OUT_OF_DATE]


async def test_another_households_call_is_not_found(slack: Slack, migrated: Settings) -> None:
    theirs = other_call(migrated, other_household(migrated))["event"]
    await slack.choose("Alex", cancel(theirs))
    await slack.choose("Alex", close(theirs))
    await slack.click("Alex", ids.CANCEL_CALL, cancel(theirs), home=True)
    await slack.click("Alex", ids.CLOSE_WINDOW, closes(Window.VOTE, theirs))
    await call_off(slack, "Alex", reason("mine now"), theirs)
    await slack.submit(
        "Alex", ids.CLOSE_VIEW, {}, theirs, origin=ids.ViewOrigin(theirs, CHANNEL, window=Window.VOTE, run=1)
    )
    assert opened(slack) == []
    assert slack.ephemerals("Alex") == ["No such call."] * 5
    assert "No such call." in json.dumps(slack.last_home("Alex"))


async def test_a_finished_call_says_so(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await call_off(slack, "Alex", reason(None), event_id)
    slack.fake.clear()
    await slack.choose("Alex", cancel(event_id))
    await slack.choose("Alex", close(event_id))
    await call_off(slack, "Alex", reason(None), event_id)
    assert opened(slack) == []
    assert slack.ephemerals("Alex") == [FINISHED] * 3


# The Call it off modal ---------------------------------------------------------------------------


async def test_calling_it_off_with_a_reason(slack: Slack, house: House) -> None:
    event_id = await kims_call(slack)
    response = await call_off(slack, "Kim", reason("  Pizza at   Grandma's "), event_id)
    assert response.status == 200 and body(response) == {}
    event = await house.event(event_id)
    assert (event.state, event.ended_by, event.end_note) == (
        EventState.CANCELLED,
        house["Kim"].id,
        "Pizza at Grandma's",
    )
    (audit,) = await house.audits("event.cancelled")
    assert audit.payload == {"from": "collecting", "as": "starter", "note": "Pizza at Grandma's"}
    assert slack.ephemerals("Kim")[-1] == CALLED_OFF.format(slot="dinner", day="Thursday, Oct 22")


async def test_calling_it_off_without_a_reason(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await call_off(slack, "Alex", {}, event_id)
    event = await house.event(event_id)
    assert (event.state, event.end_note) == (EventState.CANCELLED, None)
    (audit,) = await house.audits("event.cancelled")
    assert audit.payload == {"from": "collecting", "as": "admin", "note": None}


async def test_a_too_long_reason_is_flagged_on_the_field(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    response = await call_off(slack, "Alex", reason("x" * 141), event_id)
    assert body(response) == {"response_action": "errors", "errors": {"reason": "Keep the reason to 140 characters."}}
    assert (await house.event(event_id)).state is EventState.COLLECTING


async def test_a_forged_submission_is_still_checked(slack: Slack, house: House) -> None:
    """The modal opens only for people allowed to, but a submission is checked again."""
    event_id = await daily_call(house)
    await call_off(slack, "Kim", reason("nope"), event_id)
    assert slack.ephemerals("Kim") == [ONLY_SOME]
    assert (await house.event(event_id)).state is EventState.COLLECTING


async def test_cancel_metadata_naming_a_window_is_dropped(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    origin = ids.ViewOrigin(event_id, CHANNEL, window=Window.RESPOND, run=1)
    response = await slack.submit("Alex", ids.CANCEL_VIEW, reason(None), event_id, origin=origin)
    assert response.status == 200
    assert (await house.event(event_id)).state is EventState.COLLECTING
    assert slack.fake.calls == []


async def test_the_channel_shows_who_called_it_off_and_strikes_the_decision(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await slack.drain()
    await to_decided(slack, event_id)
    await slack.drain()
    slack.fake.clear()
    await call_off(slack, "Alex", reason("<!channel> Pizza & <@U02SAM>"), event_id)
    await slack.drain()
    updates = {c.args["text"].split(":")[0]: c.args for c in slack.fake.of("chat.update")}
    call = updates["Dinner call · Thursday, Oct 22"]
    assert call["blocks"][1]["text"]["text"] == "Called off by Alex. “&lt;!channel&gt; Pizza &amp; &lt;@U02SAM&gt;”"
    assert "call_menu" not in json.dumps(call) and "cancel_call" not in json.dumps(call)  # nothing left
    decision = updates["Dinner on Thursday, Oct 22"]
    assert decision["blocks"][0]["text"]["text"].startswith("~")
    assert "Called off by Alex." in decision["blocks"][0]["text"]["text"]
    reply = [c.args for c in slack.fake.of("chat.postMessage") if c.args.get("thread_ts")]
    assert reply and "&lt;!channel&gt;" in reply[0]["blocks"][0]["text"]["text"]


# Close now ---------------------------------------------------------------------------------------


async def test_close_now_from_the_menu_starts_the_vote(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await slack.click("Alex", "respond_in", str(event_id))
    await slack.click("Alex", "offer_pickup", str(event_id))
    await slack.click("Sam", "respond_in", str(event_id))
    origin = ids.ViewOrigin(event_id, CHANNEL, window=Window.RESPOND, run=1)
    response = await slack.submit("Alex", ids.CLOSE_VIEW, {}, event_id, origin=origin)
    assert response.status == 200 and body(response) == {}
    assert (await house.event(event_id)).state is EventState.VOTING
    assert slack.ephemerals("Alex")[-1] == CLOSED[Window.RESPOND].format(slot="dinner")
    (audit,) = await house.audits("event.window_closed")
    assert audit.payload["window"] == "respond" and audit.payload["as"] == "admin"


async def test_close_now_never_closes_a_window_nobody_confirmed(slack: Slack, house: House) -> None:
    """Confirmed while collecting, submitted once voting had started: the vote stays open."""
    event_id = await daily_call(house)
    origin = ids.ViewOrigin(event_id, CHANNEL, window=Window.RESPOND, run=1)
    await to_voting(slack, event_id)
    await slack.submit("Alex", ids.CLOSE_VIEW, {}, event_id, origin=origin)
    assert (await house.event(event_id)).state is EventState.VOTING
    assert slack.ephemerals("Alex")[-1] == WINDOW_MOVED
    assert await house.audits("event.window_closed") == []


async def test_close_now_refuses_who_cant_even_after_the_modal(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    origin = ids.ViewOrigin(event_id, CHANNEL, window=Window.RESPOND, run=1)
    await slack.submit("Kim", ids.CLOSE_VIEW, {}, event_id, origin=origin)
    assert slack.ephemerals("Kim") == [NOT_CLOSER]
    assert (await house.event(event_id)).state is EventState.COLLECTING


async def test_close_metadata_without_a_window_is_dropped(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await slack.submit("Alex", ids.CLOSE_VIEW, {}, event_id)
    payload = view_submission("U01ALEX", ids.CLOSE_VIEW, {}, '{"event":1,"channel":null,"home":false,"window":"x"}')
    await slack._send("interactive", payload)
    assert (await house.event(event_id)).state is EventState.COLLECTING
    assert slack.fake.calls == []


async def test_two_confirmations_at_once_close_one_window(slack: Slack, house: House) -> None:
    """The coordinator and an admin both confirm Start the vote now: one closes responses, and
    the other must not then close the vote it never saw."""
    event_id = await daily_call(house)
    await slack.click("Alex", "respond_in", str(event_id))
    await slack.click("Alex", "offer_pickup", str(event_id))
    await slack.click("Sam", "respond_in", str(event_id))
    await slack.click("Sam", "coordinate", str(event_id))
    origin = ids.ViewOrigin(event_id, CHANNEL, window=Window.RESPOND, run=1)
    await asyncio.gather(
        slack.submit("Alex", ids.CLOSE_VIEW, {}, event_id, origin=origin),
        slack.submit("Sam", ids.CLOSE_VIEW, {}, event_id, origin=origin),
    )
    assert (await house.event(event_id)).state is EventState.VOTING
    (audit,) = await house.audits("event.window_closed")
    assert audit.payload["window"] == "respond"
    told = sorted([slack.ephemerals("Alex")[-1], slack.ephemerals("Sam")[-1]])
    assert told == sorted([CLOSED[Window.RESPOND].format(slot="dinner"), WINDOW_MOVED])


async def test_close_window_checks_the_window_it_was_asked_to_close(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    house.clock.now = RESPOND_BY  # responses are due to close, but no tick has advanced the call yet
    with pytest.raises(WindowMoved, match=f"^{WINDOW_MOVED}$"):
        async with house.sessionmaker() as db:
            await calls.close_window(db, house["Alex"], event_id, expected_window=Window.RESPOND, now=house.clock())
    assert await house.audits("event.window_closed") == []
    house.clock.now = OPENS
    await to_voting(slack, event_id)
    with pytest.raises(WindowMoved):
        async with house.sessionmaker() as db:
            await calls.close_window(db, house["Alex"], event_id, expected_window=Window.RESPOND, now=house.clock())
    async with house.sessionmaker() as db:  # without an expected window, as before
        await calls.close_window(db, house["Alex"], event_id, now=house.clock())
    assert (await house.event(event_id)).state is EventState.DECIDED
    assert len(await house.audits("event.window_closed")) == 1


async def opened_now(slack: Slack, who: str, value: str) -> ids.ViewOrigin:
    """Pick ``value`` from the ⋯ menu and return the modal's metadata."""
    await slack.choose(who, value)
    return ids.ViewOrigin.decode(opened(slack)[-1]["private_metadata"])


async def test_an_old_modal_menu_or_button_cant_touch_a_restarted_call(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    old_cancel = await opened_now(slack, "Alex", cancel(event_id))
    old_close = await opened_now(slack, "Alex", close(event_id))
    assert (old_cancel.run, old_close.run) == (1, 1)
    await call_off(slack, "Alex", reason(None), event_id)
    await slack.click("Lee", ids.ACTION_FOR_START[Slot.DINNER], "dinner", home=True)  # run 2
    assert ((await house.event(event_id)).state, (await house.event(event_id)).run) == (EventState.COLLECTING, 2)
    slack.fake.clear()

    await slack.submit("Alex", ids.CANCEL_VIEW, reason("old"), event_id, origin=old_cancel)
    await slack.submit("Alex", ids.CLOSE_VIEW, {}, event_id, origin=old_close)
    await slack.choose("Alex", cancel(event_id))
    await slack.choose("Alex", close(event_id))
    await slack.click("Alex", ids.CANCEL_CALL, cancel(event_id))
    await slack.click("Alex", ids.CLOSE_WINDOW, closes(Window.RESPOND, event_id), home=True)
    assert slack.ephemerals("Alex") == [STALE_CALL] * 5
    assert STALE_CALL in json.dumps(slack.last_home("Alex"))
    assert opened(slack) == []
    event = await house.event(event_id)
    assert (event.state, event.run, event.end_note) == (EventState.COLLECTING, 2, None)
    await slack.choose("Alex", cancel(event_id, run=2))  # the new message's menu works
    assert ids.ViewOrigin.decode(opened(slack)[-1]["private_metadata"]).run == 2


async def test_an_old_modal_cant_touch_an_admin_rerun(slack: Slack, house: House) -> None:
    """Run 1's message keeps its menu after an admin re-runs the call. A Start the vote now
    confirmed on run 1 would match run 2's open window, so only the run tells them apart."""
    event_id = await daily_call(house)
    await to_voting(slack, event_id)
    old_cancel = await opened_now(slack, "Alex", cancel(event_id))
    async with house.sessionmaker() as db:
        await call_admin.rerun(db, house["Alex"], event_id, now=house.clock())
    assert ((await house.event(event_id)).state, (await house.event(event_id)).run) == (EventState.COLLECTING, 2)
    stale_close = ids.ViewOrigin(event_id, CHANNEL, window=Window.RESPOND, run=1)
    slack.fake.clear()
    await slack.submit("Alex", ids.CANCEL_VIEW, reason(None), event_id, origin=old_cancel)
    await slack.submit("Alex", ids.CLOSE_VIEW, {}, event_id, origin=stale_close)
    await slack.choose("Alex", close(event_id))
    assert slack.ephemerals("Alex") == [STALE_CALL] * 3
    assert (await house.event(event_id)).state is EventState.COLLECTING
    assert await house.audits("event.window_closed") == []


async def test_a_decided_calls_call_it_off_button_opens_the_modal_in_the_channel(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await to_decided(slack, event_id)
    slack.fake.clear()
    await slack.click("Alex", ids.CANCEL_CALL, cancel(event_id))
    (modal,) = opened(slack)
    assert ids.ViewOrigin.decode(modal["private_metadata"]) == ids.ViewOrigin(event_id, CHANNEL, run=1)
    await slack.click("Kim", ids.CANCEL_CALL, cancel(event_id))
    assert slack.ephemerals("Kim") == [ONLY_SOME]


# App Home ----------------------------------------------------------------------------------------


async def test_home_shows_call_it_off_and_close_now_only_to_who_can(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await slack.click("Sam", "respond_in", str(event_id))
    await slack.click("Sam", "coordinate", str(event_id))
    for who in ("Alex", "Sam", "Kim"):
        await slack.open_home(who)
    common = ["respond_in", "respond_maybe", "respond_out", "details"]
    assert home_actions(slack, "Kim") == [*common, "link_event", "start_call_lunch"]
    for who in ("Alex", "Sam"):  # an admin, and the coordinator
        assert home_actions(slack, who) == [*common, "close_window", "cancel_call", "link_event", "start_call_lunch"]
    buttons = {
        e["action_id"]: e for b in slack.last_home("Sam")["blocks"] if b["type"] == "actions" for e in b["elements"]
    }
    assert buttons["cancel_call"]["style"] == "danger" and buttons["cancel_call"]["value"] == cancel(event_id)
    assert buttons["close_window"]["value"] == closes(Window.RESPOND, event_id)
    assert buttons["close_window"]["text"]["text"] == "Start the vote now"
    assert buttons["close_window"]["confirm"]["confirm"]["text"] == "Start the vote"


async def test_home_shows_the_starter_only_call_it_off(slack: Slack, house: House) -> None:
    event_id = await kims_call(slack)
    await slack.open_home("Kim")
    assert "cancel_call" in home_actions(slack, "Kim") and "close_window" not in home_actions(slack, "Kim")
    await to_decided(slack, event_id)
    await slack.open_home("Kim")
    assert "cancel_call" not in home_actions(slack, "Kim")


async def test_call_it_off_from_home_opens_the_modal_and_answers_on_home(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await slack.click("Alex", ids.CANCEL_CALL, cancel(event_id), home=True)
    (modal,) = opened(slack)
    origin = ids.ViewOrigin.decode(modal["private_metadata"])
    assert origin == ids.ViewOrigin(event_id, None, home=True, run=1)
    await slack.submit("Alex", ids.CANCEL_VIEW, reason("Snow day"), event_id, origin=origin)
    assert (await house.event(event_id)).state is EventState.CANCELLED
    home = json.dumps(slack.last_home("Alex"), ensure_ascii=False)
    assert "You called off dinner for Thursday, Oct 22." in home and "Called off by Alex. “Snow day”" in home
    assert slack.ephemerals("Alex") == []


async def test_home_buttons_refuse_values_of_the_wrong_kind(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await slack.click("Alex", ids.CANCEL_CALL, close(event_id), home=True)
    assert OUT_OF_DATE in json.dumps(slack.last_home("Alex"))
    await slack.click("Alex", ids.CLOSE_WINDOW, cancel(event_id), home=True)
    assert OUT_OF_DATE in json.dumps(slack.last_home("Alex"))
    assert opened(slack) == [] and (await house.event(event_id)).state is EventState.COLLECTING


async def test_close_now_from_home_is_confirmed_by_slack_and_checked_again(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await slack.click("Kim", ids.CLOSE_WINDOW, closes(Window.RESPOND, event_id), home=True)
    assert NOT_CLOSER in json.dumps(slack.last_home("Kim"))
    await slack.click("Alex", ids.CLOSE_WINDOW, closes(Window.VOTE, event_id), home=True)  # stale Home
    assert WINDOW_MOVED in json.dumps(slack.last_home("Alex"))
    assert (await house.event(event_id)).state is EventState.COLLECTING
    await slack.click("Alex", ids.CLOSE_WINDOW, closes(Window.RESPOND, event_id), home=True)
    assert (await house.event(event_id)).state is EventState.ABANDONED  # nobody was in
    assert CLOSED[Window.RESPOND].format(slot="dinner") in json.dumps(slack.last_home("Alex"))


async def test_home_offers_to_start_a_called_off_slot_again(slack: Slack, house: House) -> None:
    event_id = await kims_call(slack)
    await slack.click("Kim", "respond_in", str(event_id))
    await call_off(slack, "Kim", reason(None), event_id)
    for who in ("Sam", "Lee"):  # any member, not only whoever called it off
        await slack.open_home(who)
        assert home_actions(slack, who) == ["link_event", "start_call_dinner", "start_call_lunch"]
        starts = next(b for b in slack.last_home(who)["blocks"] if b.get("block_id") == "start")["elements"]
        assert [e["text"]["text"] for e in starts] == ["Start dinner again", "Start a lunch call"]
    await slack.click("Lee", ids.ACTION_FOR_START[Slot.DINNER], "dinner", home=True)
    event = await house.event(event_id)
    assert (event.state, event.run, event.created_by) == (EventState.COLLECTING, 2, house["Lee"].id)
    assert "start_call_dinner" not in home_actions(slack, "Lee")


async def test_home_hides_start_again_once_its_too_late(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await call_off(slack, "Alex", reason(None), event_id)
    house.clock.now = DONE_AT  # the usual meal time plus two hours
    await slack.open_home("Sam")
    assert "start_call_dinner" not in home_actions(slack, "Sam")
    await slack.click("Sam", ids.ACTION_FOR_START[Slot.DINNER], "dinner", home=True)  # an old Home
    assert "too late to start today's dinner call again" in json.dumps(slack.last_home("Sam"))
    assert (await house.event(event_id)).state is EventState.CANCELLED


# /dinnerbell cancel ------------------------------------------------------------------------------


async def test_slash_cancel_calls_off_todays_only_open_call(slack: Slack, house: House) -> None:
    event_id = await kims_call(slack)
    response = await slack.command("Kim", "cancel Pizza at Grandma's &amp; Co")
    assert response.status == 200 and body(response) == {}
    event = await house.event(event_id)
    assert (event.state, event.end_note) == (EventState.CANCELLED, "Pizza at Grandma's & Co")
    assert slack.ephemerals("Kim")[-1] == CALLED_OFF.format(slot="dinner", day="Thursday, Oct 22")


async def test_slash_cancel_asks_which_when_both_are_open(slack: Slack, house: House) -> None:
    dinner = await daily_call(house)
    await slack.command("Kim", "call lunch")
    await slack.command("Alex", "cancel")
    assert slack.ephemerals("Alex") == [WHICH_CALL.format(when="today", command="/dinnerbell", day="")]
    assert "`/dinnerbell cancel lunch`" in slack.ephemerals("Alex")[0]
    assert (await house.event(dinner)).state is EventState.COLLECTING
    await slack.command("Alex", "cancel lunch Nobody's home")
    async with house.sessionmaker() as db:
        found = await event_views.calls_on(db, house["Alex"], date(2026, 10, 22))
    assert {e.slot: (e.state, e.end_note) for e in found} == {
        Slot.LUNCH: (EventState.CANCELLED, "Nobody's home"),
        Slot.DINNER: (EventState.COLLECTING, None),
    }


async def test_slash_cancel_names_the_configured_command_and_a_later_day(house: House) -> None:
    slack = await wire(house, command="/dinnerbell-test")
    try:
        house.clock.now = OPENS
        await slack.command("Kim", "call lunch tomorrow", command="/dinnerbell-test")
        await slack.command("Kim", "call dinner tomorrow", command="/dinnerbell-test")
        await slack.command("Kim", "cancel tomorrow", command="/dinnerbell-test")
        assert slack.ephemerals("Kim")[-1] == WHICH_CALL.format(
            when="on Friday, Oct 23", command="/dinnerbell-test", day=" 2026-10-23"
        )
        await slack.command("Kim", "cancel dinner tomorrow", command="/dinnerbell-test")
        assert slack.ephemerals("Kim")[-1] == CALLED_OFF.format(slot="dinner", day="Friday, Oct 23")
        reply = body(await slack.command("Kim", "help", command="/dinnerbell-test"))
        assert "`/dinnerbell-test cancel` calls off today's call" in str(reply["text"])
    finally:
        await slack.conn.stop()


async def test_slash_cancel_with_nothing_to_call_off(slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await slack.command("Alex", "cancel")
    await slack.command("Alex", "cancel lunch")
    await slack.command("Alex", "cancel 2026-10-24")
    assert slack.ephemerals("Alex") == [
        NO_OPEN_CALL.format(when="today"),
        "There's no lunch call today.",
        NO_OPEN_CALL.format(when="on Saturday, Oct 24"),
    ]


async def test_slash_cancel_refuses_who_cant_and_explains_a_finished_call(slack: Slack, house: House) -> None:
    event_id = await daily_call(house)
    await slack.command("Kim", "cancel")
    assert slack.ephemerals("Kim") == [ONLY_SOME]
    await slack.command("Alex", "cancel")
    assert (await house.event(event_id)).state is EventState.CANCELLED
    await slack.command("Alex", "cancel")  # nothing open any more
    await slack.command("Alex", "cancel dinner")  # but this one names it
    assert slack.ephemerals("Alex")[-2:] == [NO_OPEN_CALL.format(when="today"), FINISHED]
    await slack.command("Alex", "cancel 2026-13-40")
    assert slack.ephemerals("Alex")[-1].startswith("Try `/dinnerbell cancel`")


async def test_slash_cancel_by_a_stranger_is_refused(slack: Slack, house: House) -> None:
    await daily_call(house)
    await slack.command("U9STRANGER", "cancel")
    assert "I don't know your Slack account yet" in slack.ephemerals("U9STRANGER")[0]


def test_help_lists_cancel() -> None:
    assert "`{command} cancel` calls off today's call" in HELP


# Acknowledging within Slack's 3 seconds ----------------------------------------------------------


async def test_every_call_off_handler_acks_before_its_work(
    slack: Slack, house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slack gives an interaction 3 seconds. Hold every service call until each request has
    been answered, so a slow database can never make Slack show an error."""
    event_id = await daily_call(house)
    gate = asyncio.Event()
    waited: list[str] = []

    def held(name: str, real: Any) -> Any:
        async def slow(*args: Any, **kwargs: Any) -> Any:
            waited.append(name)
            await gate.wait()
            return await real(*args, **kwargs)

        return slow

    held_calls = (
        (event_views, "event_view"),
        (event_views, "calls_on"),
        (calls, "cancel_call"),
        (calls, "close_window"),
    )
    for module, name in held_calls:
        monkeypatch.setattr(module, name, held(name, getattr(module, name)))
    window = ids.ViewOrigin(event_id, CHANNEL, window=Window.RESPOND, run=1).encode()
    requests = [
        ("interactive", block_action("U01ALEX", ids.CALL_MENU, None, selected=cancel(event_id))),
        ("interactive", block_action("U01ALEX", ids.CALL_MENU, None, selected=close(event_id))),
        ("interactive", block_action("U01ALEX", ids.CANCEL_CALL, cancel(event_id), home=True)),
        ("interactive", block_action("U01ALEX", ids.CLOSE_WINDOW, closes(Window.RESPOND, event_id))),
        (
            "interactive",
            view_submission("U01ALEX", ids.CANCEL_VIEW, reason("x"), ids.ViewOrigin(event_id, run=1).encode()),
        ),
        ("interactive", view_submission("U01ALEX", ids.CLOSE_VIEW, {}, window)),
        ("slash_commands", slash("U01ALEX", "cancel dinner")),
    ]
    for kind, payload in requests:
        response = await asyncio.wait_for(send(slack.conn.app, kind, payload), timeout=1.0)
        assert response.status == 200
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(slack.conn.interactions.tasks) == len(requests)  # all still waiting on the service
    assert sorted(set(waited)) == ["calls_on", "cancel_call", "close_window", "event_view"]
    gate.set()
    await slack.conn.interactions.settle()
    assert slack.conn.interactions.tasks == set()


def test_the_modals_explain_what_happens() -> None:
    from dinnerbell.channels.slack import preview as samples
    from dinnerbell.channels.slack.views import cancel_modal, close_modal
    from tests.support.blockkit import check_view

    view = samples.COLLECTING_VIEW
    scheduled = replace(view, event=replace(view.event, state=EventState.SCHEDULED))
    modal = cancel_modal(scheduled, samples.ORIGIN)
    check_view(modal)
    assert "It hasn't been posted yet, so nothing goes to the channel." in modal["blocks"][1]["text"]["text"]
    decision = replace(samples.DECISION, proposal=replace(samples.PROPOSALS[2], voters=()))
    evil = cancel_modal(replace(samples.DECIDED_VIEW, decision=decision), samples.ORIGIN)
    check_view(evil)
    assert "*Taqueria &lt;Lupita&gt; &amp; Co*" in evil["blocks"][1]["text"]["text"]
    with pytest.raises(ValueError, match="needs the window"):
        close_modal(view, samples.ORIGIN)


async def test_modal_metadata_must_name_a_run_exactly_where_it_should(slack: Slack, house: House) -> None:
    """Call it off and Close now carry the run they were opened for; other modals never do."""
    event_id = await daily_call(house)
    no_run = ids.ViewOrigin(event_id, CHANNEL)
    await slack.submit("Alex", ids.CANCEL_VIEW, reason(None), event_id, origin=no_run)
    await slack.submit("Alex", ids.CLOSE_VIEW, {}, event_id, origin=ids.ViewOrigin(event_id, window=Window.RESPOND))
    await slack.submit("Sam", ids.DETAILS_VIEW, state(note={"value": "x"}), event_id, origin=replace(no_run, run=1))
    assert slack.fake.calls == []
    assert (await house.event(event_id)).state is EventState.COLLECTING
    assert (await house.context(event_id)).response_of(house["Sam"].id) is None
