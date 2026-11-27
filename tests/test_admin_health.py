# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Health (#339): what /healthz reports in words, the failed notifications with their
reasons, and the audit log, read only and for admins only."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_events
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, OutboxRow
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.domain import notifications as note
from dinnerbell.services import admin_health, push
from dinnerbell.services.admin_health import action_words, health_details, kind_words
from dinnerbell.services.database import Database
from dinnerbell.services.errors import Forbidden
from dinnerbell.services.health import BACKUP_STALE_AFTER, HealthReport, OutboxHealth, PushBacklog
from dinnerbell.web.routes import health as health_routes
from tests.conftest import invite, page, sign_in
from tests.support.calls import House
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.scheduler import settle
from tests.test_admin import sign_in_as
from tests.test_auth_flow import member
from tests.test_push import a_call, device, enqueue, switch, who
from tests.test_push import database as database
from tests.test_push import house as house

# Failed notifications -------------------------------------------------------------------------


async def give_up(db: AsyncSession, entry_id: int, error: str, now: datetime) -> None:
    repo = OutboxRepository(db)
    assert await repo.claim(entry_id, "t", now=now, until=now + timedelta(minutes=5))
    assert await repo.failed(entry_id, "t", error=error, retry_at=None, now=now)
    await db.commit()


async def outbox_ids(house: House) -> list[int]:
    async with house.sessionmaker() as db:
        return [e.id for e in await OutboxRepository(db).for_household(house.household.id)]


@pytest.mark.anyio
async def test_it_lists_failed_notifications_newest_first_with_who_and_why(house: House, clock: FakeClock) -> None:
    await switch(house, True)
    await device(house, "Kim")
    event = await a_call(house)
    kim = who(house, "Kim")[0]
    await enqueue(house, note.CallOpened(event, (kim,)), note.ResponseReminder(event, kim))
    opened, reminder = await outbox_ids(house)
    async with house.sessionmaker() as db:
        await give_up(db, reminder, "PermanentDeliveryError: member 3 has no Slack user", clock())
        clock.advance(timedelta(minutes=1))
        await give_up(db, opened, "PermanentDeliveryError: Slack chat.postMessage failed: channel_not_found", clock())
        clock.advance(timedelta(minutes=1))
        # Kim's pushes: one gave up, and one closed for a routine reason, which isn't a failure.
        closed = sql(clock())
        await db.execute(
            text("UPDATE push_queue SET closed_at = :t, last_error = 'failed: 400' WHERE outbox_id = :o"),
            {"t": closed, "o": opened},
        )
        await db.execute(
            text("UPDATE push_queue SET closed_at = :t, last_error = :e WHERE outbox_id = :o"),
            {"t": closed, "o": reminder, "e": push.NOT_WANTED},
        )
        await db.commit()
        details = await health_details(db, house["Alex"])
    assert all(f.event is not None and f.event.id == event.id for f in details.failed)
    assert [(f.via, f.kind, f.recipient, f.reason) for f in details.failed] == [
        ("Web push", "Call opened", "Kim", "failed: 400"),
        (
            "Slack",
            "Call opened",
            "The channel",
            "PermanentDeliveryError: Slack chat.postMessage failed: channel_not_found",
        ),
        ("Slack", "Reminder to answer", "Kim", "PermanentDeliveryError: member 3 has no Slack user"),
    ]
    assert {f.event_id for f in details.failed} == {event.id}


