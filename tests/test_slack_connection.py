# SPDX-License-Identifier: AGPL-3.0-or-later
"""SlackChannel delivery, Slack error mapping, startup checks, Socket Mode, member linking by
email, and /healthz's Slack state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import aiohttp
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from dinnerbell.app_factory import create_app
from dinnerbell.channels.slack import connection as connection_module
from dinnerbell.channels.slack.api import (
    DEFAULT_RETRY_AFTER,
    SlackApi,
    SlackAuthError,
    SlackRateLimited,
    SlackUnavailable,
)
from dinnerbell.channels.slack.connection import BoltSocket
from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow
from dinnerbell.domain import notifications as note
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.health import SlackState
from tests.conftest import BASE_URL, make_settings
from tests.support.calls import OPENS, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.fake_slack import APP_TOKEN, BOT_TOKEN, CHANNEL, OTHER_TEAM, TEAM, Failure, FakeSlack, FakeSocket
from tests.support.slack import connection, link, wire

pytestmark = pytest.mark.anyio


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


def info() -> note.EventInfo:
    from dinnerbell.channels.slack.preview import event

    return event()


# Delivery ----------------------------------------------------------------------------------------


async def test_posts_threads_dms_and_updates(house: House) -> None:
    slack = await wire(house, linked=("Kim",))
    channel = slack.conn.channel
    event = replace_household(info(), house.household.id)
    opened = await channel.deliver(note.CallOpened(event, ()))
    assert opened.ref == note.MessageRef(CHANNEL, "1.000100")
    threaded = await channel.deliver(note.ProposalsReady(event, ()), thread=opened.ref)
    kim = note.PersonInfo(house["Kim"].id, "Kim")
    dm = await channel.deliver(note.ResponseReminder(event, kim))
    await channel.update(opened.ref, note.CallUpdated(event))
    posts = slack.fake.of("chat.postMessage")
    assert [(c.args["channel"], c.args.get("thread_ts")) for c in posts] == [
        (CHANNEL, None),
        (CHANNEL, "1.000100"),
        ("U03KIM", None),
    ]
    assert threaded.ref == note.MessageRef(CHANNEL, "2.000100")
    assert dm.ref == note.MessageRef("D03KIM", "3.000100")
    (update,) = slack.fake.of("chat.update")
    assert (update.args["channel"], update.args["ts"]) == (CHANNEL, "1.000100")
    assert all(c.args["unfurl_links"] is False for c in posts)


async def test_a_dm_to_a_member_without_slack_fails_for_good(house: House) -> None:
    slack = await wire(house, linked=())
    reminder = note.ResponseReminder(
        replace_household(info(), house.household.id), note.PersonInfo(house["Lee"].id, "Lee")
    )
    with pytest.raises(note.PermanentDeliveryError, match=f"member {house['Lee'].id} has no Slack user"):
        await slack.conn.channel.deliver(reminder)
    assert slack.fake.calls == []


def replace_household(event: note.EventInfo, household_id: int) -> note.EventInfo:
    from dataclasses import replace

    return replace(event, household_id=household_id)


# Error mapping -----------------------------------------------------------------------------------


async def post_fails(failure: Failure) -> BaseException:
    fake = FakeSlack()
    fake.fail("chat.postMessage", failure)
    with pytest.raises((SlackUnavailable, note.PermanentDeliveryError)) as caught:
        await SlackApi(fake).post(CHANNEL, {"text": "hi"})
    return caught.value


async def test_rate_limits_carry_retry_after() -> None:
    error = await post_fails(Failure("ratelimited", status=429, headers={"Retry-After": "7"}))
    assert isinstance(error, SlackRateLimited) and error.retry_after == timedelta(seconds=7)
    assert str(error) == "Slack rate-limited chat.postMessage; retry after 7s"
    error = await post_fails(Failure(None, status=429))
    assert isinstance(error, SlackRateLimited) and error.retry_after == DEFAULT_RETRY_AFTER
    error = await post_fails(Failure("ratelimited", headers={"retry-after": "soon"}))
    assert isinstance(error, SlackRateLimited) and error.retry_after == DEFAULT_RETRY_AFTER
    error = await post_fails(Failure("ratelimited", headers={"Retry-After": "86400"}))
    assert isinstance(error, SlackRateLimited) and error.retry_after == timedelta(hours=1)


@pytest.mark.parametrize(
    "code",
    ["channel_not_found", "not_in_channel", "user_not_found", "account_inactive", "invalid_blocks", "msg_too_long"],
)
async def test_permanent_errors(code: str) -> None:
    error = await post_fails(Failure(code))
    assert isinstance(error, note.PermanentDeliveryError)
    assert str(error) == f"Slack chat.postMessage failed: {code}"


@pytest.mark.parametrize(
    ("failure", "kind", "message"),
    [
        (Failure("internal_error", status=500), SlackUnavailable, "Slack chat.postMessage failed: internal_error"),
        (Failure(None, status=502), SlackUnavailable, "Slack chat.postMessage failed: http_502"),
        (Failure("invalid_auth"), SlackAuthError, "Slack chat.postMessage failed: invalid_auth"),
        (Failure(raises=TimeoutError()), SlackUnavailable, "Slack chat.postMessage timed out"),
        (
            Failure(raises=aiohttp.ClientConnectionError(f"boom {BOT_TOKEN}")),
            SlackUnavailable,
            "Slack chat.postMessage failed: ClientConnectionError",
        ),
    ],
)
async def test_transient_errors(failure: Failure, kind: type[Exception], message: str) -> None:
    error = await post_fails(failure)
    assert type(error) is kind and str(error) == message


async def test_the_drainer_retries_rate_limits_and_closes_permanent_failures(house: House) -> None:
    slack = await wire(house)
    house.clock.now = OPENS
    await house.tick()
    slack.fake.fail("chat.postMessage", Failure("ratelimited", status=429, headers={"Retry-After": "3"}))
    assert (await slack.drain()).retrying == 1
    (row,) = await house.outbox()
    assert row.available_at == house.clock() + timedelta(seconds=30)
    async with house.sessionmaker() as db:
        error = (await db.execute(text("SELECT last_error FROM outbox"))).scalar_one()
    assert error == "SlackRateLimited: Slack rate-limited chat.postMessage; retry after 3s"

    house.clock.advance(timedelta(seconds=30))
    slack.fake.fail("chat.postMessage", Failure("not_in_channel"))
    assert (await slack.drain()).failed == 1
    async with house.sessionmaker() as db:
        error = (await db.execute(text("SELECT last_error FROM outbox"))).scalar_one()
    assert error == "PermanentDeliveryError: Slack chat.postMessage failed: not_in_channel"


# Startup checks ----------------------------------------------------------------------------------


async def test_checks_pass_and_the_socket_connects(house: House) -> None:
    state = SlackState()
    fake = FakeSlack()
    conn = connection(house, fake, state=state)
    assert await state.status() == "disconnected"
    assert await conn.run_once()
    checks = [m for m in fake.methods if m != "users.lookupByEmail"]  # members are linked after the checks
    assert checks == ["auth.test", "apps.connections.open", "conversations.info"]
    assert await state.status() == "connected"
    assert conn.interactions.team_id == TEAM
    socket = conn.socket
    assert isinstance(socket, FakeSocket)
    socket.connected = False  # the socket dropped; the SDK reconnects on its own
    assert await state.status() == "disconnected"
    fake.clear()
    assert await conn.run_once()  # an hour later: checks again, no second socket
    assert [m for m in fake.methods if m != "users.lookupByEmail"] == ["auth.test", "conversations.info"]
    assert conn.socket is socket
    await conn.stop()
    assert socket.closed


@pytest.mark.parametrize(
    ("setup", "problem"),
    [
        (
            lambda f: f.fail("auth.test", Failure("invalid_auth"), Failure("invalid_auth")),
            "Slack refused DINNERBELL_SLACK_BOT_TOKEN (Slack auth.test failed: invalid_auth)",
        ),
        (
            lambda f: f.fail("auth.test", Failure("account_inactive"), Failure("account_inactive")),
            "Slack refused DINNERBELL_SLACK_BOT_TOKEN",
        ),
        (
            lambda f: setattr(f, "team_id", OTHER_TEAM),
            "belongs to Slack workspace T0EVIL, but DINNERBELL_SLACK_TEAM_ID is T0HOUSE",
        ),
        (
            lambda f: f.fail("apps.connections.open", Failure("invalid_auth"), Failure("invalid_auth")),
            "Slack refused DINNERBELL_SLACK_APP_TOKEN",
        ),
        (
            lambda f: f.fail("conversations.info", Failure("channel_not_found"), Failure("channel_not_found")),
            "Slack can't find the channel C0DINNER",
        ),
        (lambda f: f.channel.update(is_member=False), "send /invite @Dinnerbell"),
        (lambda f: f.channel.update(is_archived=True), "is archived"),
    ],
)
async def test_failed_checks_are_reported_with_what_to_do(
    house: House, setup: Any, problem: str, caplog: pytest.LogCaptureFixture
) -> None:
    state = SlackState()
    fake = FakeSlack()
    setup(fake)
    conn = connection(house, fake, state=state)
    assert not await conn.run_once()
    assert await state.status() == "error"
    assert problem in (state.problem or "")
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == [
        f"slack setup problem: {state.problem}"
    ]
    assert not await conn.run_once()  # still broken: not logged again
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1
    await conn.stop()


async def test_wrong_workspace_never_connects_or_accepts_payloads(house: House) -> None:
    fake = FakeSlack(team_id=OTHER_TEAM)
    conn = connection(house, fake)
    assert not await conn.run_once()
    assert conn.socket is None and conn.interactions.team_id is None


async def test_inviting_the_bot_fixes_it_without_a_restart(house: House) -> None:
    state = SlackState()
    fake = FakeSlack()
    fake.channel["is_member"] = False
    conn = connection(house, fake, state=state)
    assert not await conn.run_once()
    assert conn.socket is not None  # interactions work while the bot waits for its invite
    fake.channel["is_member"] = True
    assert await conn.run_once()
    assert await state.status() == "connected"
    await conn.stop()


async def test_unreachable_slack_is_disconnected_not_an_error(house: House) -> None:
    state = SlackState()
    fake = FakeSlack()
    fake.fail("auth.test", Failure(raises=aiohttp.ClientConnectionError()))
    conn = connection(house, fake, state=state)
    assert not await conn.run_once()
    assert (state.problem, await state.status()) == (None, "disconnected")


async def test_an_unexpected_check_failure_is_logged(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    conn = connection(house, FakeSlack())

    async def broken() -> None:
        raise ZeroDivisionError

    monkeypatch.setattr(conn, "check", broken)
    assert not await conn.run_once()
    assert any(r.message == "slack startup check failed" for r in caplog.records)


async def test_start_runs_the_supervisor_and_stop_ends_it(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "CHECK_AGAIN_AFTER_SUCCESS", 0.01)
    fake = FakeSlack()
    conn = connection(house, fake)
    await conn.start()
    for _ in range(200):
        if fake.methods.count("auth.test") >= 2:
            break
        await asyncio.sleep(0.01)
    assert fake.methods.count("auth.test") >= 2
    await conn.stop()
    assert conn._task is not None and conn._task.done()


async def test_a_socket_that_fails_to_close_is_logged(house: House, caplog: pytest.LogCaptureFixture) -> None:
    conn = connection(house, FakeSlack())
    assert await conn.run_once()

    async def broken() -> None:
        raise OSError

    conn.socket.close = broken  # type: ignore[union-attr, method-assign]
    await conn.stop()
    assert any(r.message == "slack socket did not close cleanly" for r in caplog.records)


async def test_a_connect_failure_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def fail() -> None:
        raise OSError("no")

    task = asyncio.get_running_loop().create_task(fail())
    await asyncio.wait({task})
    connection_module._log_connect_failure(task)
    assert any(r.message == "slack socket connect failed" for r in caplog.records)


async def test_bolt_socket_builds_and_closes_without_connecting(house: House) -> None:
    conn = connection(house, FakeSlack())
    socket = BoltSocket(conn.app, APP_TOKEN)
    assert not await socket.is_connected()
    await socket.close()


# Linking members by email (FR-42) ----------------------------------------------------------------


async def test_unlinked_members_are_linked_by_email(house: House, caplog: pytest.LogCaptureFixture) -> None:
    await link(house, "Alex", "U01ALEX")
    await link(house, "Kim", None)
    fake = FakeSlack()
    fake.add_user("sam@example.com", "U02SAM")
    fake.add_user("kim@example.com", "U01ALEX")  # already Alex's: skipped
    fake.add_user("lee@example.com", "U04LEE", deleted=True)
    conn = connection(house, fake)
    assert await conn.run_once()
    assert (await house.refresh("Sam")).slack_user_id == "U02SAM"
    assert (await house.refresh("Kim")).slack_user_id is None
    assert (await house.refresh("Lee")).slack_user_id is None
    lookups = sorted(c.args["email"] for c in fake.of("users.lookupByEmail"))
    assert lookups == ["kim@example.com", "lee@example.com", "sam@example.com"]
    async with house.sessionmaker() as db:
        audits = list(await db.scalars(select(AuditRow).where(AuditRow.action == "member.slack_linked")))
    assert [(a.actor_member_id, a.target, a.payload) for a in audits] == [
        (None, f"member:{house['Sam'].id}", {"slack_user_id": "U02SAM", "matched_by": "email"})
    ]
    assert not any("@example.com" in r.getMessage() or "@example.com" in str(r.__dict__) for r in caplog.records)


async def test_bots_other_workspaces_and_bad_ids_are_not_linked(house: House) -> None:
    fake = FakeSlack()
    fake.add_user("sam@example.com", "U02SAM", is_bot=True)
    fake.add_user("kim@example.com", "U03KIM", team_id=OTHER_TEAM)
    fake.add_user("lee@example.com", "not-an-id")
    conn = connection(house, fake)
    assert await conn.run_once()
    for name in ("Sam", "Kim", "Lee"):
        assert (await house.refresh(name)).slack_user_id is None


async def test_lookup_trouble_stops_the_round_quietly(house: House, caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeSlack()
    fake.add_user("sam@example.com", "U02SAM")
    fake.fail("users.lookupByEmail", Failure("ratelimited", status=429), Failure("ratelimited", status=429))
    conn = connection(house, fake)
    assert await conn.run_once()
    assert len(fake.of("users.lookupByEmail")) == 1
    assert any(r.message == "slack user lookup failed; will retry" for r in caplog.records)


async def test_linking_failures_do_not_break_the_checks(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connection(house, FakeSlack())

    async def broken() -> list[int]:
        raise ZeroDivisionError

    monkeypatch.setattr(conn, "link_members", broken)
    assert await conn.run_once()


async def test_a_member_linked_meanwhile_or_inactive_is_skipped(house: House) -> None:
    from dinnerbell.services import slack_members

    await link(house, "Sam", "U02SAM")
    async with house.sessionmaker() as db:
        linked = await slack_members.link_slack_users(
            db, house.household.id, {house["Sam"].id: "U9NEW", 999999: "U9GHOST"}, now=house.clock()
        )
    assert linked == []
    assert (await house.refresh("Sam")).slack_user_id == "U02SAM"


# /healthz and the app ----------------------------------------------------------------------------


def slack_client(
    tmp_path: Any, clock: FakeClock, provider: FakeProvider, fake: FakeSlack, **settings: Any
) -> TestClient:
    from dinnerbell.db.migrate import run_migrations

    values = {
        "slack_bot_token": BOT_TOKEN,
        "slack_app_token": APP_TOKEN,
        "slack_channel": CHANNEL,
        "slack_team_id": TEAM,
    } | settings
    configured = make_settings(tmp_path, **values)
    run_migrations(configured.sync_database_url, configured.database_path)
    app = create_app(
        configured,
        clock=clock,
        oidc_transport=httpx.ASGITransport(app=provider.app),
        slack_client=fake,
        slack_socket=FakeSocket,
    )
    return TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False)


def wait_for_slack(client: TestClient, want: str) -> dict[str, Any]:
    import time

    for _ in range(200):
        body: dict[str, Any] = client.get("/healthz").json()
        if body["slack"] == want and body["scheduler"]["last_tick"]:
            return body
        time.sleep(0.01)
    raise AssertionError(f"slack never became {want}: {body}")


def test_healthz_reports_connected(tmp_path: Any, clock: FakeClock, provider: FakeProvider) -> None:
    fake = FakeSlack()
    with slack_client(tmp_path, clock, provider, fake) as client:
        body = wait_for_slack(client, "connected")
        assert body["status"] == "ok"
        assert client.app.state.jobs.channel is client.app.state.slack.channel  # type: ignore[attr-defined]
    assert FakeSocket.instances[-1].closed


def test_healthz_degrades_but_stays_up_on_a_slack_problem(
    tmp_path: Any, clock: FakeClock, provider: FakeProvider
) -> None:
    fake = FakeSlack()
    fake.channel["is_member"] = False
    with slack_client(tmp_path, clock, provider, fake) as client:
        body = wait_for_slack(client, "error")
        response = client.get("/healthz")
        assert response.status_code == 200 and body["status"] == "degraded"
        assert "invite" not in response.text.lower()  # the details stay in the logs


def test_partial_slack_settings_leave_slack_off(
    tmp_path: Any, clock: FakeClock, provider: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeSlack()
    with slack_client(tmp_path, clock, provider, fake, slack_channel=None) as client:
        body = wait_for_slack(client, "not_configured")
        assert body["status"] == "ok"
        assert client.app.state.slack is None  # type: ignore[attr-defined]
    assert fake.calls == []
    assert any("set DINNERBELL_SLACK_BOT_TOKEN" in r.getMessage() for r in caplog.records)


async def test_a_real_tick_delivers_through_slack(house: House) -> None:
    slack = await wire(house)
    house.clock.now = OPENS
    await house.tick()
    await slack.drain()
    event_id = await dinner_id(house)
    (post,) = slack.fake.of("chat.postMessage")
    assert post.args["blocks"][0]["text"]["text"] == "Dinner call · Thursday, Oct 22"
    assert f"https://dinnerbell.test/events/{event_id}" in str(post.args["blocks"])
    assert "<@U01ALEX>" in post.args["blocks"][1]["text"]["text"]


async def test_bolt_socket_connects_through_the_handler(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connection(house, FakeSlack())
    socket = BoltSocket(conn.app, APP_TOKEN)
    connected: list[bool] = []

    async def connect_async() -> None:
        connected.append(True)

    monkeypatch.setattr(socket.handler, "connect_async", connect_async)
    await socket.connect()
    assert connected == [True]
    await socket.close()


async def test_a_match_already_taken_links_nobody(house: House) -> None:
    for name in ("Alex", "Sam", "Lee"):
        await link(house, name, f"U0{name.upper()}")
    fake = FakeSlack()
    fake.add_user("kim@example.com", "U0SAM")
    conn = connection(house, fake)
    assert await conn.link_members() == []
    assert (await house.refresh("Kim")).slack_user_id is None


async def test_a_lost_race_for_a_slack_id_is_a_friendly_error(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    from dinnerbell.db.repositories import MemberRepository
    from dinnerbell.domain.members import MemberRuleViolation
    from dinnerbell.services import members

    await link(house, "Sam", "U02SAM")

    async def nobody(self: MemberRepository, household_id: int, slack_user_id: str) -> None:
        return None  # the check ran before Sam's link committed

    monkeypatch.setattr(MemberRepository, "by_slack_user_id", nobody)
    async with house.sessionmaker() as db:
        with pytest.raises(MemberRuleViolation, match="already linked"):
            await members.set_slack_user(db, house["Alex"], house["Kim"].id, "U02SAM", now=house.clock())
    assert (await house.refresh("Kim")).slack_user_id is None
