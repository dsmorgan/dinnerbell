# SPDX-License-Identifier: AGPL-3.0-or-later
"""Telling the channel about badges and the month (M4 slice 2, FR-39, FR-40): each badge
announced once however the app restarts or retries, one post per done call, the caps, the
privacy rules, the monthly recap's marker, month boundaries and time zones, and the badges on
App Home."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select, text

from dinnerbell.channels.slack.badge_text import tag
from dinnerbell.config import Settings
from dinnerbell.db.contributions import BadgeRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.models import BadgeAwardRow, ContributionRow
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.badges import Announcement, BadgeKey, Tier
from dinnerbell.domain.recap import bounds_utc, due_month, month_key, next_month, previous_month
from dinnerbell.scheduler import runner
from dinnerbell.services import announcements, contributions, recap, scrub, stats
from dinnerbell.services.clock import Clock
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.health import SchedulerState
from tests.support.calls import House, RecordingChannel, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import BOT_USER, FakeSlack
from tests.support.slack import SLACK_IDS, connection, link, wire
from tests.test_contributions import dinner

pytestmark = pytest.mark.anyio

CHICAGO = ZoneInfo("America/Chicago")
NOV_1_MORNING = datetime(2026, 11, 1, 9, 0, tzinfo=CHICAGO).astimezone(UTC)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def posts(house: House, kind: str) -> list[note.Notification]:
    return [note.from_payload(e.kind, e.payload) for e in await house.outbox() if e.kind == kind]


async def pending(house: House) -> list[tuple[str, str, str]]:
    async with house.sessionmaker() as db:
        rows = await db.scalars(select(BadgeAwardRow).where(BadgeAwardRow.announcement == "pending"))
        return sorted((str(r.member_id), r.badge_key, r.tier) for r in rows)


async def states(house: House) -> set[str]:
    async with house.sessionmaker() as db:
        return set(await db.scalars(select(BadgeAwardRow.announcement)))


async def announce(house: House) -> int:
    return await announcements.announce_badges(house.sessionmaker, house.clock())


async def award(
    house: House,
    name: str,
    badge: BadgeKey,
    tier: Tier,
    *,
    how: Announcement = Announcement.PENDING,
    at: Clock | None = None,
) -> None:
    async with house.sessionmaker() as db:
        await BadgeRepository(db).add(
            house.household.id,
            house[name].id,
            badge,
            tier,
            now=(at or house.clock)(),
            announcement=how,
        )
        await db.commit()


async def marker(house: House) -> Any:
    async with house.sessionmaker() as db:
        return (await household_settings(db, house.household.id)).get("recap_posted")


# Announcing a badge once ---------------------------------------------------------------------------


async def test_a_done_call_announces_its_badges_once_in_one_post(house: House) -> None:
    await dinner(house)
    waiting = await pending(house)
    assert waiting  # the call earned first badges for Alex and Sam, and nothing has told the channel
    assert await announce(house) == 1
    assert await pending(house) == [] and await states(house) == {"announced"}
    (post,) = await posts(house, "badges_earned")
    assert isinstance(post, note.BadgesEarned)
    assert [n.person.name for n in post.news] == ["Alex", "Sam"]  # by name, never by what they earned
    assert {b.tier for n in post.news for b in n.badges} == {"First"}
    assert sum(len(n.badges) for n in post.news) + sum(n.more for n in post.news) == len(waiting)
    # Another tick, a restart, a second process: nothing more.
    assert await announce(house) == 0 and await announce(house) == 0
    assert len(await posts(house, "badges_earned")) == 1


async def test_the_post_is_delivered_once_and_never_again(house: House) -> None:
    await dinner(house)
    await announce(house)
    channel = RecordingChannel()
    await house.drain(channel)
    await announce(house)
    await house.drain(channel)
    assert channel.kinds.count("badges_earned") == 1
    assert channel.sent[0].recipient is None  # a channel post, not a direct message


async def test_a_crash_before_the_commit_loses_nothing_and_posts_nothing(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    await dinner(house)
    waiting = await pending(house)

    async def crash(*_: Any, **__: Any) -> int:
        raise RuntimeError("killed between queueing and marking")

    with monkeypatch.context() as patch:
        patch.setattr(BadgeRepository, "mark_announced", crash)
        assert await announce(house) == 0  # the failure is logged and rolled back
    assert (await house.kinds()).count("badges_earned") == 0  # the queued row went with the rollback
    assert await pending(house) == waiting  # still pending, so the next tick announces it
    assert await announce(house) == 1
    assert await pending(house) == [] and len(await posts(house, "badges_earned")) == 1


async def test_two_ticks_at_once_announce_once(house: House) -> None:
    await dinner(house)
    queued = await asyncio.gather(announce(house), announce(house), announce(house))
    assert sorted(queued) == [0, 0, 1]
    assert len(await posts(house, "badges_earned")) == 1


async def test_a_repeated_write_of_the_same_post_queues_nothing_new(house: House) -> None:
    """Belt and braces: the outbox row is keyed by the post's first award."""
    await dinner(house)
    await announce(house)
    (row,) = [r for r in await house.outbox() if r.kind == "badges_earned"]
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE badge_award SET announcement = 'pending', announced_at = NULL"))
        await db.commit()
    await announce(house)  # the awards come back pending somehow: the same post, the same key
    assert len([r for r in await house.outbox() if r.kind == "badges_earned"]) == 1
    assert row.idempotency_key.startswith("badges:")


