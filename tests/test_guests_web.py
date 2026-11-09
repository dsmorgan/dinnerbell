# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guests on the web (#38): the restricted view, checked on the server for every route; inviting
and answering for guests on the event card; Admin > Members for guests, joining, and the
channel import; and Sign in with Slack joining someone from the channel (option B)."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey

from dinnerbell.config import SLACK_ISSUER, Settings
from dinnerbell.db.models import AuditRow, EventRow, InvitationRow, MemberRow, ResponseRow, VoteRow
from dinnerbell.domain import labels
from dinnerbell.domain.events import GUESTS_DONT_COORDINATE
from dinnerbell.domain.guests import REFUSED, GuestAction
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
    ("GET", "/stats"),  # a guest's own badges (#15)
    ("GET", "/stats/household"),  # sends a guest to /stats: channel posts link it (#153)
    ("POST", "/me"),
    ("POST", "/me/fun"),  # fun mode on or off for themselves (#153)
    ("POST", "/me/color-mode"),  # light, dark, or auto for themselves (#231)
    ("POST", "/me/fun/seen"),  # the den flipped for their own badges (#153)
    ("GET", "/events/{event_id}"),
    ("GET", "/events/{event_id}/card"),
    ("POST", "/events/{event_id}/respond"),
    ("POST", "/events/{event_id}/offer"),
    ("POST", "/events/{event_id}/coordinate"),  # to say why not, to a page left open (#227)
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


