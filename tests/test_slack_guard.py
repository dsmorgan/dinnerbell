# SPDX-License-Identifier: AGPL-3.0-or-later
"""The notification guard (ADR 0006): on a labeled instance the Slack adapter DMs and mentions
only members of the configured channel, and fails closed when it can't tell who they are."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest

from dinnerbell.channels.slack import connection as connection_module
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
    fake = FakeSlack()
    await checked(house, fake, limited=True)
    conn = connection(house, fake, instance_label=LABEL)
    assert await conn.run_once()
    assert len(fake.of("conversations.members")) == 2
    assert conn.audience.members == frozenset(IN_CHANNEL[:2])  # the rest count as outside
    assert any("more members than Dinnerbell reads" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("failure", [Failure("internal_error"), Failure(raises=TimeoutError())])
async def test_a_failed_read_fails_closed(house: House, failure: Failure) -> None:
    fake = FakeSlack()
    conn = await checked(house, fake, limited=True)
    fake.fail("conversations.members", failure)
    assert await conn.run_once()  # the setup is fine; posts still go
    assert conn.audience.members is None
    assert not conn._audience_known()  # so the supervisor reads again within a minute

    assert await conn.channel.deliver(note.ResponseReminder(event(house), person(house, "Alex"))) == (
        note.DeliveryResult(None)
    )
    await conn.channel.deliver(note.CallOpened(event(house), (person(house, "Alex"),)))
    ((channel, blocks),) = posted(fake)
    assert channel == CHANNEL and "<@" not in blocks

    assert await conn.run_once()
    assert conn.audience.allows(SLACK_IDS["Alex"])


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


async def test_a_setup_problem_forgets_the_members(house: House) -> None:
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
    assert ("conversations.members" in fake.methods) is limited
