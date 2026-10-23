# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the PR #5 review: sign-in collisions, holds that outlive an outage,
holds by problem, tokens revoked at runtime, the socket's connect task and logs, shutdown,
escaping, modal errors on the Home tab, and payloads written by older versions."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.channels.slack import connection as connection_module
from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.channel import HoldScope
from dinnerbell.channels.slack.connection import SOCKET_LOG, BoltSocket
from dinnerbell.channels.slack.messages import RenderContext, render
from dinnerbell.channels.slack.preview import DIRECTORY, NOTIFICATIONS
from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow
from dinnerbell.domain import notifications as note
from dinnerbell.domain.events import EventRuleViolation
from dinnerbell.domain.identity import IdentityClaims
from dinnerbell.domain.meals import Slot
from dinnerbell.services import auth, calls, members
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.health import SlackState
from tests.support.calls import OPENS, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import APP_TOKEN, TEAM, Failure, FakeSlack, FakeSocket, state
from tests.support.slack import Slack, connection, link, wire

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
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


def reminder(house: House, name: str = "Kim") -> note.ResponseReminder:
    base = NOTIFICATIONS["response_reminder"]()
    assert isinstance(base, note.ResponseReminder)
    event = replace(base.event, household_id=house.household.id)
    return note.ResponseReminder(event, note.PersonInfo(house[name].id, name))


def call_post(house: House) -> note.CallOpened:
    base = NOTIFICATIONS["call_opened"]()
    assert isinstance(base, note.CallOpened)
    return replace(base, event=replace(base.event, household_id=house.household.id))


# Sign-in when another member holds the Slack user ---------------------------------------------


async def test_sign_in_survives_a_slack_id_another_member_holds(house: House) -> None:
    async with house.sessionmaker() as db:  # the admin's mistake: Kim's Slack user on Sam
        await members.set_slack_user(db, house["Alex"], house["Sam"].id, "U03KIM", now=house.clock())
    claims = IdentityClaims(
        subject="kim-sub", email="kim@example.com", email_verified=True, slack_team_id=TEAM, slack_user_id="U03KIM"
    )
    async with house.sessionmaker() as db:
        signed_in = await auth.sign_in(db, claims, required_team_id=TEAM, now=house.clock())
    assert isinstance(signed_in, auth.SignedIn)
    kim = await house.refresh("Kim")
    assert (kim.oidc_subject, kim.slack_user_id) == ("kim-sub", None)
    assert (await house.refresh("Sam")).slack_user_id == "U03KIM"
    async with house.sessionmaker() as db:
        rows = list(await db.scalars(select(AuditRow).where(AuditRow.action == "member.slack_user_conflict")))
    assert [(r.target, r.payload) for r in rows] == [
        (f"member:{kim.id}", {"slack_user_id": "U03KIM", "held_by": house["Sam"].id})
    ]


# Holds -----------------------------------------------------------------------------------------


async def test_an_outage_keeps_a_known_setup_problem(house: House) -> None:
    state_ = SlackState()
    fake = FakeSlack()
    fake.channel["is_member"] = False
    conn = connection(house, fake, state=state_)
    assert not await conn.run_once()
    problem = state_.problem
    assert problem and conn.hold.scope is HoldScope.CHANNEL
    fake.fail("auth.test", Failure(raises=TimeoutError()))
    assert not await conn.run_once()
    assert (state_.problem, conn.hold.scope, await state_.status()) == (problem, HoldScope.CHANNEL, "error")
    with pytest.raises(note.HeldDelivery):
        await conn.channel.deliver(call_post(house))
    await conn.stop()


@pytest.mark.parametrize(
    "setup",
    [lambda f: setattr(f, "team_id", "T0WRONG"), lambda f: f.fail("auth.test", Failure("invalid_auth"))],
)
async def test_a_bad_token_or_workspace_holds_direct_messages_too(house: House, setup: Any) -> None:
    await link(house, "Kim", "U03KIM")
    fake = FakeSlack()
    setup(fake)
    conn = connection(house, fake)
    assert not await conn.run_once()
    assert conn.hold.scope is HoldScope.ALL
    fake.clear()
    with pytest.raises(note.HeldDelivery):
        await conn.channel.deliver(reminder(house))
    assert fake.of("chat.postMessage") == []