async def test_each_done_call_gets_its_own_post(house: House) -> None:
    await dinner(house, n=0)
    await dinner(house, n=1, ins=("Alex", "Kim"), pickup="Kim")
    assert await announce(house) == 2
    first, second = await posts(house, "badges_earned")
    assert isinstance(first, note.BadgesEarned) and isinstance(second, note.BadgesEarned)
    assert [n.person.name for n in second.news] == ["Kim"]  # Alex's firsts went out with call one


async def test_a_post_only_names_what_the_household_already_sees(house: House) -> None:
    await dinner(house)
    await announce(house)
    text_ = json.dumps([e.payload for e in await house.outbox() if e.kind == "badges_earned"])
    for secret in ("feedback", "veto", "not_again", "preference", "@example.com"):
        assert secret not in text_.lower()


# Food Critic and quiet awards ---------------------------------------------------------------------


async def test_food_critic_gets_no_post_and_is_marked_announced_not_pending(house: House) -> None:
    await award(house, "Kim", BadgeKey.FOOD_CRITIC, Tier.FIRST)
    assert await announce(house) == 0
    assert await posts(house, "badges_earned") == [] and await pending(house) == []
    assert await states(house) == {"announced"}
    # Beside another badge it is left out of the post: no mention, no name.
    await award(house, "Kim", BadgeKey.FOOD_CRITIC, Tier.BRONZE)
    await award(house, "Alex", BadgeKey.CHEF, Tier.FIRST)
    assert await announce(house) == 1
    (post,) = await posts(house, "badges_earned")
    assert isinstance(post, note.BadgesEarned)
    assert [n.person.name for n in post.news] == ["Alex"] and "Critic" not in json.dumps(post.news, default=str)
    assert await pending(house) == []


async def test_a_food_critic_award_already_skipped_stays_skipped(house: House) -> None:
    """Awards made while Food Critic was private were marked ``skipped``; they stay that way."""
    await award(house, "Kim", BadgeKey.FOOD_CRITIC, Tier.FIRST, how=Announcement.SKIPPED)
    assert await announce(house) == 0 and await announce(house) == 0
    assert await posts(house, "badges_earned") == [] and await states(house) == {"skipped"}


async def test_gambler_is_announced_by_name_because_votes_are_visible_on_the_call(house: House) -> None:
    await award(house, "Kim", BadgeKey.GAMBLER, Tier.FIRST)
    assert await announce(house) == 1
    (post,) = await posts(house, "badges_earned")
    assert isinstance(post, note.BadgesEarned)
    (news,) = post.news
    assert news.person.name == "Kim" and [(b.name, b.tier) for b in news.badges] == [("Gambler", "First")]
    assert await pending(house) == [] and await states(house) == {"announced"}


async def give_feedback(house: House, who: str) -> None:
    from dinnerbell.domain.feedback import FeedbackInput, Rating
    from dinnerbell.services import feedback

    async with house.sessionmaker() as db:
        meal = (await house.meals())[0]
        await feedback.give(db, house[who], meal.id, FeedbackInput(Rating.LOVED, None, frozenset()), now=house.clock())


