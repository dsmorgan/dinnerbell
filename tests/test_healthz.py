# SPDX-License-Identifier: AGPL-3.0-or-later
"""GET /healthz."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dinnerbell import __version__
from dinnerbell.db.engine import create_app_engine, create_sessionmaker
from tests.conftest import sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.scheduler import settle
from tests.support.web import submit


def wait_for_tick(client: TestClient) -> dict[str, object]:
    """/healthz once the scheduler's startup jobs are done (``settle``), so none of them
    overwrites what the test sets."""
    settle(client)
    body: dict[str, object] = client.get("/healthz").json()
    return body


def test_healthz_shape(client: TestClient, clock: FakeClock) -> None:
    body = wait_for_tick(client)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    # No "push" while the web app is off (test_the_push_queue_shows_while_the_web_app_is_on).
    assert set(body) == {"status", "version", "database", "scheduler", "slack", "backup", "outbox", "stuck_calls"}
    assert body["stuck_calls"] == 0
    # The documented keyword monitor: the top-level status, which the scheduler's own can't match.
    assert response.text.startswith('{"status":"ok","version"')
    assert body["version"] == __version__
    assert body["outbox"] == {"backlog": 0, "failed": 0, "oldest_pending_age": None}
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["slack"] == "not_configured"
    assert body["scheduler"] == {"status": "ok", "last_tick": clock.now.isoformat()}
    assert set(body["backup"]) == {"last_success", "failing"}  # type: ignore[arg-type]
    text = response.text
    assert "@" not in text
    assert "secret" not in text.lower()


@pytest.mark.parametrize("settings_overrides", [{"build": "abc1234"}])
def test_healthz_reports_the_build_stamp(client: TestClient) -> None:
    assert client.get("/healthz").json()["version"] == f"{__version__}+abc1234"


def test_healthz_needs_no_session_and_is_not_rate_limited(client: TestClient) -> None:
    for _ in range(40):
        assert client.get("/healthz").status_code == 200


def test_healthz_reports_startup_backup(client: TestClient) -> None:
    import time

    for _ in range(100):
        body = client.get("/healthz").json()
        if body["backup"]["last_success"]:
            break
        time.sleep(0.02)
    assert body["backup"]["last_success"] is not None


def test_stale_scheduler_is_degraded_not_down(client: TestClient, clock: FakeClock) -> None:
    wait_for_tick(client)
    clock.advance(timedelta(minutes=5))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["scheduler"]["status"] == "stale"


def test_stopped_scheduler_returns_503(client: TestClient, clock: FakeClock) -> None:
    """Regression: a dead scheduler only showed as "degraded" with 200, which the container
    health check treats as healthy."""
    wait_for_tick(client)
    clock.advance(timedelta(minutes=5, seconds=1))
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert response.json()["scheduler"]["status"] == "stopped"


def test_head_requests(client: TestClient) -> None:
    for path in ("/", "/healthz"):
        response = client.head(path)
        assert response.status_code == 200, path
        assert response.content == b""
        assert "frame-ancestors" in response.headers["content-security-policy"]


def test_scheduler_status_before_first_tick() -> None:
    from dinnerbell.services.health import STALE_AFTER, SchedulerState

    now = FakeClock().now
    assert SchedulerState().scheduler_status(now) == "starting"
    assert not SchedulerState().tick_is_fresh(now)
    started = SchedulerState(started_at=now)
    assert started.scheduler_status(now + STALE_AFTER) == "starting"
    assert started.scheduler_status(now + STALE_AFTER + timedelta(seconds=1)) == "stopped"


def test_scheduler_that_never_ticks_returns_503(client: TestClient, clock: FakeClock) -> None:
    """Regression: with no tick at all, /healthz stayed at "starting" with 200 forever."""
    state = client.app.state.dinnerbell.scheduler_state  # type: ignore[attr-defined]
    wait_for_tick(client)
    state.last_tick = None  # as if the heartbeat never ran
    clock.advance(timedelta(minutes=2))
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["scheduler"] == {"status": "stopped", "last_tick": None}


def test_unreachable_database_returns_503(client: TestClient, tmp_path: Path) -> None:
    context = client.app.state.dinnerbell  # type: ignore[attr-defined]
    broken = create_app_engine(f"sqlite+aiosqlite:///{tmp_path}/missing-dir/nope.db")
    object.__setattr__(context, "sessionmaker", create_sessionmaker(broken))
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert response.json()["database"] == "unreachable"


def queue_row(client: TestClient, queued_at: object) -> None:
    """Put a row into the outbox that no drain will take, such as one for a later time."""
    from sqlalchemy import text

    context = client.app.state.dinnerbell  # type: ignore[attr-defined]

    async def insert() -> None:
        async with context.sessionmaker() as db:
            await db.execute(
                text(
                    "INSERT INTO outbox (household_id, kind, payload, idempotency_key, created_at, updated_at, "
                    "available_at, attempts) VALUES (1, 'monthly_recap', '{}', 'k', :t, :t, '2099-01-01 00:00:00', 0)"
                ),
                {"t": queued_at},
            )
            await db.commit()

    client.portal.call(insert)  # type: ignore[union-attr]


def test_a_notification_waiting_over_an_hour_is_degraded(client: TestClient, clock: FakeClock) -> None:
    wait_for_tick(client)
    state = client.app.state.dinnerbell.scheduler_state  # type: ignore[attr-defined]
    queue_row(client, clock.now.strftime("%Y-%m-%d %H:%M:%S"))
    clock.advance(timedelta(minutes=59))
    state.last_tick = state.stuck_checked_at = clock.now  # the scheduler is fine; only the notification waits
    body = client.get("/healthz").json()
    assert body["outbox"]["oldest_pending_age"] == 59 * 60
    assert body["outbox"]["backlog"] == 1
    assert body["status"] == "ok", body
    clock.advance(timedelta(minutes=2))
    state.last_tick = state.stuck_checked_at = clock.now
    body = client.get("/healthz").json()
    assert (body["status"], body["outbox"]["oldest_pending_age"]) == ("degraded", 61 * 60)


def test_the_push_queue_shows_while_the_web_app_is_on(
    client: TestClient, provider: FakeProvider, clock: FakeClock
) -> None:
    """A push waiting over an hour reports "degraded", as the outbox does: with the push
    services unreachable, every push retries for a day and closes unsent (#332)."""
    from sqlalchemy import text

    from tests.test_push import FORM, admin_switch

    sign_in(client, provider)
    admin_switch(client, True)
    assert submit(client, "/app/notifications", FORM).status_code == 204
    state = client.app.state.dinnerbell.scheduler_state  # type: ignore[attr-defined]
    body = wait_for_tick(client)
    assert body["push"] == {"backlog": 0, "oldest_pending_age": None} and body["status"] == "ok", body
    queue_row(client, clock.now.strftime("%Y-%m-%d %H:%M:%S"))
    context = client.app.state.dinnerbell  # type: ignore[attr-defined]

    async def insert() -> None:  # a push no drain takes yet, as one waiting to retry
        async with context.sessionmaker() as db:
            await db.execute(
                text(
                    "INSERT INTO push_queue (household_id, outbox_id, subscription_id, member_id, created_at, "
                    "available_at, attempts) SELECT s.household_id, (SELECT max(id) FROM outbox), s.id, s.member_id, "
                    ":t, '2099-01-01 00:00:00', 1 FROM push_subscription s"
                ),
                {"t": clock.now.strftime("%Y-%m-%d %H:%M:%S")},
            )
            await db.execute(
                text("UPDATE outbox SET delivered_at = :t"), {"t": clock.now.strftime("%Y-%m-%d %H:%M:%S")}
            )
            await db.commit()

    client.portal.call(insert)  # type: ignore[union-attr]
    clock.advance(timedelta(minutes=59))
    state.last_tick = state.stuck_checked_at = clock.now  # the scheduler is fine; only the push waits
    body = client.get("/healthz").json()
    assert body["push"] == {"backlog": 1, "oldest_pending_age": 59 * 60} and body["status"] == "ok", body
    clock.advance(timedelta(minutes=2))
    state.last_tick = state.stuck_checked_at = clock.now
    body = client.get("/healthz").json()
    assert (body["status"], body["push"]["oldest_pending_age"], body["outbox"]["backlog"]) == ("degraded", 61 * 60, 0)
    admin_switch(client, False)  # deletes the queue, and the key goes with the switch
    body = client.get("/healthz").json()
    assert "push" not in body and body["status"] == "ok", body
