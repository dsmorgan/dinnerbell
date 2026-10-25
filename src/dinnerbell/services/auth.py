# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in and server-side sessions.

The browser holds a random token. The database holds only its SHA-256 digest, so a copy of
the database cannot be used to impersonate anyone.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import AuditRepository, HouseholdRepository, MemberRepository, SessionRepository
from dinnerbell.domain import sessions as rules
from dinnerbell.domain.household import Household
from dinnerbell.domain.identity import Allow, IdentityClaims, Refuse, decide_sign_in
from dinnerbell.domain.members import InvalidMemberInput, Member, normalize_email

log = logging.getLogger(__name__)
TOKEN_BYTES = 32


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SignedIn:
    member: Member
    token: str


@dataclass(frozen=True, slots=True)
class ActiveSession:
    member: Member
    household: Household
    token_hash: str
    renewed: bool


async def sign_in(
    db: AsyncSession,
    claims: IdentityClaims,
    *,
    required_team_id: str | None,
    now: datetime,
    replaces_token: str | None = None,
) -> SignedIn | Refuse:
    """Apply the sign-in rules to verified claims and, if allowed, open a new session."""
    household = await HouseholdRepository(db).first()
    if household is None:
        raise RuntimeError("no household; bootstrap has not run")
    members = MemberRepository(db)
    by_subject = await members.by_subject(household.id, claims.subject)
    by_email = await _by_email(members, household.id, claims.email) if by_subject is None else None
    by_slack = None
    if by_subject is None and required_team_id is not None and claims.slack_user_id:
        by_slack = await members.by_slack_user_id(household.id, claims.slack_user_id)
    decision = decide_sign_in(
        claims, by_subject=by_subject, by_email=by_email, required_team_id=required_team_id, by_slack_user=by_slack
    )
    if isinstance(decision, Refuse):
        await _record_refusal(db, household.id, claims, decision, now)
        return decision
    member = await _link(db, decision, claims, now)
    if replaces_token:
        await SessionRepository(db).delete(hash_token(replaces_token))
    token = secrets.token_urlsafe(TOKEN_BYTES)
    await SessionRepository(db).add(
        token_hash=hash_token(token), member=member, now=now, expires_at=rules.new_expiry(now)
    )
    await db.commit()
    log.info("member signed in", extra={"member_id": member.id})
    return SignedIn(member=member, token=token)


async def _by_email(members: MemberRepository, household_id: int, email: str | None) -> Member | None:
    if not email:
        return None
    try:
        return await members.by_email(household_id, normalize_email(email))
    except InvalidMemberInput:
        return None


async def _link(db: AsyncSession, decision: Allow, claims: IdentityClaims, now: datetime) -> Member:
    member = decision.member
    changes: dict[str, str] = {}
    if decision.link_subject:
        changes["oidc_subject"] = claims.subject
    # A link the admin set or cleared by hand stays as the admin left it (FR-42).
    slack_id = claims.slack_user_id
    holder = None
    if slack_id and slack_id != member.slack_user_id and not member.slack_link_locked:
        holder = await MemberRepository(db).by_slack_user_id(member.household_id, slack_id)
        if holder is None:
            changes["slack_user_id"] = slack_id
    updated = member
    if changes:
        updated = await MemberRepository(db).update(member.household_id, member.id, now, **changes)
        await AuditRepository(db).record(
            household_id=member.household_id,
            actor_member_id=member.id,
            action="member.linked" if decision.link_subject else "member.slack_user_updated",
            target=f"member:{member.id}",
            payload=changes,
            now=now,
        )
    if holder is not None and slack_id:
        await _slack_conflict(db, member, slack_id, holder, now)
    return updated


async def _slack_conflict(db: AsyncSession, member: Member, slack_id: str, holder: Member, now: datetime) -> None:
    """Another member has this member's Slack user, most likely by an admin's mistake. Sign-in
    still works; the link stays where it is until the admin sorts it out. Audited once per
    clash, not on every sign-in."""
    log.warning("slack user already linked to another member", extra={"member_id": member.id, "holder_id": holder.id})
    audits, target = AuditRepository(db), f"member:{member.id}"
    conflict = {"slack_user_id": slack_id, "held_by": holder.id}
    if await audits.latest(member.household_id, target) != ("member.slack_user_conflict", conflict):
        await audits.record(
            household_id=member.household_id,
            actor_member_id=member.id,
            action="member.slack_user_conflict",
            target=target,
            payload=conflict,
            now=now,
        )


async def _record_refusal(
    db: AsyncSession, household_id: int, claims: IdentityClaims, refusal: Refuse, now: datetime
) -> None:
    await AuditRepository(db).record(
        household_id=household_id,
        actor_member_id=None,
        action="auth.sign_in_refused",
        target=f"member:{refusal.member_id}" if refusal.member_id else None,
        payload={
            "reason": refusal.reason.value,
            "subject": claims.subject,
            "email": claims.email,
            "email_verified": claims.email_verified,
            "slack_team_id": claims.slack_team_id,
        },
        now=now,
    )
    await db.commit()
    log.warning(
        "sign-in refused",
        extra={"reason": refusal.reason.value, "member_id": refusal.member_id},
    )


async def resolve_session(db: AsyncSession, token: str, *, now: datetime) -> ActiveSession | None:
    """Return the session for ``token`` if valid, renewing its sliding expiry when due."""
    sessions = SessionRepository(db)
    stored = await sessions.get(hash_token(token))
    if stored is None:
        return None
    if rules.is_expired(stored.expires_at, now) or not stored.member.active:
        await sessions.delete(stored.token_hash)
        await db.commit()
        return None
    renewed = rules.needs_renewal(stored.last_seen, now)
    if renewed:
        await sessions.touch(stored.token_hash, now=now, expires_at=rules.new_expiry(now))
        await db.commit()
    return ActiveSession(
        member=stored.member, household=stored.household, token_hash=stored.token_hash, renewed=renewed
    )


async def sign_out(db: AsyncSession, token: str) -> None:
    await SessionRepository(db).delete(hash_token(token))
    await db.commit()


async def purge_expired_sessions(db: AsyncSession, *, now: datetime) -> int:
    removed = await SessionRepository(db).delete_expired(now)
    await db.commit()
    return removed
