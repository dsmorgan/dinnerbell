# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stuck calls (#142): a call that has owed the scheduler a step for over an hour shows on
/healthz and Admin, and its failure is logged at most hourly."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from dinnerbell.app_factory import create_app
from dinnerbell.config import Settings
from dinnerbell.domain import events as ev
from dinnerbell.domain.events import Coverage, Event, EventState, Response, Status, Step
from dinnerbell.scheduler.runner import CALL_TICK_INTERVAL, call_tick
from dinnerbell.services import lifecycle, stuck
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden
from dinnerbell.services.health import STUCK_CHECK_STALE_AFTER, SchedulerState
from dinnerbell.web.filters import elapsed
from tests.conftest import BASE_URL, page, sign_in
from tests.support.calls import OPENS, REMIND, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.scheduler import settle
from tests.support.web import add_item, submit
from tests.test_domain_events import event
from tests.test_events_web import to_voting

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
    (
        "a guest's answer counts, but nobody waits for one",
        event(reminder_sent_at=NOW),
        {
            "responses": [responded(1, Status.OUT, NOW + timedelta(minutes=5)), responded(5, Status.IN, NOW)],
            "active_member_ids": [1],
            "guest_ids": [5, 6],
            "coverage": Coverage(pickups=(5,)),
        },
    ),
    (
        "a maybe holds the early close",
        event(),
        {"responses": [responded(1, Status.MAYBE, NOW), responded(2, Status.OUT, NOW)], "active_member_ids": [1, 2]},
    ),
    (
        "an answer from before a re-run",
        event(),
        {"responses": [responded(1, Status.OUT, NOW - timedelta(minutes=1))], "active_member_ids": [1]},
    ),
    (
        "scheduled past its deadline",
        event(EventState.SCHEDULED, opens_at=NOW - timedelta(hours=3), respond_deadline=NOW - timedelta(hours=1)),
        {},
    ),
    (
        "scheduled, stale before it opens",
        event(EventState.SCHEDULED, opens_at=NOW + timedelta(hours=8), respond_deadline=NOW + timedelta(hours=9)),
        {},
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


def start(client: TestClient, day: str = "2026-10-22") -> int:
    response = submit(client, "/events", {"slot": "dinner", "day": day})
    assert response.status_code == 303, page(response)
    return int(response.headers["location"].rsplit("/", 1)[1])


def broken_step(monkeypatch: pytest.MonkeyPatch, step: Step = Step.CLOSE_RESPONSES) -> None:
    async def fail(*_: object) -> None:
        raise RuntimeError("a bug in the step")

    monkeypatch.setitem(lifecycle.HANDLERS, step, fail)


def tick(client: TestClient) -> None:
    """One call tick, as the scheduler runs it: advance the calls, then look for stuck ones."""
    client.portal.call(call_tick, client.app.state.jobs)  # type: ignore[attr-defined, union-attr]


def health(client: TestClient, clock: FakeClock) -> httpx.Response:
    """/healthz with a fresh heartbeat, so only the calls can make it unhealthy."""
    client.app.state.dinnerbell.scheduler_state.last_tick = clock.now  # type: ignore[attr-defined]
    return client.get("/healthz")


def banner(client: TestClient) -> str | None:
    """The stuck call banner's text on Admin > Members, or None without one."""
    html = page(client.get("/admin/members"))
    found = re.search(r'<section[^>]*id="stuck-calls".*?</section>', html, re.S)
    return None if found is None else " ".join(re.sub(r"<[^>]+>", "", found.group(0)).split())


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    sign_in(client, provider)
    add_item(client, "Siam Garden", cuisines="Thai")
    add_item(client, "Taqueria", cuisines="Mexican")
    settle(client)
    return client


@contextmanager
def running(settings: Settings, clock: FakeClock, provider: FakeProvider) -> Iterator[TestClient]:
    """Dinnerbell from start to stop, with the admin signed in. Twice in a row is a restart."""
    app = create_app(settings, clock=clock, oidc_transport=httpx.ASGITransport(app=provider.app))
    with TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False) as client:
        sign_in(client, provider)
        settle(client)
        yield client


