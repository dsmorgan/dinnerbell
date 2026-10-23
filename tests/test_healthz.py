# SPDX-License-Identifier: AGPL-3.0-or-later
"""GET /healthz."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dinnerbell import __version__
from dinnerbell.db.engine import create_app_engine, create_sessionmaker
from tests.support.clock import FakeClock


def wait_for_tick(client: TestClient) -> dict[str, object]:
    import time

    for _ in range(100):
        body: dict[str, object] = client.get("/healthz").json()
        if body["scheduler"]["last_tick"] is not None:  # type: ignore[index]
            return body
        time.sleep(0.02)
    raise AssertionError("scheduler never ticked")


def test_healthz_shape(client: TestClient, clock: FakeClock) -> None:
    body = wait_for_tick(client)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert set(body) == {"status", "version", "database", "scheduler", "slack", "backup", "outbox"}
    assert body["version"] == __version__
    assert body["outbox"] == {"backlog": 0, "failed": 0}
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["slack"] == "not_configured"
    assert body["scheduler"] == {"status": "ok", "last_tick": clock.now.isoformat()}
    assert set(body["backup"]) == {"last_success"}  # type: ignore[arg-type]
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
