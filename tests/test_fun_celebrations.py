# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fun mode's motion and celebrations (#153): the den's state riding along with every card swapped
into Today, the celebration only the member who locked or marked done the call gets, the quiet
caption everyone else gets, and the badge flip for the member who earned it, once."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from dinnerbell.config import Settings
from dinnerbell.domain.fun import DONE_CAPTION
from dinnerbell.web.middleware import STATIC_CONTENT_SECURITY_POLICY
from tests.conftest import ADMIN_EMAIL, csrf_from, invite, page
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import htmx, submit
from tests.test_auth_flow import member
from tests.test_events_web import admin, proposal_ids, sign_in_as, start, to_voting  # noqa: F401  (admin: fixture)

TODAY = {"from": "today"}
STATIC = Path(__file__).resolve().parents[1] / "src" / "dinnerbell" / "web" / "static"


def celebration(response: httpx.Response) -> dict[str, object] | None:
    header = response.headers.get("hx-trigger")
    if header is None:
        return None
    assert header.isascii()
    payload: dict[str, object] = json.loads(header)["dinnerbell:celebrate"]
    return payload


def den_state(html: str) -> str:
    match = re.search(r'<div id="den-state" hidden[^>]*></div>', html)
    assert match, "no den state"
    return match.group(0)


def execute(settings: Settings, *statements: str) -> None:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            for statement in statements:
                c.execute(text(statement))
    finally:
        engine.dispose()


def fun_off(client: TestClient) -> None:
    assert submit(client, "/me/fun", {"fun": "off"}).status_code == 303


# Celebrations for the actor -------------------------------------------------------------------------


def test_locking_and_marking_done_on_today_celebrate_for_the_actor(
    admin: TestClient,  # noqa: F811
    clock: FakeClock,
    migrated: Settings,
) -> None:
    event_id = start(admin)
    to_voting(admin, clock, event_id)
    first, _ = proposal_ids(migrated)
    locked = htmx(admin, f"/events/{event_id}/proposals/{first}/lock", TODAY)
    assert locked.status_code == 200
    payload = celebration(locked)
    assert payload is not None
    assert payload["kind"] == "decided" and payload["event"] == event_id and payload["state"] == "decided"
    assert str(payload["caption"]).endswith(" it is!")
    # The card brings the den along, out of band: he's eating, with the same caption.
    state = den_state(locked.text)
    assert 'hx-swap-oob="true"' in state and 'data-pose="eat"' in state and f'data-event="{event_id}"' in state
    assert f'data-caption="{payload["caption"]}"' in page(locked).replace("&#39;", "'")

    stale = htmx(admin, f"/events/{event_id}/proposals/{first}/lock", TODAY)
    assert stale.status_code == 409 and celebration(stale) is None  # never on a refusal

    done = htmx(admin, f"/events/{event_id}/done", TODAY)
    assert done.status_code == 200
    assert celebration(done) == {
        "kind": "done",
        "event": event_id,
        "run": payload["run"],
        "state": "done",
        "caption": DONE_CAPTION,
    }
    assert 'data-pose="sleep"' in den_state(done.text)
    again = htmx(admin, f"/events/{event_id}/done", TODAY)
    assert again.status_code == 409 and celebration(again) is None


def test_no_celebration_off_today_without_htmx_or_with_fun_mode_off(
    admin: TestClient,  # noqa: F811
    clock: FakeClock,
    migrated: Settings,
) -> None:
    event_id = start(admin)
    to_voting(admin, clock, event_id)
    first, _ = proposal_ids(migrated)
    # From the event page, which has no den: no celebration, and no den state.
    on_page = htmx(admin, f"/events/{event_id}/proposals/{first}/lock", {"from": "event"})
    assert on_page.status_code == 200 and celebration(on_page) is None and "den-state" not in on_page.text
    # A plain form post redirects: never on a 303.
    plain = submit(admin, f"/events/{event_id}/done", TODAY)
    assert plain.status_code == 303 and celebration(plain) is None


def test_fun_mode_off_gets_no_celebration_and_no_den(
    admin: TestClient,  # noqa: F811
    clock: FakeClock,
    migrated: Settings,
) -> None:
    fun_off(admin)
    event_id = start(admin)
    to_voting(admin, clock, event_id)
    first, _ = proposal_ids(migrated)
    locked = htmx(admin, f"/events/{event_id}/proposals/{first}/lock", TODAY)
    assert locked.status_code == 200 and celebration(locked) is None and "den-state" not in locked.text
    polled = admin.get(f"/events/{event_id}/card?from=today", headers={"HX-Request": "true"})
    assert "den-state" not in polled.text


