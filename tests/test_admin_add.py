# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell admin add EMAIL`` (#360): the way back in when no admin can sign in."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text

from dinnerbell import main as cli
from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, MemberRow, SessionRow
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.identity import IdentityClaims
from dinnerbell.domain.members import InvalidMemberInput
from dinnerbell.services import admin_add
from dinnerbell.services.auth import SignedIn, sign_in
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.household import NoHousehold
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.test_config import REQUIRED

pytestmark = pytest.mark.anyio
TEAM = "T0HOUSE"


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def add(house: House, email: str) -> admin_add.AdminAdded:
    async with house.sessionmaker() as db:
        return await admin_add.add_admin(db, email, now=house.clock())


async def row(house: House, member_id: int) -> MemberRow:
    async with house.sessionmaker() as db:
        found = await db.scalar(select(MemberRow).where(MemberRow.id == member_id))
    assert found is not None
    return found


async def run(house: House, sql: str, **params: Any) -> None:
    async with house.sessionmaker() as db:
        await db.execute(text(sql), params)
        await db.commit()


async def audits(house: House) -> list[AuditRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AuditRow).where(AuditRow.action == "member.admin_added")))


async def sessions(house: House, member_id: int) -> int:
    async with house.sessionmaker() as db:
        return len(list(await db.scalars(select(SessionRow).where(SessionRow.member_id == member_id))))


async def slack_sign_in(house: House, subject: str, email: str, slack_user: str) -> SignedIn:
    claims = IdentityClaims(subject, email, True, slack_team_id=TEAM, slack_user_id=slack_user)
    async with house.sessionmaker() as db:
        result = await sign_in(db, claims, required_team_id=TEAM, slack_sign_in=True, now=house.clock())
    assert isinstance(result, SignedIn), result
    return result


async def test_a_missing_member_is_invited_as_an_admin(house: House) -> None:
    result = await add(house, "  Riley@Example.com ")
    found = await row(house, result.member_id)
    assert (found.email, found.role, found.active, found.oidc_subject) == ("riley@example.com", "admin", True, None)
    assert result.lines() == [f"Invited member {result.member_id} as an admin.", "Sign in with this email to finish."]
    (audit,) = await audits(house)
    assert (audit.actor_member_id, audit.target) == (None, f"member:{result.member_id}")
    assert audit.payload == {
        "invited": True,
        "promoted": False,
        "reactivated": False,
        "unlinked": False,
        "guest_id": None,
        "guest_unlinked": False,
    }
    signed_in = await slack_sign_in(house, "sub-riley", "riley@example.com", "U01RILEY")
    assert signed_in.member.id == result.member_id and signed_in.member.is_admin


async def test_a_member_is_promoted(house: House) -> None:
    kim = house["Kim"]
    result = await add(house, "kim@example.com")
    assert result.member_id == kim.id and result.promoted and not result.invited
    assert (await row(house, kim.id)).role == "admin"
    assert f"Made member {kim.id} an admin." in result.lines()


async def test_an_inactive_member_is_reactivated(house: House) -> None:
    lee = house["Lee"]
    await run(house, "UPDATE member SET active = 0 WHERE id = :id", id=lee.id)
    result = await add(house, "lee@example.com")
    assert result.reactivated and result.promoted
    found = await row(house, lee.id)
    assert (found.active, found.role) == (True, "admin")
    assert result.lines()[:2] == [f"Reactivated member {lee.id}.", f"Made member {lee.id} an admin."]


async def test_an_admin_who_lost_their_slack_account_signs_in_with_a_new_one(house: House) -> None:
    """The only admin's old Slack account is gone. Their new one, with the same email, is told
    the email is linked elsewhere until the command clears the old links."""
    alex = house["Alex"]
    old = await slack_sign_in(house, "sub-old", alex.email or "", "U01OLD")
    await run(house, "UPDATE member SET slack_link_locked = 1 WHERE id = :id", id=alex.id)
    claims = IdentityClaims("sub-new", alex.email, True, slack_team_id=TEAM, slack_user_id="U01NEW")
    async with house.sessionmaker() as db:
        refused = await sign_in(db, claims, required_team_id=TEAM, slack_sign_in=True, now=house.clock())
    assert not isinstance(refused, SignedIn)
    result = await add(house, alex.email or "")
    assert result.unlinked and not result.promoted
    found = await row(house, alex.id)
    assert (found.oidc_subject, found.slack_user_id, found.slack_link_locked) == (None, None, False)
    assert await sessions(house, alex.id) == 0 and old.member.id == alex.id  # the old account is signed out
    assert f"Cleared the sign-in and Slack links of member {alex.id}, and signed it out everywhere." in result.lines()
    new = await slack_sign_in(house, "sub-new", alex.email or "", "U01NEW")
    assert new.member.id == alex.id
    assert (await row(house, alex.id)).slack_user_id == "U01NEW"


