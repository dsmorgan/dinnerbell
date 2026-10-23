# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Today page, event pages, and Admin > Schedule, through the browser's door: CSRF tokens,
Origin checks, HTMX swaps, and household isolation."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import (
    AuditRow,
    EventRow,
    HouseholdRow,
    OutboxRow,
    ProposalRow,
    ResponseRow,
    VoteRow,
)
from dinnerbell.services import lifecycle
from tests.conftest import csrf_from, invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.households import other_call, other_household
from tests.support.web import add_item, htmx, submit
from tests.test_architecture import audit_routes
from tests.test_auth_flow import member, rows

M2_PREFIXES = ("/events", "/admin/schedule")


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    sign_in(client, provider)
    add_item(client, "Siam Garden", cuisines="Thai")
    add_item(client, "Taqueria", cuisines="Mexican")
    return client


def sign_in_as(client: TestClient, provider: FakeProvider, email: str) -> None:
    client.cookies.clear()
    response = sign_in(client, provider, claims={"sub": f"sub-{email}", "email": email})
    assert response.status_code == 303, page(response)


def start(client: TestClient) -> int:
    response = submit(client, "/events", {"slot": "dinner", "day": "2026-10-22"})
    assert response.status_code == 303, page(response)
    return int(response.headers["location"].rsplit("/", 1)[1])


def tick(client: TestClient, clock: FakeClock) -> None:
    jobs = client.app.state.jobs  # type: ignore[attr-defined]
    client.portal.call(lifecycle.tick, jobs.sessionmaker, clock())  # type: ignore[union-attr]


