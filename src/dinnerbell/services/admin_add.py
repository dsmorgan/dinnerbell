# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell admin add EMAIL``: the way back in when no admin can sign in (#360).

Such as when ``DINNERBELL_ADMIN_EMAIL`` was mistyped at install, or the only admin lost their
Slack account. Whoever runs it has a shell on the host, which owns the database anyway, so it
adds no risk.

It makes the member with that email an active admin that their next sign-in claims: invited if
there's none, promoted, reactivated, and with its own sign-in and Slack links cleared, so a new
account with that email signs in and links again. A guest isn't a member, so it never becomes
the admin. For each guest with that Slack email:

- One that signed in on the web is folded into the admin at their next Sign in with Slack
  (``domain.identity``), unless it's a Slack guest account, which is never linked on email
  alone, or removed. Then its sign-in link is cleared instead, so the email wins.
- One that holds a Slack account and won't be folded keeps it: the output says how to move it.

Audited as ``member.admin_added``, with no actor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.engine import write_transaction
from dinnerbell.db.repositories import MemberRepository, SessionRepository
from dinnerbell.domain.members import Capabilities, Member, Role, default_display_name, normalize_email
from dinnerbell.services.common import audit
from dinnerbell.services.household import current_household

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GuestNote:
    """A guest whose Slack email is the admin's."""

    guest_id: int
    folds: bool = False
    """It signed in on the web, so its next Sign in with Slack folds it into the admin."""
    unlinked: str | None = None
    """Why its sign-in link was cleared: it's a Slack guest account, or removed."""
    holds_slack: bool = False
    """It keeps a Slack account that won't move to the admin on its own."""

    def lines(self, who: str) -> list[str]:
        guest, lines = f"guest {self.guest_id}", []
        if self.folds:
            lines.append(
                f"{guest.capitalize()} has this Slack email and signed in on the web: "
                f"their next Sign in with Slack folds it into {who}."
            )
        if self.unlinked:
            lines.append(f"Cleared the sign-in link of {guest} ({self.unlinked}), so it doesn't stop the sign-in.")
        if self.holds_slack:
            lines.append(
                f"{guest.capitalize()} holds this Slack account. Once you're in, clear its Slack link on its "
                "guest row, then set your Slack member ID on your row."
            )
        return lines


@dataclass(frozen=True, slots=True)
class AdminAdded:
    member_id: int
    invited: bool = False
    promoted: bool = False
    reactivated: bool = False
    unlinked: bool = False
    """Its sign-in and Slack links were cleared, and its sessions ended."""
    guests: tuple[GuestNote, ...] = ()

    @property
    def guests_unlinked(self) -> list[int]:
        return [g.guest_id for g in self.guests if g.unlinked]

    @property
    def changed(self) -> bool:
        return self.invited or self.promoted or self.reactivated or self.unlinked or bool(self.guests_unlinked)

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
        for guest in self.guests:
            lines += guest.lines(who)
        if not self.changed:
            lines.append(
                f"{who.capitalize()} is already an active admin that the next sign-in claims. Nothing changed."
            )
        lines.append("Sign in with this email to finish.")
        return lines

    def payload(self) -> dict[str, object]:
        return {
            "invited": self.invited,
            "promoted": self.promoted,
            "reactivated": self.reactivated,
            "unlinked": self.unlinked,
            "guests_unlinked": self.guests_unlinked,
        }


async def add_admin(db: AsyncSession, email: str, *, now: datetime) -> AdminAdded:
    """Make the member with ``email`` an active admin that their next sign-in claims. Raises
    ``InvalidMemberInput`` for a malformed email and ``household.NoHousehold`` before the first
    start. Changes nothing, and writes no audit row, when there's nothing to change."""
    clean = normalize_email(email)
    household = await current_household(db)
    async with write_transaction(db):
        members = MemberRepository(db)
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
        notes = []
        for guest in await members.guests_by_slack_email(household.id, clean):
            note = await _guest_note(db, guest, now)
            if note is not None:
                notes.append(note)
        result = replace(result, guests=tuple(notes))
        if not result.changed:
            return result
        await audit(db, household.id, None, "member.admin_added", f"member:{result.member_id}", result.payload(), now)
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


async def _guest_note(db: AsyncSession, guest: Member, now: datetime) -> GuestNote | None:
    """What happens to ``guest``, whose Slack email is the admin's, at the next sign-in. Only a
    guest that signed in on the web folds (``domain.identity``); one that can't, a Slack guest
    account or a removed guest, has its sign-in link cleared so the email wins."""
    signed_in = guest.oidc_subject is not None
    folds = signed_in and guest.active and not guest.slack_guest_account
    unlinked = None
    if signed_in and not folds:
        unlinked = "a Slack guest account" if guest.active else "removed"
        await MemberRepository(db).update(guest.household_id, guest.id, now, oidc_subject=None)
        await SessionRepository(db).delete_for_member(guest.id)
    holds_slack = guest.slack_user_id is not None and not folds
    if not (folds or unlinked or holds_slack):
        return None  # such as a guest folded into a member already
    return GuestNote(guest.id, folds=folds, unlinked=unlinked, holds_slack=holds_slack)
