# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web app's switch and each member's choice below the web (#138, spec 6.15): the domain's
defaults and rules, the services, and what the outbox does with a Slack direct message to a
member on the web app. The pages are in ``test_web_app``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import select, text

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.events import EventState
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import (
    Capabilities,
    Member,
    MemberRuleViolation,
    NotifyVia,
    Role,
    check_notify_via,
    notify_choices,
)
from dinnerbell.services import outbox, web_app
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.health import outbox_backlog, outbox_oldest_age
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.slack import Slack, link, wire

pytestmark = pytest.mark.anyio


def person(**values: object) -> Member:
    base = Member(1, 1, "Kim", "kim@example.com", Role.MEMBER, Capabilities(), active=True)
    return replace(base, **values)  # type: ignore[arg-type]


# The domain ----------------------------------------------------------------------------------------


def test_a_member_with_slack_starts_on_slack_and_one_without_on_the_web_app() -> None:
    assert person(slack_user_id="U03KIM").delivery is NotifyVia.SLACK
    assert person().delivery is NotifyVia.WEB
    assert notify_choices(person(slack_user_id="U03KIM")) == (NotifyVia.SLACK, NotifyVia.WEB, NotifyVia.BOTH)
    assert notify_choices(person()) == (NotifyVia.WEB,)


@pytest.mark.parametrize("chosen", list(NotifyVia))
def test_a_choice_counts_only_while_the_member_has_slack(chosen: NotifyVia) -> None:
    """Unlinking puts them on the web app whatever they chose; linking again gives it back."""
    assert person(slack_user_id="U03KIM", notify_via=chosen).delivery is chosen
    assert person(notify_via=chosen).delivery is NotifyVia.WEB


def test_without_slack_only_the_web_app_is_allowed() -> None:
    for chosen in (NotifyVia.SLACK, NotifyVia.BOTH):
        with pytest.raises(MemberRuleViolation, match="only in the web app"):
            check_notify_via(person(), chosen)
    check_notify_via(person(), NotifyVia.WEB)
    for chosen in NotifyVia:
        check_notify_via(person(slack_user_id="U03KIM"), chosen)


def test_guests_stay_on_slack_and_choose_nothing() -> None:
    for slack in (None, "U05PAT"):
        guest = person(role=Role.GUEST, email=None, slack_user_id=slack, notify_via=NotifyVia.WEB)
        assert guest.delivery is NotifyVia.SLACK
        assert notify_choices(guest) == ()
        with pytest.raises(MemberRuleViolation, match="Guests"):
            check_notify_via(guest, NotifyVia.SLACK)
        assert not web_app.slack_skips(guest)


def test_slack_skips_only_active_members_on_the_web_app() -> None:
    assert web_app.slack_skips(person())
    assert web_app.slack_skips(person(slack_user_id="U03KIM", notify_via=NotifyVia.WEB))
    assert not web_app.slack_skips(person(active=False))  # the adapter drops these, as before
    for chosen in (None, NotifyVia.SLACK, NotifyVia.BOTH):
        assert not web_app.slack_skips(person(slack_user_id="U03KIM", notify_via=chosen))


def test_only_true_turns_it_on() -> None:
    assert web_app.web_app_on({"web_app": True})
    for settings in ({}, {"web_app": False}, {"web_app": "true"}, {"web_app": 1}, {"web_app": None}):
        assert not web_app.web_app_on(settings), settings


# The services --------------------------------------------------------------------------------------


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def switch(house: House, on: bool, actor: str = "Alex") -> bool:
    async with house.sessionmaker() as db:
        return await web_app.set_web_app(db, house[actor], on, now=house.clock())


async def audits(house: House, action: str) -> list[AuditRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AuditRow).where(AuditRow.action == action).order_by(AuditRow.id)))


async def test_the_switch_is_off_until_an_admin_turns_it_on_and_each_change_is_audited(house: House) -> None:
    async with house.sessionmaker() as db:
        assert not await web_app.web_app_enabled(db, house.household.id)
        assert not await web_app.web_app_enabled_for(db, None)
    assert await switch(house, True)
    assert not await switch(house, True)  # already on: nothing changes, nothing is audited
    async with house.sessionmaker() as db:
        assert await web_app.web_app_enabled(db, house.household.id)
        assert await web_app.web_app_enabled_for(db, None)  # a visitor gets the install's household
    assert await switch(house, False)
    rows = await audits(house, "household.web_app_updated")
    assert [(r.actor_member_id, r.target, r.payload) for r in rows] == [
        (house["Alex"].id, f"household:{house.household.id}", {"on": True}),
        (house["Alex"].id, f"household:{house.household.id}", {"on": False}),
    ]