def to_voting(client: TestClient, clock: FakeClock, event_id: int) -> None:
    """Admin is in and picks up; a guest makes two; then responses close."""
    submit(client, f"/events/{event_id}/respond", {"status": "in"})
    submit(client, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"})
    submit(client, f"/events/{event_id}/details", {"guests": "1"})
    clock.advance(timedelta(minutes=90))
    tick(client, clock)


def proposal_ids(settings: Settings) -> list[int]:
    return [p.id for p in sorted(rows(settings, ProposalRow), key=lambda p: p.rank)]


# Pages and actions ------------------------------------------------------------------------------


def test_start_a_call_and_respond(admin: TestClient, migrated: Settings) -> None:
    assert "Start a call" in admin.get("/").text
    event_id = start(admin)
    html = page(admin.get(f"/events/{event_id}"))
    assert "Answer by" in html and "2:30 PM" in html  # opened at 13:00 Chicago, 90 minutes to answer
    card = htmx(admin, f"/events/{event_id}/respond", {"status": "in", "from": "today"})
    assert card.status_code == 200
    assert f'id="event-{event_id}"' in card.text and "<html" not in card.text
    assert re.search(r'aria-pressed="true">In<', card.text)
    plain = submit(admin, f"/events/{event_id}/respond", {"status": "maybe", "from": "today"})
    assert (plain.status_code, plain.headers["location"]) == (303, "/")
    elsewhere = submit(admin, f"/events/{event_id}/respond", {"status": "in", "from": "//evil"})
    assert elsewhere.headers["location"] == f"/events/{event_id}"  # never an open redirect
    (response,) = rows(migrated, ResponseRow)
    assert response.status == "in"
    assert f"/events/{event_id}" in admin.get("/").text


def test_details_roles_and_coordinating(admin: TestClient, migrated: Settings) -> None:
    event_id = start(admin)
    for path, data in [
        ("offer", {"role": "cook", "offered": "1"}),
        ("offer", {"role": "cook", "offered": "0"}),
        ("offer", {"role": "order", "offered": "1"}),
        ("coordinate", {}),
        (
            "details",
            {
                "guests": "2",
                "eat_from": "18:00",
                "eat_until": "19:30",
                "note": "<script>x</script>",
                "suggestion_text": "Pho",
            },
        ),
    ]:
        assert submit(admin, f"/events/{event_id}/{path}", data).status_code == 303, path
    html = admin.get(f"/events/{event_id}").text
    assert "&lt;script&gt;x&lt;/script&gt;" in html and "<script>x" not in html
    assert "Orders" in html and "+2 guests" in html and "Suggests Pho" in html and "Coordinator: admin" in html
    (response,) = rows(migrated, ResponseRow)
    assert (response.offers_cook, response.offers_order, response.guests) == (False, True, 2)
    bad = htmx(admin, f"/events/{event_id}/details", {"guests": "lots"})
    assert bad.status_code == 409 and "whole number" in bad.text
    bad_time = htmx(admin, f"/events/{event_id}/details", {"eat_from": "noon"})
    assert bad_time.status_code == 409 and "HH:MM" in page(bad_time)
    for path, data in [("respond", {"status": "hungry"}), ("offer", {"role": "juggle"}), ("hand-off", {})]:
        assert htmx(admin, f"/events/{event_id}/{path}", data).status_code == 409, path


def test_hand_off_from_the_page(admin: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    invite(admin, "sam@example.com", "Sam")
    event_id = start(admin)
    submit(admin, f"/events/{event_id}/coordinate")
    sam = member(migrated, "sam@example.com").id
    assert "Hand off coordinating" in admin.get(f"/events/{event_id}").text
    assert submit(admin, f"/events/{event_id}/hand-off", {"member_id": str(sam)}).status_code == 303
    assert rows(migrated, EventRow)[0].coordinator_id == sam


def test_vote_lock_and_mark_done(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id = start(admin)
    to_voting(admin, clock, event_id)
    html = page(admin.get(f"/events/{event_id}"))
    assert "Proposals" in html and "Lock this" in html and "New to the household." in html
    first, second = proposal_ids(migrated)
    voted = htmx(admin, f"/events/{event_id}/proposals/{first}/vote", {})
    assert voted.status_code == 200 and "1 vote: admin" in page(voted)
    htmx(admin, f"/events/{event_id}/proposals/{first}/unvote", {})
    assert rows(migrated, VoteRow) == []
    added = htmx(admin, f"/events/{event_id}/proposals", {"mode": "pickup", "free_text": "Pho Place", "cuisine_id": ""})
    assert added.status_code == 409 and "Pick a cuisine" in added.text
    assert htmx(admin, f"/events/{event_id}/proposals", {"mode": "teleport"}).status_code == 409
    assert htmx(admin, f"/events/{event_id}/proposals", {"mode": "pickup"}).status_code == 409
    locked = htmx(admin, f"/events/{event_id}/proposals/{second}/lock", {})
    assert locked.status_code == 200 and "Eating at" in locked.text and "Picking up: admin" in page(locked)
    assert htmx(admin, f"/events/{event_id}/meal-time", {"at": "19:15"}).status_code == 200
    assert "7:15 PM" in admin.get(f"/events/{event_id}").text
    assert htmx(admin, f"/events/{event_id}/meal-time", {"at": ""}).status_code == 409
    assert htmx(admin, f"/events/{event_id}/assignments", {"role": "chef"}).status_code == 409
    malformed = htmx(admin, f"/events/{event_id}/assignments", {"role": "pickup", "member_id": "abc"})
    assert malformed.status_code == 409 and "Pick a member" in malformed.text  # not a silent unassign
    assert "Picking up: admin" in page(malformed)
    assert htmx(admin, f"/events/{event_id}/assignments", {"role": "pickup", "member_id": ""}).status_code == 200
    assert 'hx-confirm="Mark this meal done?' in admin.get(f"/events/{event_id}").text
    done = htmx(admin, f"/events/{event_id}/done", {})
    assert done.status_code == 200 and "Done" in done.text
    assert htmx(admin, f"/events/{event_id}/done", {}).status_code == 409  # already done: shown, not a 500


def test_adding_a_catalog_proposal(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    from dinnerbell.db.models import CatalogItemRow

    event_id = start(admin)
    to_voting(admin, clock, event_id)
    add_item(admin, "Late Night", cuisines="Thai")  # added after ranking, so not yet proposed
    late = next(i.id for i in rows(migrated, CatalogItemRow) if i.name == "Late Night")
    response = htmx(admin, f"/events/{event_id}/proposals", {"mode": "pickup", "item_id": str(late)})
    assert response.status_code == 200 and "Added by admin." in page(response)
    assert late in {p.item_id for p in rows(migrated, ProposalRow)}


def test_starting_a_call_reports_problems(admin: TestClient) -> None:
    start(admin)
    again = submit(admin, "/events", {"slot": "dinner", "day": "2026-10-22"})
    assert again.status_code == 409 and "already a dinner call" in page(again)
    assert submit(admin, "/events", {"slot": "brunch", "day": "2026-10-22"}).status_code == 409
    assert submit(admin, "/events", {"slot": "lunch", "day": "soon"}).status_code == 409


def test_rule_errors_show_on_the_page_without_htmx(admin: TestClient, clock: FakeClock) -> None:
    event_id = start(admin)
    to_voting(admin, clock, event_id)
    late = submit(admin, f"/events/{event_id}/respond", {"status": "out"})
    assert late.status_code == 409 and "Responses are closed" in page(late) and "<html" in late.text


# Admin overrides --------------------------------------------------------------------------------


def test_admin_overrides_from_the_page(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id = start(admin)
    assert "+15 min" in admin.get(f"/events/{event_id}").text
    submit(admin, f"/events/{event_id}/admin/deadline", {"window": "respond", "shift": "plus30"})
    assert rows(migrated, EventRow)[0].respond_deadline == clock.now + timedelta(minutes=120)
    assert (
        htmx(admin, f"/events/{event_id}/admin/deadline", {"window": "respond", "shift": "sideways"}).status_code == 409
    )
    submit(admin, f"/events/{event_id}/admin/coordinator", {"member_id": str(member(migrated, "admin@example.com").id)})
    assert rows(migrated, EventRow)[0].coordinator_id is not None
    submit(admin, f"/events/{event_id}/admin/coordinator", {"member_id": ""})
    assert rows(migrated, EventRow)[0].coordinator_id is None
    cancelled = htmx(admin, f"/events/{event_id}/admin/cancel", {})
    assert cancelled.status_code == 200 and "Cancelled by an admin." in cancelled.text and "Re-run" in cancelled.text
    assert htmx(admin, f"/events/{event_id}/admin/cancel", {}).status_code == 409  # illegal transition, shown
    rerun = htmx(admin, f"/events/{event_id}/admin/rerun", {})
    assert rerun.status_code == 200 and rows(migrated, EventRow)[0].run == 2
    actions = [a.action for a in rows(migrated, AuditRow) if a.action.startswith("event.")]
    assert actions[-5:] == [
        "event.deadline_moved",
        "event.coordinator_assigned",
        "event.coordinator_assigned",
        "event.cancelled",
        "event.rerun",
    ]


def test_members_cannot_use_admin_overrides(admin: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    invite(admin, "sam@example.com", "Sam")
    event_id = start(admin)
    sign_in_as(admin, provider, "sam@example.com")
    html = admin.get(f"/events/{event_id}").text
    assert "+15 min" not in html and "Cancel this call" not in html and "/admin/" not in html
    before = len(rows(migrated, AuditRow))
    for path, data in [
        ("deadline", {"window": "respond", "shift": "close"}),
        ("cancel", {}),
        ("rerun", {}),
        ("coordinator", {"member_id": ""}),
    ]:
        assert submit(admin, f"/events/{event_id}/admin/{path}", data).status_code == 403, path
    assert rows(migrated, EventRow)[0].state == "collecting"
    assert len(rows(migrated, AuditRow)) == before
    assert admin.get("/admin/schedule").status_code == 403


# Admin > Schedule -------------------------------------------------------------------------------


def schedule_form(**changes: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "dinner_enabled": "on",
        "dinner_call_time": "15:00",
        "dinner_meal_time": "18:30",
        "dinner_response_minutes": "90",
        "dinner_vote_minutes": "45",
        "dinner_reminder_minutes": "15",
        "dinner_modes": ["pickup", "delivery"],
        "lunch_call_time": "10:30",
        "lunch_meal_time": "12:30",
        "lunch_response_minutes": "60",
        "lunch_vote_minutes": "30",
        "lunch_reminder_minutes": "15",
        "lunch_modes": ["pickup"],
        "proposal_count": "3",
        "variety_window_days": "7",
    }
    form |= {f"weight_{n}": v for n, v in (("soft_preference", "1"), ("cuisine_variety", "3"), ("novelty", "1"))}
    form |= {f"weight_{n}": v for n, v in (("suggested", "2"), ("default_mode", "1"), ("volunteer_fit", "1"))}
    return form | changes


def test_schedule_page_saves_and_audits(admin: TestClient, migrated: Settings) -> None:
    html = admin.get("/admin/schedule").text
    assert 'name="dinner_call_time" value="15:00"' in html and 'name="weight_cuisine_variety" value="3"' in html
    assert submit(admin, "/admin/schedule", schedule_form()).status_code == 303
    assert [a for a in rows(migrated, AuditRow) if a.action == "household.schedule_updated"] == []  # no change
    changed = schedule_form(
        dinner_call_time="14:00", lunch_enabled="on", weight_novelty="2.5", variety_window_days="10"
    )
    saved = submit(admin, "/admin/schedule", changed)
    assert (saved.status_code, saved.headers["location"]) == (303, "/admin/schedule?notice=saved")
    assert "Schedule saved" in admin.get("/admin/schedule?notice=saved").text
    settings = rows(migrated, HouseholdRow)[0].settings
    assert settings["slots"]["dinner"]["call_time"] == "14:00" and settings["slots"]["lunch"]["enabled"] is True
    assert settings["ranking_weights"]["novelty"] == 2.5 and settings["variety_window_days"] == 10
    (audit,) = [a for a in rows(migrated, AuditRow) if a.action == "household.schedule_updated"]
    assert set(audit.payload) == {"slots", "ranking_weights", "variety_window_days"}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"dinner_call_time": "3pm"}, "HH:MM"),
        ({"dinner_vote_minutes": "1"}, "vote window"),
        ({"dinner_reminder_minutes": ""}, "Enter the reminder time"),
        ({"dinner_response_minutes": "ninety"}, "whole number"),
        ({"dinner_modes": []}, "default way"),
        ({"dinner_modes": ["teleport"]}, "cook, pickup, or delivery"),
        ({"weight_novelty": "11"}, "Weights run"),
        ({"weight_novelty": "nan"}, "must be a number"),
        ({"weight_novelty": "x"}, "must be a number"),
        ({"proposal_count": "9"}, "proposals"),
        ({"variety_window_days": "0"}, "variety window"),
    ],
)
def test_schedule_page_rejects_bad_values(admin: TestClient, changes: dict[str, Any], message: str) -> None:
    form = schedule_form(**changes)
    response = submit(admin, "/admin/schedule", form)
    assert response.status_code == 400 and message in page(response)
    for name, value in changes.items():  # what the admin typed is still there
        if isinstance(value, str) and value:
            assert f'value="{value}"' in response.text, name


# Security ---------------------------------------------------------------------------------------


def m2_posts(client: TestClient) -> list[str]:
    paths = [
        route.path
        for route in audit_routes(client.app.routes)[0]  # type: ignore[attr-defined]
        if isinstance(route, APIRoute) and "POST" in route.methods and route.path.startswith(M2_PREFIXES)
    ]
    return [re.sub(r"\{[^}]+\}", "1", path) for path in paths]


def snapshot(settings: Settings) -> tuple[int, ...]:
    models = (EventRow, ResponseRow, ProposalRow, VoteRow, OutboxRow, AuditRow)
    return (*(len(rows(settings, m)) for m in models), rows(settings, EventRow)[0].version)


@pytest.fixture
def voting(admin: TestClient, clock: FakeClock, migrated: Settings) -> Settings:
    event_id = start(admin)
    assert event_id == 1
    to_voting(admin, clock, event_id)
    return migrated


def test_every_m2_post_route_is_enumerated(client: TestClient) -> None:
    assert len(m2_posts(client)) == 18


def test_every_admin_override_checks_for_an_admin_in_the_web_layer(client: TestClient) -> None:
    from dinnerbell.web.auth import require_admin_csrf

    admin_paths = [r for r in audit_routes(client.app.routes)[0] if "/admin/" in r.path or r.path.startswith("/admin")]  # type: ignore[attr-defined]
    assert {r.path for r in admin_paths} >= {
        "/events/{event_id}/admin/deadline",
        "/events/{event_id}/admin/cancel",
        "/events/{event_id}/admin/rerun",
        "/events/{event_id}/admin/coordinator",
        "/admin/schedule",
    }
    for route in admin_paths:
        if "POST" in route.methods:
            assert require_admin_csrf in {d.call for d in route.dependant.dependencies}, route.path


def test_every_m2_post_refuses_a_missing_or_wrong_csrf_token(client: TestClient, voting: Settings) -> None:
    before = snapshot(voting)
    body = {"status": "out", "slot": "dinner", "day": "2026-10-22", "mode": "pickup", "item_id": "1", "role": "cook"}
    for path in m2_posts(client):
        assert client.post(path, data=body).status_code == 403, path
        assert client.post(path, data=body | {"csrf_token": "0" * 64}).status_code == 403, path
        assert client.post(path, data=body, headers={"X-CSRF-Token": "nope"}).status_code == 403, path
    assert snapshot(voting) == before


def test_every_m2_post_refuses_a_foreign_origin(client: TestClient, voting: Settings) -> None:
    before = snapshot(voting)
    token = csrf_from(client)
    for path in m2_posts(client):
        response = client.post(path, data={"csrf_token": token}, headers={"Origin": "https://evil.example"})
        assert response.status_code == 403, path
    assert snapshot(voting) == before


def test_another_households_call_is_not_found(admin: TestClient, migrated: Settings) -> None:
    ids = other_household(migrated)
    theirs = other_call(migrated, ids)
    event, proposal = theirs["event"], theirs["proposal"]
    assert admin.get(f"/events/{event}").status_code == 404
    before = snapshot_other(migrated, event)
    for path in m2_posts(admin):
        if path == "/events" or path.startswith("/admin"):
            continue
        real = path.replace("/events/1", f"/events/{event}").replace("/proposals/1/", f"/proposals/{proposal}/")
        data = {"status": "in", "role": "cook", "offered": "1", "mode": "pickup", "item_id": str(ids["item"])}
        data |= {"window": "vote", "shift": "close", "member_id": "", "at": "19:00"}
        response = submit(admin, real, data)
        assert response.status_code == 404, real
    assert snapshot_other(migrated, event) == before
    assert event_not_listed(admin, event)


def snapshot_other(settings: Settings, event_id: int) -> tuple[Any, ...]:
    event = next(e for e in rows(settings, EventRow) if e.id == event_id)
    their = [r for r in rows(settings, ResponseRow) if r.event_id == event_id]
    votes = [v for v in rows(settings, VoteRow) if v.event_id == event_id]
    return (event.state, event.version, event.coordinator_id, len(their), len(votes))


def event_not_listed(client: TestClient, event_id: int) -> bool:
    return f'/events/{event_id}"' not in client.get("/").text


def test_m2_pages_have_no_inline_script_or_style(admin: TestClient, clock: FakeClock) -> None:
    event_id = start(admin)
    pages_seen = [
        admin.get("/"),
        admin.get(f"/events/{event_id}"),
        admin.get("/admin/schedule"),
        admin.get("/admin/members"),
    ]
    to_voting(admin, clock, event_id)
    pages_seen.append(admin.get(f"/events/{event_id}"))
    for response in pages_seen:
        assert response.status_code == 200
        html = response.text
        for attributes, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, flags=re.S):
            assert 'src="/static/' in attributes
            assert body.strip() == ""
        assert " style=" not in html
        assert not re.search(r"\son[a-z]+=", html)


def test_malformed_event_ids_are_not_found(admin: TestClient) -> None:
    for bad in ("0", "-1", "abc", "9" * 25):
        assert admin.get(f"/events/{bad}").status_code == 404, bad
    assert submit(admin, "/events/999/respond", {"status": "in"}).status_code == 404


def test_signed_out_visitors_are_sent_to_sign_in(client: TestClient) -> None:
    response = client.get("/events/1")
    assert response.status_code == 303 and response.headers["location"] == "/"


def test_htmx_errors_swap_in_place(admin: TestClient) -> None:
    event_id = start(admin)
    response = admin.post(
        f"/events/{event_id}/respond",
        data={"status": "sideways"},
        headers={"HX-Request": "true", "X-CSRF-Token": csrf_from(admin)},
    )
    assert response.status_code == 409 and "Pick in, out, or maybe." in page(response)
    assert isinstance(response, httpx.Response)