async def test_an_admin_that_the_next_sign_in_claims_changes_nothing(house: House) -> None:
    alex = house["Alex"]
    result = await add(house, alex.email or "")
    assert not result.changed
    assert f"Member {alex.id} is already an active admin that the next sign-in claims. Nothing changed." in (
        result.lines()
    )
    assert await audits(house) == []


async def joined_guest(house: House, email: str, *, slack_guest_account: bool = False) -> int:
    """A guest who joined from the channel with ``email`` as their Slack email, and signed in."""
    async with house.sessionmaker() as db:
        members = MemberRepository(db)
        guest = await members.add_guest(
            household_id=house.household.id,
            display_name="Riley",
            dietary_needs=frozenset(),
            slack_user_id="U01RILEY",
            slack_guest_account=slack_guest_account,
            now=house.clock(),
        )
        await members.update(house.household.id, guest.id, house.clock(), slack_email=email, oidc_subject="sub-riley")
        await db.commit()
    return guest.id


async def test_a_guest_with_the_email_is_folded_into_the_admin_at_their_next_sign_in(house: House) -> None:
    """A guest isn't a member, so it never becomes the admin: the command invites one, and the
    guest's next Sign in with Slack folds it in (#360)."""
    guest_id = await joined_guest(house, "riley@example.com")
    result = await add(house, "riley@example.com")
    assert result.invited and result.guest_id == guest_id and not result.guest_unlinked
    assert (await row(house, guest_id)).oidc_subject == "sub-riley"  # untouched
    assert f"Guest {guest_id} has this Slack email: signing in with Slack folds it into member {result.member_id}." in (
        result.lines()
    )
    signed_in = await slack_sign_in(house, "sub-riley", "riley@example.com", "U01RILEY")
    assert signed_in.member.id == result.member_id
    assert (await row(house, guest_id)).active is False


async def test_a_slack_guest_accounts_sign_in_link_is_cleared(house: House) -> None:
    """A Slack guest account is never folded on email alone, so its sign-in link would keep
    winning: the command clears it, and the email claims the admin."""
    guest_id = await joined_guest(house, "riley@example.com", slack_guest_account=True)
    result = await add(house, "riley@example.com")
    assert result.guest_unlinked
    guest = await row(house, guest_id)
    assert (guest.oidc_subject, guest.active, guest.slack_user_id) == (None, True, "U01RILEY")
    signed_in = await slack_sign_in(house, "sub-riley", "riley@example.com", "U01RILEY")
    assert signed_in.member.id == result.member_id and signed_in.member.is_admin
    (audit,) = await audits(house)
    assert audit.payload["guest_id"] == guest_id and audit.payload["guest_unlinked"] is True


async def test_a_bad_email_and_a_missing_household_are_refused(database: Database, clock: FakeClock) -> None:
    async with database.sessionmaker() as db:
        with pytest.raises(NoHousehold):
            await admin_add.add_admin(db, "riley@example.com", now=clock())
        with pytest.raises(InvalidMemberInput):
            await admin_add.add_admin(db, "not an email", now=clock())


# The command -------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DINNERBELL_INSTANCE_LABEL", raising=False)
    database = tmp_path / "data" / "dinnerbell.db"
    monkeypatch.setenv("DINNERBELL_DATABASE_URL", f"sqlite:///{database}")
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        yield database
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)


def bootstrapped(path: Path) -> None:
    async def go() -> None:
        database = open_database(f"sqlite+aiosqlite:///{path}")
        try:
            await make_house(database.sessionmaker, FakeClock(), "admin@example.com")
        finally:
            await database.close()

    asyncio.run(go())


def test_the_command_prints_what_it_did(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["migrate"]) == 0
    bootstrapped(cli_env)
    assert cli.main(["admin", "add", "riley@example.com"]) == 0
    out = capsys.readouterr().out
    with sqlite3.connect(cli_env) as db:
        (member_id,) = db.execute("SELECT id FROM member WHERE email = 'riley@example.com'").fetchone()
        (actor,) = db.execute("SELECT actor_member_id FROM audit WHERE action = 'member.admin_added'").fetchone()
    assert f"Invited member {member_id} as an admin." in out and actor is None
    assert "riley" not in out  # member IDs only


def test_the_command_refuses_without_a_database_a_household_or_an_email(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["admin", "add", "riley@example.com"]) == 1
    assert f"No database at {cli_env}. Nothing changed." in capsys.readouterr().err
    assert not cli_env.exists()
    assert cli.main(["migrate"]) == 0
    assert cli.main(["admin", "add", "riley@example.com"]) == 1
    assert "no household yet" in capsys.readouterr().err
    bootstrapped(cli_env)
    assert cli.main(["admin", "add", "riley"]) == 2
    assert "Enter a valid email address. Nothing changed." in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["admin", "add"])