async def test_feedback_days_after_the_call_earns_the_badge_without_a_post(house: House) -> None:
    await dinner(house)
    await announce(house)
    assert len(await posts(house, "badges_earned")) == 1  # the call's post
    house.clock.now += timedelta(days=3)
    await give_feedback(house, "Alex")
    mine = [a for a in await _awards(house) if a[1] is BadgeKey.FOOD_CRITIC]
    assert mine == [("Alex", BadgeKey.FOOD_CRITIC, Tier.FIRST, Announcement.PENDING)]
    assert await announce(house) == 0
    mine = [a for a in await _awards(house) if a[1] is BadgeKey.FOOD_CRITIC]
    assert mine == [("Alex", BadgeKey.FOOD_CRITIC, Tier.FIRST, Announcement.ANNOUNCED)]
    assert len(await posts(house, "badges_earned")) == 1  # still only the call's post
    async with house.sessionmaker() as db:
        recent = await BadgeRepository(db).recent(house.household.id, 20)
    assert BadgeKey.FOOD_CRITIC in [a.badge for a in recent]  # listed among the newest badges


async def test_a_food_critic_tier_up_never_posts(house: House) -> None:
    for tier in (Tier.FIRST, Tier.BRONZE, Tier.SILVER):
        await award(house, "Alex", BadgeKey.FOOD_CRITIC, tier)
        assert await announce(house) == 0
    assert await posts(house, "badges_earned") == []


async def _awards(house: House) -> list[tuple[str, BadgeKey, Tier, Announcement]]:
    from tests.test_contributions import awards

    return await awards(house)


async def test_a_member_who_left_is_not_announced(house: House) -> None:
    await award(house, "Lee", BadgeKey.CHEF, Tier.FIRST)
    async with house.sessionmaker() as db:
        from dinnerbell.db.repositories import MemberRepository

        await MemberRepository(db).update(house.household.id, house["Lee"].id, house.clock(), active=False)
        await db.commit()
    assert await announce(house) == 0
    assert await states(house) == {"skipped"}


async def test_a_backfill_and_a_lowered_threshold_announce_nothing(house: House) -> None:
    await dinner(house, n=0)
    await dinner(house, n=1)
    await announce(house)
    before = len(await posts(house, "badges_earned"))
    # The upgrade's catch-up: counts and awards wiped, then counted again from the log.
    async with house.sessionmaker() as db:
        await db.execute(delete(BadgeAwardRow))
        await db.execute(delete(ContributionRow))
        stored = await household_settings(db, house.household.id)
        stored.pop(contributions.BACKFILLED_KEY, None)
        await HouseholdRepository(db).update_settings(house.household.id, stored)
        await db.commit()
    async with house.sessionmaker() as db:
        await contributions.ensure_backfilled(db, house.household.id, now=house.clock())
    assert await states(house) == {"skipped"}
    assert await announce(house) == 0
    # A lowered count awards the tiers people already reach, quietly.
    async with house.sessionmaker() as db:
        await contributions.set_thresholds(db, house["Alex"], [1, 2, 3, 4, 5], now=house.clock())
    assert "pending" not in await states(house)
    assert await announce(house) == 0
    assert len(await posts(house, "badges_earned")) == before


# Caps ---------------------------------------------------------------------------------------------


async def test_a_big_backlog_is_capped_in_people_and_badges(house: House) -> None:
    from dinnerbell.domain.badges import BADGES

    names = [f"Member {n:02d}" for n in range(14)]
    for name in names:
        await house.add_member(name)
    for badge in BADGES:  # Kim reaches every tier of every badge at once
        for tier in Tier:
            await award(house, "Kim", badge.key, tier)
    for name in names:
        await award(house, name, BadgeKey.SCOUT, Tier.FIRST)
    assert await announce(house) == 1  # no call behind them: one post for all
    (post,) = await posts(house, "badges_earned")
    assert isinstance(post, note.BadgesEarned)
    assert len(post.news) == announcements.MAX_PEOPLE and post.more_people == 15 - announcements.MAX_PEOPLE
    kim = next(n for n in post.news if n.person.name == "Kim")
    assert (
        len(kim.badges) == announcements.MAX_BADGES and kim.more == 12 - announcements.MAX_BADGES
    )  # thirteen badges, less the silent Food Critic
    assert {b.tier for b in kim.badges} == {"Diamond"}  # five tiers at once show as the highest
    assert await pending(house) == []  # all of it is marked announced, shown or not