async def test_a_channel_problem_holds_only_channel_posts(house: House) -> None:
    await link(house, "Kim", "U03KIM")
    fake = FakeSlack()
    fake.channel["is_archived"] = True
    conn = connection(house, fake)
    assert not await conn.run_once()
    assert conn.hold.scope is HoldScope.CHANNEL
    await conn.channel.deliver(reminder(house))
    with pytest.raises(note.HeldDelivery):
        await conn.channel.deliver(call_post(house))
    await conn.stop()


async def test_a_token_revoked_at_runtime_holds_everything_at_once(house: House) -> None:
    await link(house, "Kim", "U03KIM")
    state_ = SlackState()
    fake = FakeSlack()
    conn = connection(house, fake, state=state_)
    assert await conn.run_once()
    assert await state_.status() == "connected"
    fake.fail("chat.postMessage", Failure("token_revoked"))
    with pytest.raises(Exception, match="token_revoked"):
        await conn.channel.deliver(call_post(house))
    assert await state_.status() == "error"
    assert "Slack refused DINNERBELL_SLACK_BOT_TOKEN" in (state_.problem or "")
    assert conn.hold.scope is HoldScope.ALL and conn._wake.is_set()
    with pytest.raises(note.HeldDelivery):
        await conn.channel.deliver(reminder(house))
    conn._wake.clear()
    assert await conn.run_once()  # the next check finds the token fine again
    assert (state_.problem, conn.hold.scope) == (None, HoldScope.NONE)
    await conn.stop()


