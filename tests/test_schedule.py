# SPDX-License-Identifier: AGPL-3.0-or-later
"""Any member edits a slot's standing schedule (FR-44): the domain's rules, the use case's audit
and announcement, that a change applies from the next call only, and the schedule in Slack."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from dinnerbell.config import Settings
from dinnerbell.domain import schedule as sched
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import EventState
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.notifications import ScheduleChanged, from_payload
from dinnerbell.services import calls
from dinnerbell.services import schedule as schedule_service
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden
from tests.support.calls import OPENS, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import CHANNEL, Failure
from tests.support.slack import SLACK_IDS, Slack, wire

pytestmark = pytest.mark.anyio

CHICAGO = ZoneInfo("America/Chicago")
DINNER = sched.DEFAULT_SLOTS[Slot.DINNER]
EARLIER = replace(DINNER, call_time=time(14, 0), response_minutes=60)
TODAY = date(2026, 10, 22)


# The domain ---------------------------------------------------------------------------------------


def test_voting_closes_before_the_usual_meal_time() -> None:
    assert sched.check_schedule(replace(DINNER, meal_time=time(17, 16))) is not None  # closes 17:15
    for meal in (time(17, 15), time(17, 0)):
        with pytest.raises(sched.InvalidSchedule, match="Voting would close at 5:15 PM, but the usual meal time is"):
            sched.check_schedule(replace(DINNER, meal_time=meal))
    lunch = sched.DEFAULT_SLOTS[Slot.LUNCH]
    assert sched.check_schedule(lunch) is lunch  # the defaults pass


def test_slot_changes_name_old_and_new_values() -> None:
    assert sched.slot_changes(DINNER, DINNER) == {}
    assert sched.slot_changes(DINNER, replace(EARLIER, modes=frozenset({Mode.COOK}))) == {
        "call_time": {"from": "15:00", "to": "14:00"},
        "response_minutes": {"from": 90, "to": 60},
        "modes": {"from": ["pickup", "delivery"], "to": ["cook"]},
    }


def test_fingerprints_tell_schedules_apart() -> None:
    assert sched.slot_fingerprint(DINNER) == sched.slot_fingerprint(replace(DINNER))
    assert sched.slot_fingerprint(DINNER) != sched.slot_fingerprint(EARLIER)
    assert len(sched.slot_fingerprint(DINNER)) == 16


@pytest.mark.parametrize(
    ("now", "taken", "expected"),
    [
        (datetime(2026, 10, 22, 18, 0, tzinfo=UTC), set(), TODAY),  # 1 PM: before today's 2 PM call
        (datetime(2026, 10, 22, 19, 30, tzinfo=UTC), set(), TODAY),  # 2:30 PM: it can still open today
        (datetime(2026, 10, 22, 20, 0, tzinfo=UTC), set(), date(2026, 10, 23)),  # 3 PM: answers closed
        (datetime(2026, 10, 22, 18, 0, tzinfo=UTC), {TODAY}, date(2026, 10, 23)),  # today's call exists
        (datetime(2026, 10, 22, 18, 0, tzinfo=UTC), {TODAY, date(2026, 10, 23)}, date(2026, 10, 24)),
        (datetime(2026, 10, 22, 18, 0, tzinfo=UTC), {date(2026, 10, 23)}, TODAY),
    ],
)
def test_first_day_using_a_new_schedule(now: datetime, taken: set[date], expected: date) -> None:
    assert sched.first_day_using(EARLIER, today=TODAY, now=now, zone=CHICAGO, taken=taken) == expected


# The use case -------------------------------------------------------------------------------------


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def update(house: House, who: str, schedule: sched.SlotSchedule, *, seen: str | None = None) -> bool:
    async with house.sessionmaker() as db:
        return await schedule_service.update_slot(db, house[who], Slot.DINNER, schedule, seen=seen, now=house.clock())


async def stored(house: House) -> sched.SlotSchedule:
    async with house.sessionmaker() as db:
        return (await schedule_service.slot_schedules_for(db, house["Kim"]))[Slot.DINNER]


async def test_a_member_changes_the_schedule_and_it_is_audited_and_announced(house: House) -> None:
    assert not house["Kim"].is_admin
    assert await update(house, "Kim", EARLIER, seen=sched.slot_fingerprint(DINNER)) is True
    assert await stored(house) == EARLIER
    (audit,) = await house.audits("household.slot_schedule_updated")
    assert audit.actor_member_id == house["Kim"].id and audit.target == f"household:{house.household.id}"
    assert audit.payload["slot"] == "dinner" and audit.payload["starts_on"] == "2026-10-22"
    assert audit.payload["changes"] == {
        "call_time": {"from": "15:00", "to": "14:00"},
        "response_minutes": {"from": 90, "to": 60},
    }
    (row,) = await house.outbox()
    assert (row.kind, row.event_id, row.message_key, row.thread_key) == ("schedule_changed", None, None, None)
    announced = from_payload(row.kind, row.payload)
    assert isinstance(announced, ScheduleChanged)
    assert (announced.changed_by.name, announced.slot, announced.starts_on) == ("Kim", Slot.DINNER, TODAY)
    assert (announced.before.call_time, announced.after.call_time) == (time(15, 0), time(14, 0))
    assert announced.change_id == audit.payload["change_id"]
    assert row.idempotency_key == f"schedule:dinner:{announced.change_id}"


async def test_each_change_is_its_own_announcement(house: House) -> None:
    await update(house, "Kim", EARLIER)
    await update(house, "Sam", DINNER)
    await update(house, "Kim", EARLIER)
    assert await house.kinds() == ["schedule_changed"] * 3
    assert len(await house.audits("household.slot_schedule_updated")) == 3


async def test_a_save_that_changes_nothing_writes_and_announces_nothing(house: House) -> None:
    assert await update(house, "Kim", DINNER) is False
    assert await house.outbox() == [] and await house.audits("household.slot") == []


async def test_bad_or_stale_saves_change_nothing(house: House) -> None:
    with pytest.raises(sched.InvalidSchedule, match="usual meal time"):
        await update(house, "Kim", replace(DINNER, meal_time=time(16, 0)))
    with pytest.raises(schedule_service.ScheduleMoved, match="Someone changed the dinner schedule"):
        await update(house, "Kim", EARLIER, seen=sched.slot_fingerprint(EARLIER))
    house.people["Kim"] = replace(house["Kim"], active=False)
    with pytest.raises(Forbidden, match="Only active members"):
        await update(house, "Kim", EARLIER)
    with pytest.raises(Forbidden):
        await stored(house)
    assert await house.outbox() == [] and await house.audits("household.slot") == []


async def test_a_change_applies_from_the_next_call(house: House, clock: FakeClock) -> None:
    """Today's call is open and tomorrow's is scheduled: both keep the times they were created
    with. The first call on the new schedule is the day after tomorrow's."""
    clock.now = OPENS
    await house.tick()
    async with house.sessionmaker() as db:
        await calls.start_call(db, house["Kim"], Slot.DINNER, date(2026, 10, 23), now=clock())
    today, tomorrow = await dinner_id(house), await dinner_id(house, date(2026, 10, 23))
    before = [await house.event(today), await house.event(tomorrow)]
    assert [e.state for e in before] == [EventState.COLLECTING, EventState.SCHEDULED]

    assert await update(house, "Sam", replace(EARLIER, vote_minutes=30, reminder_minutes=10)) is True
    (row,) = [e for e in await house.outbox() if e.kind == "schedule_changed"]
    assert row.payload["starts_on"] == "2026-10-24"
    assert [await house.event(today), await house.event(tomorrow)] == before

    clock.now = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)  # 3 PM tomorrow: its call opens as scheduled
    await house.tick()
    opened = await house.event(tomorrow)
    assert opened.state is EventState.COLLECTING
    assert (opened.respond_deadline, opened.vote_minutes) == (before[1].respond_deadline, 45)

    clock.now = datetime(2026, 10, 24, 18, 59, tzinfo=UTC)  # 1:59 PM the day after: not yet
    await house.tick()
    with pytest.raises(AssertionError):
        await dinner_id(house, date(2026, 10, 24))
    clock.now = datetime(2026, 10, 24, 19, 0, tzinfo=UTC)  # 2 PM: the new call time
    await house.tick()
    fresh = await house.event(await dinner_id(house, date(2026, 10, 24)))
    assert fresh.state is EventState.COLLECTING
    assert fresh.respond_deadline == datetime(2026, 10, 24, 20, 0, tzinfo=UTC)  # 60 minutes to answer
    assert (fresh.vote_minutes, fresh.reminder_minutes) == (30, 10)


