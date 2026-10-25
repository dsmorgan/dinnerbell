# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guests on the web (#38): the restricted view, checked on the server for every route; inviting
and answering for guests on the event card; Admin > Members for guests, joining, and the
channel import; and Sign in with Slack joining someone from the channel (option B)."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey

from dinnerbell.config import SLACK_ISSUER, Settings
from dinnerbell.db.models import AuditRow, EventRow, InvitationRow, MemberRow, ResponseRow, VoteRow
from dinnerbell.services.guests import NotJoinable, SlackPerson
from dinnerbell.web.auth import require_anyone, require_csrf_anyone
from tests.conftest import csrf_from, invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import add_item, htmx, submit
from tests.test_architecture import audit_routes
from tests.test_auth_flow import rows
from tests.test_events_web import tick

TEAM = "T-HOUSE"
SLACK_ADMIN = {"sub": "subject-admin", "https://slack.com/team_id": TEAM, "https://slack.com/user_id": "U01ADMIN"}
pytestmark = pytest.mark.parametrize("settings_overrides", [{"slack_team_id": TEAM, "oidc_issuer": SLACK_ISSUER}])


@pytest.fixture
def provider(signing_key: RSAKey, clock: FakeClock, settings: Settings) -> FakeProvider:
    """Sign in with Slack, as configured: guests match by Slack user only when Slack is the
    issuer (#38)."""
    return FakeProvider(key=signing_key, clock=clock, issuer=settings.oidc_issuer)


GUEST_ROUTES = {
    ("GET", "/"),
    ("HEAD", "/"),
    ("GET", "/me"),
    ("POST", "/me"),
    ("GET", "/events/{event_id}"),
    ("GET", "/events/{event_id}/card"),
    ("POST", "/events/{event_id}/respond"),
    ("POST", "/events/{event_id}/offer"),
    ("POST", "/events/{event_id}/coordinate"),
    ("POST", "/events/{event_id}/hand-off"),
    ("POST", "/events/{event_id}/meal-time"),
    ("POST", "/events/{event_id}/assignments"),
    ("POST", "/events/{event_id}/done"),
    ("POST", "/events/{event_id}/close"),
}
"""Every route a signed-in guest may use (#38). The rest refuse them before any handler runs."""
PUBLIC = ("/auth/", "/healthz", "/static", "/favicon")


class FakePeople:
    """``services.guests.SlackPeople`` with set answers, for Sign in with Slack and the import."""

    def __init__(self, *people: SlackPerson, refuse: NotJoinable | None = None) -> None:
        self.people = {p.slack_user_id: p for p in people}
        self.refuse = refuse
        self.asked: list[str] = []

    async def person(self, slack_user_id: str) -> SlackPerson | NotJoinable:
        self.asked.append(slack_user_id)
        if self.refuse is not None:
            return self.refuse
        return self.people.get(slack_user_id, NotJoinable.NOT_IN_CHANNEL)

    async def channel_people(self) -> list[SlackPerson]:
        return list(self.people.values())


def use_people(client: TestClient, people: FakePeople) -> None:
    state = client.app.state  # type: ignore[attr-defined]
    state.dinnerbell = replace(state.dinnerbell, slack_people=people)


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    assert sign_in(client, provider, claims=SLACK_ADMIN).status_code == 303
    add_item(client, "Siam Garden", cuisines="Thai")
    submit(client, "/catalog/tags", {"kind": "dietary", "name": "Vegetarian"})
    return client


def add_guest(client: TestClient, name: str, slack: str | None = None) -> int:
    assert submit(client, "/admin/guests", {"display_name": name}).status_code == 303
    guest_id = int(re.findall(r'id="guest-(\d+)"', client.get("/admin/members").text)[-1])
    if slack:
        assert submit(client, f"/admin/guests/{guest_id}/slack", {"slack_user_id": slack}).status_code == 303
    return guest_id


