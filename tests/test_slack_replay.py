# SPDX-License-Identifier: AGPL-3.0-or-later
"""Slack redelivers an interaction after a reconnect, people double tap, and workspace members who aren't in
Dinnerbell run commands in a loop (audit #14). None of it may undo a vote, reach a deactivated member,
or spend Slack calls."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.interactions import SEEN_TRIGGERS, Recent
from dinnerbell.config import Settings
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain import notifications as note
from dinnerbell.services.database import Database, open_database
from tests.support.calls import OPENS, REMIND, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import block_action
from tests.support.slack import Slack, wire

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


async def _voting(slack: Slack, house: House) -> tuple[int, int]:
    house.clock.now = OPENS
    await house.tick()
    eid = await dinner_id(house)
    await slack.click("Alex", "respond_in", str(eid))
    await slack.click("Alex", "offer_pickup", str(eid))
    await slack.click("Sam", "respond_in", str(eid))
    house.clock.now = RESPOND_BY
    await house.tick()
    return eid, (await house.context(eid)).proposals[0].id


async def test_a_redelivered_vote_tap_does_not_take_the_vote_back(slack: Slack, house: House) -> None:
    eid, pid = await _voting(slack, house)
    payload = block_action("U01ALEX", "vote", ids.proposal_value(eid, pid))
    await slack._send("interactive", payload)  # the original delivery
    await slack._send("interactive", payload)  # Slack redelivers the same payload (same trigger_id) after a reconnect
    assert house["Alex"].id in (await house.context(eid)).votes.get(pid, [])


async def test_a_redelivered_offer_tap_does_not_withdraw_the_offer(slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    eid = await dinner_id(house)
    payload = block_action("U02SAM", "offer_cook", str(eid))
    await slack._send("interactive", payload)
    await slack._send("interactive", payload)
    r = (await house.context(eid)).response_of(house["Sam"].id)
    assert r is not None and r.offers


async def test_a_double_tap_on_vote_does_not_take_the_vote_back(slack: Slack, house: House) -> None:
    eid, pid = await _voting(slack, house)
    value = ids.proposal_value(eid, pid)
    await slack.click("Alex", "vote", value)  # a second click is a new interaction with its own trigger ID
    await slack.click("Alex", "vote", value)
    assert house["Alex"].id in (await house.context(eid)).votes.get(pid, [])


async def test_a_vote_tapped_again_later_still_takes_it_back(slack: Slack, house: House) -> None:
    eid, pid = await _voting(slack, house)
    value = ids.proposal_value(eid, pid)
    await slack.click("Alex", "vote", value)
    house.clock.advance(timedelta(minutes=3))
    await slack.click("Alex", "vote", value)
    assert house["Alex"].id not in (await house.context(eid)).votes.get(pid, [])


async def test_two_people_voting_at_once_both_count(slack: Slack, house: House) -> None:
    eid, pid = await _voting(slack, house)
    value = ids.proposal_value(eid, pid)
    await slack.click("Alex", "vote", value)
    await slack.click("Sam", "vote", value)
    assert {house["Alex"].id, house["Sam"].id} <= set((await house.context(eid)).votes.get(pid, []))


async def test_a_double_tap_on_an_offer_button_keeps_the_offer(slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    eid = await dinner_id(house)
    await slack.click("Sam", "offer_cook", str(eid))
    await slack.click("Sam", "offer_cook", str(eid))
    r = (await house.context(eid)).response_of(house["Sam"].id)
    assert r is not None and r.offers
    house.clock.advance(timedelta(minutes=3))
    await slack.click("Sam", "offer_cook", str(eid))  # a tap minutes later withdraws it, as before
    r = (await house.context(eid)).response_of(house["Sam"].id)
    assert r is None or not r.offers


def test_the_trigger_memory_is_bounded_and_forgets_with_time() -> None:
    recent, now = Recent(), datetime(2026, 10, 22, 12, tzinfo=UTC)
    for n in range(SEEN_TRIGGERS + 10):
        assert not recent.trigger_seen(f"t{n}", now)
    assert len(recent.triggers) == SEEN_TRIGGERS
    assert not recent.trigger_seen("t0", now)  # evicted, so new again
    assert recent.trigger_seen(f"t{SEEN_TRIGGERS + 9}", now)
    assert not recent.trigger_seen(f"t{SEEN_TRIGGERS + 9}", now + timedelta(minutes=11))
    assert not recent.trigger_seen(None, now) and not recent.trigger_seen("", now)


async def test_a_queued_dm_to_a_member_deactivated_before_delivery_is_dropped(slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    await slack.drain()
    slack.fake.clear()
    house.clock.now = REMIND
    await house.tick()
    async with house.sessionmaker() as db:
        await MemberRepository(db).update(house.household.id, house["Kim"].id, house.clock(), active=False)
        await db.commit()
    await slack.drain()
    dms = [c.args["channel"] for c in slack.fake.of("chat.postMessage") if c.args["channel"].startswith("U")]
    assert dms and "U03KIM" not in dms  # the others still get theirs


async def test_a_deactivated_member_renders_as_a_plain_name(slack: Slack, house: House) -> None:
    async with house.sessionmaker() as db:
        await MemberRepository(db).update(house.household.id, house["Kim"].id, house.clock(), active=False)
        await db.commit()
    directory = await slack.conn.channel._directory(house.household.id)
    assert directory.mention(note.PersonInfo(house["Kim"].id, "Kim")) == "Kim"
    assert directory.mention(note.PersonInfo(house["Sam"].id, "Sam")) == "<@U02SAM>"


async def test_an_unlinked_user_gets_a_few_answers_then_none(slack: Slack, house: House) -> None:
    slack.fake.clear()
    for _ in range(60):
        await slack.command("U9STRANGER", "call")
    assert len(slack.fake.of("chat.postEphemeral", "chat.postMessage", "views.publish")) < 10
    house.clock.advance(timedelta(minutes=6))
    await slack.command("U9STRANGER", "call")  # the window passed: answered again
    assert slack.ephemerals("U9STRANGER")[-1].startswith("You're not in Dinnerbell yet")


async def test_one_strangers_loop_does_not_silence_another(slack: Slack) -> None:
    for _ in range(20):
        await slack.command("U9STRANGER", "call")
    await slack.command("U8OTHER", "call")
    assert slack.ephemerals("U8OTHER")