async def test_the_announcement_drains_without_an_event(house: House) -> None:
    await update(house, "Kim", EARLIER)
    channel = RecordingChannel()
    result = await house.drain(channel)
    assert result.delivered == 1
    assert channel.kinds == ["schedule_changed"]


# Slack -------------------------------------------------------------------------------------------


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


async def test_the_announcement_is_posted_in_the_channel(slack: Slack, house: House) -> None:
    await update(house, "Kim", EARLIER)
    slack.fake.clear()
    await slack.drain()
    (post,) = slack.fake.of("chat.postMessage")
    assert post.args["channel"] == CHANNEL and post.args.get("thread_ts") is None
    assert post.args["text"].startswith("Kim changed the dinner schedule, starting Thursday, Oct 22: call time")
    assert "<@" not in json.dumps(post.args["blocks"])  # it names the member, and pings nobody


async def test_slash_schedule_shows_the_schedule_and_links_to_the_web(slack: Slack, house: House) -> None:
    await update(house, "Kim", EARLIER)
    slack.fake.clear()
    await slack.command("Sam", "schedule")
    (reply,) = slack.fake.of("chat.postEphemeral")
    assert (reply.args["channel"], reply.args["user"]) == (CHANNEL, SLACK_IDS["Sam"])
    text = json.dumps(reply.args["blocks"])
    assert "the call goes out at *2:00 PM*. Answers close at 3:00 PM, voting at 3:45 PM" in text
    assert "*Lunch:* no daily call." in text and "https://dinnerbell.test/schedule" in text
    assert "coming soon" not in reply.args["text"]


async def test_slash_schedule_falls_back_to_a_direct_message(slack: Slack) -> None:
    slack.fake.fail("chat.postEphemeral", Failure("channel_not_found"))
    await slack.command("Sam", "schedule")
    (dm,) = slack.fake.of("chat.postMessage")
    assert dm.args["channel"] == SLACK_IDS["Sam"] and "the call goes out at *3:00 PM*" in json.dumps(dm.args)


async def test_slash_schedule_refuses_strangers(slack: Slack) -> None:
    await slack.command("U9STRANGER", "schedule")
    (refusal,) = slack.ephemerals("U9STRANGER")
    assert refusal.startswith("I don") and "When calls go out" not in refusal