# The Slack message, and the test instance's guard -------------------------------------------------


IN_CHANNEL = [BOT_USER, SLACK_IDS["Alex"], SLACK_IDS["Sam"]]


async def test_a_labeled_instance_mentions_only_people_in_the_channel(house: House) -> None:
    for name, slack_id in SLACK_IDS.items():
        await link(house, name, slack_id)
    post = note.BadgesEarned(
        house.household.id,
        1,
        (
            note.BadgeNews(note.PersonInfo(house["Kim"].id, "Kim"), (note.BadgeTier("Chef", "First"),)),
            note.BadgeNews(note.PersonInfo(house["Sam"].id, "Sam"), (note.BadgeTier("Scout", "First"),)),
        ),
    )
    for label, kim_mention in (("", f"<@{SLACK_IDS['Kim']}>"), ("TEST", "Kim")):
        fake = FakeSlack()
        fake.channel_members = list(IN_CHANNEL)
        conn = connection(house, fake, instance_label=label)
        assert await conn.run_once()
        fake.clear()
        await conn.channel.deliver(post)
        (call,) = fake.of("chat.postMessage")
        body = str(call.args["blocks"])
        assert f"{kim_mention} earned" in body
        assert f"<@{SLACK_IDS['Sam']}> earned" in body
        if label:
            assert f"<@{SLACK_IDS['Kim']}>" not in body  # a name, not a ping
        assert "<!channel>" not in body and "<!here>" not in body and call.args["channel"] != SLACK_IDS["Kim"]


# The monthly recap --------------------------------------------------------------------------------


async def recapped(house: House) -> int:
    return await recap.post_recaps(house.sessionmaker, house.clock())


async def october(house: House) -> None:
    """Three October dinners, then the clock still in October."""
    for n in range(3):
        await dinner(house, n=n, pickup="Alex" if n else "Kim")
    await announce(house)


async def test_the_recap_posts_once_on_the_first_of_the_month(house: House) -> None:
    await october(house)
    house.clock.now = datetime(2026, 11, 1, 8, 59, tzinfo=CHICAGO)
    assert await recapped(house) == 0 and await marker(house) is None  # a morning post, not a midnight one
    house.clock.now = NOV_1_MORNING
    assert await recapped(house) == 1
    assert await marker(house) == "2026-10"
    for later in (timedelta(0), timedelta(minutes=1), timedelta(hours=9), timedelta(days=3)):
        house.clock.now = NOV_1_MORNING + later
        assert await recapped(house) == 0  # restarts and later ticks find the marker
    (post,) = await posts(house, "monthly_recap")
    assert isinstance(post, note.MonthlyRecap)
    assert post.month == date(2026, 10, 1) and post.meals == 3
    assert post.cuisines and post.new_items >= 3  # the furnished catalog was created in the month
    channel = RecordingChannel()
    await house.drain(channel)
    house.clock.now = NOV_1_MORNING + timedelta(days=1)
    await recapped(house)
    await house.drain(channel)
    assert channel.kinds.count("monthly_recap") == 1


async def test_the_recap_thanks_people_by_group_with_no_counts(house: House) -> None:
    await october(house)  # Kim picked up once, Alex twice, Sam coordinated three times
    house.clock.now = NOV_1_MORNING
    await recapped(house)
    (post,) = await posts(house, "monthly_recap")
    assert isinstance(post, note.MonthlyRecap)
    groups = {g.kind: [p.name for p in g.people] for g in post.thanks}
    assert groups == {"picked_up": ["Alex", "Kim"], "coordinated": ["Sam"]}  # A to Z, not most to least
    assert "Lee" not in json.dumps(post.thanks, default=str)  # Lee never pitched in: no line says so
    from dinnerbell.channels.slack.messages import monthly_recap_message

    text_ = json.dumps(monthly_recap_message(post).blocks)
    assert "Picked up:* Alex and Kim" in text_ and "Coordinated:* Sam" in text_
    for word in ("most", "least", "top", "rank", "leaderboard", "#1", "fewest", " times"):
        assert word not in text_.lower()


