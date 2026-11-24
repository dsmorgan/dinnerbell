# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who may sign in. A pure decision over verified identity claims and the member list."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum

from dinnerbell.domain.members import Member


@dataclass(frozen=True, slots=True)
class IdentityClaims:
    """Claims from an ID token whose signature, issuer, audience, and nonce were already verified."""

    subject: str
    email: str | None
    email_verified: bool
    slack_team_id: str | None = None
    slack_user_id: str | None = None


class Refusal(StrEnum):
    UNKNOWN_EMAIL = "unknown_email"
    EMAIL_NOT_VERIFIED = "email_not_verified"
    INACTIVE = "inactive"
    WRONG_TEAM = "wrong_team"
    EMAIL_LINKED_ELSEWHERE = "email_linked_elsewhere"


@dataclass(frozen=True, slots=True)
class Allow:
    member: Member
    link_subject: bool
    fold_guest: Member | None = None
    """A guest linked to this identity, to fold into ``member`` before linking it (#360)."""


@dataclass(frozen=True, slots=True)
class Refuse:
    reason: Refusal
    member_id: int | None = None


SignInDecision = Allow | Refuse


def decide_sign_in(
    claims: IdentityClaims,
    *,
    by_subject: Member | None,
    by_email: Member | None,
    required_team_id: str | None,
    by_slack_user: Member | None = None,
    slack_sign_in: bool = False,
) -> SignInDecision:
    """Decide whether an identity may sign in.

    ``by_subject`` is the member already linked to ``claims.subject``. ``by_email`` is the
    member whose email matches ``claims.email`` case-insensitively; the caller looks it up only
    by the normalized email, and needs it when ``by_subject`` is None or a guest. An ``Allow``
    with ``fold_guest`` asks the caller to fold that guest into the member, then link it.
    """
    if required_team_id is not None and not _same(claims.slack_team_id, required_team_id):
        return Refuse(Refusal.WRONG_TEAM, by_subject.id if by_subject else None)
    if by_subject is not None:
        if not by_subject.active:
            return Refuse(Refusal.INACTIVE, by_subject.id)
        invited = by_email if slack_sign_in and required_team_id is not None else None
        if invited is not None and _invitation_wins(claims, by_subject, invited):
            return Allow(invited, link_subject=True, fold_guest=by_subject)
        return Allow(by_subject, link_subject=False)
    # An invited member who hasn't signed in yet matches by verified email first, even when a
    # guest record holds their Slack account (#38: they joined from the channel before the
    # admin added them). The admin links or retires that guest afterward.
    member_by_email = by_email is not None and by_email.is_member and not by_email.is_linked
    if member_by_email and claims.email_verified and claims.email and by_email is not None:
        return Allow(by_email, link_subject=True)
    # #38: a guest, such as one who joined from the channel, signs in with the Slack account
    # they're linked to. Only with Slack as the provider (``slack_sign_in``: the configured issuer
    # is Slack's), whose workspace was checked above, and only for a guest: members match by email.
    if slack_sign_in and required_team_id is not None and by_slack_user is not None and by_slack_user.is_guest:
        if by_slack_user.is_linked:
            return Refuse(Refusal.EMAIL_LINKED_ELSEWHERE, by_slack_user.id)
        if not by_slack_user.active:
            return Refuse(Refusal.INACTIVE, by_slack_user.id)
        return Allow(by_slack_user, link_subject=True)
    if not claims.email_verified or not claims.email:
        return Refuse(Refusal.EMAIL_NOT_VERIFIED)
    if by_email is None or by_email.is_guest:  # guests sign in only by their Slack user (#38)
        return Refuse(Refusal.UNKNOWN_EMAIL)
    if by_email.is_linked:
        return Refuse(Refusal.EMAIL_LINKED_ELSEWHERE, by_email.id)
    if not by_email.active:
        return Refuse(Refusal.INACTIVE, by_email.id)
    return Allow(by_email, link_subject=True)


def _invitation_wins(claims: IdentityClaims, guest: Member, invited: Member) -> bool:
    """#360: someone who joined from the channel as a guest, and was invited as a member since,
    such as a first admin whose ``DINNERBELL_ADMIN_EMAIL`` was mistyped at first, signs in as the
    member. Their Slack email, verified in the household's workspace, proves it, as at a first
    sign-in. Only for an invitation an automatic Slack link may claim (as the hourly match:
    no Slack user yet, none set or cleared by hand), and never for a Slack guest account, which
    joining and the hourly match never link on email alone. An admin's invitation qualifies:
    the proof is the email, not a display name, as it is for **Link to member**."""
    return (
        guest.is_guest
        and not guest.slack_guest_account
        and claims.email_verified
        and bool(claims.email)
        and invited.is_member
        and not invited.is_linked
        and invited.slack_user_id is None
        and not invited.slack_link_locked
    )


def _same(claimed: str | None, required: str) -> bool:
    return claimed is not None and hmac.compare_digest(claimed.encode(), required.encode())
