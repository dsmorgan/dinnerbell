# SPDX-License-Identifier: AGPL-3.0-or-later
"""End to end through Slack (spec 13.10): the scheduler's tick opens a call, the outbox drains
through SlackChannel into a fake Slack, and household members answer, vote, lock, and mark the
meal done by pressing buttons through the interaction handlers. The exact Web API calls, in
order, are the contract with Slack: posts, threads, in-place updates, and direct messages."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from dinnerbell.channels.slack import ids
from dinnerbell.config import Settings
from dinnerbell.domain.events import EventState, Status
from dinnerbell.services.database import Database, open_database
from tests.support.calls import DONE_AT, OPENS, REMIND, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import CHANNEL, Call, state
from tests.support.slack import Slack, wire

pytestmark = pytest.mark.anyio


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def slack(database: Database, clock: FakeClock, migrated: Settings) -> AsyncIterator[Slack]:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


def summary(call: Call) -> tuple[Any, ...]:
    """What a call did, without the message bodies: method, where, threading, and its text."""
    args = call.args
    match call.method:
        case "chat.postMessage":
            return ("post", args["channel"], args.get("thread_ts"), args["text"][:40])
        case "chat.update":
            return ("update", args["channel"], args["ts"], args["text"][:40])
        case "chat.postEphemeral":
            return ("ephemeral", args["channel"], args["user"], args["text"])
        case "views.open":
            return ("modal", args["view"]["callback_id"])
        case _:
            return (call.method,)


def block_text(call: Call, index: int) -> str:
    text: str = call.args["blocks"][index]["text"]["text"]
    return text


async def test_a_dinner_decided_in_slack(slack: Slack) -> None:
    house: House = slack.house
    clock = house.clock
    clock.now = OPENS
    await house.tick()
    await slack.drain()
    event_id = await dinner_id(house)
    e = str(event_id)

    # Members answer in the channel.
    await slack.click("Alex", "respond_in", e)
    await slack.click("Alex", "offer_pickup", e)
    await slack.click("Sam", "offer_cook", e)
    await slack.click("Sam", "details", e)
    tacos = house.items["Sam's Tacos"].id
    details = state(
        guests={"value": "1"},
        eat_from={"selected_time": "18:40"},
        suggest_item={"selected_option": {"value": str(tacos)}},
    )
    await slack.submit("Sam", ids.DETAILS_VIEW, details, event_id)
    await slack.click("Kim", "respond_maybe", e)
    await slack.drain()

    # Lee hasn't answered, gets a reminder, and answers from the DM.
    clock.now = REMIND
    await house.tick()
    await slack.drain()
    await slack.click("Lee", "respond_out", e, channel="D04LEE")
    await slack.drain()

    # Responses close: Kim's maybe turns out, and the picks are posted in the thread.
    clock.now = RESPOND_BY
    await house.tick()
    await slack.drain()
    ctx = await house.context(event_id)
    assert ctx.response_of(house["Kim"].id).status is Status.OUT  # type: ignore[union-attr]
    first = ids.proposal_value(event_id, ctx.proposals[0].id)
    await slack.click("Alex", "vote", first)
    await slack.click("Sam", "vote", first)
    await slack.click("Sam", "lock", first)  # not Sam's call to make
    await slack.click("Sam", "coordinate", e)
    await slack.drain()
    await slack.click("Sam", "lock", first)  # now Sam coordinates
    await slack.drain()
    assert (await house.event(event_id)).state is EventState.DECIDED

    clock.now = DONE_AT
    await slack.click("Alex", "mark_done", e)
    await slack.drain()
    assert (await house.event(event_id)).state is EventState.DONE

    call, thread = (CHANNEL, "1.000100"), "1.000100"
    picks_ts, decision_ts = "4.000100", "5.000100"
    assert [summary(c) for c in slack.fake.calls] == [
        ("post", CHANNEL, None, "Dinner call · Thursday, Oct 22: Who's in"),
        ("ephemeral", CHANNEL, "U01ALEX", "You're in for dinner. Thanks!"),
        ("ephemeral", CHANNEL, "U01ALEX", "Thanks! You're offering to pick up. Tap again to take it back."),
        ("ephemeral", CHANNEL, "U02SAM", "Thanks! You're offering to cook. Tap again to take it back."),
        ("modal", ids.DETAILS_VIEW),
        ("ephemeral", CHANNEL, "U03KIM", "Got it: you're a maybe for dinner."),
        ("update", *call, "Dinner call · Thursday, Oct 22: Who's in"),
        ("post", "U04LEE", None, "Are you in for dinner on Thursday, Oct 2"),
        ("conversations.open",),  # is D04LEE really Lee's DM with the bot?
        ("ephemeral", "D04LEE", "U04LEE", "Got it: you're out for dinner this time."),
        ("update", *call, "Dinner call · Thursday, Oct 22: Who's in"),
        ("post", "U03KIM", None, "You're counted out for dinner on Thursda"),
        ("post", CHANNEL, thread, "Picks for dinner on Thursday, Oct 22: Sa"),
        ("update", *call, "Dinner call · Thursday, Oct 22: Thanks f"),
        ("ephemeral", CHANNEL, "U01ALEX", "Vote counted. Tap Vote again to take it back."),
        ("ephemeral", CHANNEL, "U02SAM", "Vote counted. Tap Vote again to take it back."),
        ("ephemeral", CHANNEL, "U02SAM", "Only the coordinator or an admin can lock a proposal."),
        ("ephemeral", CHANNEL, "U02SAM", "You're coordinating dinner. Thanks for steering!"),
        ("update", CHANNEL, picks_ts, "Picks for dinner on Thursday, Oct 22: Sa"),
        ("update", *call, "Dinner call · Thursday, Oct 22: Thanks f"),
        ("post", CHANNEL, thread, "Dinner on Thursday, Oct 22: Sam's Tacos "),
        ("post", "U02SAM", None, "You're cooking and coordinating for dinn"),  # one DM, both roles
        ("update", CHANNEL, picks_ts, "Picks for dinner on Thursday, Oct 22: Sa"),
        ("update", *call, "Dinner call · Thursday, Oct 22: It's dec"),
        ("ephemeral", CHANNEL, "U01ALEX", "Marked done. Thanks!"),
        ("update", CHANNEL, decision_ts, "Dinner on Thursday, Oct 22: Sam's Tacos "),
        ("update", *call, "Dinner call · Thursday, Oct 22: Dinner i"),
    ]

    posts = slack.fake.of("chat.postMessage")
    updates = slack.fake.of("chat.update")
    assert "<@U01ALEX> <@U03KIM> <@U04LEE> <@U02SAM>" in block_text(posts[0], 1)  # everyone mentioned
    roster = block_text(updates[0], 2)
    assert "*In* · 3 people eating" in roster and "<@U02SAM> · +1 guest · can cook" in roster
    assert "*Maybe:* <@U03KIM>" in roster and "Still to answer: Lee" in roster
    assert "*Out:* <@U03KIM>, <@U04LEE>" in block_text(updates[2], 2)
    assert "2 votes: <@U01ALEX>, <@U02SAM>" in block_text(updates[3], 1)
    assert "*Coordinator:* <@U02SAM>" in block_text(updates[4], 2)
    decision = posts[4]
    assert "Locked in by <@U02SAM>." in block_text(decision, 0)
    assert "*Cooking:* <@U02SAM>" in block_text(decision, 2)
    assert decision.args["blocks"][1]["fields"][0]["text"] == "*When*\n6:45 PM"
    assert "*Dinner is done: Sam's Tacos.* Thanks to everyone who pitched in!" in block_text(updates[-2], 0)
    assert "Locked in by <@U02SAM>." in block_text(updates[-2], 0)  # still there after the edit