async def test_the_recap_names_only_active_members(house: House) -> None:
    await october(house)
    async with house.sessionmaker() as db:
        from dinnerbell.db.repositories import MemberRepository

        await MemberRepository(db).update(house.household.id, house["Kim"].id, house.clock(), active=False)
        await db.commit()
    house.clock.now = NOV_1_MORNING
    await recapped(house)
    (post,) = await posts(house, "monthly_recap")
    assert isinstance(post, note.MonthlyRecap)
    assert "Kim" not in json.dumps(post.thanks, default=str) + json.dumps(post.badges, default=str)
    assert "Alex" in json.dumps(post.thanks, default=str)


async def test_the_recap_lists_food_critic_but_not_quiet_awards(house: House) -> None:
    await october(house)
    await award(house, "Kim", BadgeKey.FOOD_CRITIC, Tier.FIRST, how=Announcement.ANNOUNCED)
    await award(house, "Lee", BadgeKey.CHEF, Tier.FIRST, how=Announcement.SKIPPED)
    house.clock.now = NOV_1_MORNING
    await recapped(house)
    (post,) = await posts(house, "monthly_recap")
    assert isinstance(post, note.MonthlyRecap)
    shown = json.dumps(post.badges, default=str)
    assert "Food Critic" in shown and "Kim" in shown and "Lee" not in shown


async def test_a_missed_first_posts_once_on_the_next_tick_within_the_window(house: House) -> None:
    await october(house)
    house.clock.now = NOV_1_MORNING + timedelta(days=4)  # the app was down on the first
    assert await recapped(house) == 1 and await recapped(house) == 0
    assert [p.month for p in await posts(house, "monthly_recap")] == [date(2026, 10, 1)]  # type: ignore[attr-defined]


async def test_a_long_downtime_recaps_only_the_month_just_ended_and_never_twice(house: House) -> None:
    await october(house)
    async with house.sessionmaker() as db:
        await HouseholdRepository(db).update_settings(house.household.id, {"recap_posted": "2026-07"})
        await db.commit()
    house.clock.now = NOV_1_MORNING + timedelta(days=1)
    assert await recapped(house) == 1  # August and September are not replayed
    assert await marker(house) == "2026-10"
    house.clock.now = NOV_1_MORNING + timedelta(days=40)
    assert await recapped(house) == 0
    assert len(await posts(house, "monthly_recap")) == 1


async def test_a_recap_past_the_window_is_left_out(house: House) -> None:
    await october(house)
    house.clock.now = datetime(2026, 11, 9, 12, 0, tzinfo=CHICAGO)
    assert await recapped(house) == 0 and await marker(house) is None
    assert await posts(house, "monthly_recap") == []


async def test_a_month_with_no_meals_posts_nothing_and_moves_on(house: House) -> None:
    house.clock.now = datetime(2026, 12, 1, 9, 0, tzinfo=CHICAGO)
    assert await recapped(house) == 0
    assert await marker(house) == "2026-11"  # decided once, not asked again on every tick
    assert await posts(house, "monthly_recap") == []
    assert await recapped(house) == 0


async def test_two_ticks_at_once_post_the_recap_once(house: House) -> None:
    await october(house)
    house.clock.now = NOV_1_MORNING
    assert sorted(await asyncio.gather(recapped(house), recapped(house), recapped(house))) == [0, 0, 1]


async def test_a_crash_while_recapping_posts_nothing_and_tries_again(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    await october(house)
    house.clock.now = NOV_1_MORNING

    async def crash(*_: Any, **__: Any) -> None:
        raise RuntimeError("killed before the commit")

    with monkeypatch.context() as patch:
        patch.setattr(HouseholdRepository, "update_settings", crash)
        assert await recapped(house) == 0
    assert await marker(house) is None and await posts(house, "monthly_recap") == []
    assert await recapped(house) == 1


async def test_the_recap_follows_the_household_clock_not_utc(house: House) -> None:
    await october(house)
    house.clock.now = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # Nov 1 in UTC, 12:30 a.m. in Chicago
    assert await recapped(house) == 0
    house.clock.now = datetime(2026, 11, 1, 14, 59, tzinfo=UTC)  # 8:59 a.m. CST: DST ended that night
    assert await recapped(house) == 0
    house.clock.now = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)  # 9:00 a.m. CST
    assert await recapped(house) == 1


