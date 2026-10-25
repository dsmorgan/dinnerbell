# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure domain rules: members, sign-in decisions, sessions, backup retention."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from dinnerbell.domain import sessions
from dinnerbell.domain.backups import backups_to_delete, backups_to_keep
from dinnerbell.domain.identity import Allow, IdentityClaims, Refusal, Refuse, decide_sign_in
from dinnerbell.domain.members import (
    _EMAIL_PATTERN,
    MAX_EMAIL_LENGTH,
    Capabilities,
    InvalidMemberInput,
    Member,
    MemberRuleViolation,
    MemberStatus,
    Role,
    check_deactivation,
    check_email_change,
    check_role_change,
    default_display_name,
    normalize_display_name,
    normalize_email,
)


def make_member(member_id: int, role: Role = Role.MEMBER, *, active: bool = True, subject: str | None = None) -> Member:
    return Member(
        id=member_id,
        household_id=1,
        display_name=f"M{member_id}",
        email=f"m{member_id}@example.com",
        role=role,
        capabilities=Capabilities(),
        active=active,
        oidc_subject=subject,
    )


ADMIN = make_member(1, Role.ADMIN, subject="s1")
OTHER_ADMIN = make_member(2, Role.ADMIN, subject="s2")
MEMBER = make_member(3, subject="s3")


class TestMembers:
    def test_normalize_email(self) -> None:
        assert normalize_email("  Sam@Example.COM ") == "sam@example.com"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "sam",
            "sam@",
            "@example.com",
            "sam@example",
            "a b@example.com",
            "sam@exa\x00mple.com",
            "x" * 250 + "@example.com",
            "sam@.example.com",
            "sam@example..com",
            "sam@example.com.",
        ],
    )
    def test_invalid_emails(self, value: str) -> None:
        with pytest.raises(InvalidMemberInput):
            normalize_email(value)

    def test_valid_emails(self) -> None:
        for value in ("sam@example.com", "sam.smith+food@mail.example.co.uk", "x" * 242 + "@example.com"):
            assert normalize_email(value) == value

    def test_email_pattern_takes_linear_time(self) -> None:
        """CodeQL py/polynomial-redos: the old pattern took quadratic time on many "!." pairs
        followed by a character that can't match. Addresses are capped at 254 characters, so
        check the cap, then run the pattern itself on 200,000 characters. The old pattern needs
        about a minute of CPU for that; the new one takes milliseconds, far under the ceiling."""
        attack = "!@!." + "!." * 124 + "!@"
        assert len(attack) == MAX_EMAIL_LENGTH
        with pytest.raises(InvalidMemberInput):
            normalize_email(attack)
        started = time.process_time()
        assert _EMAIL_PATTERN.match("!@!." + "!." * 100_000 + "@") is None
        assert time.process_time() - started < 2

    def test_display_name(self) -> None:
        assert normalize_display_name("  Sam   Smith ") == "Sam Smith"
        for bad in ("", "   ", "x" * 81, "Sam‎"):
            with pytest.raises(InvalidMemberInput):
                normalize_display_name(bad)

    def test_default_display_name(self) -> None:
        assert default_display_name("dsmorgan25@example.com") == "dsmorgan25"

    def test_status(self) -> None:
        assert make_member(9).status is MemberStatus.INVITED
        assert MEMBER.status is MemberStatus.ACTIVE
        assert make_member(9, active=False, subject="x").status is MemberStatus.DEACTIVATED

    def test_cannot_deactivate_self(self) -> None:
        with pytest.raises(MemberRuleViolation, match="yourself"):
            check_deactivation(ADMIN, ADMIN, [ADMIN, OTHER_ADMIN])

    def test_cannot_deactivate_last_active_admin(self) -> None:
        inactive_admin = make_member(4, Role.ADMIN, active=False)
        with pytest.raises(MemberRuleViolation, match="last active admin"):
            check_deactivation(MEMBER, ADMIN, [ADMIN, MEMBER, inactive_admin])

    def test_can_deactivate_admin_when_another_remains(self) -> None:
        check_deactivation(ADMIN, OTHER_ADMIN, [ADMIN, OTHER_ADMIN, MEMBER])
        check_deactivation(ADMIN, MEMBER, [ADMIN, MEMBER])

    def test_role_change_rules(self) -> None:
        check_role_change(ADMIN, MEMBER, Role.ADMIN, [ADMIN, MEMBER])
        check_role_change(ADMIN, OTHER_ADMIN, Role.MEMBER, [ADMIN, OTHER_ADMIN])
        check_role_change(ADMIN, ADMIN, Role.ADMIN, [ADMIN])  # no change is always fine
        with pytest.raises(MemberRuleViolation, match="own role"):
            check_role_change(ADMIN, ADMIN, Role.MEMBER, [ADMIN, OTHER_ADMIN])
        with pytest.raises(MemberRuleViolation, match="last active admin"):
            check_role_change(MEMBER, ADMIN, Role.MEMBER, [ADMIN, MEMBER])

    def test_email_change_locked_after_link(self) -> None:
        check_email_change(make_member(9), "new@example.com")
        check_email_change(MEMBER, MEMBER.email)
        with pytest.raises(MemberRuleViolation):
            check_email_change(MEMBER, "new@example.com")


