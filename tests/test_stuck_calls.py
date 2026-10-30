# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stuck calls (#142): a call that has owed the scheduler a step for over an hour shows on
/healthz and Admin, and its failure is logged at most hourly."""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.domain import events as ev
from dinnerbell.domain.events import Coverage, Event, EventState, Response, Status, Step
from dinnerbell.services import lifecycle, stuck
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden
from dinnerbell.web.filters import elapsed
from tests.conftest import page, sign_in
from tests.support.calls import OPENS, REMIND, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import add_item
from tests.test_domain_events import event
from tests.test_events_web import start, to_voting

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
STALE = NOW + timedelta(hours=6)


# Domain: when a call started owing a step -------------------------------------------------------


def responded(member_id: int, status: Status, at: datetime) -> Response:
    return Response(member_id, status, updated_at=at)


OWED_CASES: list[tuple[str, Event, dict[str, Any]]] = [
    ("scheduled", event(EventState.SCHEDULED, opens_at=NOW + timedelta(hours=1)), {}),
    ("collecting", event(), {}),
    ("reminded", event(reminder_sent_at=NOW + timedelta(minutes=75)), {}),
    (
        "everyone out",
        event(),
        {"responses": [responded(1, Status.OUT, NOW + timedelta(minutes=10))], "active_member_ids": [1]},
    ),
    (
        "in and covered",
        event(reminder_sent_at=NOW),
        {
            "responses": [responded(1, Status.IN, NOW + timedelta(minutes=5)), responded(2, Status.IN, NOW)],
            "active_member_ids": [1, 2],
            "coverage": Coverage(pickups=(1,)),
        },
    ),
    (
        "in, nobody can get it",
        event(reminder_sent_at=NOW),
        {"responses": [responded(1, Status.IN, NOW), responded(2, Status.IN, NOW)], "active_member_ids": [1, 2]},
    ),
    ("voting", event(EventState.VOTING, vote_deadline=NOW + timedelta(hours=2)), {}),
    ("voting, no deadline", event(EventState.VOTING), {}),
    (
        "decided, roles due",
        event(EventState.DECIDED, roles_due_at=NOW + timedelta(hours=2), meal_time=NOW + timedelta(hours=3)),
        {},
    ),
    ("decided", event(EventState.DECIDED, meal_time=NOW + timedelta(hours=3)), {}),
    ("decided, no meal time", event(EventState.DECIDED), {}),
    ("offer", event(kind=ev.EventKind.OFFER), {}),
    ("offer decided", event(EventState.DECIDED, kind=ev.EventKind.OFFER, meal_time=NOW + timedelta(hours=1)), {}),
    ("done", event(EventState.DONE), {}),
    ("abandoned", event(EventState.ABANDONED), {}),
    ("cancelled", event(EventState.CANCELLED), {}),
]


@pytest.mark.parametrize(("case", "call", "inputs"), OWED_CASES, ids=[c[0] for c in OWED_CASES])
def test_owed_since_is_when_due_step_starts_returning_a_step(case: str, call: Event, inputs: dict[str, Any]) -> None:
    owed = ev.owed_since(call, stale_after=STALE, **inputs)
    for minutes in range(-120, 60 * 30, 5):
        at = NOW + timedelta(minutes=minutes)
        due = ev.due_step(call, at, stale_after=STALE, **inputs)
        assert (due is not None) == (owed is not None and at >= owed), (case, at, due, owed)


def test_owed_since_examples() -> None:
    assert ev.owed_since(event(), stale_after=STALE) == NOW + timedelta(minutes=75)  # the reminder
    out = {"responses": [responded(1, Status.OUT, NOW + timedelta(minutes=10))], "active_member_ids": [1]}
    assert ev.owed_since(event(), stale_after=STALE, **out) == NOW + timedelta(minutes=10)  # closes at once
    assert ev.owed_since(event(EventState.DONE), stale_after=STALE) is None


def test_stuck_after_an_hour_counted_from_the_start_at_the_earliest() -> None:
    running = NOW - timedelta(days=1)
    assert ev.stuck_for(None, NOW + timedelta(days=1), running_since=running) is None
    assert ev.stuck_for(NOW, NOW + ev.STUCK_AFTER, running_since=running) is None
    assert ev.stuck_for(NOW, NOW + timedelta(minutes=61), running_since=running) == timedelta(minutes=61)
    restarted = NOW + timedelta(hours=5)  # the step fell due while Dinnerbell was down
    assert ev.stuck_for(NOW, restarted + timedelta(minutes=59), running_since=restarted) is None
    assert ev.stuck_for(NOW, restarted + timedelta(minutes=61), running_since=restarted) == timedelta(minutes=61)


def test_closes_early_keeps_its_rules() -> None:
    call = event()
    everyone = [responded(1, Status.IN, NOW), responded(2, Status.IN, NOW)]
    covered = Coverage(cooks=(1,))
    assert not ev.closes_early(call, NOW + timedelta(hours=1), everyone, [1, 2], Coverage())
    assert not ev.closes_early(call, NOW, everyone, [1, 2], covered)  # still in the quiet spell
    assert ev.closes_early(call, NOW + ev.EARLY_CLOSE_QUIET, everyone, [1, 2], covered)
    assert not ev.closes_early(call, NOW, [responded(1, Status.MAYBE, NOW)], [1], covered)
    assert ev.closes_early(call, NOW, [responded(1, Status.OUT, NOW)], [1], Coverage())


def test_elapsed() -> None:
    assert elapsed(timedelta(minutes=45, seconds=59)) == "45 min"
    assert elapsed(timedelta(hours=1)) == "1 hr"
    assert elapsed(timedelta(hours=2, minutes=5)) == "2 hr 5 min"


# Through the app ------------------------------------------------------------------------------


def broken_step(monkeypatch: pytest.MonkeyPatch, step: Step = Step.CLOSE_RESPONSES) -> None:
    async def fail(*_: object) -> None:
        raise RuntimeError("a bug in the step")

    monkeypatch.setitem(lifecycle.HANDLERS, step, fail)


def tick(client: TestClient, clock: FakeClock, failures: lifecycle.FailureLog | None = None) -> None:
    jobs = client.app.state.jobs  # type: ignore[attr-defined]
    run = functools.partial(lifecycle.tick, jobs.sessionmaker, clock(), failures=failures)
    client.portal.call(run)  # type: ignore[union-attr]


def health(client: TestClient, clock: FakeClock) -> httpx.Response:
    """/healthz with a fresh heartbeat, so only the calls can make it unhealthy."""
    client.app.state.dinnerbell.scheduler_state.last_tick = clock.now  # type: ignore[attr-defined]
    return client.get("/healthz")


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    sign_in(client, provider)
    add_item(client, "Siam Garden", cuisines="Thai")
    add_item(client, "Taqueria", cuisines="Mexican")
    return client


def test_a_step_that_always_fails_is_stuck_after_an_hour(
    admin: TestClient, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = start(admin)  # 13:00 Chicago; the reminder is due at 14:15, responses close at 14:30
    clock.advance(timedelta(minutes=75))
    tick(admin, clock)  # the reminder goes out
    broken_step(monkeypatch)
    clock.advance(timedelta(minutes=15))
    for _ in range(4):
        tick(admin, clock)  # responses never close
        clock.advance(timedelta(minutes=15))
    assert health(admin, clock).json()["stuck_calls"] == 0  # an hour after the deadline: not yet
    assert "stuck-calls" not in admin.get("/admin/members").text
    clock.advance(timedelta(minutes=1))
    tick(admin, clock)
    response = health(admin, clock)
    assert response.status_code == 200  # never a restart: a restart can't fix the step
    body = response.json()
    assert (body["status"], body["stuck_calls"]) == ("degraded", 1)
    assert "dinner" not in response.text.lower()  # a count only, nothing about the call
    html = page(admin.get("/admin/members"))
    assert 'id="stuck-calls"' in html and f'href="/events/{event_id}"' in html
    assert "Dinner, Thu Oct 22: Who's in?, stuck for 1 hr 1 min" in re.sub(r"<[^>]+>", "", html)
    assert 'id="stuck-calls"' in admin.get("/admin/schedule").text  # on every Admin tab
    assert "stuck-calls" not in admin.get("/").text  # but only there
    assert f'action="/events/{event_id}/cancel"' in admin.get(f"/events/{event_id}").text  # where the admin fixes it


def test_a_call_on_schedule_is_never_stuck(admin: TestClient, clock: FakeClock) -> None:
    event_id = start(admin)
    to_voting(admin, clock, event_id)  # 90 minutes with no tick, then catches up at once
    for _ in range(8 * 4):  # through voting, the decision, and done two hours after the meal
        assert health(admin, clock).json()["stuck_calls"] == 0
        clock.advance(timedelta(minutes=15))
        tick(admin, clock)
    assert "stuck-calls" not in admin.get("/admin/members").text
    assert "Done" in page(admin.get(f"/events/{event_id}"))


def test_after_a_restart_a_call_that_catches_up_is_never_stuck(admin: TestClient, clock: FakeClock) -> None:
    event_id = start(admin)
    clock.advance(timedelta(hours=4))  # down since the call opened: every deadline passed
    admin.app.state.dinnerbell.scheduler_state.started_at = clock.now  # type: ignore[attr-defined]
    assert health(admin, clock).json()["stuck_calls"] == 0  # before the first tick
    clock.advance(timedelta(minutes=59))
    assert health(admin, clock).json()["stuck_calls"] == 0
    tick(admin, clock)  # caught up
    for _ in range(4):
        clock.advance(timedelta(minutes=30))
        tick(admin, clock)
        assert health(admin, clock).json()["stuck_calls"] == 0
    assert "stuck-calls" not in admin.get("/admin/members").text
    assert f'href="/events/{event_id}"' not in admin.get("/admin/members").text


def test_a_failed_check_degrades_but_never_fails_healthz(
    admin: TestClient, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    start(admin)
    clock.advance(timedelta(hours=2))

    async def broken(*_: object) -> None:
        raise ValueError("bad data")

    monkeypatch.setattr(stuck.cc, "owed", broken)
    response = health(admin, clock)
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["stuck_calls"]) == ("degraded", None)
    assert admin.get("/admin/members").status_code == 200  # no banner, but the page works
    assert [r.message for r in caplog.records].count("stuck call check failed") == 2


# The service and the log ----------------------------------------------------------------------


@pytest.fixture
async def house(migrated: Settings, clock: FakeClock) -> AsyncIterator[House]:
    database: Database = open_database(migrated.async_database_url)
    try:
        yield await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    finally:
        await database.close()


async def list_stuck(house: House, name: str = "Alex", *, since: datetime = OPENS) -> list[stuck.StuckCall]:
    async with house.sessionmaker() as db:
        return await stuck.stuck_calls(db, house[name], house.clock(), running_since=since)


def failure_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.message == "could not advance call"]


@pytest.mark.anyio
async def test_a_failing_call_logs_at_most_hourly(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    house.clock.now = REMIND
    await house.tick()
    broken_step(monkeypatch)
    failures = lifecycle.FailureLog()
    house.clock.now = RESPOND_BY
    for _ in range(120):  # an hour of ticks, 30 seconds apart
        await lifecycle.tick(house.sessionmaker, house.clock(), failures=failures)
        house.clock.advance(timedelta(seconds=30))
    (first,) = failure_lines(caplog)
    assert (first.levelno, first.event_id, first.step, first.failures) == (  # type: ignore[attr-defined]
        logging.ERROR,
        event_id,
        "close_responses",
        1,
    )
    assert first.exc_info is not None
    await lifecycle.tick(house.sessionmaker, house.clock(), failures=failures)  # an hour after the first line
    assert [r.failures for r in failure_lines(caplog)] == [1, 120]  # type: ignore[attr-defined]
    assert failure_lines(caplog)[-1].exc_info is not None
    assert await list_stuck(house) == []  # an hour exactly: not yet
    house.clock.advance(timedelta(seconds=1))
    (call,) = await list_stuck(house)
    assert (call.event.id, call.event.state) == (event_id, EventState.COLLECTING)
    assert call.stuck_for == timedelta(hours=1, seconds=1)
    monkeypatch.undo()  # fixed: the call moves on and is forgotten
    await lifecycle.tick(house.sessionmaker, house.clock(), failures=failures)
    assert failures.logged_at == {} and await list_stuck(house) == []
    assert len(failure_lines(caplog)) == 2


@pytest.mark.anyio
async def test_without_a_failure_log_every_failure_logs(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    house.clock.now = OPENS
    await house.tick()
    broken_step(monkeypatch, Step.REMIND)
    house.clock.now = REMIND
    await house.tick()
    await house.tick()
    assert [r.step for r in failure_lines(caplog)] == ["remind", "remind"]  # type: ignore[attr-defined]


def test_failure_log_forgets_calls_that_are_gone(caplog: pytest.LogCaptureFixture) -> None:
    failures = lifecycle.FailureLog()
    try:
        raise RuntimeError("x")
    except RuntimeError:
        failures.failed(3, None, NOW)
        failures.failed(3, None, NOW + timedelta(minutes=1))
    assert failures.unlogged == {3: 1}
    assert failure_lines(caplog)[0].step is None  # type: ignore[attr-defined]  # failed before a step ran
    failures.keep_only({4})
    assert failures.logged_at == {} and failures.unlogged == {}


@pytest.mark.anyio
async def test_only_admins_see_stuck_calls_and_never_in_the_first_hour(house: House) -> None:
    house.clock.now = OPENS + timedelta(days=1)
    with pytest.raises(Forbidden):
        await list_stuck(house, "Sam")
    assert await list_stuck(house, since=house.clock() - timedelta(minutes=59)) == []
