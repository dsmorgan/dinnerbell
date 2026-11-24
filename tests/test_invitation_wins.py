# SPDX-License-Identifier: AGPL-3.0-or-later
"""#360: an invitation by email wins over a guest record the same person joined as. Signing in
with Slack, with the verified email of an invited member, signs them in as the member and folds
the guest into it, so a first admin whose DINNERBELL_ADMIN_EMAIL was mistyped isn't locked out."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey
from sqlalchemy import create_engine, text

from dinnerbell.app_factory import create_app
from dinnerbell.config import SLACK_ISSUER, Settings
from dinnerbell.db.models import AuditRow, MemberRow, SessionRow
from dinnerbell.services.guests import SlackPerson
from tests.conftest import BASE_URL, invite, no_push_service, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.weather import FakeOpenMeteo
from tests.test_auth_flow import rows
from tests.test_guests_web import SLACK_ADMIN, TEAM, FakePeople, use_people

RIGHT = "riley@example.com"
RILEY = {
    "sub": "sub-riley",
    "email": RIGHT,
    "https://slack.com/team_id": TEAM,
    "https://slack.com/user_id": "U01RILEY",
}
SLACK = {"slack_team_id": TEAM, "oidc_issuer": SLACK_ISSUER}


@pytest.fixture
def provider(signing_key: RSAKey, clock: FakeClock, settings: Settings) -> FakeProvider:
    return FakeProvider(key=signing_key, clock=clock, issuer=settings.oidc_issuer)


@contextmanager
def started(settings: Settings, clock: FakeClock, provider: FakeProvider) -> Iterator[TestClient]:
    """Start Dinnerbell on ``settings``, as ``serve`` does: bootstrap runs at start."""
    app = create_app(
        settings,
        clock=clock,
        oidc_transport=httpx.ASGITransport(app=provider.app),
        push_transport=httpx.MockTransport(no_push_service),
        weather_transport=httpx.MockTransport(FakeOpenMeteo()),
    )
    with TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False) as client:
        yield client


def by_id(settings: Settings) -> dict[int, MemberRow]:
    return {m.id: m for m in rows(settings, MemberRow)}


def sessions_of(settings: Settings, member_id: int) -> int:
    return sum(1 for s in rows(settings, SessionRow) if s.member_id == member_id)


def sql(settings: Settings, statement: str, **params: Any) -> None:
    engine = create_engine(settings.sync_database_url)
    with engine.begin() as c:
        c.execute(text(statement), params)
    engine.dispose()


@pytest.mark.parametrize("settings_overrides", [SLACK | {"admin_email": "rilye@example.com"}])
def test_a_first_admin_with_a_mistyped_email_gets_in_after_fixing_it(
    migrated: Settings, clock: FakeClock, provider: FakeProvider
) -> None:
    """The issue's steps: a typo invites the wrong email; Riley joins from the channel as a
    guest; the operator fixes the email and restarts; Riley's next sign-in lands on the admin."""
    people = FakePeople(SlackPerson("U01RILEY", "Riley", RIGHT))
    with started(migrated, clock, provider) as client:
        use_people(client, people)
        assert sign_in(client, provider, claims=RILEY).status_code == 303  # joined as a guest
        assert 'href="/admin/members"' not in page(client.get("/"))
    (guest,) = [m for m in rows(migrated, MemberRow) if m.role == "guest"]
    assert (guest.oidc_subject, guest.slack_user_id, guest.slack_email) == ("sub-riley", "U01RILEY", RIGHT)

    fixed = migrated.model_copy(update={"admin_email": RIGHT})
    with started(fixed, clock, provider) as client:
        admin = next(m for m in rows(migrated, MemberRow) if m.email == RIGHT)
        assert (admin.role, admin.oidc_subject) == ("admin", None)  # bootstrap invited the right email
        use_people(client, people)
        signed_in = sign_in(client, provider, claims=RILEY)
        assert signed_in.status_code == 303, page(signed_in)
        assert 'href="/admin/members"' in page(client.get("/"))
        assert (sessions_of(migrated, admin.id), sessions_of(migrated, guest.id)) == (1, 0)
        client.cookies.clear()
        assert sign_in(client, provider, claims=RILEY).status_code == 303  # and stays the admin
        assert sessions_of(migrated, admin.id) == 2
    members = by_id(migrated)
    folded, retired = members[admin.id], members[guest.id]
    assert (folded.oidc_subject, folded.slack_user_id, folded.slack_link_locked) == ("sub-riley", "U01RILEY", True)
    assert (retired.active, retired.oidc_subject, retired.slack_user_id) == (False, None, None)
    audit = {a.action: a for a in rows(migrated, AuditRow)}
    fold = audit["guest.linked_to_member"]
    assert (fold.actor_member_id, fold.target) == (admin.id, f"member:{admin.id}")
    assert fold.payload == {"guest_id": guest.id, "slack_user_id": "U01RILEY", "override": False, "via": "sign_in"}
    assert audit["member.linked"].actor_member_id == admin.id


