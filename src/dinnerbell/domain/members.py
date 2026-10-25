# SPDX-License-Identifier: AGPL-3.0-or-later
"""Members, guests, roles, and the rules that protect the household from locking itself out.

A guest (#38) is a ``member`` row with the role ``guest``: someone who eats with the household
when a member invites them. Registered guests have a Slack user; unregistered ones don't, and
the member who invites them answers for them. Guests have no email unless an admin gives one
when promoting them. ``domain.guests`` says what a guest may do.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

MAX_EMAIL_LENGTH = 254
MAX_DISPLAY_NAME_LENGTH = 80
# Dots split the domain into labels that can't contain dots, so there's one way to match any
# address and the match takes linear time (CodeQL py/polynomial-redos).
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"
    GUEST = "guest"


class MemberStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class InvalidMemberInput(ValueError):
    """Input that can never be valid, such as a malformed email address."""


class MemberRuleViolation(Exception):
    """An action the rules forbid. The message is written for the person who tried it."""


@dataclass(frozen=True, slots=True)
class Capabilities:
    can_cook: bool = False
    can_pickup: bool = False
    can_order: bool = False


@dataclass(frozen=True, slots=True)
class Member:
    id: int
    household_id: int
    display_name: str
    email: str | None
    """None only for a guest, who signs in with Slack if at all."""
    role: Role
    capabilities: Capabilities
    active: bool
    oidc_subject: str | None = None
    slack_user_id: str | None = None
    slack_link_locked: bool = False
    """The admin set or cleared ``slack_user_id`` by hand; nothing automatic changes it."""
    mute_dm_reminders: bool = False
    feedback_reminder: bool = False
    dietary_needs: frozenset[int] = frozenset()
    """A guest's dietary tags: hard constraints whenever they're in or maybe, as a member's
    guests' needs are (spec 5.5)."""
    join_refused: bool = False
    """An admin removed this guest: they can't join from the channel again (#38)."""
    slack_email: str | None = None
    """A joined guest's Slack email (#38): never used to sign in."""
    slack_guest_account: bool = False
    """The guest joined with a Slack guest account, not as a full member of the workspace."""

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    @property
    def is_guest(self) -> bool:
        return self.role is Role.GUEST

    @property
    def is_member(self) -> bool:
        """An active member or admin of the household: not a guest, not deactivated."""
        return self.active and not self.is_guest

    @property
    def is_registered_guest(self) -> bool:
        """A guest with a Slack user, who taps their own buttons and gets their own messages."""
        return self.is_guest and self.slack_user_id is not None

    @property
    def is_linked(self) -> bool:
        return self.oidc_subject is not None

    @property
    def status(self) -> MemberStatus:
        if not self.active:
            return MemberStatus.DEACTIVATED
        return MemberStatus.ACTIVE if self.is_linked else MemberStatus.INVITED


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def normalize_email(value: str) -> str:
    """Return the canonical, lowercase form of an email address or raise InvalidMemberInput."""
    email = value.strip().lower()
    if not email or len(email) > MAX_EMAIL_LENGTH or _has_control_characters(email):
        raise InvalidMemberInput("Enter a valid email address.")
    if not _EMAIL_PATTERN.match(email):
        raise InvalidMemberInput("Enter a valid email address.")
    return email


def normalize_display_name(value: str) -> str:
    name = " ".join(value.split())
    if not name:
        raise InvalidMemberInput("Enter a display name.")
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise InvalidMemberInput(f"Display names are at most {MAX_DISPLAY_NAME_LENGTH} characters.")
    if _has_control_characters(name):
        raise InvalidMemberInput("Display names can't contain control characters.")
    return name


SLACK_USER_ID_PATTERN = re.compile(r"^[UW][A-Z0-9]{2,31}$")


def normalize_slack_user_id(value: str) -> str | None:
    """A Slack user ID such as U0123ABCD (W for Enterprise Grid), or None for a blank value."""
    text = value.strip().upper()
    if not text:
        return None
    if not SLACK_USER_ID_PATTERN.match(text):
        raise InvalidMemberInput(
            "Enter a Slack member ID: it starts with U and has only letters and digits. "
            "In Slack, open the person's profile, select the three dots, then Copy member ID."
        )
    return text


def default_display_name(email: str) -> str:
    """A starting display name for a bootstrapped admin: the email's local part."""
    local = email.split("@", 1)[0]
    return normalize_display_name(local[:MAX_DISPLAY_NAME_LENGTH] or "Admin")


def _other_active_admins(target: Member, members: list[Member]) -> int:
    return sum(1 for m in members if m.active and m.is_admin and m.id != target.id)


def check_deactivation(actor: Member, target: Member, members: list[Member]) -> None:
    """Raise MemberRuleViolation if ``actor`` may not deactivate ``target``."""
    if actor.id == target.id:
        raise MemberRuleViolation("You can't deactivate yourself. Ask another admin.")
    if target.is_admin and target.active and _other_active_admins(target, members) == 0:
        raise MemberRuleViolation("You can't deactivate the last active admin.")


def check_role_change(actor: Member, target: Member, new_role: Role, members: list[Member]) -> None:
    """Raise MemberRuleViolation if ``actor`` may not give ``target`` the role ``new_role``."""
    if new_role is target.role:
        return
    if actor.id == target.id:
        raise MemberRuleViolation("You can't change your own role. Ask another admin.")
    if target.is_admin and target.active and _other_active_admins(target, members) == 0:
        raise MemberRuleViolation("You can't remove the last active admin.")


def check_email_change(target: Member, new_email: str | None) -> None:
    if new_email != target.email and target.is_linked:
        raise MemberRuleViolation(
            "This member has already signed in, so their email can't change. "
            "Their sign-in is tied to their account, not their email."
        )