async def test_an_interaction_that_hits_a_revoked_token_reports_it(slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    slack.fake.fail("chat.postEphemeral", Failure("account_inactive"))
    slack.fake.fail("chat.postMessage", Failure("account_inactive"))
    await slack.click("Sam", "respond_in", str(event_id))
    assert slack.conn.state.problem is not None and slack.conn.hold.scope is HoldScope.ALL


async def test_a_refused_token_during_a_check_does_not_wake_the_supervisor(house: House) -> None:
    fake = FakeSlack()
    fake.fail("auth.test", Failure("invalid_auth"))
    conn = connection(house, fake)
    assert not await conn.run_once()
    assert conn.hold.scope is HoldScope.ALL and not conn._wake.is_set()


# The socket --------------------------------------------------------------------------------------


class HangingSocket(FakeSocket):
    async def connect(self) -> None:
        await asyncio.Event().wait()


async def test_stop_cancels_a_connect_still_in_progress(house: House) -> None:
    fake = FakeSlack()
    conn = connection(house, fake)
    conn.socket_factory = HangingSocket
    assert await conn.run_once()
    task = conn._connect_task
    assert task is not None and not task.done()
    await conn.stop()
    assert task.cancelled()


async def test_a_socket_down_for_long_rechecks_the_app_token(house: House) -> None:
    now = [1000.0]
    state_ = SlackState()
    fake = FakeSlack()
    conn = connection(house, fake, state=state_)
    conn.socket_factory = HangingSocket  # never connects
    conn.monotonic = lambda: now[0]
    assert await conn.run_once()
    fake.clear()
    now[0] += 60
    assert await conn.run_once()
    assert "apps.connections.open" not in fake.methods  # not yet
    now[0] += 61
    fake.fail("apps.connections.open", Failure("invalid_auth"))
    assert not await conn.run_once()
    assert "Slack refused DINNERBELL_SLACK_APP_TOKEN" in (state_.problem or "")
    assert await state_.status() == "error"
    assert conn.hold.scope is HoldScope.NONE  # the bot token still delivers
    await conn.stop()


async def test_a_healthy_socket_skips_the_app_token_check(house: House) -> None:
    fake = FakeSlack()
    conn = connection(house, fake)
    now = [0.0]
    conn.monotonic = lambda: now[0]
    assert await conn.run_once()
    now[0] += 10_000
    fake.clear()
    assert await conn.run_once()
    assert "apps.connections.open" not in fake.methods
    await conn.stop()


async def test_the_supervisor_rechecks_soon_while_the_socket_is_down(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection_module, "CHECK_AGAIN_AFTER_FAILURE", 0.01)
    monkeypatch.setattr(connection_module, "MIN_CHECK_GAP", 0.0)
    monkeypatch.setattr(connection_module, "CHECK_AGAIN_AFTER_SUCCESS", 3600)
    fake = FakeSlack()
    conn = connection(house, fake)
    conn.socket_factory = HangingSocket
    await conn.start()
    for _ in range(200):
        if fake.methods.count("auth.test") >= 2:
            break
        await asyncio.sleep(0.01)
    assert fake.methods.count("auth.test") >= 2
    await conn.stop()


def test_socket_logs_drop_tracebacks_and_ticket_urls(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    try:
        raise OSError("boom")
    except OSError:
        SOCKET_LOG.exception(
            "Failed to connect (error: 403 url='wss://wss-primary.slack.com/link/?ticket=abc123'); Retrying..."
        )
    SOCKET_LOG.debug("Received message (data: {'secret': 'payload'})")
    records = [r for r in caplog.records if r.name == SOCKET_LOG.name]
    assert records  # pytest also captures at the non-propagating logger, so it may see each twice
    for record in records:
        assert (record.levelname, record.exc_info) == ("WARNING", None)
        assert "ticket=" not in record.getMessage() and "wss://(redacted)" in record.getMessage()
    assert "payload" not in caplog.text


async def test_the_real_socket_logs_through_the_quiet_logger(house: House) -> None:
    conn = connection(house, FakeSlack())
    socket = BoltSocket(conn.app, APP_TOKEN, lambda: None)
    assert socket.handler.client.logger is SOCKET_LOG
    await socket.close()


# Shutdown --------------------------------------------------------------------------------------


async def test_shutdown_is_bounded_and_cancels_leftover_work(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "SOCKET_CLOSE_SECONDS", 0.05)
    monkeypatch.setattr(connection_module, "SETTLE_SECONDS", 0.05)
    conn = connection(house, FakeSlack())
    assert await conn.run_once()

    async def hang() -> None:
        await asyncio.Event().wait()

    conn.socket.close = hang  # type: ignore[union-attr, method-assign]
    conn.interactions._spawn(hang())
    (leftover,) = conn.interactions.tasks
    loop = asyncio.get_running_loop()
    started = loop.time()
    await conn.stop()
    assert loop.time() - started < 2
    assert leftover.cancelled()
    assert connection_module.SOCKET_CLOSE_SECONDS + connection_module.SETTLE_SECONDS < 10


# Escaping and replies ----------------------------------------------------------------------------


def test_a_cancelled_posts_fallback_text_is_escaped_once() -> None:
    base = NOTIFICATIONS["event_cancelled"]()
    assert isinstance(base, note.EventCancelled)
    message = render(
        replace(base, cancelled_by=note.PersonInfo(1, "Tom & Jerry")), RenderContext("https://x", DIRECTORY)
    )
    assert message.text.startswith("Tom &amp; Jerry cancelled")
    assert message.blocks[0]["text"]["text"].startswith("Tom &amp; Jerry cancelled")


async def test_replies_escape_what_services_say(slack: Slack, house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*args: object, **kwargs: object) -> None:
        raise EventRuleViolation("<!channel> can't do that")

    monkeypatch.setattr(calls, "respond", refuse)
    await slack.click("Sam", "respond_in", "7")
    assert slack.ephemerals("Sam") == ["&lt;!channel&gt; can't do that"]


async def test_the_home_notice_after_starting_a_call_has_no_raw_link(slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await slack.click("Alex", ids.ACTION_FOR_START[Slot.LUNCH], "lunch", home=True)
    home = json.dumps(slack.last_home("Alex"))
    assert "Started the lunch call. It's posted in the Dinnerbell channel." in home
    assert "&lt;#" not in home and "<#" not in home


async def test_modal_errors_from_the_home_tab_show_on_the_home_tab(slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    await slack.click("Sam", "details", str(event_id), home=True)
    (opened,) = slack.fake.of("views.open")
    origin = ids.ViewOrigin.decode(opened.args["view"]["private_metadata"])
    assert origin == ids.ViewOrigin(event_id, None, home=True)
    from tests.support.fake_slack import view_submission

    payload = view_submission("U02SAM", ids.DETAILS_VIEW, state(note={"value": "bad\u0007bell"}), origin.encode())
    slack.fake.clear()
    await slack._send("interactive", payload)
    assert slack.fake.of("chat.postEphemeral", "chat.postMessage") == []
    assert "A note can't contain control characters." in json.dumps(slack.last_home("Sam"))


# Old payloads ------------------------------------------------------------------------------------


def test_payloads_from_before_a_field_was_added_still_decode() -> None:
    updated = NOTIFICATIONS["decision_updated"]()
    payload = note.to_payload(updated)
    payload.pop("locked_by")
    decoded = note.from_payload("decision_updated", payload)
    assert isinstance(decoded, note.DecisionUpdated) and decoded.locked_by is None


@pytest.mark.parametrize("payload", [[], "x", None, {"event": []}, 7])
def test_payloads_of_the_wrong_shape_fail_safe(payload: Any) -> None:
    with pytest.raises(note.PayloadError):
        note.from_payload("assignment_made", payload)


def test_an_old_assignment_payload_fails_safe() -> None:
    payload = note.to_payload(NOTIFICATIONS["assignment_made"]())
    payload.pop("roles")
    payload["role"] = "cook"
    with pytest.raises(note.PayloadError):
        note.from_payload("assignment_made", payload)


@pytest.mark.parametrize(
    "metadata",
    [
        '{"event": 1, "channel": null, "home": "yes"}',
        '{"event": true, "channel": null, "home": false}',
        '{"event": 0, "channel": null, "home": false}',
        '{"event": 1, "channel": "#general", "home": false}',
        '{"event": 1, "channel": null}',
    ],
)
def test_modal_metadata_must_be_exactly_what_the_app_wrote(metadata: str) -> None:
    with pytest.raises(ids.BadValue):
        ids.ViewOrigin.decode(metadata)


async def test_an_email_lookup_outage_is_retryable() -> None:
    from dinnerbell.channels.slack.api import SlackApi, SlackUnavailable

    fake = FakeSlack()
    fake.fail("users.lookupByEmail", Failure(raises=TimeoutError()))
    with pytest.raises(SlackUnavailable, match="timed out"):
        await SlackApi(fake).user_by_email("sam@example.com")


# Re-review: scope errors, waking, drops, shutdown, conflicts, and log forms -----------------------


@pytest.mark.parametrize("code", ["missing_scope", "not_allowed_token_type"])
async def test_a_method_level_refusal_holds_nothing(house: House, code: str) -> None:
    """Regression (re-review S1): missing_scope from the hourly email match held every delivery."""
    state_ = SlackState()
    fake = FakeSlack()
    fake.fail("users.lookupByEmail", *[Failure(code) for _ in range(10)])
    conn = connection(house, fake, state=state_)
    assert await conn.run_once()
    assert (conn.hold.scope, state_.problem, conn._wake.is_set()) == (HoldScope.NONE, None, False)
    assert await state_.status() == "connected"


async def test_the_supervisor_with_a_missing_scope_stays_calm(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "CHECK_AGAIN_AFTER_SUCCESS", 3600)
    fake = FakeSlack()
    fake.fail("users.lookupByEmail", *[Failure("missing_scope") for _ in range(50)])
    conn = connection(house, fake)
    await conn.start()
    await asyncio.sleep(0.3)
    await conn.stop()
    assert (fake.methods.count("auth.test"), conn.hold.scope) == (1, HoldScope.NONE)


async def test_a_wake_during_a_pass_is_not_lost(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "CHECK_AGAIN_AFTER_SUCCESS", 3600)
    monkeypatch.setattr(connection_module, "MIN_CHECK_GAP", 0.0)
    conn = connection(house, FakeSlack())
    passes = 0

    async def run_once() -> bool:
        nonlocal passes
        passes += 1
        if passes == 1:
            conn._wake.set()  # something wakes it while the first pass runs
        return True

    monkeypatch.setattr(conn, "run_once", run_once)
    await conn.start()
    await asyncio.sleep(0.05)
    await conn.stop()
    assert passes == 2


async def test_wakes_never_check_slack_nonstop(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "MIN_CHECK_GAP", 0.2)
    conn = connection(house, FakeSlack())
    passes = 0

    async def run_once() -> bool:
        nonlocal passes
        passes += 1
        conn._wake.set()  # woken on every pass
        await asyncio.sleep(0)
        return False

    monkeypatch.setattr(conn, "run_once", run_once)
    await conn.start()
    await asyncio.sleep(0.5)
    await conn.stop()
    assert passes <= 3


async def test_a_drop_after_connecting_starts_the_app_token_clock(house: House) -> None:
    now = [0.0]
    fake = FakeSlack()
    conn = connection(house, fake)
    conn.monotonic = lambda: now[0]
    assert await conn.run_once()
    socket = conn.socket
    assert isinstance(socket, FakeSocket) and socket.connected
    conn._wake.clear()
    socket.drop()
    assert conn._wake.is_set() and conn._down_since == 0.0
    now[0] += 121
    fake.fail("apps.connections.open", Failure("invalid_auth"))
    assert not await conn.run_once()
    assert "Slack refused DINNERBELL_SLACK_APP_TOKEN" in (conn.state.problem or "")
    await conn.stop()


async def test_stop_keeps_to_the_shared_deadline(house: House) -> None:
    conn = connection(house, FakeSlack())
    assert await conn.run_once()

    async def hang() -> None:
        await asyncio.Event().wait()

    conn.socket.close = hang  # type: ignore[union-attr, method-assign]
    loop = asyncio.get_running_loop()
    started = loop.time()
    await conn.stop(deadline=started + 0.1)
    assert loop.time() - started < 1.5


def test_the_app_shuts_down_within_podman_s_timeout() -> None:
    from dinnerbell import app_factory

    assert app_factory.SHUTDOWN_SECONDS <= 8


async def test_a_slack_conflict_is_audited_once(house: House) -> None:
    async with house.sessionmaker() as db:
        await members.set_slack_user(db, house["Alex"], house["Sam"].id, "U03KIM", now=house.clock())
    claims = IdentityClaims(
        subject="kim-sub", email="kim@example.com", email_verified=True, slack_team_id=TEAM, slack_user_id="U03KIM"
    )
    for _ in range(3):
        async with house.sessionmaker() as db:
            await auth.sign_in(db, claims, required_team_id=TEAM, now=house.clock())
    async with house.sessionmaker() as db:
        rows = list(await db.scalars(select(AuditRow).where(AuditRow.action == "member.slack_user_conflict")))
    assert len(rows) == 1


def test_every_form_of_socket_log_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """Regression (re-review N5): mixed case, https with a query, and child loggers."""
    caplog.set_level(logging.DEBUG)
    SOCKET_LOG.warning("url %s", "wss://x.slack.com/link/?ticket=SECRET1")
    SOCKET_LOG.warning("dict %(u)s", {"u": "wss://x.slack.com/link/?ticket=SECRET2"})
    try:
        raise OSError("wss://x.slack.com/link/?ticket=SECRET3")
    except OSError:
        SOCKET_LOG.error("failed", exc_info=True)
        SOCKET_LOG.error("failed %s", "x", stack_info=True)
    SOCKET_LOG.warning("quoted 'wss://x.slack.com/link/?ticket=SECRET4'")
    SOCKET_LOG.warning("https https://x.slack.com/link/?ticket=SECRET5")
    SOCKET_LOG.warning("WSS://X.SLACK.COM/link/?ticket=SECRET6")
    SOCKET_LOG.warning("ws url=wss://x.slack.com/link/?ticket=SECRET7,next")
    logging.getLogger(f"{SOCKET_LOG.name}.child").error("child wss://x.slack.com/link/?ticket=SECRET8")
    logging.getLogger(f"{SOCKET_LOG.name}.child").debug("child payload")
    records = [r for r in caplog.records if r.name.startswith(SOCKET_LOG.name)]
    assert {r.name for r in records} == {SOCKET_LOG.name, f"{SOCKET_LOG.name}.child"}
    assert all(r.levelno <= logging.WARNING and r.exc_info is None and r.stack_info is None for r in records)
    assert "SECRET" not in caplog.text and "Traceback" not in caplog.text and "child payload" not in caplog.text