def test_a_guest_turns_fun_mode_off_on_their_page(
    admin: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    """A guest's switch for Dinnerbone's Den (#153) is on their dietary-needs page, the one page
    of their own, and changes only their row."""
    pat = add_guest(admin, "Pat", slack="U07PAT")
    guest = as_guest(admin, provider)
    today = guest.get("/").text
    assert 'data-fun="on"' in today and '<div class="den" data-pose="potter"' in today  # no invitations: the clock
    assert 'name="fun" value="on" checked' in guest.get("/me").text
    response = submit(guest, "/me/fun", {"fun": "off"})
    assert (response.status_code, response.headers["location"]) == (303, "/me?saved=fun#fun-mode")
    me = guest.get("/me").text
    assert 'name="fun" value="on">' in me and "data-fun" not in me and "fun.js" not in me
    today = guest.get("/").text
    assert "data-fun" not in today and 'class="den' not in today
    assert {m.id: m.fun_mode for m in rows(migrated, MemberRow)} == {1: True, pat: False}


def test_a_guests_den_follows_the_call_on_its_page_only_when_invited(
    admin: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    """On a call's page (#241), a guest's den follows that call, by their own page rules: they
    see the page, and so the den, only for a call they're invited to."""
    event_id = start(admin)
    pat = add_guest(admin, "Pat", slack="U07PAT")
    guest = as_guest(admin, provider)
    assert guest.get(f"/events/{event_id}").status_code == 404
    as_admin(guest, provider)
    assert submit(admin, f"/events/{event_id}/guests", {"guest_id": str(pat), "run": "1"}).status_code == 303
    guest = as_guest(admin, provider)
    html = guest.get(f"/events/{event_id}").text
    assert '<div class="greeting greeting-back">' in html
    assert re.search(rf'<div id="den-state" hidden data-pose="laps"[^>]* data-event="{event_id}"', html)
    card = guest.get(f"/events/{event_id}/card?from=event", headers={"HX-Request": "true"}).text
    assert re.search(rf'<div id="den-state" hidden[^>]* data-event="{event_id}"[^>]* hx-swap-oob="true"', card)
    assert submit(guest, "/me/fun", {"fun": "off"}).status_code == 303
    html = guest.get(f"/events/{event_id}").text
    assert "den-state" not in html and "fun.js" not in html


def test_a_guest_chooses_light_or_dark_on_their_page(
    admin: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    """A guest's light, dark, or auto (#231) is beside fun mode on their dietary-needs page, and
    changes only their row and their pages."""
    pat = add_guest(admin, "Pat", slack="U07PAT")
    guest = as_guest(admin, provider)
    me = guest.get("/me").text
    assert 'action="/me/color-mode"' in me and 'name="mode" value="auto" checked' in me
    assert me.index('id="fun-mode"') < me.index('id="color-mode"')
    response = submit(guest, "/me/color-mode", {"mode": "dark"})
    assert (response.status_code, response.headers["location"]) == (303, "/me?saved=color#color-mode")
    me = page(guest.get(response.headers["location"]))
    assert "Saved." in me.split('id="color-mode"', 1)[1].split("</form>", 1)[0]
    assert "Saved." not in me.split('id="color-mode"', 1)[0]  # not on the needs or fun mode card
    for path in ("/", "/me", "/stats"):
        assert '<html lang="en" data-theme="dark">' in guest.get(path).text, path
    assert {m.id: m.color_mode for m in rows(migrated, MemberRow)} == {1: "auto", pat: "dark"}


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


def _roster(html: str) -> list[str]:
    """The rows of **Who's in**, one string each."""
    found = re.search(r'<ul class="roster">(.*?)</ul>', html, re.S)
    assert found is not None
    return re.findall(r"<li[^>]*>.*?</li>", found.group(1), re.S)


def _row(rows_: list[str], name: str) -> str:
    (row,) = [r for r in rows_ if f"<strong>{name}</strong>" in r]
    return row


def test_a_guest_shows_once_on_the_roster_marked_with_who_invited_them(
    admin: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    """#238: each guest is listed once, in **Who's in**, marked "Guest invited by …", with the
    controls for them on their row. **Invite a guest** stays; there's no second list. A guest
    with Slack who hasn't answered has a row while answers are open, and one who's out stays."""
    event_id = start(admin)
    submit(admin, f"/events/{event_id}/respond", {"status": "in"})
    submit(admin, f"/events/{event_id}/guests", {"name": "Jo", "run": "1"})
    pat = add_guest(admin, "Pat", slack="U07PAT")
    submit(admin, f"/events/{event_id}/guests", {"guest_id": str(pat), "run": "1"})
    jo = next(m.id for m in rows(migrated, MemberRow) if m.display_name == "Jo")

    html = page(admin.get(f"/events/{event_id}"))
    assert "<h3>Guests</h3>" not in html and "Invite a guest" in html
    assert html.count("<strong>Jo</strong>") == 1 and html.count("<strong>Pat</strong>") == 1
    roster = _roster(html)
    assert len(roster) == 3
    assert "Guest invited by" not in _row(roster, "admin")
    mine = _row(roster, "Jo")
    assert "Guest invited by admin" in mine and ">In<" in mine
    assert f"/events/{event_id}/guests/{jo}/answer" in mine and f"/events/{event_id}/guests/{jo}/remove" in mine
    waiting = _row(roster, "Pat")  # Slack, so Pat answers; the admin can still take them off
    assert "No answer yet" in waiting and "Guest invited by admin" in waiting
    assert f"/guests/{pat}/answer" not in waiting and f"/events/{event_id}/guests/{pat}/remove" in waiting
    assert "2 eating" in html

    guest = as_guest(admin, provider)
    assert submit(guest, f"/events/{event_id}/respond", {"status": "out"}).status_code == 303
    seen = page(guest.get(f"/events/{event_id}"))
    assert seen.count("<strong>Pat</strong>") == 1
    out = _row(_roster(seen), "Pat")
    assert ">Out<" in out and "Guest invited by admin" in out and "/remove" not in out  # a guest removes nobody


def test_a_guest_taken_off_or_removed_stays_marked_a_guest_on_the_roster(admin: TestClient, migrated: Settings) -> None:
    """#238: a guest taken off the call keeps their answer, out, with no invitation, and a guest
    removed on Admin > Members keeps theirs too. Their rows still say "Guest", without "invited
    by", and with no controls, while a guest who stays keeps theirs."""
    event_id = start(admin)
    submit(admin, f"/events/{event_id}/respond", {"status": "in"})
    for name in ("Jo", "Kai", "Ren"):
        assert submit(admin, f"/events/{event_id}/guests", {"name": name, "run": "1"}).status_code == 303
    ids = {m.display_name: m.id for m in rows(migrated, MemberRow)}
    assert submit(admin, f"/events/{event_id}/guests/{ids['Jo']}/remove", {}).status_code == 303
    assert submit(admin, f"/admin/guests/{ids['Ren']}/remove", {}).status_code == 303

    roster = _roster(page(admin.get(f"/events/{event_id}")))
    for name in ("Jo", "Ren"):
        gone = _row(roster, name)
        assert ">Guest</span>" in gone and "invited by" not in gone, name
        assert "/answer" not in gone and "/remove" not in gone, name
    assert ">Out<" in _row(roster, "Jo")
    stays = _row(roster, "Kai")
    assert "Guest invited by admin" in stays and f"/guests/{ids['Kai']}/remove" in stays
    assert "Guest" not in _row(roster, "admin")


def test_a_guest_who_never_answered_leaves_the_roster_once_answers_close(
    admin: TestClient, clock: FakeClock, migrated: Settings
) -> None:
    """As a member who never answered does (#238): their row was only for answering, or taking
    them off, which end with the answers."""
    event_id = start(admin)
    submit(admin, f"/events/{event_id}/respond", {"status": "in"})
    submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"})
    submit(admin, f"/events/{event_id}/guests", {"name": "Jo", "run": "1"})
    pat = add_guest(admin, "Pat", slack="U07PAT")
    submit(admin, f"/events/{event_id}/guests", {"guest_id": str(pat), "run": "1"})
    clock.advance(timedelta(minutes=90))
    tick(admin, clock)
    assert next(e for e in rows(migrated, EventRow) if e.id == event_id).state == "voting"
    html = page(admin.get(f"/events/{event_id}"))
    assert "<strong>Pat</strong>" not in html and "No answer yet" not in html
    jo = _row(_roster(html), "Jo")
    assert "Guest invited by admin" in jo and "/answer" not in jo and "/remove" not in jo


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
    submit(admin, f"/events/{event_id}/details", {"note": "Home by six"})
    guest = as_guest(admin, provider)
    submit(guest, f"/events/{event_id}/respond", {"status": "in"})

    clock.advance(timedelta(minutes=90))
    tick(guest, clock)
    html = page(guest.get(f"/events/{event_id}"))
    assert "Proposals" in html and ">Vote<" not in html and "Lock this" not in html and "Add a proposal" not in html
    assert "coordinator-row" not in html and "Join late and vote" not in html  # neither tier (#212)
    assert "Admin" not in re.sub(r"<header.*?</header>", "", html, flags=re.S) and "Call it off" not in html
    proposal = rows(migrated, EventRow)[0]
    assert proposal.state == "voting"
    assert submit(guest, f"/events/{event_id}/proposals/1/vote", {}).status_code == 403
    assert rows(migrated, VoteRow) == []


def test_the_admin_links_a_guest_to_a_member_on_the_web(admin: TestClient, migrated: Settings) -> None:
    """Pat's Slack email isn't Kim's, so the page shows both and links only after the admin
    types Kim's name. Admins are never offered."""
    pat = add_guest(admin, "Pat", slack="U07PAT")
    invite(admin, "kim@example.com", "Kim")
    kim = next(m for m in rows(migrated, MemberRow) if m.display_name == "Kim")
    me = next(m for m in rows(migrated, MemberRow) if m.role == "admin")
    html = page(admin.get("/admin/members"))
    assert "Link to member" in html and f'<option value="{kim.id}">Kim (kim@example.com)</option>' in html
    assert f'<option value="{me.id}">' not in html
    assert submit(admin, f"/admin/guests/{pat}/link", {"member_id": ""}).status_code == 400
    check = submit(admin, f"/admin/guests/{pat}/link", {"member_id": str(kim.id)})
    assert check.status_code == 200
    html = page(check)
    assert "Link Pat to Kim?" in html and "kim@example.com" in html and "Slack shares no email for them" in html
    assert 'name="confirm"' in html and "U07PAT" in html
    wrong = submit(admin, f"/admin/guests/{pat}/link", {"member_id": str(kim.id), "confirm": "Pat"})
    assert wrong.status_code == 400 and "Type Kim exactly" in page(wrong)
    assert next(m for m in rows(migrated, MemberRow) if m.id == kim.id).slack_user_id is None
    linked = submit(admin, f"/admin/guests/{pat}/link", {"member_id": str(kim.id), "confirm": "Kim"})
    assert linked.status_code == 303
    by_id = {m.id: m for m in rows(migrated, MemberRow)}
    assert (by_id[kim.id].slack_user_id, by_id[pat].slack_user_id, by_id[pat].active) == ("U07PAT", None, False)
    (row,) = [a for a in rows(migrated, AuditRow) if a.action == "guest.linked_to_member"]
    assert row.payload["override"] is True


def test_a_guest_cant_be_linked_to_an_admin_on_the_web(admin: TestClient, migrated: Settings) -> None:
    pat = add_guest(admin, "Pat", slack="U07PAT")
    me = next(m for m in rows(migrated, MemberRow) if m.role == "admin")
    refused = submit(admin, f"/admin/guests/{pat}/link", {"member_id": str(me.id), "confirm": me.display_name})
    assert refused.status_code == 400 and "linked to an admin" in page(refused)
    assert next(m for m in rows(migrated, MemberRow) if m.id == me.id).slack_user_id != "U07PAT"


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
        c.execute(text("UPDATE member SET slack_email = 'nina@example.com' WHERE id = :id"), {"id": nina.id})
    engine.dispose()
    refused = invite(admin, "nina@example.com", "Nina")
    assert refused.status_code == 400
    html = page(refused)
    assert "joined from the Slack channel as a guest" in html and f'action="/admin/guests/{nina.id}/promote"' in html
    assert "Slack guest account" not in html
    assert (
        submit(admin, f"/admin/guests/{nina.id}/promote", {"role": "member", "email": "nina@example.com"}).status_code
        == 303
    )
    row = next(m for m in rows(migrated, MemberRow) if m.id == nina.id)
    assert (row.role, row.slack_user_id) == ("member", "U09NINA")


def test_the_promote_instead_form_says_when_the_guest_is_a_slack_guest_account(
    admin: TestClient, migrated: Settings
) -> None:
    use_people(admin, FakePeople(SlackPerson("U09OLA", "Ola", "ola@example.com", full_member=False)))
    assert submit(admin, "/admin/channel", {"slack_user_id": "U09OLA", "role": "guest"}).status_code == 303
    ola = next(m for m in rows(migrated, MemberRow) if m.slack_user_id == "U09OLA")
    assert ola.slack_guest_account is True
    from sqlalchemy import create_engine, text

    engine = create_engine(migrated.sync_database_url)
    with engine.begin() as c:
        c.execute(text("UPDATE member SET slack_email = 'ola@example.com' WHERE id = :id"), {"id": ola.id})
    engine.dispose()
    html = page(invite(admin, "ola@example.com", "Ola"))
    assert "They use a Slack guest account, not a full member of your workspace." in html
    assert "(a Slack guest account), Slack email ola@example.com" in html


def test_linking_a_slack_guest_account_always_asks_for_the_members_name(admin: TestClient, migrated: Settings) -> None:
    invite(admin, "kim@example.com", "Kim")  # before Ola, whose Slack email would hold it
    use_people(admin, FakePeople(SlackPerson("U09OLA", "Ola", "kim@example.com", full_member=False)))
    assert submit(admin, "/admin/channel", {"slack_user_id": "U09OLA", "role": "guest"}).status_code == 303
    ola = next(m for m in rows(migrated, MemberRow) if m.slack_user_id == "U09OLA")
    from sqlalchemy import create_engine, text

    engine = create_engine(migrated.sync_database_url)
    with engine.begin() as c:
        c.execute(text("UPDATE member SET slack_email = 'kim@example.com' WHERE id = :id"), {"id": ola.id})
    engine.dispose()
    kim = next(m for m in rows(migrated, MemberRow) if m.display_name == "Kim")
    check = submit(admin, f"/admin/guests/{ola.id}/link", {"member_id": str(kim.id)})
    assert check.status_code == 200 and "Ola uses a Slack guest account" in page(check)
    assert next(m for m in rows(migrated, MemberRow) if m.id == kim.id).slack_user_id is None
    linked = submit(admin, f"/admin/guests/{ola.id}/link", {"member_id": str(kim.id), "confirm": "Kim"})
    assert linked.status_code == 303


def test_a_guests_page_offers_no_coordinating_and_says_why_on_a_stale_tap(
    admin: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    """#227: no **I'll coordinate** on a guest's page; a page left open is told why not."""
    event_id = start(admin)
    pat = add_guest(admin, "Pat", slack="U07PAT")
    submit(admin, f"/events/{event_id}/guests", {"guest_id": str(pat), "run": "1"})
    guest = as_guest(admin, provider)
    html = page(guest.get(f"/events/{event_id}"))
    assert "/coordinate" not in html and labels.COORDINATE not in html
    assert submit(guest, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
    refused = submit(guest, f"/events/{event_id}/coordinate", {})
    assert REFUSED[GuestAction.COORDINATE] in page(refused)
    (row,) = rows(migrated, EventRow)
    assert row.coordinator_id is None
    # Everything else a coordinator posts stops at the door, with nothing changed.
    forms = {"member_id": "1", "shift": "plus15", "time": "19:00", "role": "pickup", "mode": "pickup", "run": "1"}
    for path in ("hand-off", "meal-time", "assignments", "roles", "decision-time", "done", "close"):
        assert submit(guest, f"/events/{event_id}/{path}", forms).status_code == 403, path


def test_the_hand_off_and_coordinator_forms_list_only_members(
    admin: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    """#227: a guest with Slack and one without, both invited. They're refused when forged."""
    invite(admin, "sam@example.com", "Sam")  # someone to hand off to, so the form shows (#239)
    event_id = start(admin)
    pat, jo = add_guest(admin, "Pat", slack="U07PAT"), add_guest(admin, "Jo")
    for guest in (pat, jo):
        submit(admin, f"/events/{event_id}/guests", {"guest_id": str(guest), "run": "1"})
    submit(admin, f"/events/{event_id}/respond", {"status": "in"})
    submit(admin, f"/events/{event_id}/coordinate", {})
    html = page(admin.get(f"/events/{event_id}"))
    for form in (f'action="/events/{event_id}/hand-off"', f'action="/events/{event_id}/admin/coordinator"'):
        at = html[html.index(form) :]
        select = at[: at.index("</select>")]
        assert f'value="{pat}"' not in select and f'value="{jo}"' not in select, form
    for guest in (pat, jo):
        for path in ("hand-off", "admin/coordinator"):
            refused = submit(admin, f"/events/{event_id}/{path}", {"member_id": str(guest)})
            assert GUESTS_DONT_COORDINATE in page(refused), path
    (row,) = rows(migrated, EventRow)
    assert row.coordinator_id not in {pat, jo}


def test_a_guest_who_is_in_can_still_be_assigned_a_role_on_the_web(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    from datetime import timedelta

    from dinnerbell.db.models import AssignmentRow, ProposalRow

    event_id = start(admin)
    pat = add_guest(admin, "Pat", slack="U07PAT")
    submit(admin, f"/events/{event_id}/guests", {"guest_id": str(pat), "run": "1"})
    submit(admin, f"/events/{event_id}/respond", {"status": "in"})
    submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"})
    guest = as_guest(admin, provider)
    submit(guest, f"/events/{event_id}/respond", {"status": "in"})
    assert submit(guest, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"}).status_code == 303
    clock.advance(timedelta(minutes=90))
    tick(guest, clock)
    guest.cookies.clear()
    sign_in(guest, provider, claims=SLACK_ADMIN)
    (first, *_) = sorted(rows(migrated, ProposalRow), key=lambda p: p.rank)
    assert submit(guest, f"/events/{event_id}/proposals/{first.id}/lock", {}).status_code == 303
    html = page(guest.get(f"/events/{event_id}"))
    at = html[html.index(f'action="/events/{event_id}/assignments"') :]
    assert f'value="{pat}"' in at[: at.index("</form>")]  # on the role form, for a guest who's in
    form = {"role": "pickup", "member_id": str(pat)}
    assert submit(guest, f"/events/{event_id}/assignments", form).status_code == 303
    assert (pat, "pickup") in {(a.member_id, a.role) for a in rows(migrated, AssignmentRow)}


def test_a_pending_choice_with_nobody_coordinating_says_an_admin_can_on_the_page(
    admin: TestClient, clock: FakeClock, migrated: Settings
) -> None:
    """As after the upgrade (0029): the time to choose runs on, and the page says whose it is."""
    from datetime import timedelta

    from dinnerbell.db.models import CatalogItemRow, ProposalRow

    add_item(admin, "Pho Place", cuisines="Vietnamese", modes=["pickup", "delivery"])
    event_id = start(admin)
    submit(admin, f"/events/{event_id}/guests", {"guest_id": str(add_guest(admin, "Jo")), "run": "1"})  # in at once
    submit(admin, f"/events/{event_id}/respond", {"status": "in"})
    submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"})
    submit(admin, f"/events/{event_id}/coordinate", {})
    clock.advance(timedelta(minutes=90))
    tick(admin, clock)
    pho = next(i.id for i in rows(migrated, CatalogItemRow) if i.name == "Pho Place")
    pick = next(p for p in rows(migrated, ProposalRow) if p.item_id == pho)
    assert submit(admin, f"/events/{event_id}/proposals/{pick.id}/lock", {}).status_code == 303
    assert "is choosing" in page(admin.get(f"/events/{event_id}"))
    assert submit(admin, f"/events/{event_id}/admin/coordinator", {"member_id": ""}).status_code == 303
    html = page(admin.get(f"/events/{event_id}"))
    assert "An admin can choose" in html and "If nobody does" in html and "is choosing" not in html


def test_no_form_makes_a_member_a_guest(admin: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    """Nobody is demoted to a guest, so a coordinator never becomes one: the admin's member form
    refuses the role (#227), and a guest is only ever added, invited, or joined as one."""
    assert submit(admin, "/admin/members", {"display_name": "Kim", "email": "kim@example.com", "role": "member"})
    kim = next(m for m in rows(migrated, MemberRow) if m.display_name == "Kim")
    form = {"display_name": "Kim", "email": "kim@example.com", "role": "guest"}
    assert submit(admin, f"/admin/members/{kim.id}", form).status_code == 400
    assert next(m for m in rows(migrated, MemberRow) if m.id == kim.id).role == "member"


def test_a_guest_sees_only_their_own_badges(admin: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    """The Stats page for a guest (#15): their pitching-in badges, no counts that guests can't
    earn, no household view, and none of the members' names."""
    from datetime import UTC, date, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from dinnerbell.db.models import BadgeAwardRow, ContributionRow, HouseholdRow

    event_id = start(admin)
    add_guest(admin, "Riley", slack="U07PAT")
    as_guest(admin, provider)
    engine = create_engine(migrated.sync_database_url)
    with Session(engine) as db:
        guest = next(m for m in rows(migrated, MemberRow) if m.slack_user_id == "U07PAT")
        db.add(
            BadgeAwardRow(
                household_id=db.query(HouseholdRow).one().id,
                member_id=guest.id,
                badge_key="road_runner",
                tier="first",
                awarded_at=datetime.now(UTC),
                announcement="pending",
            )
        )
        db.commit()
    engine.dispose()
    html = page(admin.get("/stats"))
    assert "<h1>Your badges</h1>" in html and 'aria-current="page">Your badges' in html
    assert html.count("badge-row") == 4 and "Road Runner<span" in html and "Newly earned" in html
    for hidden in ("Full Table", "Food Critic", "Meals shared", "The household", 'id="lately-title"', "Stats</a>"):
        assert hidden not in html
    assert "Meals cooked" in html
    assert "Meals coordinated" not in html and "Quarterback" not in html  # guests no longer coordinate (#227)
    assert 'aria-label="Stats"' not in html and "/stats/household" not in html  # no tabs for a guest (#153)
    moved = admin.get("/stats/household")
    assert (moved.status_code, moved.headers["location"]) == (303, "/stats")
    # A guest who coordinated before that keeps the count, and sees it.
    engine = create_engine(migrated.sync_database_url)
    with Session(engine) as db:
        guest = next(m for m in rows(migrated, MemberRow) if m.slack_user_id == "U07PAT")
        db.add(
            ContributionRow(
                household_id=db.query(HouseholdRow).one().id,
                member_id=guest.id,
                kind="coordinated",
                day=date(2026, 10, 21),
                event_id=event_id,
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
    engine.dispose()
    html = page(admin.get("/stats"))
    assert "Meals coordinated" in html and "Quarterback" in html and html.count("badge-row") == 5