def test_a_step_that_always_fails_is_stuck_after_an_hour(
    admin: TestClient, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = start(admin)  # 13:00 Chicago; the reminder is due at 14:15, responses close at 14:30
    clock.advance(timedelta(minutes=75))
    tick(admin)  # the reminder goes out
    broken_step(monkeypatch)
    clock.advance(timedelta(minutes=15))
    for _ in range(4):
        tick(admin)  # responses never close
        clock.advance(timedelta(minutes=15))
    tick(admin)
    assert health(admin, clock).json()["stuck_calls"] == 0  # an hour after the deadline: not yet
    assert banner(admin) is None
    clock.advance(timedelta(minutes=1))
    tick(admin)
    response = health(admin, clock)
    assert response.status_code == 200  # never a restart: a restart can't fix the step
    body = response.json()
    assert (body["status"], body["stuck_calls"]) == ("degraded", 1)
    assert "dinner" not in response.text.lower()  # a count only, nothing about the call
    assert '"status":"ok","version"' not in response.text  # what the keyword monitor looks for
    text = banner(admin)
    assert text is not None and "Dinner, Thu Oct 22: Who's in?, stuck for 1 hr 1 min" in text
    assert f'href="/events/{event_id}"' in admin.get("/admin/members").text
    assert 'id="stuck-calls"' in admin.get("/admin/schedule").text  # on every Admin tab
    assert "stuck-calls" not in admin.get("/admin/members", headers={"HX-Request": "true"}).text  # not on a fragment
    assert "stuck-calls" not in admin.get("/").text  # and only on Admin
    assert f'action="/events/{event_id}/cancel"' in admin.get(f"/events/{event_id}").text  # where the admin fixes it


def test_a_call_on_schedule_is_never_stuck(admin: TestClient, clock: FakeClock) -> None:
    event_id = start(admin)
    to_voting(admin, clock, event_id)  # 90 minutes with no tick, then catches up at once
    for _ in range(8 * 4):  # through voting, the decision, and done two hours after the meal
        tick(admin)
        assert health(admin, clock).json()["stuck_calls"] == 0
        clock.advance(timedelta(minutes=15))
    assert banner(admin) is None
    assert "Done" in page(admin.get(f"/events/{event_id}"))


def test_after_a_restart_a_call_that_catches_up_is_never_stuck(
    migrated: Settings, clock: FakeClock, provider: FakeProvider
) -> None:
    with running(migrated, clock, provider) as before:
        event_id = start(before)
    clock.advance(timedelta(hours=4))  # down since the call opened: every deadline passed
    with running(migrated, clock, provider) as after:
        assert health(after, clock).json()["stuck_calls"] == 0  # the startup tick caught it up
        for _ in range(4):
            clock.advance(timedelta(minutes=30))
            tick(after)
            assert health(after, clock).json()["stuck_calls"] == 0
        assert banner(after) is None
        assert "Who's in?" not in page(after.get(f"/events/{event_id}"))


def test_a_step_failing_across_a_restart_is_stuck_after_an_hour_of_uptime(
    migrated: Settings, clock: FakeClock, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running(migrated, clock, provider) as before:
        start(before)
    broken_step(monkeypatch)
    clock.advance(timedelta(hours=4))  # responses were due 2.5 hours ago
    with running(migrated, clock, provider) as after:
        tick(after)
        assert health(after, clock).json()["stuck_calls"] == 0
        clock.advance(timedelta(minutes=59))
        tick(after)
        assert health(after, clock).json()["stuck_calls"] == 0  # due for 3.5 hours, up for 59 minutes
        clock.advance(timedelta(minutes=2))
        tick(after)
        assert health(after, clock).json()["stuck_calls"] == 1
        text = banner(after)
        assert text is not None and "stuck for 1 hr 1 min" in text


def test_a_call_whose_check_fails_counts_as_stuck_and_hides_no_other(
    admin: TestClient, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    today = start(admin)
    tomorrow = start(admin, "2026-10-23")
    broken_step(monkeypatch)
    real_owed = stuck.cc.owed

    async def owed(db: object, call: Event) -> datetime | None:
        if call.id == tomorrow:
            raise ValueError("bad data")
        return await real_owed(db, call)  # type: ignore[arg-type]

    monkeypatch.setattr(stuck.cc, "owed", owed)
    clock.advance(timedelta(minutes=75))
    tick(admin)  # the reminder goes out; tomorrow's call can't be checked
    clock.advance(timedelta(minutes=15))
    tick(admin)  # responses fail to close
    response = health(admin, clock)
    assert (response.status_code, response.json()["stuck_calls"], response.json()["status"]) == (200, 1, "degraded")
    clock.advance(timedelta(minutes=61))
    tick(admin)
    assert health(admin, clock).json()["stuck_calls"] == 2
    text = banner(admin)
    assert text is not None
    assert "Dinner, Thu Oct 22: Who's in?, stuck for 1 hr 1 min" in text
    assert "Dinner, Fri Oct 23: Scheduled, stuck for an unknown time" in text
    lines = [r for r in caplog.records if r.message == "could not check a call for being stuck"]
    # Three failed checks: the first logs, the second waits, and the third, over an hour on, logs both.
    assert [(r.event_id, r.failures) for r in lines] == [(tomorrow, 1), (tomorrow, 2)]  # type: ignore[attr-defined]
    assert today != tomorrow


def test_a_failed_check_degrades_but_never_fails_healthz(
    admin: TestClient, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        def __init__(self, *_: object) -> None:
            pass

        async def all(self) -> None:
            raise ValueError("bad data")

    monkeypatch.setattr(stuck, "HouseholdRepository", Broken)
    for _ in range(3):  # the warning logs once, then waits an hour
        tick(admin)
        clock.advance(timedelta(minutes=30))
    response = health(admin, clock)
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["stuck_calls"]) == ("degraded", None)
    assert banner(admin) is None and admin.get("/admin/members").status_code == 200
    warnings = [r for r in caplog.records if r.message == stuck.CHECK_FAILED]
    assert [(r.levelno, r.failures) for r in warnings] == [(logging.WARNING, 1), (logging.WARNING, 2)]  # type: ignore[attr-defined]
    assert not hasattr(warnings[0], "event_id") and warnings[0].exc_info is not None
    monkeypatch.undo()
    tick(admin)
    assert health(admin, clock).json()["stuck_calls"] == 0


def test_a_call_tick_that_stops_checking_degrades_healthz(
    admin: TestClient, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat keeps ticking, but the call tick hung: its last result can't be trusted."""
    assert STUCK_CHECK_STALE_AFTER == 3 * CALL_TICK_INTERVAL
    start(admin)
    broken_step(monkeypatch)
    clock.advance(timedelta(hours=3))  # responses have been due for 90 minutes
    tick(admin)
    assert health(admin, clock).json()["stuck_calls"] == 1
    clock.advance(STUCK_CHECK_STALE_AFTER)
    body = health(admin, clock).json()
    assert (body["status"], body["stuck_calls"], body["scheduler"]["status"]) == ("degraded", 1, "ok")
    assert banner(admin) is not None
    clock.advance(timedelta(seconds=1))
    response = health(admin, clock)
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["stuck_calls"]) == ("degraded", None)
    assert banner(admin) is None  # out of date, so not shown
    tick(admin)
    assert health(admin, clock).json()["stuck_calls"] == 1


def test_a_fresh_start_reports_no_stuck_call_before_its_first_check() -> None:
    state = SchedulerState(started_at=NOW)
    assert state.current_stuck(NOW + STUCK_CHECK_STALE_AFTER) == []
    assert state.current_stuck(NOW + STUCK_CHECK_STALE_AFTER + timedelta(seconds=1)) is None
    assert SchedulerState().current_stuck(NOW) is None


# The service and the log ----------------------------------------------------------------------


@pytest.fixture
async def house(migrated: Settings, clock: FakeClock) -> AsyncIterator[House]:
    database: Database = open_database(migrated.async_database_url)
    try:
        yield await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    finally:
        await database.close()


async def list_stuck(house: House) -> list[stuck.StuckCall]:
    found = await stuck.find_stuck(house.sessionmaker, house.clock(), running_since=OPENS)
    return stuck.for_admin(house["Alex"], found)


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


@pytest.mark.anyio
async def test_a_failure_after_a_step_succeeded_names_no_step(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    house.clock.now = OPENS
    await house.tick()
    real_due = lifecycle.cc.due
    calls = 0

    async def due(*args: Any) -> Step | None:
        nonlocal calls
        calls += 1
        if calls > 2:  # the reminder's two reads pass; the next pass fails before it picks a step
            raise RuntimeError("a bug in the rules")
        return await real_due(*args)

    monkeypatch.setattr(lifecycle.cc, "due", due)
    house.clock.now = REMIND
    await house.tick()
    (line,) = failure_lines(caplog)
    assert line.step is None  # type: ignore[attr-defined]  # not "remind", which succeeded


def test_failure_log_forgets_calls_that_are_gone(caplog: pytest.LogCaptureFixture) -> None:
    failures = lifecycle.FailureLog()
    try:
        raise RuntimeError("x")
    except RuntimeError:
        failures.failed(3, NOW, step=None)
        failures.failed(3, NOW + timedelta(minutes=1), step=None)
    assert failures.unlogged == {3: 1}
    assert failure_lines(caplog)[0].step is None  # type: ignore[attr-defined]
    failures.keep_only({4})
    assert failures.logged_at == {} and failures.unlogged == {}


@pytest.mark.anyio
async def test_only_admins_see_stuck_calls(house: House) -> None:
    with pytest.raises(Forbidden):
        stuck.for_admin(house["Sam"], [])
    assert stuck.for_admin(house["Alex"], None) == []
