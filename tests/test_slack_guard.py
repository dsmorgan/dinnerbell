# SPDX-License-Identifier: AGPL-3.0-or-later
"""The notification guard (ADR 0006): on a labeled instance the Slack adapter DMs and mentions
only members of the configured channel, and fails closed when it can't tell who they are."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from dinnerbell.channels.slack import connection as connection_module
from dinnerbell.channels.slack.channel import HOLD_RETRY_SECONDS
from dinnerbell.channels.slack.connection import SlackConnection
from dinnerbell.config import Settings
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.domain import notifications as note
from dinnerbell.services import outbox
from dinnerbell.services.database import Database, open_database
from tests.support.calls import OPENS, REMIND, House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.fake_slack import BOT_USER, CHANNEL, Failure, FakeSlack
from tests.support.slack import SLACK_IDS, connection, link

pytestmark = pytest.mark.anyio

LABEL = "TEST"
IN_CHANNEL = [BOT_USER, SLACK_IDS["Alex"], SLACK_IDS["Sam"]]
"""Kim and Lee are linked to Slack but aren't in the channel."""


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    for name, slack_id in SLACK_IDS.items():
        await link(house, name, slack_id)
    return house


async def checked(house: House, fake: FakeSlack, *, limited: bool) -> SlackConnection:
    fake.channel_members = list(IN_CHANNEL)
    conn = connection(house, fake, instance_label=LABEL if limited else "")
    assert await conn.run_once()
    fake.clear()
    return conn


def event(house: House) -> note.EventInfo:
    from dinnerbell.channels.slack.preview import event

    return replace(event(), household_id=house.household.id)


def person(house: House, name: str) -> note.PersonInfo:
    return note.PersonInfo(house[name].id, name)


def posted(fake: FakeSlack) -> list[tuple[str, str]]:
    return [(c.args["channel"], str(c.args["blocks"])) for c in fake.of("chat.postMessage")]


# Labeled -----------------------------------------------------------------------------------------