def claims(**overrides: object) -> IdentityClaims:
    values: dict[str, object] = {"subject": "sub", "email": "m3@example.com", "email_verified": True}
    values.update(overrides)
    return IdentityClaims(**values)  # type: ignore[arg-type]


INVITED = make_member(5)


class TestSignInDecision:
    def test_first_sign_in_links(self) -> None:
        decision = decide_sign_in(claims(), by_subject=None, by_email=INVITED, required_team_id=None)
        assert decision == Allow(INVITED, link_subject=True)

    def test_subject_match_wins(self) -> None:
        decision = decide_sign_in(
            claims(email=None, email_verified=False), by_subject=MEMBER, by_email=None, required_team_id=None
        )
        assert decision == Allow(MEMBER, link_subject=False)

    @pytest.mark.parametrize(
        ("kwargs", "by_subject", "by_email", "team", "reason"),
        [
            ({}, None, None, None, Refusal.UNKNOWN_EMAIL),
            ({"email_verified": False}, None, INVITED, None, Refusal.EMAIL_NOT_VERIFIED),
            ({"email": None}, None, None, None, Refusal.EMAIL_NOT_VERIFIED),
            ({}, None, replace(INVITED, active=False), None, Refusal.INACTIVE),
            ({}, replace(MEMBER, active=False), None, None, Refusal.INACTIVE),
            ({}, None, MEMBER, None, Refusal.EMAIL_LINKED_ELSEWHERE),
            ({"slack_team_id": "T2"}, MEMBER, None, "T1", Refusal.WRONG_TEAM),
            ({}, None, INVITED, "T1", Refusal.WRONG_TEAM),
        ],
    )
    def test_refusals(
        self,
        kwargs: dict[str, object],
        by_subject: Member | None,
        by_email: Member | None,
        team: str | None,
        reason: Refusal,
    ) -> None:
        decision = decide_sign_in(claims(**kwargs), by_subject=by_subject, by_email=by_email, required_team_id=team)
        assert isinstance(decision, Refuse)
        assert decision.reason is reason

    def test_matching_team_is_allowed(self) -> None:
        decision = decide_sign_in(claims(slack_team_id="T1"), by_subject=None, by_email=INVITED, required_team_id="T1")
        assert isinstance(decision, Allow)


class TestSessions:
    NOW = datetime(2026, 1, 1, tzinfo=UTC)

    def test_expiry(self) -> None:
        expires = sessions.new_expiry(self.NOW)
        assert expires - self.NOW == timedelta(days=90)
        assert not sessions.is_expired(expires, expires - timedelta(seconds=1))
        assert sessions.is_expired(expires, expires)

    def test_renewal_interval(self) -> None:
        assert not sessions.needs_renewal(self.NOW, self.NOW + timedelta(minutes=59))
        assert sessions.needs_renewal(self.NOW, self.NOW + timedelta(hours=1))


class TestBackupRetention:
    def test_keeps_everything_when_few(self) -> None:
        days = [date(2026, 10, 1) + timedelta(days=i) for i in range(10)]
        assert backups_to_keep(days) == set(days)
        assert backups_to_delete(days) == set()

    def test_daily_and_weekly(self) -> None:
        # 120 consecutive nightly backups ending Thursday 2026-10-22.
        end = date(2026, 10, 22)
        days = [end - timedelta(days=i) for i in range(120)]
        keep = backups_to_keep(days)
        daily = {end - timedelta(days=i) for i in range(14)}
        assert daily <= keep
        weekly = keep - daily
        # The newest backup of each of the 8 newest ISO weeks. The 14 dailies (Oct 9-22) already
        # include the newest backup of the 3 newest weeks, so 5 Sundays are added.
        assert all(d.isoweekday() == 7 for d in weekly)
        weeks = {(d.isocalendar().year, d.isocalendar().week) for d in keep}
        assert len(weeks) == 8
        assert len(keep) == 14 + 5
        assert min(keep) == date(2026, 9, 6)
        assert backups_to_delete(days) == set(days) - keep

    def test_gaps_and_duplicates(self) -> None:
        days = [date(2026, 1, 5), date(2026, 1, 5), date(2025, 6, 1), date(2024, 1, 1)]
        assert backups_to_keep(days, daily=1, weekly=2) == {date(2026, 1, 5), date(2025, 6, 1)}

    def test_retention_is_stable(self) -> None:
        end = date(2026, 10, 22)
        days = [end - timedelta(days=i) for i in range(200)]
        kept = backups_to_keep(days)
        assert backups_to_keep(kept) == kept  # pruning twice changes nothing
