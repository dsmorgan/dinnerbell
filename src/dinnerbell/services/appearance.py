# SPDX-License-Identifier: AGPL-3.0-or-later
"""Light, dark, or auto (#231): each person's choice of how the web pages look.

Members choose on Preferences, and guests on their dietary-needs page, beside fun mode. Like fun
mode (``services.fun``), a person's own choice isn't audited, since it changes nothing anyone
else sees, and ``set_member_color_mode`` is an admin's choice for another member (#181), which
is.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.members import ColorMode, Member
from dinnerbell.services.common import audit
from dinnerbell.services.errors import Forbidden
from dinnerbell.services.preferences import member_for_admin


async def set_my_color_mode(db: AsyncSession, actor: Member, mode: ColorMode, *, now: datetime) -> Member:
    """Set the actor's color mode. A member or a guest, as long as they're active."""
    if not actor.active:
        raise Forbidden("Sign in again.")
    updated = await MemberRepository(db).update(actor.household_id, actor.id, now, color_mode=mode)
    await db.commit()
    return updated


async def set_member_color_mode(
    db: AsyncSession, actor: Member, member_id: int, mode: ColorMode, *, now: datetime
) -> Member:
    """An admin sets another member's color mode (#181). Audited as ``member.color_mode_set``,
    unless it was already that way."""
    member = await member_for_admin(db, actor, member_id)
    if member.color_mode is mode:
        return member
    updated = await MemberRepository(db).update(actor.household_id, member.id, now, color_mode=mode)
    payload = {"from": member.color_mode.value, "to": mode.value}
    await audit(db, actor.household_id, actor, "member.color_mode_set", f"member:{member.id}", payload, now)
    await db.commit()
    return updated