async def test_a_dm_to_someone_outside_the_channel_is_dropped(house: House, caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeSlack()
    conn = await checked(house, fake, limited=True)
    caplog.set_level(logging.INFO, logger="dinnerbell.channels.slack")
    dropped = await conn.channel.deliver(note.ResponseReminder(event(house), person(house, "Kim")))
    assert dropped == note.DeliveryResult(None)
    assert fake.calls == []
    (record,) = [r for r in caplog.records if "dropped" in r.getMessage()]
    assert record.recipient_id == house["Kim"].id  # type: ignore[attr-defined]
    assert SLACK_IDS["Kim"] not in str(record.__dict__) and "@example.com" not in str(record.__dict__)

    await conn.channel.deliver(note.ResponseReminder(event(house), person(house, "Sam")))
    assert [channel for channel, _ in posted(fake)] == [SLACK_IDS["Sam"]]


async def test_mentions_of_people_outside_the_channel_become_names(house: House) -> None:
    fake = FakeSlack()
    conn = await checked(house, fake, limited=True)
    opened = note.CallOpened(event(house), (person(house, "Alex"), person(house, "Kim")))
    result = await conn.channel.deliver(opened)
    ((channel, blocks),) = posted(fake)
    assert channel == CHANNEL
    assert f"<@{SLACK_IDS['Alex']}>" in blocks
    assert f"<@{SLACK_IDS['Kim']}>" not in blocks and "Kim" in blocks

    assert result.ref is not None
    await conn.channel.update(result.ref, note.CallUpdated(event(house)))
    (update,) = fake.of("chat.update")
    assert SLACK_IDS["Kim"] not in str(update.args["blocks"])
    assert SLACK_IDS["Lee"] not in str(update.args["blocks"])


async def test_mentions_in_a_thread_reply_and_a_dm_body_are_filtered(house: House) -> None:
    from dinnerbell.channels.slack import preview

    fake = FakeSlack()
    conn = await checked(house, fake, limited=True)
    kim, sam = person(house, "Kim"), person(house, "Sam")
    decision = replace(
        preview.DECISION,
        assignments=(
            note.AssignmentInfo(note.AssignmentRole.COOK, sam),
            note.AssignmentInfo(note.AssignmentRole.COORDINATOR, kim),
        ),
    )
    decided = replace(event(house), state=preview.DECIDED.state)
    thread = note.MessageRef(CHANNEL, "1.000100")
    await conn.channel.deliver(note.DecisionMade(decided, decision, locked_by=kim), thread=thread)
    await conn.channel.deliver(note.AssignmentMade(decided, 1, sam, (note.AssignmentRole.COOK,), decision))
    (reply, dm) = fake.of("chat.postMessage")
    assert reply.args["thread_ts"] == "1.000100"
    assert f"<@{SLACK_IDS['Sam']}>" in str(reply.args["blocks"])
    for call in (reply, dm):
        assert SLACK_IDS["Kim"] not in str(call.args) and "Kim" in str(call.args["blocks"])
    assert dm.args["channel"] == SLACK_IDS["Sam"]


async def test_a_reminder_round_reaches_only_the_channel(house: House) -> None:
    """End to end: a call opens, and the reminder DMs only people in the channel. The dropped
    ones close as delivered: nothing retries, holds, or fails."""
    fake = FakeSlack()
    conn = await checked(house, fake, limited=True)
    house.clock.now = OPENS
    await house.tick()
    await outbox.drain(house.sessionmaker, conn.channel, house.clock)
    house.clock.now = REMIND
    await house.tick()
    fake.clear()
    drained = await outbox.drain(house.sessionmaker, conn.channel, house.clock)
    assert {channel for channel, _ in posted(fake)} == {SLACK_IDS["Alex"], SLACK_IDS["Sam"]}
    assert (drained.delivered, drained.retrying, drained.held, drained.failed) == (4, 0, 0, 0)
    assert conn.state.dms_dropped == 2
    async with house.sessionmaker() as db:
        assert await OutboxRepository(db).backlog() == (0, 0)  # nothing waits to retry


# Unlabeled ---------------------------------------------------------------------------------------


async def test_an_unlabeled_instance_reaches_everyone_and_never_reads_members(house: House) -> None:
    fake = FakeSlack()
    conn = connection(house, fake)
    assert await conn.run_once()
    assert "conversations.members" not in fake.methods
    fake.clear()
    await conn.channel.deliver(note.ResponseReminder(event(house), person(house, "Kim")))
    await conn.channel.deliver(note.CallOpened(event(house), (person(house, "Kim"),)))
    (dm, post) = posted(fake)
    assert dm[0] == SLACK_IDS["Kim"]
    assert f"<@{SLACK_IDS['Kim']}>" in post[1]
    assert "conversations.members" not in fake.methods


# Reading the members -----------------------------------------------------------------------------


async def test_members_are_read_page_by_page(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "MEMBERS_PAGE_SIZE", 2)
    fake = FakeSlack()
    conn = await checked(house, fake, limited=True)
    assert conn.audience.members == frozenset(IN_CHANNEL)
    fake.channel_members = [*IN_CHANNEL, SLACK_IDS["Kim"]]
    assert await conn.run_once()  # every supervisor pass reads them again
    pages = fake.of("conversations.members")
    assert [c.args.get("cursor") for c in pages] == [None, "2"]
    assert all(c.args["channel"] == CHANNEL and c.args["limit"] == 2 for c in pages)
    assert conn.audience.allows(SLACK_IDS["Kim"])


async def test_reading_stops_at_the_page_cap(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(connection_module, "MEMBERS_PAGE_SIZE", 1)
    monkeypatch.setattr(connection_module, "MAX_MEMBER_PAGES", 2)
    caplog.set_level(logging.WARNING, logger="dinnerbell.channels.slack")
    fake = FakeSlack()
    fake.channel_members = list(IN_CHANNEL)
    conn = connection(house, fake, instance_label=LABEL)
    assert await conn.run_once()
    assert [c.args.get("cursor") for c in fake.of("conversations.members")] == [None, "1"]
    assert conn.audience.members == frozenset(IN_CHANNEL[:2])  # the rest count as outside
    assert any("more members than Dinnerbell reads" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("failure", [Failure("internal_error"), Failure(raises=TimeoutError()), Failure("ratelimited")])
async def test_an_unreachable_read_keeps_the_last_list(house: House, failure: Failure) -> None:
    fake = FakeSlack()
    conn = await checked(house, fake, limited=True)
    fake.fail("conversations.members", failure)
    assert await conn.run_once()
    assert conn.audience.members == frozenset(IN_CHANNEL)
    assert not conn._settled(True)  # read again within a minute, not an hour
    await conn.channel.deliver(note.ResponseReminder(event(house), person(house, "Alex")))
    assert [channel for channel, _ in posted(fake)] == [SLACK_IDS["Alex"]]  # nothing lost to a blip
    assert await conn.run_once()
    assert conn._settled(True)


async def refused_read(house: House, fake: FakeSlack) -> SlackConnection:
    """A connection whose members are unknown because Slack refused the read."""
    conn = await checked(house, fake, limited=True)
    fake.fail("conversations.members", Failure("channel_not_found"))
    assert await conn.run_once()  # the setup is fine; posts still go
    assert conn.audience.members is None and not conn.audience.known
    fake.clear()
    return conn


async def test_unknown_members_hold_dms_and_mention_nobody(house: House) -> None:
    fake = FakeSlack()
    conn = await refused_read(house, fake)
    with pytest.raises(note.HeldDelivery, match="channel members not known"):
        await conn.channel.deliver(note.ResponseReminder(event(house), person(house, "Alex")))
    await conn.channel.deliver(note.CallOpened(event(house), (person(house, "Alex"),)))
    ((channel, blocks),) = posted(fake)
    assert channel == CHANNEL and "<@" not in blocks
    assert conn.state.dms_dropped == 0  # held, not dropped

    assert await conn.run_once()  # the next pass reads them, and DMs go again
    await conn.channel.deliver(note.ResponseReminder(event(house), person(house, "Alex")))
    assert posted(fake)[-1][0] == SLACK_IDS["Alex"]


async def test_an_unexpected_error_while_reading_never_breaks_the_check(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSlack()
    conn = await checked(house, fake, limited=True)

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(conn.api, "channel_members", broken)
    assert await conn.run_once()
    assert conn.audience.members is None


async def test_a_bad_app_token_still_reads_the_members(house: House) -> None:
    """The app-level token only serves Socket Mode; deliveries use the bot token and still go,
    so a DM to someone in the channel must not be held or dropped meanwhile."""
    fake = FakeSlack()
    fake.channel_members = list(IN_CHANNEL)
    fake.fail("apps.connections.open", Failure("invalid_auth"))
    conn = connection(house, fake, instance_label=LABEL)
    assert not await conn.run_once()
    assert conn.state.problem is not None and "APP_TOKEN" in conn.state.problem
    assert conn.audience.members == frozenset(IN_CHANNEL)
    fake.clear()
    await conn.channel.deliver(note.ResponseReminder(event(house), person(house, "Alex")))
    assert [channel for channel, _ in posted(fake)] == [SLACK_IDS["Alex"]]

    fake.fail("apps.connections.open", Failure("invalid_auth"))
    fake.fail("conversations.info", Failure(raises=TimeoutError()))
    assert not await conn.run_once()  # an unreachable channel check doesn't hide the token problem
    assert "APP_TOKEN" in (conn.state.problem or "")


async def test_a_bad_app_token_and_a_channel_problem_forget_the_members(house: House) -> None:
    fake = FakeSlack()
    conn = await checked(house, fake, limited=True)
    conn.socket = None  # as on a restart, so the app-level token is checked again
    fake.fail("apps.connections.open", Failure("invalid_auth"))
    fake.channel["is_member"] = False
    assert not await conn.run_once()
    assert "APP_TOKEN" in (conn.state.problem or "")
    assert conn.audience.members is None


async def test_an_unlabeled_instance_with_a_bad_app_token_checks_nothing_more(house: House) -> None:
    fake = FakeSlack()
    fake.fail("apps.connections.open", Failure("invalid_auth"))
    assert not await connection(house, fake).run_once()
    assert fake.methods == ["auth.test", "apps.connections.open"]


async def test_dms_wait_for_the_first_read_after_startup(house: House) -> None:
    """Before the supervisor's first pass, reminders are held, not lost; once the members are
    read, they reach the people in the channel and the rest are dropped and counted."""
    fake = FakeSlack()
    fake.channel_members = list(IN_CHANNEL)
    conn = connection(house, fake, instance_label=LABEL)
    house.clock.now = OPENS
    await house.tick()
    house.clock.now = REMIND
    await house.tick()
    drained = await outbox.drain(house.sessionmaker, conn.channel, house.clock)
    assert drained.held == 4  # the four reminders; the call message went out
    assert [channel for channel, _ in posted(fake)] == [CHANNEL]

    assert await conn.run_once()
    fake.clear()
    house.clock.now += timedelta(seconds=HOLD_RETRY_SECONDS)
    drained = await outbox.drain(house.sessionmaker, conn.channel, house.clock)
    assert (drained.delivered, drained.held, drained.failed) == (4, 0, 0)
    assert {channel for channel, _ in posted(fake)} == {SLACK_IDS["Alex"], SLACK_IDS["Sam"]}
    assert conn.state.dms_dropped == 2


async def test_a_missing_scope_fails_closed_with_a_hint(house: House, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dinnerbell.channels.slack")
    fake = FakeSlack()
    fake.channel_members = list(IN_CHANNEL)
    fake.fail("conversations.members", Failure("missing_scope"))
    conn = connection(house, fake, instance_label=LABEL)
    assert await conn.run_once()
    assert conn.audience.members is None
    assert conn.state.problem is None  # not a held setup problem: channel posts still go
    (record,) = [r for r in caplog.records if "missing_scope" in r.getMessage()]
    assert record.levelno == logging.ERROR
    assert "channels:read" in record.getMessage() and "groups:read" in record.getMessage()

    fake.fail("conversations.members", Failure("missing_scope"))
    assert await conn.run_once()  # the retry a minute later doesn't log it again
    assert len([r for r in caplog.records if "missing_scope" in r.getMessage()]) == 1
    assert await conn.run_once()
    assert conn.audience.members == frozenset(IN_CHANNEL)
    assert any("DMs and mentions resume" in r.getMessage() for r in caplog.records)


async def test_a_channel_problem_forgets_the_members(house: House) -> None:
    fake = FakeSlack()
    conn = await checked(house, fake, limited=True)
    fake.channel["is_member"] = False
    assert not await conn.run_once()
    assert conn.audience.members is None


async def test_unreachable_slack_keeps_the_members_already_read(house: House) -> None:
    fake = FakeSlack()
    conn = await checked(house, fake, limited=True)
    fake.fail("auth.test", Failure(raises=TimeoutError()))
    assert not await conn.run_once()
    assert conn.audience.members == frozenset(IN_CHANNEL)


# Wiring ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "limited"), [("TEST", True), ("", False)])
def test_the_label_turns_the_guard_on(
    tmp_path: Any, clock: FakeClock, provider: FakeProvider, label: str, limited: bool
) -> None:
    from tests.test_slack_connection import slack_client, wait_for_slack

    fake = FakeSlack()
    with slack_client(tmp_path, clock, provider, fake, instance_label=label) as client:
        wait_for_slack(client, "connected")
        assert client.app.state.slack.audience.limited is limited  # type: ignore[attr-defined]
        body = client.get("/healthz").json()
        assert body.get("slack_dms_dropped") == (0 if limited else None)
        assert ("slack_dms_dropped" in body) is limited  # production's body is unchanged
    assert ("conversations.members" in fake.methods) is limited