def as_guest(client: TestClient, provider: FakeProvider, slack: str = "U07PAT") -> TestClient:
    client.cookies.clear()
    claims = {
        "sub": f"sub-{slack}",
        "email": f"{slack.lower()}@elsewhere.example",
        "https://slack.com/team_id": TEAM,
        "https://slack.com/user_id": slack,
    }
    response = sign_in(client, provider, claims=claims)
    assert response.status_code == 303, page(response)
    return client


def as_admin(client: TestClient, provider: FakeProvider) -> TestClient:
    client.cookies.clear()
    assert sign_in(client, provider, claims=SLACK_ADMIN).status_code == 303
    return client


def start(client: TestClient) -> int:
    response = submit(client, "/events", {"slot": "dinner", "day": "2026-10-22"})
    assert response.status_code == 303, page(response)
    return int(response.headers["location"].rsplit("/", 1)[1])


def snapshot(settings: Settings) -> tuple[int, ...]:
    counts = [len(rows(settings, m)) for m in (EventRow, ResponseRow, VoteRow, InvitationRow, MemberRow)]
    return (*counts, sum(e.version for e in rows(settings, EventRow)))


# The restricted view -------------------------------------------------------------------------------


def test_the_routes_open_to_guests_are_exactly_these(client: TestClient) -> None:
    api, problems = audit_routes(client.app.routes)  # type: ignore[attr-defined]
    assert problems == []
    open_to_guests = {
        (method, route.path)
        for route in api
        if isinstance(route, APIRoute)
        for method in route.methods
        if {d.call for d in route.dependant.dependencies} & {require_anyone, require_csrf_anyone}
    }
    assert open_to_guests | {("HEAD", "/"), ("GET", "/")} == GUEST_ROUTES


