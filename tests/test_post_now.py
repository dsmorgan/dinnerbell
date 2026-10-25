# SPDX-License-Identifier: AGPL-3.0-or-later
"""Asking about a later day's meal now (issue #59): a call started for a later day is scheduled
for that day's usual time, says so, and anyone who could start it can post it now instead. The
deadlines stay that day's, and posting races the scheduler's tick safely. Also the run on
**I'll coordinate** while voting (issue #54)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.interactions import OUT_OF_DATE, STALE
from dinnerbell.config import Settings
from dinnerbell.domain import notifications as n
from dinnerbell.domain.events import EventRuleViolation, EventState, RoleOffer, StaleRun, Status, Step
from dinnerbell.domain.meals import Slot
from dinnerbell.services import calls, event_views
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from tests.support.calls import OPENS, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import Failure
from tests.support.slack import Slack, wire

pytestmark = pytest.mark.anyio

TOMORROW = date(2026, 10, 23)
LUNCH_POSTS = datetime(2026, 10, 23, 15, 30, tzinfo=UTC)
"""Tomorrow's lunch call time, 10:30 AM in Chicago."""
LUNCH_REMIND = datetime(2026, 10, 23, 16, 15, tzinfo=UTC)
LUNCH_BY = datetime(2026, 10, 23, 16, 30, tzinfo=UTC)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    house.clock.now = OPENS  # Thursday, 3 PM in Chicago
    return house


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