async def test_a_simulated_month_change_with_the_clock_posts_once(house: House) -> None:
    await october(house)
    house.clock.now = datetime(2026, 10, 31, 23, 59, tzinfo=CHICAGO)
    assert await recapped(house) == 0
    house.clock.advance(timedelta(hours=9, minutes=1))  # across midnight into Nov 1, 9:00 a.m.
    assert await recapped(house) == 1
    house.clock.advance(timedelta(hours=1))
    assert await recapped(house) == 0


async def test_the_scheduler_tick_announces_and_recaps_then_drains(house: House, migrated: Settings) -> None:
    for n in range(2):
        await dinner(house, n=n)
    await award(house, "Lee", BadgeKey.ORDER_UP, Tier.FIRST)
    house.clock.now = NOV_1_MORNING
    channel = RecordingChannel()
    ctx = runner.JobContext(
        state=SchedulerState(),
        clock=house.clock,
        zone=CHICAGO,
        database_path=migrated.database_path,
        backup_dir=migrated.backup_dir,
        sessionmaker=house.sessionmaker,
        channel=channel,
    )
    await runner.call_tick(ctx)
    assert channel.kinds.count("monthly_recap") == 1 and channel.kinds.count("badges_earned") >= 2
    assert await pending(house) == []
    sent = len(channel.sent)
    await runner.call_tick(ctx)
    assert len(channel.sent) == sent  # the second tick has nothing new to say


async def test_a_failing_announcement_does_not_stop_the_tick(
    house: House, migrated: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*_: Any, **__: Any) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(announcements, "announce_badges", broken)
    monkeypatch.setattr(recap, "post_recaps", broken)
    ctx = runner.JobContext(
        state=SchedulerState(),
        clock=house.clock,
        zone=CHICAGO,
        database_path=migrated.database_path,
        backup_dir=migrated.backup_dir,
        sessionmaker=house.sessionmaker,
        channel=RecordingChannel(),
    )
    with caplog.at_level(logging.ERROR):
        await runner.call_tick(ctx)
    assert "badge announcements failed" in caplog.text and "monthly recap failed" in caplog.text


# Month boundaries and time zones ------------------------------------------------------------------


def test_months_wrap_the_year_and_leap_february() -> None:
    assert previous_month(date(2027, 1, 5)) == date(2026, 12, 1)
    assert previous_month(date(2026, 3, 1)) == date(2026, 2, 1)
    assert next_month(date(2026, 12, 1)) == date(2027, 1, 1)
    assert next_month(date(2028, 2, 1)) == date(2028, 3, 1)
    assert month_key(date(2026, 3, 9)) == "2026-03"


@pytest.mark.parametrize(
    ("local", "posted", "expected"),
    [
        (datetime(2026, 11, 1, 8, 59, tzinfo=CHICAGO), None, None),
        (datetime(2026, 11, 1, 9, 0, tzinfo=CHICAGO), None, date(2026, 10, 1)),
        (datetime(2026, 11, 1, 9, 0, tzinfo=CHICAGO), "2026-10", None),
        (datetime(2026, 11, 1, 9, 0, tzinfo=CHICAGO), "2026-11", None),  # a marker from the future: leave it
        (datetime(2026, 11, 1, 9, 0, tzinfo=CHICAGO), "2026-09", date(2026, 10, 1)),
        (datetime(2026, 11, 7, 23, 59, tzinfo=CHICAGO), None, date(2026, 10, 1)),
        (datetime(2026, 11, 8, 0, 0, tzinfo=CHICAGO), None, None),
        (datetime(2027, 1, 1, 9, 0, tzinfo=CHICAGO), "2026-11", date(2026, 12, 1)),
        (datetime(2026, 11, 1, 9, 0, tzinfo=CHICAGO), 202610, date(2026, 10, 1)),  # junk reads as none
        (datetime(2026, 11, 1, 9, 0, tzinfo=CHICAGO), "garbage", date(2026, 10, 1)),
        (datetime(2026, 11, 1, 9, 0, tzinfo=CHICAGO), "2099-01", date(2026, 10, 1)),  # a future marker is a mistake
        (datetime(2026, 11, 1, 9, 0, tzinfo=CHICAGO), "9999-99", date(2026, 10, 1)),
    ],
)
def test_when_a_recap_is_due(local: datetime, posted: object, expected: date | None) -> None:
    assert due_month(local, posted) == expected