@pytest.mark.anyio
async def test_a_payload_that_cant_be_read_still_lists_and_the_list_is_capped(house: House, clock: FakeClock) -> None:
    async with house.sessionmaker() as db:
        for n in range(admin_health.LIMIT + 3):
            db.add(
                OutboxRow(
                    household_id=house.household.id,
                    kind="response_reminder",
                    payload={"nonsense": n},
                    idempotency_key=f"k{n}",
                    created_at=clock(),
                    updated_at=clock(),
                    available_at=clock(),
                    attempts=1,
                    failed_at=clock() + timedelta(seconds=n),
                    last_error=None,
                )
            )
        await db.commit()
        details = await health_details(db, house["Alex"])
    assert len(details.failed) == admin_health.LIMIT
    assert {(f.recipient, f.reason) for f in details.failed} == {("Unknown", "No reason recorded")}
    assert details.failed[0].at == clock() + timedelta(seconds=admin_health.LIMIT + 2)


@pytest.mark.anyio
async def test_only_an_admin_reads_it(house: House) -> None:
    async with house.sessionmaker() as db:
        with pytest.raises(Forbidden):
            await health_details(db, house["Kim"])


def sql(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


# The audit log ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_audit_log_says_who_did_what_to_what(house: House, clock: FakeClock) -> None:
    event = await a_call(house)
    sam = house["Sam"]
    rows = [
        (house["Alex"].id, "member.invited", f"member:{sam.id}"),
        (None, "event.voting", f"event:{event.id}"),
        (None, "member.admin_added", f"member:{sam.id}"),
        (sam.id, "catalog.item_created", "item:999"),
        (None, "member.join_refused", "slack:U07PAT"),
        (None, "member.join_refused", "slack:redacted"),
        (sam.id, "household.slot_schedule_updated", f"household:{house.household.id}"),
        (sam.id, "meal.logged", "meal:4"),
        (sam.id, "something.new", "odd"),
        (sam.id, "member.slack_user_set", None),
    ]
    async with house.sessionmaker() as db:
        for n, (actor, action, target) in enumerate(rows):
            db.add(
                AuditRow(
                    household_id=house.household.id,
                    actor_member_id=actor,
                    action=action,
                    target=target,
                    payload={},
                    created_at=clock() + timedelta(seconds=n),
                )
            )
        await db.commit()
        details = await health_details(db, house["Alex"])
    lines = [(a.who, a.words, a.target, a.target_path) for a in details.audit[: len(rows)]]
    assert lines == [
        ("Sam", "Member Slack user set", None, None),
        ("Sam", "something.new", "odd", None),
        ("Sam", "Meal logged", "Meal 4", None),
        ("Sam", "Household slot schedule updated", "The household", None),
        ("Dinnerbell", "Member join refused", "A Slack account", None),
        ("Dinnerbell", "Member join refused", "Slack user U07PAT", None),
        ("Sam", "Catalog item created", "Catalog item 999", None),
        ("The host", "Admin added from the host", "Sam", f"/admin/members/{sam.id}"),
        ("Dinnerbell", "Voting opened", None, f"/events/{event.id}"),
        ("Alex", "Member invited", "Sam", f"/admin/members/{sam.id}"),
    ]
    voting = details.audit[8]
    assert voting.event is not None and voting.event.id == event.id


@pytest.mark.anyio
async def test_catalog_targets_show_their_names(house: House) -> None:
    dumplings = await house.add_item("Dumplings", cuisine="Chinese")
    tag = house.tags["Chinese"]
    async with house.sessionmaker() as db:
        for target in (f"item:{dumplings.id}", f"tag:{tag.id}", "tag:999", "event:999"):
            db.add(
                AuditRow(
                    household_id=house.household.id,
                    actor_member_id=None,
                    action="catalog.tag_renamed",
                    target=target,
                    payload={},
                    created_at=house.clock(),
                )
            )
        await db.commit()
        details = await health_details(db, house["Alex"])
    found = {(a.target, a.target_path) for a in details.audit}
    assert {
        ("Dumplings", f"/catalog/items/{dumplings.id}"),
        ("Chinese", None),
        ("Tag 999", None),
        ("Call 999", None),
    } <= found


def test_plain_words() -> None:
    assert kind_words("call_opened") == "Call opened"
    assert kind_words("event_cancelled") == "Call called off"
    assert action_words("event.cancelled") == "Call called off"
    assert action_words("member.slack_linked") == "Member Slack linked"
    assert action_words("member.") == "member."
    assert action_words("files.upload") == "files.upload"


# Fast on a large database -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_lists_read_down_indexes(house: House, database: Database) -> None:
    """Each list's query, as the repositories send it, reads an index and stops at ``LIMIT``.
    The audit log and the outbox read theirs in order, so a big table costs nothing more. The
    push queue keeps only a week (``services.push.PURGE_AFTER``), so sorting what it holds for
    the household stays small."""
    sent: list[tuple[str, object]] = []

    def capture(_conn: object, _cursor: object, statement: str, parameters: object, *_: object) -> None:
        if statement.startswith("SELECT") and (
            "audit" in statement or "failed_at" in statement or "closed_at" in statement
        ):
            sent.append((statement, parameters))

    sql_events.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with house.sessionmaker() as db:
            await health_details(db, house["Alex"])
    finally:
        sql_events.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    plans = {}
    async with house.sessionmaker() as db:
        for statement, parameters in sent:
            connection = await db.connection()
            raw = await connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)  # type: ignore[arg-type]
            plans[statement] = " ".join(str(row[-1]) for row in raw)
    audit = next(plan for sql, plan in plans.items() if "FROM audit" in sql)
    outbox = next(plan for sql, plan in plans.items() if "outbox.failed_at IS NOT NULL" in sql)
    pushes = next(plan for sql, plan in plans.items() if "FROM push_queue" in sql)
    assert "ix_audit_household_created" in audit and "TEMP B-TREE" not in audit, audit
    assert "ix_outbox_pending" in outbox and "TEMP B-TREE" not in outbox, outbox
    assert "SEARCH push_queue USING INDEX" in pushes, pushes
    assert all("LIMIT" in sql for sql in plans)