async def test_only_an_admin_turns_it_on(house: House) -> None:
    with pytest.raises(Forbidden):
        await switch(house, True, actor="Sam")
    async with house.sessionmaker() as db:
        assert not await web_app.web_app_enabled(db, house.household.id)
    assert await audits(house, "household.web_app_updated") == []


async def test_without_a_household_it_is_off(database: Database) -> None:
    async with database.sessionmaker() as db:
        assert not await web_app.web_app_enabled_for(db, None)
        with pytest.raises(NotFound):
            await web_app.require_web_app(db, None)


async def test_turning_it_off_keeps_each_choice(house: House) -> None:
    await link(house, "Kim", "U03KIM")
    await switch(house, True)
    async with house.sessionmaker() as db:
        await web_app.set_my_notify_via(db, house["Kim"], NotifyVia.BOTH, now=house.clock())
    await switch(house, False)
    await house.refresh("Kim")
    assert house["Kim"].notify_via is NotifyVia.BOTH


async def test_a_choice_needs_the_switch_on_and_slack_for_slack(house: House) -> None:
    await link(house, "Kim", "U03KIM")
    async with house.sessionmaker() as db:
        with pytest.raises(NotFound):
            await web_app.set_my_notify_via(db, house["Kim"], NotifyVia.WEB, now=house.clock())
        with pytest.raises(NotFound):
            await web_app.set_member_notify_via(db, house["Alex"], house["Kim"].id, NotifyVia.WEB, now=house.clock())
    await switch(house, True)
    async with house.sessionmaker() as db:
        with pytest.raises(MemberRuleViolation):  # Lee has no Slack user
            await web_app.set_my_notify_via(db, house["Lee"], NotifyVia.SLACK, now=house.clock())
        with pytest.raises(MemberRuleViolation):
            await web_app.set_member_notify_via(db, house["Alex"], house["Lee"].id, NotifyVia.BOTH, now=house.clock())
        # Lee's only choice is what counts already, so nothing is stored, and linking moves them.
        await web_app.set_my_notify_via(db, house["Lee"], NotifyVia.WEB, now=house.clock())
        updated = await web_app.set_my_notify_via(db, house["Kim"], NotifyVia.WEB, now=house.clock())
    assert updated.notify_via is NotifyVia.WEB
    await house.refresh("Lee")
    assert house["Lee"].notify_via is None
    await link(house, "Lee", "U04LEE")
    assert house["Lee"].delivery is NotifyVia.SLACK
    assert await audits(house, "member.notify_via_set") == []  # a member's own choice isn't audited


async def test_an_admins_choice_for_a_member_is_audited(house: House) -> None:
    await link(house, "Kim", "U03KIM")
    await switch(house, True)
    async with house.sessionmaker() as db:
        await web_app.set_member_notify_via(db, house["Alex"], house["Kim"].id, NotifyVia.SLACK, now=house.clock())
        await web_app.set_member_notify_via(db, house["Alex"], house["Kim"].id, NotifyVia.BOTH, now=house.clock())
        with pytest.raises(Forbidden):
            await web_app.set_member_notify_via(db, house["Sam"], house["Kim"].id, NotifyVia.WEB, now=house.clock())
    rows = await audits(house, "member.notify_via_set")
    assert [(r.actor_member_id, r.target, r.payload) for r in rows] == [
        (house["Alex"].id, f"member:{house['Kim'].id}", {"from": "slack", "to": "both"})
    ]


# The outbox ----------------------------------------------------------------------------------------


DAY = date(2026, 10, 22)


async def a_call(house: House) -> note.EventInfo:
    async with house.sessionmaker() as db:
        await db.execute(
            text(
                "INSERT INTO event (id, household_id, kind, slot, date, state, run, version, opens_at, "
                "respond_deadline, reminder_minutes, vote_minutes, created_at, updated_at) VALUES "
                "(1, :h, 'call', 'dinner', '2026-10-22', 'collecting', 1, 1, :t, :t, 15, 45, :t, :t)"
            ),
            {"h": house.household.id, "t": "2026-10-22 20:00:00"},
        )
        await db.commit()
    now = house.clock()
    return note.EventInfo(1, house.household.id, Slot.DINNER, DAY, EventState.COLLECTING, 1, "/events/1", "UTC", now)


