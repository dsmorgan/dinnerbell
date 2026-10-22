# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin use cases for members. Every change writes an audit row (SEC-9)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import AuditRepository, MemberRepository, SessionRepository
from dinnerbell.domain.members import (
    Capabilities,
    Member,
    MemberRuleViolation,
    Role,
    check_deactivation,
    check_email_change,
    check_role_change,
    normalize_display_name,
    normalize_email,
)
from dinnerbell.services.errors import Forbidden, NotFound


@dataclass(frozen=True, slots=True)
class MemberInput:
    """Raw admin form input; validated by ``_clean``."""

    display_name: str
    email: str
    role: Role
    capabilities: Capabilities


def _require_admin(actor: Member) -> None:
    if not (actor.active and actor.is_admin):
        raise Forbidden("Only admins can manage members.")


def _clean(data: MemberInput) -> MemberInput:
    return MemberInput(
        display_name=normalize_display_name(data.display_name),
        email=normalize_email(data.email),
        role=data.role,
        capabilities=data.capabilities,
    )


async def list_members(db: AsyncSession, actor: Member) -> list[Member]:
    _require_admin(actor)
    return await MemberRepository(db).list(actor.household_id)


async def get_member(db: AsyncSession, actor: Member, member_id: int) -> Member:
    _require_admin(actor)
    member = await MemberRepository(db).get(actor.household_id, member_id)
    if member is None:
        raise NotFound("No such member.")
    return member


async def invite_member(db: AsyncSession, actor: Member, data: MemberInput, *, now: datetime) -> Member:
    _require_admin(actor)
    clean = _clean(data)
    members = MemberRepository(db)
    if await members.by_email(actor.household_id, clean.email) is not None:
        raise MemberRuleViolation("A member with that email already exists.")
    async with _unique_email(db):
        member = await members.add(
            household_id=actor.household_id,
            display_name=clean.display_name,
            email=clean.email,
            role=clean.role,
            capabilities=clean.capabilities,
            now=now,
        )
        await _audit(db, actor, "member.invited", member, _describe(member), now)
        await db.commit()
    return member


async def update_member(db: AsyncSession, actor: Member, member_id: int, data: MemberInput, *, now: datetime) -> Member:
    _require_admin(actor)
    clean = _clean(data)
    members = MemberRepository(db)
    target = await get_member(db, actor, member_id)
    everyone = await members.list(actor.household_id)
    check_role_change(actor, target, clean.role, everyone)
    check_email_change(target, clean.email)
    if clean.email != target.email and await members.by_email(actor.household_id, clean.email) is not None:
        raise MemberRuleViolation("A member with that email already exists.")
    async with _unique_email(db):
        updated = await members.update(
            actor.household_id,
            member_id,
            now,
            display_name=clean.display_name,
            email=clean.email,
            role=clean.role,
            can_cook=clean.capabilities.can_cook,
            can_pickup=clean.capabilities.can_pickup,
            can_order=clean.capabilities.can_order,
        )
        before = _describe(target)
        changes = {key: value for key, value in _describe(updated).items() if before[key] != value}
        await _audit(db, actor, "member.updated", updated, changes, now)
        await db.commit()
    return updated


async def set_member_active(db: AsyncSession, actor: Member, member_id: int, active: bool, *, now: datetime) -> Member:
    _require_admin(actor)
    members = MemberRepository(db)
    target = await get_member(db, actor, member_id)
    if target.active == active:
        return target
    if not active:
        check_deactivation(actor, target, await members.list(actor.household_id))
        await SessionRepository(db).delete_for_member(target.id)
    updated = await members.update(actor.household_id, member_id, now, active=active)
    action = "member.reactivated" if active else "member.deactivated"
    await _audit(db, actor, action, updated, {}, now)
    await db.commit()
    return updated


def _describe(member: Member) -> dict[str, object]:
    """Audit payload fields. Emails stay in the database, never in logs (SEC-5)."""
    return {
        "display_name": member.display_name,
        "email": member.email,
        "role": member.role.value,
        "can_cook": member.capabilities.can_cook,
        "can_pickup": member.capabilities.can_pickup,
        "can_order": member.capabilities.can_order,
    }


async def _audit(
    db: AsyncSession, actor: Member, action: str, target: Member, payload: dict[str, object], now: datetime
) -> None:
    await AuditRepository(db).record(
        household_id=actor.household_id,
        actor_member_id=actor.id,
        action=action,
        target=f"member:{target.id}",
        payload=payload,
        now=now,
    )


@asynccontextmanager
async def _unique_email(db: AsyncSession) -> AsyncIterator[None]:
    """Turn a lost race on the unique email constraint into a friendly rule violation."""
    try:
        yield
    except IntegrityError:
        await db.rollback()
        raise MemberRuleViolation("A member with that email already exists.") from None
