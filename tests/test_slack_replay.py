# SPDX-License-Identifier: AGPL-3.0-or-later
"""Slack redelivers an interaction after a reconnect, people double tap, and workspace members who aren't in
Dinnerbell run commands in a loop (audit #14). None of it may undo a vote, reach a deactivated member,
or spend Slack calls."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.interactions import BROKEN, DOUBLE_TAP, SEEN_TRIGGERS, TRY_LATER, Recent
from dinnerbell.config import Settings
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.events import AssignmentRole, EventState
from dinnerbell.domain.meals import Slot
from dinnerbell.services import calls
from dinnerbell.services.database import Database, open_database
from tests.support.calls import OPENS, REMIND, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import Failure, block_action
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


async def test_a_vote_tapped_again_within_two_seconds_is_ignored_and_says_so(slack: Slack, house: House) -> None:
    eid, pid = await _voting(slack, house)
    value = ids.proposal_value(eid, pid)
    await slack.click("Alex", "vote", value)
    house.clock.advance(timedelta(seconds=1.5))
    await slack.click("Alex", "vote", value)
    assert house["Alex"].id in (await house.context(eid)).votes.get(pid, [])
    assert slack.ephemerals("Alex")[-1] == DOUBLE_TAP
    house.clock.advance(timedelta(seconds=1))  # 2.5 s after the first tap: a deliberate undo
    await slack.click("Alex", "vote", value)
    assert house["Alex"].id not in (await house.context(eid)).votes.get(pid, [])


async def test_votes_for_two_picks_within_two_seconds_both_count(slack: Slack, house: House) -> None:
    eid, first = await _voting(slack, house)
    second = (await house.context(eid)).proposals[1].id
    await slack.click("Alex", "vote", ids.proposal_value(eid, first))
    await slack.click("Alex", "vote", ids.proposal_value(eid, second))
    votes = (await house.context(eid)).votes
    assert house["Alex"].id in votes.get(first, []) and house["Alex"].id in votes.get(second, [])


async def test_different_toggles_within_two_seconds_both_count(slack: Slack, house: House) -> None:
    eid, pid = await _voting(slack, house)
    await slack.click("Alex", "vote", ids.proposal_value(eid, pid))
    await slack.click("Alex", "offer_order", str(eid))
    ctx = await house.context(eid)
    response = ctx.response_of(house["Alex"].id)
    assert house["Alex"].id in ctx.votes.get(pid, []) and response is not None and response.offers


async def test_a_failed_tap_is_not_taken_for_a_duplicate_when_retried(
    slack: Slack, house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    eid, pid = await _voting(slack, house)
    real, failures = calls.toggle_vote, []

    async def flaky(*args: object, **kwargs: object) -> bool:
        if not failures:
            failures.append(1)
            raise RuntimeError("database blip")
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(calls, "toggle_vote", flaky)
    payload = block_action("U01ALEX", "vote", ids.proposal_value(eid, pid))
    await slack._send("interactive", payload)
    assert slack.ephemerals("Alex")[-1] == BROKEN
    await slack._send("interactive", payload)  # the same payload again, at once: a retry, not a duplicate
    assert house["Alex"].id in (await house.context(eid)).votes.get(pid, [])


async def test_a_stranger_past_the_limit_gets_one_try_later_home_view(slack: Slack) -> None:
    for _ in range(5):
        await slack.open_home("U9STRANGER")
    slack.fake.clear()
    await slack.open_home("U9STRANGER")
    await slack.open_home("U9STRANGER")
    (view,) = slack.fake.of("views.publish")
    assert TRY_LATER in json.dumps(view.args["view"])


async def test_a_broken_member_lookup_acknowledges_with_a_try_again_reply(
    slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(_: str) -> bool:
        raise RuntimeError("database down")

    monkeypatch.setattr(slack.conn.interactions, "_known", down)
    response = await slack.command("Alex", "help")
    assert json.loads(response.body)["text"] == BROKEN  # a member is never told to join


async def test_a_failed_art_forget_after_the_text_post_does_not_fail_the_delivery(
    slack: Slack, house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_slack_badge_art import earn

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database blip")

    monkeypatch.setattr(slack.conn.badge_files, "forget", broken)
    refused = Failure("invalid_blocks")
    slack.fake.fail("chat.postMessage", refused, refused, refused)
    await earn(house)
    result = await slack.drain()
    assert (result.delivered, result.failed) == (1, 0)
    assert len(slack.fake.of("chat.postMessage")) == 4  # three tries with art, then one text post: once


async def test_dropping_a_deactivated_assignees_dm_is_counted_and_warned(
    slack: Slack, house: House, caplog: pytest.LogCaptureFixture
) -> None:
    house.clock.now = OPENS
    await house.tick()
    eid = await dinner_id(house)
    channel = slack.conn.channel
    assert channel.state is not None
    async with house.sessionmaker() as db:
        await MemberRepository(db).update(house.household.id, house["Kim"].id, house.clock(), active=False)
        await db.commit()
    kim = note.PersonInfo(house["Kim"].id, "Kim")
    event = note.EventInfo(
        id=eid,
        household_id=house.household.id,
        slot=Slot.DINNER,
        day=date(2026, 10, 22),
        state=EventState.DECIDED,
        run=1,
        path=f"/events/{eid}",
        timezone="America/Chicago",
        respond_deadline=house.clock(),
        vote_deadline=house.clock(),
        coordinator=None,
    )
    vacated = note.RoleVacated(event, kim, kim, (AssignmentRole.PICKUP,))
    with caplog.at_level(logging.WARNING):
        result = await channel.deliver(vacated)
    assert result.ref is None and channel.state.dms_dropped_inactive == 1
    assert any("role message dropped" in r.getMessage() and r.event_id == eid for r in caplog.records)  # type: ignore[attr-defined]