# The page ---------------------------------------------------------------------------------------


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    sign_in(client, provider)
    return client


def add_audit(settings: Settings, **values: object) -> None:
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            "INSERT INTO audit (household_id, actor_member_id, action, target, payload, created_at) "
            "VALUES (1, NULL, :action, :target, :payload, :at)",
            values,
        )


def test_the_page_shows_status_failures_and_the_audit_log(admin: TestClient, migrated: Settings) -> None:
    settle(admin)
    assert invite(admin, "sam@example.com", "Sam").status_code == 303
    sam = member(migrated, "sam@example.com").id
    # A refused sign-in keeps a stranger's email and subject, and a row can hold a token: the page
    # never shows a payload.
    add_audit(
        migrated,
        action="auth.sign_in_refused",
        target=None,
        payload='{"email": "stranger@example.com", "subject": "sub-stranger", "token": "xoxb-not-a-real-token"}',
        at="2026-10-22 18:00:00.000000",
    )
    response = admin.get("/admin/health")
    assert response.status_code == 200
    html = page(response)
    assert 'aria-current="page">Health</a>' in html
    assert 'class="admin-page"' in html or "admin-page" in html.split("<main", 1)[1].split(">", 1)[0]
    assert "Everything is working." in html or "Something needs attention" in html
    status = html.split('id="status"', 1)[1].split("</section>", 1)[0]
    assert "<dt>Scheduler</dt><dd>Running" in status
    assert "<dt>Database</dt><dd>Reachable</dd>" in status
    assert "<dt>Slack</dt><dd>Not set up</dd>" in status
    assert re.search(
        r"<dt>Last backup</dt><dd><time [^>]+>.+</time>, <code>dinnerbell-\d{4}-\d{2}-\d{2}\.db</code>", status
    )
    assert "<dt>Slack notifications that failed</dt><dd>0</dd>" in status
    assert "<dt>Version</dt>" in status
    assert "None. Every notification went out" in html
    assert "Slack failures go about" not in html  # the footnote comes with a list
    audit = html.split('id="audit"', 1)[1]
    assert f'<strong>Member invited</strong>: <a href="/admin/members/{sam}">Sam</a>' in audit
    assert "<strong>Sign-in refused</strong></div>" in audit
    for secret in ("stranger@example.com", "sub-stranger", "xoxb-"):
        assert secret not in html
    assert "<form" not in html.split("<main", 1)[1].replace('<form method="post" action="/auth/logout"', "")


