# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin use cases for members. Every change writes an audit row (SEC-9)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import MemberRepository, SessionRepository
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
    normalize_slack_user_id,
)
from dinnerbell.services.common import audit, require_admin
from dinnerbell.services.errors import NotFound

MANAGE_MEMBERS = "Only admins can manage members."


@dataclass(frozen=True, slots=True)
class MemberInput:
    """Raw admin form input; validated by ``_clean``."""

    display_name: str
    email: str
    role: Role
    capabilities: Capabilities


def _clean(data: MemberInput) -> MemberInput:
    return MemberInput(
        display_name=normalize_display_name(data.display_name),
        email=normalize_email(data.email),
        role=data.role,
        capabilities=data.capabilities,
    )


async def list_members(db: AsyncSession, actor: Member) -> list[Member]:
    require_admin(actor, MANAGE_MEMBERS)
    return await MemberRepository(db).list(actor.household_id)


async def get_member(db: AsyncSession, actor: Member, member_id: int) -> Member:
    require_admin(actor, MANAGE_MEMBERS)
    member = await MemberRepository(db).get(actor.household_id, member_id)
    if member is None:
        raise NotFound("No such member.")
    return member


async def invite_member(db: AsyncSession, actor: Member, data: MemberInput, *, now: datetime) -> Member:
    require_admin(actor, MANAGE_MEMBERS)
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
        await audit(db, actor.household_id, actor, "member.invited", f"member:{member.id}", _describe(member), now)
        await db.commit()
    return member


async def update_member(db: AsyncSession, actor: Member, member_id: int, data: MemberInput, *, now: datetime) -> Member:
    require_admin(actor, MANAGE_MEMBERS)
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
        await _ensure_an_admin_remains(db, actor.household_id)
        before = _describe(target)
        changes = {key: value for key, value in _describe(updated).items() if before[key] != value}
        await audit(db, actor.household_id, actor, "member.updated", f"member:{updated.id}", changes, now)
        await db.commit()
    return updated


async def set_member_active(db: AsyncSession, actor: Member, member_id: int, active: bool, *, now: datetime) -> Member:
    require_admin(actor, MANAGE_MEMBERS)
    members = MemberRepository(db)
    target = await get_member(db, actor, member_id)
    if target.active == active:
        return target
    if not active:
        check_deactivation(actor, target, await members.list(actor.household_id))
    updated = await members.update(actor.household_id, member_id, now, active=active)
    await _ensure_an_admin_remains(db, actor.household_id)
    if not active:
        await SessionRepository(db).delete_for_member(target.id)
    action = "member.reactivated" if active else "member.deactivated"
    await audit(db, actor.household_id, actor, action, f"member:{updated.id}", {}, now)
    await db.commit()
    return updated


async def set_slack_user(
    db: AsyncSession, actor: Member, member_id: int, slack_user_id: str, *, now: datetime
) -> Member:
    """FR-42: the admin links a member to a Slack user ID, or unlinks them with a blank value.
    Audited. Dinnerbell links unlinked members by email every hour, so an unlinked member whose
    email matches a Slack account is linked again."""
    require_admin(actor, MANAGE_MEMBERS)
    target = await get_member(db, actor, member_id)
    value = normalize_slack_user_id(slack_user_id)
    if value == target.slack_user_id:
        return target
    members = MemberRepository(db)
    holder = await members.by_slack_user_id(actor.household_id, value) if value else None
    if holder is not None:
        raise MemberRuleViolation(f"{holder.display_name} is already linked to that Slack user.")
    try:
        updated = await members.update(actor.household_id, member_id, now, slack_user_id=value)
        action = "member.slack_user_set" if value else "member.slack_user_cleared"
        payload = {"from": target.slack_user_id, "to": value}
        await audit(db, actor.household_id, actor, action, f"member:{member_id}", payload, now)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise MemberRuleViolation("Another member is already linked to that Slack user.") from None
    return updated


async def _ensure_an_admin_remains(db: AsyncSession, household_id: int) -> None:
    """Re-check inside the write transaction, after the change.

    The domain checks run on a read taken before the write. Two admins demoting or deactivating
    each other at the same moment would both pass them; this count sees the other's committed
    change, because the write transaction starts at the UPDATE.
    """
    if await MemberRepository(db).count_active_admins(household_id) == 0:
        await db.rollback()
        raise MemberRuleViolation("You can't remove the last active admin.")


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


@asynccontextmanager
async def _unique_email(db: AsyncSession) -> AsyncIterator[None]:
    """Turn a lost race on the unique email constraint into a friendly rule violation."""
    try:
        yield
    except IntegrityError:
        await db.rollback()
        raise MemberRuleViolation("A member with that email already exists.") from None