async def act(house: House, use_case: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


async def schedule_lunch(house: House, who: str = "Sam") -> int:
    event = await act(house, calls.start_call, house[who], Slot.LUNCH, TOMORROW)
    assert event.state is EventState.SCHEDULED
    return int(event.id)


async def opened_audits(house: House) -> list[dict[str, Any]]:
    return [dict(a.payload) | {"actor": a.actor_member_id} for a in await house.audits() if a.action == "event.opened"]


async def call_messages(house: House, event_id: int) -> list[n.Notification]:
    return [
        n.from_payload(e.kind, e.payload)
        for e in await house.outbox()
        if e.event_id == event_id and e.kind == "call_opened"
    ]


# The service ---------------------------------------------------------------------------------------


async def test_posting_now_opens_responses_and_keeps_that_days_deadlines(house: House) -> None:
    event_id = await schedule_lunch(house)
    before = await house.event(event_id)
    assert (before.opens_at, before.respond_deadline) == (LUNCH_POSTS, LUNCH_BY)

    event = await act(house, calls.post_now, house["Kim"], event_id, run=1)
    assert event.state is EventState.COLLECTING
    after = await house.event(event_id)
    assert after.opens_at == OPENS  # responses opened now
    assert (after.respond_deadline, after.remind_at, after.vote_minutes) == (LUNCH_BY, LUNCH_REMIND, 30)
    (opened,) = await call_messages(house, event_id)
    assert isinstance(opened, n.CallOpened) and opened.event.early and opened.event.day == TOMORROW
    (audit,) = await opened_audits(house)
    assert audit["actor"] == house["Kim"].id and audit["scheduled_for"] == LUNCH_POSTS.isoformat()

    # The scheduler finds nothing to open at the usual time, and keeps the day's reminder and deadline.
    house.clock.now = LUNCH_POSTS
    assert await house.tick() == []
    house.clock.now = LUNCH_REMIND
    assert await house.tick() == [Step.REMIND]
    house.clock.now = LUNCH_BY
    assert Step.CLOSE_RESPONSES in await house.tick()
    assert len(await call_messages(house, event_id)) == 1


async def test_posting_twice_or_after_the_tick_changes_nothing(house: House) -> None:
    event_id = await schedule_lunch(house)
    await act(house, calls.post_now, house["Kim"], event_id)
    outbox = len(await house.outbox())
    again = await act(house, calls.post_now, house["Lee"], event_id, run=1)
    assert again.state is EventState.COLLECTING
    assert len(await house.outbox()) == outbox and len(await opened_audits(house)) == 1

    # A call the tick opened is left as it is too.
    later = await act(house, calls.start_call, house["Sam"], Slot.DINNER, TOMORROW)
    house.clock.now = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
    await house.tick()
    outbox = len(await house.outbox())
    await act(house, calls.post_now, house["Kim"], later.id)
    assert len(await house.outbox()) == outbox
    assert [a["actor"] for a in await opened_audits(house)] == [house["Kim"].id, None]


async def test_posting_now_races_the_tick_safely(house: House) -> None:
    """Both take the event's lock, so whichever runs second finds the call posted."""
    event_id = await schedule_lunch(house)
    house.clock.now = LUNCH_POSTS
    async with house.sessionmaker() as db:
        tick, posted = await asyncio.gather(
            house.tick(), calls.post_now(db, house["Kim"], event_id, now=LUNCH_POSTS), return_exceptions=True
        )
    assert not isinstance(tick, BaseException) and not isinstance(posted, BaseException)
    assert (await house.event(event_id)).state is EventState.COLLECTING
    assert len(await call_messages(house, event_id)) == 1 and len(await opened_audits(house)) == 1


async def test_who_may_post_now_and_what_is_refused(house: House) -> None:
    event_id = await schedule_lunch(house)
    async with house.sessionmaker() as db:
        view = await event_views.event_view(db, house["Kim"], event_id, now=house.clock())
    assert view.can.post_now and not view.can.cancel  # anyone may post it; only Sam started it

    with pytest.raises(StaleRun):
        await act(house, calls.post_now, house["Kim"], event_id, run=2)
    with pytest.raises(NotFound):
        await act(house, calls.post_now, house["Kim"], event_id + 1000)
    await house.add_member("Pat")
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE member SET active = 0 WHERE id = :id"), {"id": house["Pat"].id})
        await db.commit()
    with pytest.raises(EventRuleViolation, match="Only active members"):
        await act(house, calls.post_now, await house.refresh("Pat"), event_id)

    await act(house, calls.cancel_call, house["Sam"], event_id)
    with pytest.raises(EventRuleViolation, match="already finished"):
        await act(house, calls.post_now, house["Kim"], event_id)
    assert (await house.event(event_id)).state is EventState.CANCELLED


async def test_a_scheduled_call_past_its_deadline_is_left_to_expire(house: House) -> None:
    event_id = await schedule_lunch(house)
    house.clock.now = LUNCH_BY
    async with house.sessionmaker() as db:
        view = await event_views.event_view(db, house["Kim"], event_id, now=house.clock())
    assert not view.can.post_now
    with pytest.raises(EventRuleViolation, match="too late"):
        await act(house, calls.post_now, house["Kim"], event_id)
    assert await house.tick() == [Step.EXPIRE]


async def test_answers_given_the_day_before_count_toward_closing_early(house: House) -> None:
    """Responses open when it's posted, so everyone answering then can close it early (FR-56)."""
    event_id = await schedule_lunch(house)
    await act(house, calls.post_now, house["Kim"], event_id)
    for name in ("Alex", "Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    assert (await house.event(event_id)).state is EventState.ABANDONED


async def test_today_lists_scheduled_calls_as_coming_up(house: House) -> None:
    dinner = await act(house, calls.start_call, house["Sam"], Slot.DINNER, date(2026, 10, 22))
    lunch = await schedule_lunch(house)
    async with house.sessionmaker() as db:
        today = await event_views.today_view(db, house["Kim"], now=house.clock())
    assert [v.id for v in today.current] == [dinner.id]
    assert [v.id for v in today.upcoming] == [lunch]
    assert not today.current[0].info.early


# Slack ---------------------------------------------------------------------------------------------


def buttons(blocks: list[dict[str, Any]]) -> dict[str, str | None]:
    return {e["action_id"]: e.get("value") for b in blocks if b["type"] == "actions" for e in b["elements"]}


async def test_slack_says_when_a_later_days_call_posts_and_posts_it_now(slack: Slack, house: House) -> None:
    await slack.command("Kim", "call lunch tomorrow")
    (reply,) = slack.fake.of("chat.postEphemeral")
    assert reply.args["text"] == (
        "Scheduled the lunch call for Friday, Oct 23. It posts in <#C0DINNER> at 10:30 AM that day."
    )
    async with house.sessionmaker() as db:
        (lunch,) = await event_views.calls_on(db, house["Kim"], TOMORROW)
    assert lunch.state is EventState.SCHEDULED
    post = buttons(reply.args["blocks"])[ids.POST_NOW]
    assert post == ids.card_value(lunch.id, 1)
    await slack.drain()
    assert slack.fake.of("chat.postMessage") == []  # nothing in the channel yet

    slack.fake.clear()
    await slack.click("Lee", ids.POST_NOW, post, ephemeral=True)
    assert (await house.event(lunch.id)).state is EventState.COLLECTING
    assert slack.ephemerals("Lee") == [
        "The lunch call for Friday, Oct 23 is posted in <#C0DINNER>. Answers are due by 11:30 AM that day."
    ]
    await slack.drain()
    (call,) = slack.fake.of("chat.postMessage")
    status = call.args["blocks"][1]["text"]["text"]
    assert status.startswith("Who's in for lunch on Friday, Oct 23? Answer by *11:30 AM* that day.")

    # A second tap, or one after the tick posted it, only says where it is.
    await slack.click("Kim", ids.POST_NOW, post, ephemeral=True)
    assert slack.ephemerals("Kim")[-1].startswith("The lunch call for Friday, Oct 23 is posted")


async def test_slack_call_now_posts_a_later_days_call_at_once(slack: Slack, house: House) -> None:
    await slack.command("Kim", "call lunch tomorrow now")
    async with house.sessionmaker() as db:
        (lunch,) = await event_views.calls_on(db, house["Kim"], TOMORROW)
    assert lunch.state is EventState.COLLECTING and lunch.respond_deadline == LUNCH_BY
    assert slack.ephemerals("Kim") == [
        "The lunch call for Friday, Oct 23 is posted in <#C0DINNER>. Answers are due by 11:30 AM that day."
    ]
    # And `early` posts one that's scheduled already; without it, it's already there.
    await slack.command("Sam", "call dinner tomorrow")
    await slack.command("Sam", "call dinner tomorrow")
    assert "already" in slack.ephemerals("Sam")[-1]
    await slack.command("Sam", "call early dinner tomorrow")
    assert slack.ephemerals("Sam")[-1].startswith("The dinner call for Friday, Oct 23 is posted")
    await slack.command("Sam", "call early dinner tomorrow")  # posted already: not scheduled any more
    assert "already" in slack.ephemerals("Sam")[-1]


async def test_a_reply_with_buttons_outside_the_channel_comes_as_a_direct_message(slack: Slack, house: House) -> None:
    """Its buttons work only in the household channel and the bot's direct message."""
    await slack.command("Kim", "call lunch tomorrow", channel="C0ELSEWHERE")
    assert slack.fake.of("chat.postEphemeral") == []
    (dm,) = slack.fake.of("chat.postMessage")
    assert dm.args["channel"] == "U03KIM" and ids.POST_NOW in buttons(dm.args["blocks"])


async def test_post_it_now_from_before_a_restart_is_refused(slack: Slack, house: House) -> None:
    event_id = await schedule_lunch(house)
    await act(house, calls.cancel_call, house["Sam"], event_id)
    await act(house, calls.start_call, house["Kim"], Slot.LUNCH, TOMORROW)
    assert (await house.event(event_id)).run == 2
    await slack.click("Kim", ids.POST_NOW, ids.card_value(event_id, 1), ephemeral=True)
    assert slack.ephemerals("Kim") == [STALE]
    assert (await house.event(event_id)).state is EventState.SCHEDULED


async def test_home_lists_what_is_coming_up(slack: Slack, house: House) -> None:
    lunch = await schedule_lunch(house)
    await act(house, calls.start_call, house["Sam"], Slot.DINNER, TOMORROW)
    await slack.open_home("Kim")
    home = slack.last_home("Kim")
    text = json.dumps(home, ensure_ascii=False)
    assert "*Coming up*" in text and "*Lunch* · Friday, Oct 23 · posts at 10:30 AM" in text
    # A call for tomorrow doesn't take the place of today's dinner.
    assert buttons(home["blocks"])["start_call_dinner"] == "dinner"
    assert "cancel_call" not in buttons(home["blocks"])  # Kim didn't start them
    await slack.open_home("Sam")
    assert "cancel_call" in buttons(slack.last_home("Sam")["blocks"])

    await slack.click("Kim", ids.POST_NOW, ids.card_value(lunch, 1), home=True)
    after = json.dumps(slack.last_home("Kim"), ensure_ascii=False)
    assert "The lunch call for Friday, Oct 23 is posted in the Dinnerbell channel." in after
    assert "*Lunch* · Friday, Oct 23 · Who's in?" in after
    assert slack.ephemerals("Kim") == []


# I'll coordinate while voting names its run (issue #54) ------------------------------------------


async def to_voting(slack: Slack, house: House) -> int:
    await house.tick()
    event_id = await dinner_id(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    return event_id


async def test_ill_coordinate_while_voting_claims_the_run_it_was_drawn_for(slack: Slack, house: House) -> None:
    event_id = await to_voting(slack, house)
    await slack.drain()
    (update,) = [c for c in slack.fake.of("chat.update") if ids.CALL_COORDINATE in buttons(c.args["blocks"])][-1:]
    claim = buttons(update.args["blocks"])[ids.CALL_COORDINATE]
    assert claim == ids.card_value(event_id, 1)
    await slack.click("Kim", ids.CALL_COORDINATE, claim, home=True)
    assert OUT_OF_DATE in json.dumps(slack.last_home("Kim"))
    await slack.click("Kim", ids.CALL_COORDINATE, claim)
    assert (await house.event(event_id)).coordinator_id == house["Kim"].id
    assert "You're coordinating." in json.dumps(slack.cards("Kim")[-1])


async def test_ill_coordinate_from_before_a_restart_cannot_claim_the_new_run(slack: Slack, house: House) -> None:
    """A voting message from run 1 that hasn't been redrawn yet, after a call-off and restart."""
    event_id = await to_voting(slack, house)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    await act(house, calls.start_call, house["Lee"], Slot.DINNER, date(2026, 10, 22))
    assert (await house.event(event_id)).run == 2
    await slack.click("Kim", ids.CALL_COORDINATE, ids.card_value(event_id, 1))
    assert slack.ephemerals("Kim") == [STALE]
    assert (await house.event(event_id)).coordinator_id is None


async def test_a_reply_slack_wont_show_in_the_channel_falls_back_to_a_dm(
    slack: Slack, house: House, caplog: pytest.LogCaptureFixture
) -> None:
    slack.fake.fail("chat.postEphemeral", Failure("user_not_in_channel"))
    await slack.command("Kim", "call lunch tomorrow")
    (dm,) = slack.fake.of("chat.postMessage")
    assert dm.args["channel"] == "U03KIM" and ids.POST_NOW in buttons(dm.args["blocks"])

    slack.fake.fail("chat.postEphemeral", Failure("user_not_in_channel"))
    slack.fake.fail("chat.postMessage", Failure(raises=TimeoutError()))
    await slack.command("Kim", "call dinner tomorrow")  # no exception escapes
    assert any(r.message == "slack reply failed" for r in caplog.records)