def test_only_admins_reach_the_page(client: TestClient, provider: FakeProvider) -> None:
    assert client.get("/admin/health", follow_redirects=False).status_code in {302, 303, 401}
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    assert "/admin/health" in client.get("/admin/members").text
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    assert client.get("/admin/health").status_code == 403
    assert client.post("/admin/health").status_code in {403, 405}


def test_an_old_or_failing_backup_warns(admin: TestClient) -> None:
    settle(admin)
    state = admin.app.state.dinnerbell.scheduler_state  # type: ignore[attr-defined]
    clock = admin.app.state.dinnerbell.clock  # type: ignore[attr-defined]
    state.last_backup = clock() - BACKUP_STALE_AFTER - timedelta(minutes=1)
    html = page(admin.get("/admin/health"))
    assert "more than 36 hours old" in warning(html, "backup")
    assert "Everything is working." not in html  # /healthz says ok, but the page warns
    assert "Something needs attention" in html
    state.last_backup, state.last_backup_file = None, None
    assert "no backup yet" in warning(page(admin.get("/admin/health")), "backup")
    state.backup_failing = True
    html = page(admin.get("/admin/health"))
    assert "The last backup failed." in warning(html, "backup")
    assert "Something needs attention" in html
    state.backup_failing = False
    state.last_backup = clock()
    html = page(admin.get("/admin/health"))
    assert 'id="warn-' not in html
    assert "Everything is working." in html


def warning(html: str, cause: str) -> str:
    return html.split(f'id="warn-{cause}"', 1)[1].split("</p>", 1)[0]


NOW = datetime(2026, 10, 22, 18, tzinfo=UTC)
HEALTHY = HealthReport(True, "ok", NOW, "connected", OutboxHealth(0, 0, None), None, NOW, "x.db", False, 0)
LATE = timedelta(hours=1, minutes=5)
DEGRADED = {
    "scheduler": [{"scheduler": "stale"}, {"scheduler": "starting", "last_tick": None}],
    "slack": [{"slack": "disconnected"}, {"slack": "error"}],
    "outbox": [{"outbox": OutboxHealth(3, 0, LATE)}],
    "push": [{"push": PushBacklog(2, LATE)}],
    "backup": [{"backup_failing": True}],
    "stuck_calls": [{"stuck_calls": 2}, {"stuck_calls": None}],
}
"""Each cause of "degraded", and the changes that set it."""


def test_each_cause_degrades_the_report_and_stopping_is_an_error() -> None:
    assert HEALTHY.status == "ok" and HEALTHY.causes == frozenset() and HEALTHY.healthy
    for cause, changes in DEGRADED.items():
        for change in changes:
            report = replace(HEALTHY, **change)  # type: ignore[arg-type]
            assert (report.status, report.causes) == ("degraded", {cause}), change
    # Waiting up to the hour, or "not_configured" Slack, is fine.
    fine = replace(HEALTHY, slack="not_configured", outbox=OutboxHealth(1, 4, timedelta(hours=1)))
    assert fine.status == "ok"
    assert replace(fine, push=PushBacklog(1, timedelta(hours=1))).status == "ok"
    stopped = replace(HEALTHY, scheduler="stopped")
    assert (stopped.status, stopped.healthy, stopped.causes) == ("error", False, {"scheduler"})
    unreachable = replace(HEALTHY, database_ok=False, outbox=None)
    assert (unreachable.status, unreachable.healthy) == ("error", False)