def test_only_the_actor_celebrates_everyone_else_gets_the_quiet_caption(
    admin: TestClient,  # noqa: F811
    provider: FakeProvider,
    clock: FakeClock,
    migrated: Settings,
) -> None:
    invite(admin, "sam@example.com", "Sam")
    event_id = start(admin)
    to_voting(admin, clock, event_id)
    first, _ = proposal_ids(migrated)
    payload = celebration(htmx(admin, f"/events/{event_id}/proposals/{first}/lock", TODAY))
    assert payload is not None
    sign_in_as(admin, provider, "sam@example.com")
    # Sam's poll of the card, and his Today, carry the decision and its caption: no trigger.
    polled = admin.get(f"/events/{event_id}/card?from=today", headers={"HX-Request": "true"})
    assert celebration(polled) is None
    for html in (polled.text, admin.get("/").text):
        state = page(httpx.Response(200, text=den_state(html)))
        assert 'data-state="decided"' in state and f'data-caption="{payload["caption"]}"' in state
    assert "dinnerbell:den:" in (STATIC / "fun.js").read_text()  # once per call and state per session


def test_a_unicode_dish_is_ascii_in_the_header(
    admin: TestClient,  # noqa: F811
    clock: FakeClock,
    migrated: Settings,
) -> None:
    execute(migrated, "UPDATE catalog_item SET name = 'Crème brûlée ' || id")
    event_id = start(admin)
    to_voting(admin, clock, event_id)
    first, _ = proposal_ids(migrated)
    locked = htmx(admin, f"/events/{event_id}/proposals/{first}/lock", TODAY)
    assert "\\u00e8me br\\u00fbl\\u00e9e" in locked.headers["hx-trigger"]
    assert str((celebration(locked) or {})["caption"]).startswith("Crème brûlée ")


# The badge flip -------------------------------------------------------------------------------------


def award(
    settings: Settings, award_id: int, member_id: int, announcement: str = "announced", tier: str = "first"
) -> None:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO badge_award (id, household_id, member_id, badge_key, tier, awarded_at, announcement) "
                    "VALUES (:id, 1, :member, 'chef', :tier, '2026-10-22 17:00:00', :announcement)"
                ),
                {"id": award_id, "member": member_id, "tier": tier, "announcement": announcement},
            )
    finally:
        engine.dispose()


def seen(client: TestClient, award_id: object, **headers: str) -> httpx.Response:
    return client.post("/me/fun/seen", data={"award": str(award_id)}, headers=headers)


def test_a_badge_flips_once_for_the_member_who_earned_it(
    admin: TestClient,  # noqa: F811
    provider: FakeProvider,
    migrated: Settings,
) -> None:
    me = member(migrated, ADMIN_EMAIL).id
    invite(admin, "sam@example.com", "Sam")
    sam = member(migrated, "sam@example.com").id
    award(migrated, 7, me, announcement="skipped")  # made quietly: never flips
    assert "data-badge" not in den_state(admin.get("/").text)
    award(migrated, 8, me, tier="bronze")
    award(migrated, 9, sam)
    state = page(httpx.Response(200, text=den_state(admin.get("/").text)))
    assert 'data-badge="8" data-badge-caption="Chef · Bronze"' in state

    token = csrf_from(admin)
    assert seen(admin, 8).status_code == 403  # the CSRF header is required
    assert seen(admin, 9, **{"X-CSRF-Token": token}).status_code == 204  # Sam's: changes nothing
    assert member(migrated, ADMIN_EMAIL).fun_seen_award == 0
    assert seen(admin, "x", **{"X-CSRF-Token": token}).status_code == 204
    assert seen(admin, 8, **{"X-CSRF-Token": token}).status_code == 204
    assert member(migrated, ADMIN_EMAIL).fun_seen_award == 8
    assert "data-badge" not in den_state(admin.get("/").text)  # once per member, every device
    award(migrated, 6, me, tier="silver")  # an older ID never moves the marker back
    assert seen(admin, 6, **{"X-CSRF-Token": token}).status_code == 204
    assert member(migrated, ADMIN_EMAIL).fun_seen_award == 8

    sign_in_as(admin, provider, "sam@example.com")
    assert 'data-badge="9"' in den_state(admin.get("/").text)  # only Sam flips for Sam's
    # Fun mode off: no den, so no flip.
    fun_off(admin)
    assert "den-state" not in admin.get("/").text


# fun.js -------------------------------------------------------------------------------------------


def test_fun_js_writes_text_only_and_is_static(client: TestClient) -> None:
    script = (STATIC / "fun.js").read_text()
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert unsafe not in script, unsafe
    assert "caption.textContent = text" in script
    assert "prefers-reduced-motion: reduce" in script
    response = client.get("/static/fun.js")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == STATIC_CONTENT_SECURITY_POLICY
