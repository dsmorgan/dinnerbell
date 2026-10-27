# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discarding a call and starting one again at any time, through every surface (#60): the web
page and its confirmation, ``/dinnerbell-test discard``, the App Home, and ``dinnerbell
discard-call``. Each works on a labeled instance and refuses on production."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dinnerbell import main as cli
from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.interactions import DISCARD_REFUSED
from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, EventRow
from dinnerbell.domain.events import EventState
from dinnerbell.domain.meals import Slot
from dinnerbell.services import calls
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.household import current_household
from tests.conftest import invite, page, sign_in
from tests.support.calls import OPENS, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.slack import Slack, wire
from tests.support.web import submit
from tests.test_auth_flow import rows
from tests.test_events_web import sign_in_as, start
from tests.test_scrub import LABEL, Terminal, cli_env  # noqa: F401 - cli_env is a fixture

LOCAL = ZoneInfo("America/Chicago")
TOO_LATE = datetime(2026, 10, 22, 20, 31, tzinfo=LOCAL).astimezone(UTC)
MEMBER = "sam@example.com"


def discard_link(event_id: int) -> str:
    return f'href="/events/{event_id}/admin/discard"'


# The web --------------------------------------------------------------------------------------------


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    """The admin is signed in, with Sam invited as a plain member."""
    sign_in(client, provider)
    assert invite(client, MEMBER, "Sam").status_code == 303
    return client


