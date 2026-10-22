# SPDX-License-Identifier: AGPL-3.0-or-later
"""Service-level tests for paths the web round trip does not reach."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.config import Settings
from dinnerbell.db.models import MemberRow, SessionRow
from dinnerbell.db.types import UTCDateTime
from dinnerbell.domain.identity import IdentityClaims, Refusal, Refuse
from dinnerbell.domain.members import Capabilities, MemberRuleViolation, Role
from dinnerbell.services import auth, members
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.database import open_database
from dinnerbell.services.errors import Forbidden
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio


@pytest.fixture
async def db(migrated: Settings, clock: FakeClock) -> AsyncIterator[AsyncSession]:
    database = open_database(migrated.async_database_url)
    async with database.sessionmaker() as session:
        await bootstrap(session, household_name="Home", timezone="UTC", admin_email=migrated.admin_email, now=clock())
        yield session
    await database.close()


async def admin(db: AsyncSession) -> members.Member:
    row = await db.scalar(select(MemberRow).where(MemberRow.role == "admin"))
    assert row is not None
    from dinnerbell.db.repositories import to_member

    return to_member(row)


ADMIN_CLAIMS = IdentityClaims(subject="sub-admin", email="admin@example.com", email_verified=True)


async def test_sign_in_replaces_previous_session(db: AsyncSession, clock: FakeClock) -> None:
    first = await auth.sign_in(db, ADMIN_CLAIMS, required_team_id=None, now=clock())
    assert isinstance(first, auth.SignedIn)
    second = await auth.sign_in(db, ADMIN_CLAIMS, required_team_id=None, now=clock(), replaces_token=first.token)
    assert isinstance(second, auth.SignedIn)
    hashes = set(await db.scalars(select(SessionRow.id)))
    assert hashes == {auth.hash_token(second.token)}


async def test_malformed_email_claim_is_unknown(db: AsyncSession, clock: FakeClock) -> None:
    claims = IdentityClaims(subject="x", email="not an email", email_verified=True)
    result = await auth.sign_in(db, claims, required_team_id=None, now=clock())
    assert result == Refuse(Refusal.UNKNOWN_EMAIL)


async def test_sign_in_without_household_fails_loudly(migrated: Settings, clock: FakeClock) -> None:
    database = open_database(migrated.async_database_url)
    try:
        async with database.sessionmaker() as session:
            with pytest.raises(RuntimeError, match="bootstrap"):
                await auth.sign_in(session, ADMIN_CLAIMS, required_team_id=None, now=clock())
    finally:
        await database.close()


async def test_purge_expired_sessions(db: AsyncSession, clock: FakeClock) -> None:
    from datetime import timedelta

    await auth.sign_in(db, ADMIN_CLAIMS, required_team_id=None, now=clock())
    assert await auth.purge_expired_sessions(db, now=clock()) == 0
    assert await auth.purge_expired_sessions(db, now=clock() + timedelta(days=91)) == 1


async def test_members_only_for_admins(db: AsyncSession, clock: FakeClock) -> None:
    actor = await admin(db)
    sam = await members.invite_member(
        db, actor, members.MemberInput("Sam", "sam@example.com", Role.MEMBER, Capabilities()), now=clock()
    )
    with pytest.raises(Forbidden):
        await members.list_members(db, sam)


async def test_email_change_to_existing_email_is_refused(db: AsyncSession, clock: FakeClock) -> None:
    actor = await admin(db)
    data = members.MemberInput("Sam", "sam@example.com", Role.MEMBER, Capabilities())
    sam = await members.invite_member(db, actor, data, now=clock())
    with pytest.raises(MemberRuleViolation, match="already exists"):
        await members.update_member(
            db, actor, sam.id, members.MemberInput("Sam", "admin@example.com", Role.MEMBER, Capabilities()), now=clock()
        )


async def test_set_active_is_idempotent(db: AsyncSession, clock: FakeClock) -> None:
    actor = await admin(db)
    sam = await members.invite_member(
        db, actor, members.MemberInput("Sam", "sam@example.com", Role.MEMBER, Capabilities()), now=clock()
    )
    assert (await members.set_member_active(db, actor, sam.id, True, now=clock())) == sam


async def test_unique_race_becomes_rule_violation(
    db: AsyncSession, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = await admin(db)
    data = members.MemberInput("Sam", "sam@example.com", Role.MEMBER, Capabilities())
    await members.invite_member(db, actor, data, now=clock())

    async def not_found(*_args: object) -> None:
        return None  # simulate a concurrent invite that the pre-check missed

    monkeypatch.setattr(members.MemberRepository, "by_email", not_found)
    with pytest.raises(MemberRuleViolation, match="already exists"):
        await members.invite_member(db, actor, data, now=clock())


def test_utc_datetime_type() -> None:
    column = UTCDateTime()
    assert column.process_bind_param(None, None) is None  # type: ignore[arg-type]
    assert column.process_result_value(None, None) is None  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="naive"):
        column.process_bind_param(datetime(2026, 1, 1), None)  # type: ignore[arg-type]  # noqa: DTZ001
    aware = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert column.process_result_value(column.process_bind_param(aware, None), None) == aware  # type: ignore[arg-type]


async def test_channel_protocol_is_structural() -> None:
    from dinnerbell.channels.base import Channel
    from dinnerbell.domain.notifications import DeliveryResult, MessageRef, Notification

    class Recorder:
        def __init__(self) -> None:
            self.sent: list[Notification] = []

        async def deliver(self, notification: Notification) -> DeliveryResult:
            self.sent.append(notification)
            return DeliveryResult(MessageRef("C1", "1.0"))

        async def update(self, ref: MessageRef, notification: Notification) -> None:
            self.sent.append(notification)

    channel: Channel = Recorder()
    result = await channel.deliver(Notification("key-1"))
    assert result.ref == MessageRef("C1", "1.0")
