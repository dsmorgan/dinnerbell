# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell admin add EMAIL``: the way back in when no admin can sign in (#360).

Such as when ``DINNERBELL_ADMIN_EMAIL`` was mistyped at install, or the only admin lost their
Slack account. Whoever runs it has a shell on the host, which owns the database anyway, so it
adds no risk.

It makes the member with that email an active admin that their next sign-in claims: invited if
there's none, promoted, reactivated, and with its own sign-in and Slack links cleared, so a new
account with that email signs in and links again. A guest isn't a member, so it never becomes
the admin: a guest who joined from the channel with that Slack email is folded into the admin at
their next Sign in with Slack (``domain.identity``), except a Slack guest account, which is never
linked on email alone; that guest's sign-in link is cleared instead, so the email wins. Audited
as ``member.admin_added``, with no actor.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.engine import write_transaction
from dinnerbell.db.repositories import MemberRepository, SessionRepository
from dinnerbell.domain.members import Capabilities, Member, Role, default_display_name, normalize_email
from dinnerbell.services.common import audit
from dinnerbell.services.household import current_household

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdminAdded:
    member_id: int
    invited: bool = False
    promoted: bool = False
    reactivated: bool = False
    unlinked: bool = False
    """Its sign-in and Slack links were cleared, and its sessions ended."""
    guest_id: int | None = None
    """An active guest who joined with this email as their Slack email."""
    guest_unlinked: bool = False
    """That guest uses a Slack guest account, so its sign-in link was cleared."""

    @property
    def changed(self) -> bool:
        return self.invited or self.promoted or self.reactivated or self.unlinked or self.guest_unlinked

    def lines(self) -> list[str]:
        """What it did, by member ID: never a name or an email."""
        who = f"member {self.member_id}"
        lines = []
        if self.invited:
            lines.append(f"Invited {who} as an admin.")
        if self.reactivated:
            lines.append(f"Reactivated {who}.")
        if self.promoted:
            lines.append(f"Made {who} an admin.")
        if self.unlinked:
            lines.append(f"Cleared the sign-in and Slack links of {who}, and signed it out everywhere.")
        if self.guest_id is not None and self.guest_unlinked:
            lines.append(
                f"Cleared the sign-in link of guest {self.guest_id}, who uses a Slack guest account. "
                f"Once you're in, remove that guest or clear its Slack link on Admin > Members."
            )
        elif self.guest_id is not None:
            lines.append(f"Guest {self.guest_id} has this Slack email: signing in with Slack folds it into {who}.")
        if not self.changed:
            lines.append(
                f"{who.capitalize()} is already an active admin that the next sign-in claims. Nothing changed."
            )
        lines.append("Sign in with this email to finish.")
        return lines


async def add_admin(db: AsyncSession, email: str, *, now: datetime) -> AdminAdded:
    """Make the member with ``email`` an active admin that their next sign-in claims. Raises
    ``InvalidMemberInput`` for a malformed email and ``household.NoHousehold`` before the first
    start. Changes nothing, and writes no audit row, when there's nothing to change."""
    clean = normalize_email(email)
    household = await current_household(db)
    async with write_transaction(db):
        members, sessions = MemberRepository(db), SessionRepository(db)
        found = await members.by_email(household.id, clean)
        if found is None:
            member = await members.add(
                household_id=household.id,
                display_name=default_display_name(clean),
                email=clean,
                role=Role.ADMIN,
                capabilities=Capabilities(),
                now=now,
            )
            result = AdminAdded(member.id, invited=True)
        else:
            result = await _claimable(db, found, now)
        guest = await members.guest_by_slack_email(household.id, clean)
        if guest is not None:
            unlink = guest.slack_guest_account and guest.oidc_subject is not None
            if unlink:
                await members.update(household.id, guest.id, now, oidc_subject=None)
                await sessions.delete_for_member(guest.id)
            result = replace(result, guest_id=guest.id, guest_unlinked=unlink)
        if not result.changed:
            return result
        payload = {k: v for k, v in asdict(result).items() if k != "member_id"}
        await audit(db, household.id, None, "member.admin_added", f"member:{result.member_id}", payload, now)
        await db.commit()
    log.info("admin added from the command line", extra={"member_id": result.member_id})
    return result


async def _claimable(db: AsyncSession, member: Member, now: datetime) -> AdminAdded:
    """``member``, an existing member with the email, as an active admin with no links."""
    changes: dict[str, object] = {}
    if member.role is not Role.ADMIN:
        changes["role"] = Role.ADMIN
    if not member.active:
        changes["active"] = True
    linked = member.oidc_subject is not None or member.slack_user_id is not None or member.slack_link_locked
    if linked:
        # Whoever signed in with the old account isn't let back in by it, and a new Slack
        # account with this email links at the next sign-in, or by the hourly match.
        changes |= {"oidc_subject": None, "slack_user_id": None, "slack_link_locked": False}
        await SessionRepository(db).delete_for_member(member.id)
    if changes:
        await MemberRepository(db).update(member.household_id, member.id, now, **changes)
    return AdminAdded(member.id, promoted="role" in changes, reactivated="active" in changes, unlinked=linked)