NOVA = {"sub": "sub-nova", "email": "nova@example.com", "https://slack.com/team_id": TEAM}
NOVA_SLACK = NOVA | {"https://slack.com/user_id": "U09NOVA"}


@pytest.fixture
def joined(client: TestClient, provider: FakeProvider, migrated: Settings) -> tuple[int, int]:
    """Nova joins from the web before the admin invites her (Slack shares no email for her), and
    the admin invites her afterward. Returns the guest's and the member's IDs."""
    assert sign_in(client, provider, claims=SLACK_ADMIN).status_code == 303
    use_people(client, FakePeople(SlackPerson("U09NOVA", "Nova")))
    client.cookies.clear()
    assert sign_in(client, provider, claims=NOVA_SLACK).status_code == 303
    guest = next(m for m in rows(migrated, MemberRow) if m.slack_user_id == "U09NOVA")
    client.cookies.clear()
    assert sign_in(client, provider, claims=SLACK_ADMIN).status_code == 303
    assert invite(client, "nova@example.com", "Nova").status_code == 303
    member = next(m for m in rows(migrated, MemberRow) if m.email == "nova@example.com")
    client.cookies.clear()
    return guest.id, member.id


@pytest.mark.parametrize("settings_overrides", [SLACK])
def test_a_member_invited_after_joining_from_the_web_signs_in_as_the_member(
    client: TestClient, provider: FakeProvider, migrated: Settings, joined: tuple[int, int]
) -> None:
    guest_id, member_id = joined
    assert sign_in(client, provider, claims=NOVA_SLACK).status_code == 303
    members = by_id(migrated)
    assert (members[member_id].oidc_subject, members[member_id].slack_user_id) == ("sub-nova", "U09NOVA")
    assert (members[guest_id].active, members[guest_id].oidc_subject) == (False, None)
    assert "No invitations right now" not in page(client.get("/"))  # a member's Today, not a guest's


@pytest.mark.parametrize("settings_overrides", [SLACK])
@pytest.mark.parametrize(
    ("claims", "change"),
    [
        (NOVA_SLACK | {"email_verified": False}, None),
        (NOVA_SLACK, "UPDATE member SET oidc_subject = 'sub-other' WHERE id = :member"),
        (NOVA_SLACK, "UPDATE member SET active = 0 WHERE id = :member"),
        (NOVA_SLACK, "UPDATE member SET slack_guest_account = 1 WHERE id = :guest"),
    ],
    ids=["unverified", "member_linked", "member_inactive", "slack_guest_account"],
)
def test_the_guest_signs_in_as_before_when_the_invitation_cant_claim_them(
    client: TestClient,
    provider: FakeProvider,
    migrated: Settings,
    joined: tuple[int, int],
    claims: dict[str, Any],
    change: str | None,
) -> None:
    guest_id, member_id = joined
    if change:
        sql(migrated, change, member=member_id, guest=guest_id)
    before = by_id(migrated)[member_id]
    assert sign_in(client, provider, claims=claims).status_code == 303
    members = by_id(migrated)
    assert members[guest_id].active and members[guest_id].oidc_subject == "sub-nova"
    assert (members[member_id].oidc_subject, members[member_id].slack_user_id) == (
        before.oidc_subject,
        before.slack_user_id,
    )
    assert sessions_of(migrated, member_id) == 0 and sessions_of(migrated, guest_id) == 2
    assert not [a for a in rows(migrated, AuditRow) if a.action == "guest.linked_to_member"]