def test_a_month_runs_from_local_midnight_across_a_clock_change() -> None:
    start, end = bounds_utc(date(2026, 11, 1), CHICAGO)  # CDT at the start, CST at the end
    assert (start, end) == (datetime(2026, 11, 1, 5, 0, tzinfo=UTC), datetime(2026, 12, 1, 6, 0, tzinfo=UTC))
    start, end = bounds_utc(date(2027, 3, 1), CHICAGO)  # spring forward on the 14th
    assert (start, end) == (datetime(2027, 3, 1, 6, 0, tzinfo=UTC), datetime(2027, 4, 1, 5, 0, tzinfo=UTC))
    assert bounds_utc(date(2026, 10, 1), ZoneInfo("Pacific/Auckland"))[0] == datetime(2026, 9, 30, 11, 0, tzinfo=UTC)


async def test_a_badge_earned_at_the_edge_of_the_month_counts_in_its_local_month(house: House) -> None:
    await dinner(house)  # a meal in October so the recap exists
    edge = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)  # Nov 1, 00:00 in Chicago
    await award(
        house, "Kim", BadgeKey.SCOUT, Tier.FIRST, at=lambda: edge - timedelta(minutes=1), how=Announcement.ANNOUNCED
    )
    await award(house, "Lee", BadgeKey.SCOUT, Tier.FIRST, at=lambda: edge, how=Announcement.ANNOUNCED)
    async with house.sessionmaker() as db:
        built = await recap.build(db, house.household.id, date(2026, 10, 1), CHICAGO)
    assert built is not None
    assert "Kim" in [n.person.name for n in built.badges] and "Lee" not in [n.person.name for n in built.badges]


# The test instance --------------------------------------------------------------------------------


async def test_scrub_keeps_a_restored_copy_quiet(house: House, clock: FakeClock) -> None:
    await dinner(house)  # leaves awards pending, as production would between ticks
    assert await pending(house)
    clock.now = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    async with house.sessionmaker() as db:
        summary = await scrub.scrub_test(db, instance_label="TEST", keep=[house["Alex"].email], now=clock())
    assert summary.badges_quieted > 0
    assert await pending(house) == [] and await states(house) == {"skipped"}
    assert await marker(house) == "2026-10"  # October's recap is not posted into the test channel
    assert await announce(house) == 0 and await recapped(house) == 0


# App Home -----------------------------------------------------------------------------------------


def home_text(view: dict[str, Any]) -> str:
    return json.dumps(view, ensure_ascii=False)


def no_art(key: str, tier: Tier) -> bytes:
    raise RuntimeError("no art in this test")


async def test_home_shows_a_members_own_badges_and_the_next_one(house: House) -> None:
    """The text tags, which show when art can't (tests/test_slack_badge_art.py has the art)."""
    await dinner(house)
    slack = await wire(house, art_for=no_art)
    await slack.open_home("Alex")
    home = slack.last_home("Alex")
    body = home_text(home)
    assert "Your badges" in body
    assert tag("Full Table", "First") in body and tag("Road Runner", "First") in body
    assert "Next:" in body and " to go (" in body
    # Only their own: Sam's Quarterback is not on Alex's Home.
    assert "Quarterback" not in body
    await slack.open_home("Sam")
    assert tag("Quarterback", "First") in home_text(slack.last_home("Sam"))


async def test_home_for_a_member_with_no_badges_invites_the_first(house: House) -> None:
    slack = await wire(house)
    await slack.open_home("Kim")
    body = home_text(slack.last_home("Kim"))
    assert "Your badges" in body and "None yet" in body and "Next:" in body


async def test_home_for_a_guest_has_no_badges(house: House) -> None:
    from dinnerbell.db.repositories import MemberRepository
    from dinnerbell.domain.members import Role

    async with house.sessionmaker() as db:
        await MemberRepository(db).update(
            house.household.id, house["Lee"].id, house.clock(), role=Role.GUEST, email=None
        )
        await db.commit()
    slack = await wire(house)
    await slack.open_home("Lee")
    assert "Your badges" not in home_text(slack.last_home("Lee"))


