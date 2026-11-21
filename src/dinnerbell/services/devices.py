# SPDX-License-Identifier: AGPL-3.0-or-later
"""Devices (spec 6.15): a member's sessions on Preferences, signing one out, and an admin
signing a member out of every device.

A page names a session by its ``public_id``, never the token's hash. Each session's label
comes from its browser when it signed in (``domain.sessions.device_label``).

Like the rest of the web app, this exists only while Admin's **Web app** switch is on, so an
install that never turns it on looks exactly as it did. Signing out a device of your own isn't
audited, as **Sign out** isn't; an admin signing a member out everywhere is (SEC-9).
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import MemberRepository, SessionRepository
from dinnerbell.db.sign_in_codes import SignInCodeRepository
from dinnerbell.domain.members import Member
from dinnerbell.services.common import audit, require_admin, require_member
from dinnerbell.services.errors import NotFound
from dinnerbell.services.web_app import require_web_app

log = logging.getLogger(__name__)

ADMINS_ONLY = "Only the household admin can sign a member out of their devices."
GONE = "That device is already signed out."
MAX_PUBLIC_ID_LENGTH = 32


@dataclass(frozen=True, slots=True)
class Device:
    public_id: str
    label: str
    signed_in: datetime
    last_seen: datetime
    current: bool
    """The session this request came with: "This device"."""


async def my_devices(db: AsyncSession, actor: Member, current_session: str, *, now: datetime) -> list[Device]:
    """The actor's signed-in devices, the most recently seen first. ``current_session`` is the
    request's session hash, only compared, never returned."""
    require_member(actor)
    await require_web_app(db, actor.household_id)
    sessions = await SessionRepository(db).for_member(actor.household_id, actor.id, now)
    return [
        Device(
            public_id=s.public_id,
            label=s.device,
            signed_in=s.created_at,
            last_seen=s.last_seen,
            current=hmac.compare_digest(s.token_hash, current_session),
        )
        for s in sessions
    ]


async def sign_out_device(db: AsyncSession, actor: Member, public_id: str, current_session: str) -> bool:
    """End one of the actor's sessions. Returns whether it was the current one, so the page can
    clear the cookie. NotFound if the actor has no such session."""
    require_member(actor)
    await require_web_app(db, actor.household_id)
    if not public_id or len(public_id) > MAX_PUBLIC_ID_LENGTH:
        raise NotFound(GONE)
    token_hash = await SessionRepository(db).delete_device(actor.household_id, actor.id, public_id)
    if token_hash is None:
        raise NotFound(GONE)
    await db.commit()
    log.info("member signed out a device", extra={"member_id": actor.id})
    return hmac.compare_digest(token_hash, current_session)


async def sign_out_everywhere(db: AsyncSession, actor: Member, member_id: int, *, now: datetime) -> Member:
    """An admin signs a member out of every device, and deletes their unused sign-in code, in
    one transaction. Audited as ``member.signed_out_everywhere`` with how many sessions ended
    and codes went."""
    require_admin(actor, ADMINS_ONLY)
    await require_web_app(db, actor.household_id)
    member = await MemberRepository(db).get(actor.household_id, member_id)
    if member is None:
        raise NotFound("No such member.")
    ended = await SessionRepository(db).delete_for_member(member.id)
    # A lost phone and an unused code mustn't sign in again afterward.
    codes = await SignInCodeRepository(db).delete_for_member(member.id)
    payload = {"sessions": ended, "codes": codes}
    await audit(db, actor.household_id, actor, "member.signed_out_everywhere", f"member:{member.id}", payload, now)
    await db.commit()
    log.info("member signed out everywhere", extra={"member_id": member.id, "actor_id": actor.id, "sessions": ended})
    return member