def test_a_guest_is_refused_on_every_other_route(admin: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    """Every route a guest isn't allowed answers 403 or 404, changes nothing, and never renders
    a member's page: catalog, preferences, history, stats, schedule, admin pages, offers, or
    another call."""
    event_id = start(admin)
    add_guest(admin, "Pat", slack="U07PAT")
    guest = as_guest(admin, provider)
    before = snapshot(migrated)
    api, _ = audit_routes(guest.app.routes)  # type: ignore[attr-defined]
    token = csrf_from(guest)
    checked = 0
    for route in api:
        for method in sorted(route.methods - {"HEAD"}):
            if (method, route.path) in GUEST_ROUTES or route.path.startswith(PUBLIC):
                continue
            url = re.sub(r"\{[^}]+\}", "1", route.path).replace("/events/1", f"/events/{event_id}")
            if method == "GET":
                response = guest.get(url)
            else:
                response = guest.post(url, data={"csrf_token": token, "run": "1", "status": "in", "name": "X"})
            assert response.status_code in {403, 404}, (method, route.path, response.status_code)
            checked += 1
    assert checked > 40
    assert snapshot(migrated) == before
    for path in ("/catalog", "/preferences", "/history", "/schedule", "/admin/members", "/admin/schedule"):
        assert guest.get(path).status_code == 403, path


def test_a_guest_sees_only_their_invitations(admin: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    event_id = start(admin)
    pat = add_guest(admin, "Pat", slack="U07PAT")
    guest = as_guest(admin, provider)
    today = page(guest.get("/"))
    assert "No invitations right now" in today and f"/events/{event_id}" not in today
    assert "Catalog" not in today and "Your dietary needs" in today
    assert guest.get(f"/events/{event_id}").status_code == 404
    assert guest.get(f"/events/{event_id}/card").status_code == 404
    assert submit(guest, f"/events/{event_id}/respond", {"status": "in"}).status_code == 404
    as_admin(guest, provider)
    assert submit(admin, f"/events/{event_id}/guests", {"guest_id": str(pat), "run": "1"}).status_code == 303
    guest = as_guest(admin, provider)
    html = page(guest.get("/"))
    assert f"/events/{event_id}" in html and ">Maybe<" not in html and "Guests, times, a suggestion" not in html
    assert submit(guest, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
    refused = submit(guest, f"/events/{event_id}/respond", {"status": "maybe"})
    assert refused.status_code == 409 and "in or out" in page(refused)
    assert submit(guest, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"}).status_code == 303
    assert submit(guest, f"/events/{event_id}/details", {"guests": "3"}).status_code == 403
    (response,) = [r for r in rows(migrated, ResponseRow) if r.member_id == pat]
    assert (response.status, response.offers_pickup, response.guests) == ("in", True, 0)


def test_a_guest_sets_their_dietary_needs(admin: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    pat = add_guest(admin, "Pat", slack="U07PAT")
    veg = re.search(r'name="dietary_needs" value="(\d+)"', admin.get("/admin/members").text)
    assert veg is not None
    guest = as_guest(admin, provider)
    assert "Vegetarian" in page(guest.get("/me"))
    assert submit(guest, "/me", {"needs": [veg.group(1)]}).headers["location"] == "/me?saved=1"
    row = next(m for m in rows(migrated, MemberRow) if m.id == pat)
    assert row.dietary_needs == [int(veg.group(1))]
    bad = submit(guest, "/me", {"needs": ["99999"]})
    assert bad.status_code == 400
    as_admin(guest, provider)
    assert admin.get("/me").headers["location"] == "/preferences"  # members have preferences


# Members inviting and answering for guests -----------------------------------------------------------


def test_a_member_invites_and_answers_for_a_guest_without_slack(admin: TestClient, migrated: Settings) -> None:
    event_id = start(admin)
    card = htmx(admin, f"/events/{event_id}/guests", {"name": "Jo", "run": "1", "from": "event"})
    assert card.status_code == 200 and "Jo" in page(card) and "invited by" in page(card)
    jo = next(m for m in rows(migrated, MemberRow) if m.display_name == "Jo")
    assert (jo.role, jo.email) == ("guest", None)
    answer = f"/events/{event_id}/guests/{jo.id}/answer"
    assert submit(admin, answer, {"role": "cook", "offered": "1", "run": "1"}).status_code == 303
    assert submit(admin, answer, {"status": "maybe", "run": "1"}).status_code == 409
    assert submit(admin, answer, {"status": "out"}).status_code == 400  # no run
    (response,) = [r for r in rows(migrated, ResponseRow) if r.member_id == jo.id]
    assert (response.status, response.offers_cook) == ("in", True)
    assert submit(admin, f"/events/{event_id}/guests/{jo.id}/remove", {}).status_code == 303
    assert rows(migrated, InvitationRow) == []


# Admin > Members ---------------------------------------------------------------------------------


def test_the_admin_manages_guests_and_joining(admin: TestClient, migrated: Settings) -> None:
    jo = add_guest(admin, "Jo")
    html = page(admin.get("/admin/members"))
    assert "Join from the channel is on" in html and 'id="guests"' in html
    assert submit(admin, "/admin/joining", {"on": "0"}).headers["location"].endswith("joining_off")
    assert "Join from the channel is off" in page(admin.get("/admin/members"))
    assert submit(admin, f"/admin/guests/{jo}", {"display_name": "Joanna"}).status_code == 303
    assert submit(admin, f"/admin/guests/{jo}/promote", {"role": "guest"}).status_code == 400
    assert (
        submit(admin, f"/admin/members/{jo}", {"display_name": "Joanna", "email": "jo@example.com"}).status_code == 400
    )
    assert (
        submit(admin, f"/admin/guests/{jo}/promote", {"role": "member", "email": "jo@example.com"}).status_code == 303
    )
    row = next(m for m in rows(migrated, MemberRow) if m.id == jo)
    assert (row.role, row.email, row.display_name) == ("member", "jo@example.com", "Joanna")
    actions = [a.action for a in rows(migrated, AuditRow)]
    assert {"household.joining_updated", "guest.added", "guest.updated", "member.promoted"} <= set(actions)


def test_a_member_cant_manage_guests(admin: TestClient, provider: FakeProvider) -> None:
    jo = add_guest(admin, "Jo")
    invite(admin, "sam@example.com", "Sam")
    admin.cookies.clear()
    claims = {"sub": "sub-sam", "email": "sam@example.com", "https://slack.com/team_id": TEAM}
    assert sign_in(admin, provider, claims=claims).status_code == 303
    for path in (
        "/admin/guests",
        f"/admin/guests/{jo}",
        f"/admin/guests/{jo}/promote",
        f"/admin/guests/{jo}/remove",
        "/admin/joining",
        "/admin/channel",
    ):
        assert (
            submit(admin, path, {"display_name": "X", "role": "member", "on": "0", "slack_user_id": "U1"}).status_code
            == 403
        ), path
    assert admin.get("/admin/channel").status_code == 403


def test_the_admin_adds_people_from_the_channel(admin: TestClient, migrated: Settings) -> None:
    people = FakePeople(SlackPerson("U09ANN", "Ann", "ann@example.com"), SlackPerson("U09BO", "Bo", full_member=False))
    use_people(admin, people)
    html = page(admin.get("/admin/channel"))
    assert "Ann" in html and "Bo" in html and "Slack guest" in html
    assert submit(admin, "/admin/channel", {"slack_user_id": "U09ANN", "role": "member"}).status_code == 303
    assert submit(admin, "/admin/channel", {"slack_user_id": "U09BO", "role": "member"}).status_code == 400
    assert submit(admin, "/admin/channel", {"slack_user_id": "U09BO", "role": "guest"}).status_code == 303
    assert submit(admin, "/admin/channel", {"slack_user_id": "U09NOPE", "role": "guest"}).status_code == 400
    roles = {m.slack_user_id: m.role for m in rows(migrated, MemberRow)}
    assert (roles["U09ANN"], roles["U09BO"]) == ("member", "guest")
    assert people.asked == ["U09ANN", "U09BO", "U09BO", "U09NOPE"]  # Slack is asked again on each post


# Sign in with Slack joins someone from the channel (option B) ----------------------------------------


def stranger(client: TestClient, provider: FakeProvider, slack: str = "U09NEW") -> Any:
    client.cookies.clear()
    claims = {
        "sub": f"sub-{slack}",
        "email": "new@elsewhere.example",
        "https://slack.com/team_id": TEAM,
        "https://slack.com/user_id": slack,
    }
    return sign_in(client, provider, claims=claims)


def test_signing_in_with_slack_joins_someone_in_the_channel(
    admin: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    use_people(admin, FakePeople(SlackPerson("U09NEW", "Nova")))
    response = stranger(admin, provider)
    assert response.status_code == 303
    guest = next(m for m in rows(migrated, MemberRow) if m.slack_user_id == "U09NEW")
    assert (guest.role, guest.display_name, guest.oidc_subject, guest.email) == ("guest", "Nova", "sub-U09NEW", None)
    assert "No invitations right now" in page(admin.get("/"))


@pytest.mark.parametrize(
    "people",
    [FakePeople(), FakePeople(refuse=NotJoinable.NOT_A_PERSON), None],
    ids=["not_in_channel", "bot", "no_slack"],
)
def test_signing_in_with_slack_refuses_anyone_else(
    admin: TestClient, provider: FakeProvider, migrated: Settings, people: FakePeople | None
) -> None:
    if people is not None:
        use_people(admin, people)
    before = len(rows(migrated, MemberRow))
    assert stranger(admin, provider).status_code == 403
    assert len(rows(migrated, MemberRow)) == before


def test_signing_in_with_slack_is_refused_while_joining_is_off(
    admin: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    people = FakePeople(SlackPerson("U09NEW", "Nova"))
    use_people(admin, people)
    submit(admin, "/admin/joining", {"on": "0"})
    assert stranger(admin, provider).status_code == 403
    assert people.asked == []
    (row,) = [a for a in rows(migrated, AuditRow) if a.action == "member.join_refused"]
    assert row.payload == {"slack_user_id": "U09NEW", "reason": "off", "via": "web"}


def test_signing_in_with_slack_is_rate_limited(admin: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    people = FakePeople(refuse=NotJoinable.NOT_IN_CHANNEL)
    use_people(admin, people)
    for _ in range(6):
        assert stranger(admin, provider).status_code == 403
    assert len(people.asked) == 5
    reasons = [a.payload["reason"] for a in rows(migrated, AuditRow) if a.action == "member.join_refused"]
    assert reasons.count("rate_limited") == 1 and reasons.count("not_in_channel") == 5


def test_a_guest_never_sees_votes_or_admin_controls(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = start(admin)
    pat = add_guest(admin, "Pat", slack="U07PAT")
    submit(admin, f"/events/{event_id}/guests", {"guest_id": str(pat), "run": "1"})
    submit(admin, f"/events/{event_id}/respond", {"status": "in"})
    submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"})
    submit(admin, f"/events/{event_id}/details", {"guests": "1"})
    guest = as_guest(admin, provider)
    submit(guest, f"/events/{event_id}/respond", {"status": "in"})
    from datetime import timedelta

    clock.advance(timedelta(minutes=90))
    tick(guest, clock)
    html = page(guest.get(f"/events/{event_id}"))
    assert "Proposals" in html and ">Vote<" not in html and "Lock this" not in html and "Add a proposal" not in html
    assert "Admin" not in re.sub(r"<header.*?</header>", "", html, flags=re.S) and "Call it off" not in html
    proposal = rows(migrated, EventRow)[0]
    assert proposal.state == "voting"
    assert submit(guest, f"/events/{event_id}/proposals/1/vote", {}).status_code == 403
    assert rows(migrated, VoteRow) == []


def test_the_admin_links_a_guest_to_a_member_on_the_web(admin: TestClient, migrated: Settings) -> None:
    pat = add_guest(admin, "Pat", slack="U07PAT")
    invite(admin, "kim@example.com", "Kim")
    kim = next(m for m in rows(migrated, MemberRow) if m.display_name == "Kim")
    assert "Link to member" in page(admin.get("/admin/members"))
    assert submit(admin, f"/admin/guests/{pat}/link", {"member_id": ""}).status_code == 400
    assert submit(admin, f"/admin/guests/{pat}/link", {"member_id": str(kim.id)}).status_code == 303
    by_id = {m.id: m for m in rows(migrated, MemberRow)}
    assert (by_id[kim.id].slack_user_id, by_id[pat].slack_user_id, by_id[pat].active) == ("U07PAT", None, False)


def test_a_member_sets_their_dietary_needs_on_preferences(admin: TestClient, migrated: Settings) -> None:
    html = page(admin.get("/preferences"))
    veg = re.search(r'name="needs" value="(\d+)"', html)
    assert veg is not None and "Your dietary needs" in html
    response = submit(admin, "/me", {"needs": [veg.group(1)]})
    assert response.headers["location"] == "/preferences?notice=settings"
    me = next(m for m in rows(migrated, MemberRow) if m.role == "admin")
    assert me.dietary_needs == [int(veg.group(1))]


def test_adding_a_member_with_a_joined_guests_email_offers_to_promote_them(
    admin: TestClient, migrated: Settings
) -> None:
    use_people(admin, FakePeople(SlackPerson("U09NINA", "Nina", "nina@example.com")))
    response = submit(admin, "/admin/channel", {"slack_user_id": "U09NINA", "role": "guest"})
    assert response.status_code == 303
    nina = next(m for m in rows(migrated, MemberRow) if m.slack_user_id == "U09NINA")
    from sqlalchemy import create_engine, text

    engine = create_engine(migrated.sync_database_url)
    with engine.begin() as c:  # as joining from the channel keeps it
        c.execute(text("UPDATE member SET email = 'nina@example.com' WHERE id = :id"), {"id": nina.id})
    engine.dispose()
    refused = invite(admin, "nina@example.com", "Nina")
    assert refused.status_code == 400
    html = page(refused)
    assert "joined from the Slack channel as a guest" in html and f'action="/admin/guests/{nina.id}/promote"' in html
    assert (
        submit(admin, f"/admin/guests/{nina.id}/promote", {"role": "member", "email": "nina@example.com"}).status_code
        == 303
    )
    row = next(m for m in rows(migrated, MemberRow) if m.id == nina.id)
    assert (row.role, row.slack_user_id) == ("member", "U09NINA")