async def test_home_still_draws_when_the_badges_cannot_be_read(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("no stats today")

    monkeypatch.setattr(stats, "my_stats", broken)
    slack = await wire(house)
    await slack.open_home("Kim")
    body = home_text(slack.last_home("Kim"))
    assert "Your badges" not in body and "Your preferences" in body


# Rendering ----------------------------------------------------------------------------------------


def test_a_big_post_keeps_its_mentions_and_its_size_in_bounds() -> None:
    from dinnerbell.channels.slack import preview as samples
    from dinnerbell.channels.slack.messages import render
    from tests.support.blockkit import check_message

    for kind in ("badges_earned_many", "monthly_recap_big"):
        message = render(samples.NOTIFICATIONS[kind](), _ctx())
        check_message(message.payload())
        assert json.dumps(message.blocks).count("<@") <= announcements.MAX_PEOPLE
        assert "<!channel>" not in json.dumps(message.blocks)


def _ctx() -> Any:
    from dinnerbell.channels.slack import preview as samples
    from dinnerbell.channels.slack.messages import RenderContext

    return RenderContext(samples.BASE_URL, samples.DIRECTORY)


def test_the_recap_never_mentions_anyone() -> None:
    from dinnerbell.channels.slack import preview as samples
    from dinnerbell.channels.slack.messages import render

    assert "<@" not in json.dumps(render(samples.NOTIFICATIONS["monthly_recap"](), _ctx()).blocks)


def test_next_up_points_at_the_closest_started_badge() -> None:
    from dinnerbell.channels.slack import preview as samples
    from dinnerbell.domain.badges import next_up

    step = next_up(samples.KIM_BADGES.progress)
    assert step is not None and step.badge.key is BadgeKey.CHEF and step.remaining == 7
    fresh = next_up(samples.KIM_NO_BADGES.progress)
    assert fresh is not None and fresh.badge.key is BadgeKey.CHEF and fresh.count == 0


def test_the_caps_hold_at_the_boundary() -> None:
    """One over each cap is counted, not shown: 11 members and 7 badges."""
    from datetime import datetime as dt

    from dinnerbell.domain.badges import BADGES, Award
    from dinnerbell.domain.members import Capabilities, Member, Role

    members = {
        n: Member(n, 1, f"Member {n:02d}", f"m{n}@example.com", Role.MEMBER, Capabilities(), True) for n in range(1, 12)
    }
    when = dt(2026, 10, 22, tzinfo=UTC)
    got = [Award(n, 1, 1, b.key, Tier.FIRST, when, Announcement.PENDING) for n, b in enumerate(BADGES[:7], 100)]
    got += [Award(200 + n, 1, n, BadgeKey.SCOUT, Tier.FIRST, when, Announcement.PENDING) for n in range(2, 12)]
    news, more_people = announcements.news_for(got, members)
    assert len(news) == announcements.MAX_PEOPLE == 10 and more_people == 1
    first = news[0]
    assert len(first.badges) == announcements.MAX_BADGES == 6 and first.more == 1
    exact, none_left = announcements.news_for(got[:6] + got[7:16], {n: members[n] for n in range(1, 11)})
    assert none_left == 0 and exact[0].more == 0 and len(exact) == 10


async def test_a_pending_food_critic_is_in_the_newest_badges_like_any_other(house: House) -> None:
    await award(house, "Kim", BadgeKey.FOOD_CRITIC, Tier.FIRST)
    await award(house, "Alex", BadgeKey.CHEF, Tier.FIRST)
    async with house.sessionmaker() as db:
        recent = await BadgeRepository(db).recent(house.household.id, 10)
    assert [a.badge for a in recent] == [BadgeKey.CHEF, BadgeKey.FOOD_CRITIC]


async def test_a_bad_time_zone_logs_one_warning_and_posts_nothing(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    from sqlalchemy import update

    from dinnerbell.db.models import HouseholdRow

    async with house.sessionmaker() as db:
        await db.execute(update(HouseholdRow).values(timezone="Mars/Olympus"))
        await db.commit()
    recap._BAD_ZONES.clear()
    house.clock.now = NOV_1_MORNING
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert await recapped(house) == 0
    assert [r.levelname for r in caplog.records if "time zone" in r.getMessage()] == ["WARNING"]
    assert not [r for r in caplog.records if r.exc_info]