async def remind(house: House, event: note.EventInfo, *names: str) -> None:
    async with house.sessionmaker() as db:
        for name in names:
            reminder = note.ResponseReminder(event, note.PersonInfo(house[name].id, name))
            await OutboxRepository(db).enqueue(house.household.id, event.id, reminder, house.clock())
        await db.commit()


async def closed(house: House) -> dict[int, tuple[bool, bool, str | None]]:
    """Each row's recipient: (delivered, failed, last_error)."""
    async with house.sessionmaker() as db:
        rows = await OutboxRepository(db).for_household(house.household.id)
        found = {}
        for row in rows:
            state = (
                await db.execute(
                    text(
                        "SELECT delivered_at IS NOT NULL, failed_at IS NOT NULL, last_error FROM outbox WHERE id = :i"
                    ),
                    {"i": row.id},
                )
            ).one()
            found[int(row.payload["member"]["id"])] = (bool(state[0]), bool(state[1]), state[2])
    return found


@pytest.fixture
async def slack(house: House) -> Slack:
    """Kim has a Slack user; Lee has none."""
    return await wire(house, linked=("Alex", "Kim"))


async def test_switched_off_a_dm_to_a_member_without_slack_still_fails_for_good(house: House, slack: Slack) -> None:
    """Off is exactly as before: even a member who chose the web app while it was on gets Slack."""
    await switch(house, True)
    async with house.sessionmaker() as db:
        await web_app.set_my_notify_via(db, house["Kim"], NotifyVia.WEB, now=house.clock())
    await switch(house, False)
    event = await a_call(house)
    await remind(house, event, "Kim", "Lee")
    result = await outbox.drain(house.sessionmaker, slack.conn.channel, house.clock)
    assert (result.delivered, result.failed, result.skipped) == (1, 1, 0)
    rows = await closed(house)
    assert rows[house["Kim"].id] == (True, False, None)
    assert rows[house["Lee"].id] == (False, True, f"PermanentDeliveryError: member {house['Lee'].id} has no Slack user")
    assert [c.args["channel"] for c in slack.fake.of("chat.postMessage")] == ["U03KIM"]
    assert await outbox_backlog(house.sessionmaker) == (0, 1)


async def test_switched_on_a_dm_to_a_member_on_the_web_app_is_skipped_not_failed(house: House, slack: Slack) -> None:
    await link(house, "Sam", "U02SAM")
    await switch(house, True)
    async with house.sessionmaker() as db:
        await web_app.set_my_notify_via(db, house["Kim"], NotifyVia.WEB, now=house.clock())
        await web_app.set_my_notify_via(db, house["Sam"], NotifyVia.BOTH, now=house.clock())
    event = await a_call(house)
    await remind(house, event, "Kim", "Lee", "Sam", "Alex")
    result = await outbox.drain(house.sessionmaker, slack.conn.channel, house.clock)
    assert (result.delivered, result.failed, result.skipped) == (2, 0, 2)
    rows = await closed(house)
    for name in ("Kim", "Lee"):
        assert rows[house[name].id] == (True, False, outbox.SKIPPED_WEB_APP), name
    for name in ("Sam", "Alex"):  # Both, and Slack by default, still get Slack
        assert rows[house[name].id] == (True, False, None), name
    assert sorted(c.args["channel"] for c in slack.fake.of("chat.postMessage")) == ["U01ALEX", "U02SAM"]
    # /healthz counts: nothing waits and nothing failed, so it isn't degraded.
    assert await outbox_backlog(house.sessionmaker) == (0, 0)
    assert await outbox_oldest_age(house.sessionmaker, house.clock()) is None


async def test_switched_on_channel_posts_and_a_deactivated_members_dms_go_as_before(house: House, slack: Slack) -> None:
    await switch(house, True)
    event = await a_call(house)
    async with house.sessionmaker() as db:
        await OutboxRepository(db).enqueue(house.household.id, event.id, note.CallOpened(event, ()), house.clock())
        await db.commit()
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE member SET active = 0 WHERE id = :i"), {"i": house["Lee"].id})
        await db.commit()
    await remind(house, event, "Lee")
    result = await outbox.drain(house.sessionmaker, slack.conn.channel, house.clock)
    assert (result.delivered, result.failed, result.skipped) == (2, 0, 0)
    assert slack.conn.channel.state is not None and slack.conn.channel.state.dms_dropped_inactive == 1
    assert [c.args["channel"] for c in slack.fake.of("chat.postMessage")] == [slack.conn.channel.channel_id]
