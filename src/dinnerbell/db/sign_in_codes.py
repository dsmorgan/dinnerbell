# SPDX-License-Identifier: AGPL-3.0-or-later
"""One-time sign-in codes (spec 6.15). Rows hold the code's SHA-256 hash, never the code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import MemberRow, SignInCodeRow
from dinnerbell.db.repositories import to_member
from dinnerbell.domain.members import Member


@dataclass(frozen=True, slots=True)
class StoredCode:
    """A code that can still be used: not used yet, and not expired."""

    id: int
    code_hash: str
    member: Member


class SignInCodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace(
        self, *, member: Member, code_hash: str, created_by: Member, now: datetime, expires_at: datetime
    ) -> int:
        """Give ``member`` this code in place of any they had, used or not. Returns its row ID."""
        await self._session.execute(delete(SignInCodeRow).where(SignInCodeRow.member_id == member.id))
        row = SignInCodeRow(
            household_id=member.household_id,
            member_id=member.id,
            code_hash=code_hash,
            created_by=created_by.id,
            created_at=now,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def usable(self, household_id: int, code_hash: str, now: datetime) -> StoredCode | None:
        """The unused, unexpired code with this hash, and its member. Reads only."""
        found = (
            await self._session.execute(
                select(SignInCodeRow, MemberRow)
                .join(MemberRow, MemberRow.id == SignInCodeRow.member_id)
                .where(
                    SignInCodeRow.household_id == household_id,
                    SignInCodeRow.code_hash == code_hash,
                    SignInCodeRow.used_at.is_(None),
                    SignInCodeRow.expires_at > now,
                )
            )
        ).first()
        if found is None:
            return None
        code, member = found
        return StoredCode(id=code.id, code_hash=code.code_hash, member=to_member(member))

    async def use(self, household_id: int, code_hash: str, now: datetime) -> tuple[int, int] | None:
        """Mark the code used, in one conditional UPDATE, so of two requests racing with one code
        only one changes the row. Returns the code's row ID and member ID, or None if the code
        doesn't exist, was used, or expired."""
        result = await self._session.execute(
            update(SignInCodeRow)
            .where(
                SignInCodeRow.household_id == household_id,
                SignInCodeRow.code_hash == code_hash,
                SignInCodeRow.used_at.is_(None),
                SignInCodeRow.expires_at > now,
            )
            .values(used_at=now)
            .returning(SignInCodeRow.id, SignInCodeRow.member_id)
        )
        found = result.first()
        return None if found is None else (int(found[0]), int(found[1]))

    async def delete_spent(self, now: datetime) -> int:
        """Delete every code that was used or has expired. Returns how many."""
        result = await self._session.execute(
            delete(SignInCodeRow).where(or_(SignInCodeRow.used_at.is_not(None), SignInCodeRow.expires_at <= now))
        )
        return int(getattr(result, "rowcount", 0) or 0)