@pytest.mark.parametrize(
    ("cause", "change"), [(cause, change) for cause, changes in DEGRADED.items() for change in changes]
)
def test_each_cause_shows_a_warning(
    admin: TestClient, monkeypatch: pytest.MonkeyPatch, cause: str, change: dict[str, object]
) -> None:
    """The page's "Look for the warnings below" always has one to point to."""
    report = replace(HEALTHY, **change)  # type: ignore[arg-type]

    async def checked(*_: object) -> HealthReport:
        return report

    monkeypatch.setattr(health_routes, "check_health", checked)
    html = page(admin.get("/admin/health"))
    assert "Something needs attention. Look for the warnings below." in html
    assert re.findall(r'id="warn-([a-z-]+)"', html) == [cause.replace("_", "-")]
    assert warning(html, cause.replace("_", "-")).strip()


def test_an_error_says_why(admin: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    for change, cause in (
        ({"scheduler": "stopped"}, "scheduler"),
        ({"database_ok": False, "outbox": None}, "database"),
    ):
        report = replace(HEALTHY, **change)  # type: ignore[arg-type]

        async def checked(*_: object, report: HealthReport = report) -> HealthReport:
            return report

        monkeypatch.setattr(health_routes, "check_health", checked)
        html = page(admin.get("/admin/health"))
        assert "answers 503" in html
        assert cause in re.findall(r'id="warn-([a-z-]+)"', html)


def test_a_failure_names_its_call_and_a_call_thats_gone_has_no_link(
    admin: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = admin_health.FailedNotification(NOW, "Slack", "Call opened", "The channel", "x", 7, None)

    async def details(*_: object) -> admin_health.HealthDetails:
        return admin_health.HealthDetails([gone], [])

    monkeypatch.setattr(health_routes, "health_details", details)
    failed = page(admin.get("/admin/health")).split('id="failed"', 1)[1].split("</section>", 1)[0]
    assert "for The channel · Call 7</div>" in failed
    assert "Slack failures go about 90 days after their call ends, and failed pushes after 7 days." in failed
    assert "/events/7" not in failed


def test_the_report_knows_when_a_backup_is_stale() -> None:
    assert not HEALTHY.backup_stale(NOW + BACKUP_STALE_AFTER)
    assert HEALTHY.backup_stale(NOW + BACKUP_STALE_AFTER + timedelta(seconds=1))
    assert replace(HEALTHY, last_backup=None).backup_stale(NOW)


# The outbox's failed count (/healthz) -----------------------------------------------------------


@pytest.mark.anyio
async def test_the_failed_count_reads_the_index_and_counts_the_same(
    house: House, database: Database, clock: FakeClock
) -> None:
    event = await a_call(house)
    kim, sam = who(house, "Kim", "Sam")
    await enqueue(house, note.ResponseReminder(event, kim), note.ResponseReminder(event, sam))
    first, second = await outbox_ids(house)
    sent: list[tuple[str, object]] = []

    def capture(_conn: object, _cursor: object, statement: str, parameters: object, *_: object) -> None:
        if "failed_at IS NOT NULL" in statement:
            sent.append((statement, parameters))

    async with house.sessionmaker() as db:
        await give_up(db, first, "PermanentDeliveryError: no Slack user", clock())
        repo = OutboxRepository(db)
        assert await repo.claim(second, "u", now=clock(), until=clock() + timedelta(minutes=5))
        assert await repo.delivered(second, "u", now=clock(), ref=None)
        await db.commit()
        sql_events.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            assert await repo.backlog() == (0, 1)
        finally:
            sql_events.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        [(statement, parameters)] = sent
        connection = await db.connection()
        raw = await connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)  # type: ignore[arg-type]
        plan = " ".join(str(row[-1]) for row in raw)
    assert "ix_outbox_pending" in plan and "SCAN outbox" not in plan.replace("USING COVERING INDEX", ""), plan
