# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data access. Repositories translate between rows and domain entities.

Only the services layer calls these, and only services commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import AuditRow, HouseholdRow, MemberRow, SessionRow
from dinnerbell.domain.household import Household
from dinnerbell.domain.members import Capabilities, Member, Role


@dataclass(frozen=True, slots=True)
class StoredSession:
    token_hash: str
    member: Member
    household: Household
    expires_at: datetime
    last_seen: datetime


def _household(row: HouseholdRow) -> Household:
    return Household(id=row.id, name=row.name, timezone=row.timezone)


def to_member(row: MemberRow) -> Member:
    return Member(
        id=row.id,
        household_id=row.household_id,
        display_name=row.display_name,
        email=row.email,
        role=Role(row.role),
        capabilities=Capabilities(row.can_cook, row.can_pickup, row.can_order),
        active=row.active,
        oidc_subject=row.oidc_subject,
        slack_user_id=row.slack_user_id,
        slack_link_locked=row.slack_link_locked,
        mute_dm_reminders=row.mute_dm_reminders,
        feedback_reminder=row.feedback_reminder,
        dietary_needs=frozenset(int(t) for t in row.dietary_needs or []),
        join_refused=row.join_refused,
        slack_email=row.slack_email,
        slack_guest_account=row.slack_guest_account,
    )


class HouseholdRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def first(self) -> Household | None:
        row = await self._session.scalar(select(HouseholdRow).order_by(HouseholdRow.id).limit(1))
        return None if row is None else _household(row)

    async def get(self, household_id: int) -> Household | None:
        row = await self._session.get(HouseholdRow, household_id)
        return None if row is None else _household(row)

    async def all(self) -> list[Household]:
        rows = await self._session.scalars(select(HouseholdRow).order_by(HouseholdRow.id))
        return [_household(row) for row in rows]

    async def update_settings(self, household_id: int, settings: dict[str, Any]) -> None:
        """Replace the settings JSON. Callers merge their keys into the current settings first."""
        await self._session.execute(
            update(HouseholdRow)
            .where(HouseholdRow.id == household_id)
            .values(settings=settings)
            .execution_options(synchronize_session=False)
        )

    async def create(
        self, name: str, timezone: str, now: datetime, settings: dict[str, Any] | None = None
    ) -> Household:
        row = HouseholdRow(name=name, timezone=timezone, settings=dict(settings or {}), created_at=now)
        self._session.add(row)
        await self._session.flush()
        return _household(row)


class MemberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, household_id: int, member_id: int) -> Member | None:
        row = await self._row(household_id, member_id)
        return None if row is None else to_member(row)

    async def _row(self, household_id: int, member_id: int) -> MemberRow | None:
        return await self._session.scalar(
            select(MemberRow).where(MemberRow.household_id == household_id, MemberRow.id == member_id)
        )

    async def list(self, household_id: int) -> list[Member]:
        rows = await self._session.scalars(
            select(MemberRow)
            .where(MemberRow.household_id == household_id)
            .order_by(MemberRow.active.desc(), func.lower(MemberRow.display_name), MemberRow.id)
        )
        return [to_member(row) for row in rows]

    async def by_email(self, household_id: int, email: str) -> Member | None:
        row = await self._session.scalar(
            select(MemberRow).where(MemberRow.household_id == household_id, MemberRow.email == email)
        )
        return None if row is None else to_member(row)

    async def guest_by_slack_email(self, household_id: int, email: str) -> Member | None:
        """An active guest who joined with ``email`` as their Slack email (#38)."""
        row = await self._session.scalar(
            select(MemberRow).where(
                MemberRow.household_id == household_id,
                MemberRow.role == "guest",
                MemberRow.active.is_(True),
                MemberRow.slack_email == email,
            )
        )
        return None if row is None else to_member(row)

    async def by_subject(self, household_id: int, subject: str) -> Member | None:
        row = await self._session.scalar(
            select(MemberRow).where(MemberRow.household_id == household_id, MemberRow.oidc_subject == subject)
        )
        return None if row is None else to_member(row)

    async def by_slack_user_id(self, household_id: int, slack_user_id: str) -> Member | None:
        row = await self._session.scalar(
            select(MemberRow).where(MemberRow.household_id == household_id, MemberRow.slack_user_id == slack_user_id)
        )
        return None if row is None else to_member(row)

    async def count_active_admins(self, household_id: int) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(MemberRow)
            .where(MemberRow.household_id == household_id, MemberRow.active.is_(True), MemberRow.role == "admin")
        )
        return int(count or 0)

    async def add(
        self,
        *,
        household_id: int,
        display_name: str,
        email: str,
        role: Role,
        capabilities: Capabilities,
        now: datetime,
    ) -> Member:
        row = MemberRow(
            household_id=household_id,
            display_name=display_name,
            email=email,
            role=role.value,
            can_cook=capabilities.can_cook,
            can_pickup=capabilities.can_pickup,
            can_order=capabilities.can_order,
            mute_dm_reminders=False,
            feedback_reminder=False,
            active=True,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return to_member(row)

    async def add_guest(
        self,
        *,
        household_id: int,
        display_name: str,
        dietary_needs: frozenset[int],
        now: datetime,
        slack_user_id: str | None = None,
        slack_guest_account: bool = False,
    ) -> Member:
        """A guest (#38): no email, no capabilities, and a Slack user only if they have one."""
        row = MemberRow(
            household_id=household_id,
            display_name=display_name,
            email=None,
            slack_user_id=slack_user_id,
            role=Role.GUEST.value,
            can_cook=False,
            can_pickup=False,
            can_order=False,
            mute_dm_reminders=False,
            feedback_reminder=False,
            active=True,
            dietary_needs=sorted(dietary_needs),
            join_refused=False,
            slack_guest_account=slack_guest_account,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return to_member(row)

    async def update(self, household_id: int, member_id: int, now: datetime, **values: Any) -> Member:
        row = await self._row(household_id, member_id)
        if row is None:
            raise LookupError(member_id)
        for name, value in values.items():
            if isinstance(value, frozenset):
                value = sorted(value)  # dietary needs, stored as a JSON list
            setattr(row, name, value.value if isinstance(value, Role) else value)
        row.updated_at = now
        await self._session.flush()
        return to_member(row)


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, *, token_hash: str, member: Member, now: datetime, expires_at: datetime) -> None:
        self._session.add(
            SessionRow(
                id=token_hash,
                household_id=member.household_id,
                member_id=member.id,
                created_at=now,
                expires_at=expires_at,
                last_seen=now,
            )
        )
        await self._session.flush()

    async def get(self, token_hash: str) -> StoredSession | None:
        result = await self._session.execute(
            select(SessionRow, MemberRow, HouseholdRow)
            .join(MemberRow, MemberRow.id == SessionRow.member_id)
            .join(HouseholdRow, HouseholdRow.id == SessionRow.household_id)
            .where(SessionRow.id == token_hash)
        )
        found = result.first()
        if found is None:
            return None
        session_row, member_row, household_row = found
        return StoredSession(
            token_hash=session_row.id,
            member=to_member(member_row),
            household=_household(household_row),
            expires_at=session_row.expires_at,
            last_seen=session_row.last_seen,
        )

    async def touch(self, token_hash: str, *, now: datetime, expires_at: datetime) -> None:
        await self._session.execute(
            update(SessionRow).where(SessionRow.id == token_hash).values(last_seen=now, expires_at=expires_at)
        )

    async def delete(self, token_hash: str) -> None:
        await self._session.execute(delete(SessionRow).where(SessionRow.id == token_hash))

    async def delete_for_member(self, member_id: int) -> None:
        await self._session.execute(delete(SessionRow).where(SessionRow.member_id == member_id))

    async def delete_expired(self, now: datetime) -> int:
        result = await self._session.execute(delete(SessionRow).where(SessionRow.expires_at <= now))
        return int(getattr(result, "rowcount", 0) or 0)


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        household_id: int,
        actor_member_id: int | None,
        action: str,
        target: str | None,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        self._session.add(
            AuditRow(
                household_id=household_id,
                actor_member_id=actor_member_id,
                action=action,
                target=target,
                payload=payload,
                created_at=now,
            )
        )
        await self._session.flush()

    async def redact(self, household_id: int, prefixes: tuple[str, ...], payload: dict[str, Any]) -> int:
        """Replace the payload of every audit row whose action starts with one of ``prefixes``,
        and a target naming a Slack user, for ``scrub-test``. Returns how many changed."""
        rows = list(
            await self._session.scalars(
                select(AuditRow).where(
                    AuditRow.household_id == household_id,
                    or_(*(AuditRow.action.startswith(prefix) for prefix in prefixes)),
                )
            )
        )
        changed = 0
        for row in rows:
            target = "slack:redacted" if (row.target or "").startswith("slack:") else row.target
            if row.payload != payload or row.target != target:
                row.payload, row.target = dict(payload), target
                changed += 1
        await self._session.flush()
        return changed

    async def redact_about(
        self, household_id: int, actions: tuple[str, ...], targets: set[str], payload: dict[str, Any]
    ) -> int:
        """Replace the payload of every audit row with one of ``actions`` about one of
        ``targets``, for ``scrub-test``. Returns how many changed."""
        if not targets:
            return 0
        rows = await self._session.scalars(
            select(AuditRow).where(
                AuditRow.household_id == household_id,
                AuditRow.action.in_(actions),
                AuditRow.target.in_(sorted(targets)),
            )
        )
        changed = 0
        for row in rows:
            if row.payload != payload:
                row.payload = dict(payload)
                changed += 1
        await self._session.flush()
        return changed

    async def latest(self, household_id: int, target: str) -> tuple[str, dict[str, Any]] | None:
        """The newest audit row about ``target``, as (action, payload), or None."""
        row = await self._session.scalar(
            select(AuditRow)
            .where(AuditRow.household_id == household_id, AuditRow.target == target)
            .order_by(AuditRow.id.desc())
            .limit(1)
        )
        return None if row is None else (row.action, dict(row.payload))
