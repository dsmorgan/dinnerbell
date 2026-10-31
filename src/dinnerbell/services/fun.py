# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fun mode (#153): each person's own switch for Dinnerbone's Den.

Members turn it on or off on Preferences, and guests on their dietary-needs page. It acts on
``actor`` only. It isn't audited, like the other settings people change about themselves on
Preferences: it changes nothing anyone else sees.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.members import Member
from dinnerbell.services.errors import Forbidden


async def set_my_fun_mode(db: AsyncSession, actor: Member, on: bool, *, now: datetime) -> Member:
    """Turn the actor's fun mode on or off. A member or a guest, as long as they're active."""
    if not actor.active:
        raise Forbidden("Sign in again.")
    updated = await MemberRepository(db).update(actor.household_id, actor.id, now, fun_mode=on)
    await db.commit()
    return updated