@pytest.mark.parametrize("settings_overrides", [{"instance_label": LABEL}])
def test_an_admin_discards_a_call_on_the_web_and_starts_a_fresh_one(
    admin: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    event_id = start(admin)
    assert submit(admin, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
    card = page(admin.get(f"/events/{event_id}"))
    assert discard_link(event_id) in card and "Discard (test)" in card

    confirm = admin.get(f"/events/{event_id}/admin/discard")
    assert confirm.status_code == 200
    text = page(confirm)
    assert "Discard dinner for" in text and "Answers: 1" in text and "Notifications not yet sent: 2" in text
    assert f'action="/events/{event_id}/admin/discard"' in text

    assert admin.post(f"/events/{event_id}/admin/discard", data={}).status_code == 403  # no CSRF token
    assert len(rows(migrated, EventRow)) == 1
    done = submit(admin, f"/events/{event_id}/admin/discard")
    assert (done.status_code, done.headers["location"]) == (303, "/")
    assert rows(migrated, EventRow) == []
    audit = [a for a in rows(migrated, AuditRow) if a.action == "event.discarded"]
    assert [a.target for a in audit] == [f"event:{event_id}"]
    assert admin.get(f"/events/{event_id}").status_code == 404
    assert submit(admin, f"/events/{event_id}/admin/discard").status_code == 404  # already gone

    fresh = start(admin)
    assert fresh > event_id


@pytest.mark.parametrize("settings_overrides", [{"instance_label": LABEL}])
def test_a_member_never_sees_or_reaches_discard(admin: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    event_id = start(admin)
    sign_in_as(admin, provider, MEMBER)
    assert discard_link(event_id) not in admin.get(f"/events/{event_id}").text
    assert discard_link(event_id) not in admin.get("/").text
    assert admin.get(f"/events/{event_id}/admin/discard").status_code == 403
    assert submit(admin, f"/events/{event_id}/admin/discard").status_code == 403
    assert len(rows(migrated, EventRow)) == 1


def test_production_has_no_discard(admin: TestClient, migrated: Settings) -> None:
    """Unlabeled, the control never renders and both routes are a 404, even for the admin
    with a good CSRF token."""
    event_id = start(admin)
    assert discard_link(event_id) not in admin.get(f"/events/{event_id}").text
    assert "Discard (test)" not in admin.get("/").text
    assert admin.get(f"/events/{event_id}/admin/discard").status_code == 404
    assert submit(admin, f"/events/{event_id}/admin/discard").status_code == 404
    assert len(rows(migrated, EventRow)) == 1
    assert [a for a in rows(migrated, AuditRow) if a.action == "event.discarded"] == []


@pytest.mark.parametrize(("settings_overrides", "any_time"), [({"instance_label": LABEL}, True), ({}, False)])
def test_start_again_after_the_too_late_time_only_on_test(
    admin: TestClient, clock: FakeClock, migrated: Settings, any_time: bool
) -> None:
    event_id = start(admin)
    assert submit(admin, f"/events/{event_id}/cancel", {"run": "1"}).status_code == 303
    clock.now = TOO_LATE
    card = page(admin.get(f"/events/{event_id}"))
    assert ("Start dinner again" in card) is any_time
    assert ("Start dinner again" in page(admin.get("/"))) is any_time
    restarted = submit(admin, f"/events/{event_id}/restart", {"run": "1"})
    assert restarted.status_code == (303 if any_time else 409)
    started = submit(admin, "/events", {"slot": "dinner", "day": "2026-10-22"})
    assert started.status_code == 409  # running again, or still too late
    state = rows(migrated, EventRow)[0]
    assert (state.state, state.run) == (("collecting", 2) if any_time else ("cancelled", 1))


# Slack ---------------------------------------------------------------------------------------------


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


@pytest.fixture
async def test_slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house, command="/dinnerbell-test", instance_label=LABEL)
    yield wired
    await wired.conn.stop()


@pytest.fixture
async def production_slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


def reply(response: object) -> str:
    raw = getattr(response, "body", "")
    return str(json.loads(raw)["text"]) if raw else ""


async def calls_left(house: House) -> list[tuple[Slot, int]]:
    async with house.sessionmaker() as db:
        return [(Slot(r.slot), r.id) for r in await db.scalars(select(EventRow).order_by(EventRow.id))]


@pytest.mark.anyio
async def test_slash_discard_on_test(test_slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await test_slack.command("Kim", "call", command="/dinnerbell-test")
    await test_slack.command("Kim", "call lunch tomorrow", command="/dinnerbell-test")
    dinner = await dinner_id(house)
    assert "`/dinnerbell-test discard`" in reply(await test_slack.command("Alex", "help", command="/dinnerbell-test"))

    await test_slack.command("Kim", "discard", command="/dinnerbell-test")
    assert test_slack.ephemerals("Kim")[-1] == "Only an admin can discard a call."
    await test_slack.command("Alex", "discard lunch", command="/dinnerbell-test")
    assert test_slack.ephemerals("Alex")[-1] == "There's no lunch call today."
    await test_slack.command("Alex", "discard 2026-13-40", command="/dinnerbell-test")
    assert test_slack.ephemerals("Alex")[-1].startswith("Try `/dinnerbell-test discard`")
    assert len(await calls_left(house)) == 2

    await test_slack.command("Alex", "discard", command="/dinnerbell-test")
    assert test_slack.ephemerals("Alex")[-1] == (
        "Discarded the dinner call today. Start a fresh one with `/dinnerbell-test call`."
    )
    await test_slack.command("Alex", "discard lunch tomorrow", command="/dinnerbell-test")
    assert test_slack.ephemerals("Alex")[-1].startswith("Discarded the lunch call for Friday, Oct 23.")
    assert await calls_left(house) == []

    await test_slack.click("Kim", "respond_in", str(dinner))  # a tap on the old message
    assert test_slack.ephemerals("Kim")[-1] == "No such call."
    await test_slack.command("Kim", "call", command="/dinnerbell-test")
    assert [slot for slot, _ in await calls_left(house)] == [Slot.DINNER]


@pytest.mark.anyio
async def test_slash_discard_on_production_is_refused(production_slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await production_slack.command("Alex", "call")
    assert reply(await production_slack.command("Alex", "discard")) == DISCARD_REFUSED
    assert "discard" not in reply(await production_slack.command("Alex", "help"))
    assert len(await calls_left(house)) == 1


@pytest.mark.anyio
async def test_home_offers_start_again_after_the_too_late_time_on_test(test_slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    async with house.sessionmaker() as db:
        await calls.cancel_call(db, house["Alex"], event_id, now=house.clock())
    house.clock.now = TOO_LATE
    await test_slack.open_home("Sam")
    home = test_slack.last_home("Sam")
    actions = [e["action_id"] for b in home["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert "start_call_dinner" in actions
    await test_slack.click("Sam", ids.ACTION_FOR_START[Slot.DINNER], "dinner", home=True)
    event = await house.event(event_id)
    assert (event.state, event.run) == (EventState.COLLECTING, 2)


# The command ------------------------------------------------------------------------------------


@pytest.fixture
def labeled_cli(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, int]:  # noqa: F811
    """A labeled instance with today's dinner call, started by the admin. Yields the database
    and the call's ID."""
    monkeypatch.setenv("DINNERBELL_INSTANCE_LABEL", LABEL)
    assert cli.main(["migrate"]) == 0

    async def seed() -> int:
        database = open_database(f"sqlite+aiosqlite:///{cli_env}")
        try:
            async with database.sessionmaker() as db:
                await bootstrap(db, household_name="Home", timezone="UTC", admin_email="admin@example.com", now=OPENS)
                await db.commit()
                household = await current_household(db)
                from dinnerbell.db.repositories import MemberRepository

                admin = (await MemberRepository(db).list(household.id))[0]
                event = await calls.start_call(db, admin, Slot.DINNER, date(2026, 10, 22), now=OPENS)
                return event.id
        finally:
            await database.close()

    return cli_env, asyncio.run(seed())


def events_in(path: Path) -> list[int]:
    async def read() -> list[int]:
        database = open_database(f"sqlite+aiosqlite:///{path}")
        try:
            async with database.sessionmaker() as db:
                return list(await db.scalars(select(EventRow.id)))
        finally:
            await database.close()

    return asyncio.run(read())


def test_the_command_refuses_without_a_label_before_touching_the_database(
    cli_env: Path,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["discard-call", "1", "--yes"]) == 1
    assert "only on a labeled instance" in capsys.readouterr().err
    assert not cli_env.exists()  # not even migrated


def test_the_command_previews_then_discards_with_yes(
    labeled_cli: tuple[Path, int], capsys: pytest.CaptureFixture[str]
) -> None:
    path, event_id = labeled_cli
    assert cli.main(["discard-call", str(event_id), "--yes"]) == 0
    out = capsys.readouterr().out
    # #75: the discard's log line, on stdout as JSON, says the command line did it.
    (line,) = [json.loads(o) for o in out.splitlines() if o.startswith("{") and '"msg": "discard"' in o]
    assert (line["channel"], line["event_id"], line["state_before"]) == ("cli", event_id, "collecting")
    assert line["request_id"]
    assert f"Database: {path}" in out and f"Call: dinner on 2026-10-22 (ID {event_id}, run 1)" in out
    assert (
        "Notifications not yet sent: 1" in out and f"Discarded the dinner call for 2026-10-22 (ID {event_id})." in out
    )
    assert events_in(path) == []
    assert cli.main(["discard-call", str(event_id), "--yes"]) == 1
    assert f"There's no call with ID {event_id}. Nothing changed." in capsys.readouterr().err


def test_a_typed_confirmation_at_a_terminal_discards(
    labeled_cli: tuple[Path, int], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path, event_id = labeled_cli
    monkeypatch.setattr("sys.stdin", Terminal("discard\n"))
    assert cli.main(["discard-call", str(event_id)]) == 0
    assert 'Type "discard" to discard it' in capsys.readouterr().out
    assert events_in(path) == []


@pytest.mark.parametrize(
    ("stdin", "message"),
    [(Terminal("yes\n"), "Not discarded"), (Terminal(""), "Not discarded"), (io.StringIO("discard\n"), "No terminal")],
)
def test_no_confirmation_changes_nothing(
    labeled_cli: tuple[Path, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: io.StringIO,
    message: str,
) -> None:
    path, event_id = labeled_cli
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.main(["discard-call", str(event_id)]) == 1
    assert message in capsys.readouterr().err
    assert events_in(path) == [event_id]


@pytest.mark.parametrize("value", ["0", "-3", "dinner"])
def test_the_command_needs_a_call_id(cli_env: Path, value: str) -> None:  # noqa: F811
    with pytest.raises(SystemExit):
        cli.main(["discard-call", value])


def test_the_command_on_a_database_with_no_household(
    cli_env: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DINNERBELL_INSTANCE_LABEL", LABEL)
    assert cli.main(["discard-call", "1", "--yes"]) == 1
    assert "no household yet" in capsys.readouterr().err