@pytest.mark.parametrize("settings_overrides", [SLACK])
def test_the_wrong_workspace_is_refused(
    client: TestClient, provider: FakeProvider, migrated: Settings, joined: tuple[int, int]
) -> None:
    guest_id, member_id = joined
    assert sign_in(client, provider, claims=NOVA_SLACK | {"https://slack.com/team_id": "T-ELSE"}).status_code == 403
    members = by_id(migrated)
    assert members[guest_id].active and members[member_id].oidc_subject is None


@pytest.mark.parametrize("settings_overrides", [{"slack_team_id": TEAM}])  # another provider
def test_another_provider_never_folds_a_guest(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    """Without Slack as the issuer nobody joins from the web, so the guest is set up by hand."""
    assert sign_in(client, provider, claims=SLACK_ADMIN | {"email": "admin@example.com"}).status_code == 303
    assert invite(client, "nova@example.com", "Nova").status_code == 303
    sql(
        migrated,
        "INSERT INTO member (household_id, display_name, email, slack_user_id, oidc_subject, role, can_cook, "
        "can_pickup, can_order, mute_dm_reminders, feedback_reminder, active, slack_link_locked, created_at, "
        "updated_at) VALUES (1, 'Nova', NULL, 'U09NOVA', 'sub-nova', 'guest', 0, 0, 0, 0, 0, 1, 0, "
        "'2026-10-22', '2026-10-22')",
    )
    client.cookies.clear()
    assert sign_in(client, provider, claims=NOVA_SLACK).status_code == 303
    nova = next(m for m in rows(migrated, MemberRow) if m.email == "nova@example.com")
    assert nova.oidc_subject is None
    assert not [a for a in rows(migrated, AuditRow) if a.action == "guest.linked_to_member"]


@pytest.mark.anyio
async def test_the_fold_decides_again_under_the_write_lock(
    migrated: Settings, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer links the member between the first look and the write lock: the sign-in
    decides again on what it wrote, and signs in as the guest instead of taking the member."""
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession

    from dinnerbell.db.repositories import MemberRepository
    from dinnerbell.domain.identity import IdentityClaims
    from dinnerbell.services import auth
    from dinnerbell.services.database import open_database
    from tests.support.calls import make_house

    database = open_database(migrated.async_database_url)
    try:
        house = await make_house(database.sessionmaker, clock, migrated.admin_email)
        riley = await house.add_member("Riley", admin=True)
        async with house.sessionmaker() as db:
            members = MemberRepository(db)
            guest = await members.add_guest(
                household_id=house.household.id,
                display_name="Riley",
                dietary_needs=frozenset(),
                slack_user_id="U01RILEY",
                slack_guest_account=False,
                now=clock(),
            )
            await members.update(house.household.id, guest.id, clock(), oidc_subject="sub-riley")
            await db.commit()
        real = auth.write_transaction

        @asynccontextmanager
        async def linked_meanwhile(db: AsyncSession) -> Any:
            async with house.sessionmaker() as other:
                await MemberRepository(other).update(house.household.id, riley.id, clock(), oidc_subject="sub-other")
                await other.commit()
            async with real(db):
                yield

        monkeypatch.setattr(auth, "write_transaction", linked_meanwhile)
        claims = IdentityClaims("sub-riley", riley.email, True, slack_team_id=TEAM, slack_user_id="U01RILEY")
        async with house.sessionmaker() as db:
            result = await auth.sign_in(db, claims, required_team_id=TEAM, slack_sign_in=True, now=clock())
        assert isinstance(result, auth.SignedIn) and result.member.id == guest.id
        assert (await house.refresh("Riley")).oidc_subject == "sub-other"
    finally:
        await database.close()
