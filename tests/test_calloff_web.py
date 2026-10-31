# SPDX-License-Identifier: AGPL-3.0-or-later
"""Call it off, Close now, Start again, and Skip a day on the web (FR-6, FR-56): who sees each
control in which state, that the server checks again on every post, and what the page shows
after a call-off."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, EventRow
from dinnerbell.domain.meals import Slot
from tests.conftest import BASE_URL, csrf_from, invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.households import other_call, other_household
from tests.support.web import add_item, htmx, submit
from tests.test_auth_flow import member, rows
from tests.test_events_web import proposal_ids, sign_in_as, tick
from tests.test_schedule_web import seen_on_page, slot_form

People = dict[str, str]
"""Role name to the email that plays it."""

CREW: People = {
    "starter": "sam@example.com",
    "coordinator": "pat@example.com",
    "other": "riley@example.com",
}
STATES = ("scheduled", "collecting", "voting", "decided")
STALE_SHOWN = "That can't happen anymore. Reload the page to see what changed."


def as_admin(client: TestClient, provider: FakeProvider) -> None:
    client.cookies.clear()
    assert sign_in(client, provider).status_code == 303


def act_as(client: TestClient, provider: FakeProvider, role: str) -> None:
    if role == "admin":
        as_admin(client, provider)
    else:
        sign_in_as(client, provider, CREW[role])


@pytest.fixture
def house(client: TestClient, provider: FakeProvider) -> TestClient:
    """An admin, Sam (who starts the call), Pat (who coordinates), and Riley (who doesn't)."""
    sign_in(client, provider)
    add_item(client, "Siam Garden", cuisines="Thai")
    add_item(client, "Taqueria", cuisines="Mexican")
    for name, email in (("Sam", CREW["starter"]), ("Pat", CREW["coordinator"]), ("Riley", CREW["other"])):
        assert invite(client, email, name).status_code == 303
    return client


def call_in(client: TestClient, provider: FakeProvider, clock: FakeClock, settings: Settings, state: str) -> int:
    """Sam starts today's dinner; Pat is in with a guest, picks up, and coordinates. Then it
    moves on to ``state``. Leaves Pat signed in. For ``scheduled``, Sam starts tomorrow's
    dinner, which nobody can answer or coordinate yet."""
    sign_in_as(client, provider, CREW["starter"])
    day = "2026-10-23" if state == "scheduled" else "2026-10-22"
    started = submit(client, "/events", {"slot": "dinner", "day": day})
    assert started.status_code == 303, page(started)
    event_id = int(started.headers["location"].rsplit("/", 1)[1])
    sign_in_as(client, provider, CREW["coordinator"])
    if state == "scheduled":
        assert state_of(settings, event_id) == state
        return event_id
    for path, data in [
        ("respond", {"status": "in"}),
        ("offer", {"role": "pickup", "offered": "1"}),
        ("details", {"guests": "1"}),
        ("coordinate", {}),
    ]:
        assert submit(client, f"/events/{event_id}/{path}", data).status_code == 303, path
    if state in ("voting", "decided"):
        clock.advance(timedelta(minutes=90))
        tick(client, clock)
    if state == "decided":
        submit(client, f"/events/{event_id}/proposals/{proposal_ids(settings)[0]}/lock")
    assert state_of(settings, event_id) == state
    return event_id


def event_row(settings: Settings, event_id: int) -> EventRow:
    return next(e for e in rows(settings, EventRow) if e.id == event_id)


def state_of(settings: Settings, event_id: int) -> str:
    return str(event_row(settings, event_id).state)


def event_audits(settings: Settings) -> int:
    """How many audit rows record something done to a call; signing in writes others."""
    return sum(1 for a in rows(settings, AuditRow) if a.action.startswith("event."))


def audits(settings: Settings, action: str) -> list[AuditRow]:
    return [a for a in rows(settings, AuditRow) if a.action == action]


def offers_cancel(html: str, event_id: int) -> bool:
    return f'action="/events/{event_id}/cancel"' in html


def close_label(html: str, event_id: int) -> str | None:
    found = re.search(rf'action="/events/{event_id}/close".*?<button[^>]*>([^<]+)</button>', html, flags=re.S)
    return found.group(1) if found else None


# Who sees what, and the server checks again ------------------------------------------------------

CAN_CANCEL = {
    "starter": {"scheduled", "collecting", "voting"},
    "coordinator": {"collecting", "voting", "decided"},  # Pat can't coordinate a call before it opens
    "admin": set(STATES),
    "other": set(),
}
CLOSE_LABELS = {"scheduled": None, "collecting": "Start the vote now", "voting": "Decide now", "decided": None}


@pytest.mark.parametrize("state", STATES)
def test_who_sees_call_it_off_and_close_now(
    house: TestClient, provider: FakeProvider, clock: FakeClock, state: str, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, state)
    for role in ("starter", "coordinator", "admin", "other"):
        act_as(house, provider, role)
        for where in (f"/events/{event_id}", "/"):
            html = house.get(where).text
            assert offers_cancel(html, event_id) == (state in CAN_CANCEL[role]), (role, where)
            closes = role in ("coordinator", "admin")
            assert close_label(html, event_id) == (CLOSE_LABELS[state] if closes else None), (role, where)
            assert "Start dinner again" not in html


@pytest.mark.parametrize("state", STATES)
def test_the_server_refuses_whoever_the_page_hides_the_buttons_from(
    house: TestClient, provider: FakeProvider, clock: FakeClock, state: str, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, state)
    before = (event_row(migrated, event_id).version, event_audits(migrated))
    refusals = [("other", "cancel", "Only the coordinator, whoever started this call, or an admin")]
    if state == "scheduled":
        refusals += [("coordinator", "cancel", "Only the coordinator, whoever started this call, or an admin")]
        refusals += [(role, "close", "There's no open window to close.") for role in ("other", "starter", "admin")]
    elif state == "decided":
        refusals += [("starter", "cancel", "It's decided now, so only the coordinator or an admin")]
        refusals += [(role, "close", "There's no open window to close.") for role in ("other", "starter", "admin")]
    else:
        refusals += [(role, "close", "Only the coordinator or an admin") for role in ("other", "starter")]
    fields = {"reason": "nope", "run": "1", "window": "vote" if state == "voting" else "respond"}
    for role, action, message in refusals:
        act_as(house, provider, role)
        refused = htmx(house, f"/events/{event_id}/{action}", fields)
        assert refused.status_code == 409 and message in page(refused), (role, action)
        assert f'id="event-{event_id}"' in refused.text  # the card, swapped in place with the reason
        plain = submit(house, f"/events/{event_id}/{action}", fields)
        assert plain.status_code == 409 and "<html" in plain.text and message in page(plain), (role, action)
    assert state_of(migrated, event_id) == state
    assert (event_row(migrated, event_id).version, event_audits(migrated)) == before


def test_a_deactivated_starter_cannot_call_it_off(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, "collecting")
    sign_in_as(house, provider, CREW["starter"])
    sam_cookies, sam_token = dict(house.cookies), csrf_from(house)
    as_admin(house, provider)
    sam = member(migrated, CREW["starter"]).id
    assert submit(house, f"/admin/members/{sam}/deactivate").status_code == 303
    house.cookies.clear()
    house.cookies.update(sam_cookies)
    for action in ("cancel", "close", "restart"):
        response = house.post(f"/events/{event_id}/{action}", data={"csrf_token": sam_token})
        assert (response.status_code, response.headers["location"]) == (303, "/"), action  # signed out everywhere
    assert state_of(migrated, event_id) == "collecting"
    assert audits(migrated, "event.cancelled") == []


def test_another_households_call_cannot_be_called_off(house: TestClient, migrated: Settings) -> None:
    settings = migrated
    theirs = other_call(settings, other_household(settings))["event"]
    for action in ("cancel", "close", "restart"):
        fields = {"reason": "x", "run": "1", "window": "vote"}
        assert submit(house, f"/events/{theirs}/{action}", fields).status_code == 404, action
    assert state_of(migrated, theirs) == "voting"


# Calling it off ----------------------------------------------------------------------------------


def test_the_starter_calls_it_off_with_a_reason(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, "collecting")
    sign_in_as(house, provider, CREW["starter"])
    html = house.get(f"/events/{event_id}").text
    assert '<summary class="small">Call it off</summary>' in html and 'name="reason" maxlength="140"' in html
    assert 'hx-confirm="Call off dinner for everyone?"' in html
    reason = 'Pizza at <b>GrandmaZq7x\'s</b> & "cake"'
    card = htmx(house, f"/events/{event_id}/cancel", {"reason": reason, "run": "1", "from": "event"})
    assert card.status_code == 200 and "Called off by Sam." in card.text
    assert "Pizza at &lt;b&gt;GrandmaZq7x&#39;s&lt;/b&gt; &amp; &#34;cake&#34;" in card.text
    assert "<b>GrandmaZq7x" not in card.text
    assert "data-poll" not in card.text and not offers_cancel(card.text, event_id)
    row = event_row(migrated, event_id)
    assert (row.state, row.end_note, row.ended_by) == ("cancelled", reason, member(migrated, CREW["starter"]).id)
    (audit,) = audits(migrated, "event.cancelled")
    assert audit.payload == {"from": "collecting", "as": "starter", "note": reason}
    again = htmx(house, f"/events/{event_id}/cancel", {"run": "1"})
    assert again.status_code == 409 and "This call already finished." in again.text


def test_the_coordinator_calls_off_a_decided_call_from_today(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, "decided")
    done = submit(house, f"/events/{event_id}/cancel", {"run": "1", "from": "today"})
    assert (done.status_code, done.headers["location"]) == (303, "/")
    html = page(house.get("/"))
    assert "Called off by Pat." in html and "<q" not in html  # no reason, no quote
    (audit,) = audits(migrated, "event.cancelled")
    assert audit.payload == {"from": "decided", "as": "coordinator", "note": None}


def test_an_admin_calls_off_anyone_s_call_and_admin_cancel_is_gone(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, "voting")
    as_admin(house, provider)
    html = house.get(f"/events/{event_id}").text
    assert "/admin/cancel" not in html and "Cancel this call" not in html and ">Close now<" not in html
    assert submit(house, f"/events/{event_id}/admin/cancel").status_code == 404
    long = "x" * 200
    assert htmx(house, f"/events/{event_id}/cancel", {"reason": long, "run": "1"}).status_code == 200
    assert event_row(migrated, event_id).end_note == "x" * 140  # cut, not refused
    (audit,) = audits(migrated, "event.cancelled")
    assert audit.payload["as"] == "admin"


def test_a_call_cancelled_before_it_said_who_reads_plainly(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    from sqlalchemy import create_engine, text

    event_id = call_in(house, provider, clock, migrated, "collecting")
    engine = create_engine(migrated.sync_database_url)
    with engine.begin() as c:  # a call cancelled before migration 0006 recorded who did it
        c.execute(text("UPDATE event SET state = 'cancelled' WHERE id = :e"), {"e": event_id})
    engine.dispose()
    assert "This call was cancelled." in house.get(f"/events/{event_id}").text


# Close now ---------------------------------------------------------------------------------------


def test_close_now_starts_the_vote_then_decides(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, "collecting")
    html = house.get(f"/events/{event_id}").text
    assert 'hx-confirm="Start the vote now? Responses close for everyone now' in html  # Slack's words (#149)
    voting = htmx(house, f"/events/{event_id}/close", {"run": "1", "window": "respond"})
    assert voting.status_code == 200 and state_of(migrated, event_id) == "voting"
    assert close_label(voting.text, event_id) == "Decide now" and "Proposals" in voting.text
    decided = submit(house, f"/events/{event_id}/close", {"run": "1", "window": "vote", "from": "event"})
    assert (decided.status_code, decided.headers["location"]) == (303, f"/events/{event_id}")
    assert state_of(migrated, event_id) == "decided"
    assert close_label(house.get(f"/events/{event_id}").text, event_id) is None
    assert [a.payload["window"] for a in audits(migrated, "event.window_closed")] == ["respond", "vote"]
    late = htmx(house, f"/events/{event_id}/close", {"run": "1", "window": "vote"})
    assert late.status_code == 409 and "There's no open window to close." in page(late)


def test_close_now_hides_once_the_deadline_passes(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    """Until the next tick moves the call on, the card offers nothing the service would refuse."""
    event_id = call_in(house, provider, clock, migrated, "collecting")
    deadline = event_row(migrated, event_id).respond_deadline
    second = timedelta(seconds=1)
    for at, label in [(deadline - second, "Start the vote now"), (deadline, None), (deadline + second, None)]:
        clock.now = at
        for where in (f"/events/{event_id}", "/"):
            html = house.get(where).text
            assert close_label(html, event_id) == label and offers_cancel(html, event_id), (at, where)
    assert state_of(migrated, event_id) == "collecting"


def test_close_now_names_the_window_it_closes(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, "collecting")
    assert 'name="window" value="respond"' in house.get(f"/events/{event_id}").text
    malformed: list[dict[str, str]] = [{"run": "1", "window": bad} for bad in ("Respond", "respond ", "later", "0")]
    malformed += [{"run": "1"}, {"run": "1", "window": ""}, {"window": "respond"}, {"run": "0", "window": "respond"}]
    for fields in malformed:
        # HTMX drops a 400 and shows its own "Not saved" banner, so only the plain post's body is seen.
        assert htmx(house, f"/events/{event_id}/close", fields).status_code == 400, fields
        plain = submit(house, f"/events/{event_id}/close", fields)
        assert plain.status_code == 400 and "<html" in plain.text and STALE_SHOWN in page(plain), fields
    moved = htmx(house, f"/events/{event_id}/close", {"run": "1", "window": "vote"})  # not open yet
    assert moved.status_code == 409 and STALE_SHOWN in page(moved)
    assert state_of(migrated, event_id) == "collecting" and audits(migrated, "event.window_closed") == []
    voting = htmx(house, f"/events/{event_id}/close", {"run": "1", "window": "respond"})
    assert voting.status_code == 200 and 'name="window" value="vote"' in voting.text
    assert state_of(migrated, event_id) == "voting"


@pytest.mark.parametrize("run", [None, "", " 1", "1.0", "-1", "first", "99999999999999999999"])
def test_call_it_off_and_start_again_need_the_run_the_card_showed(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings, run: str | None
) -> None:
    """Every card that posts here writes ``run``; a post without it, or with one no card writes,
    is refused before anything changes."""
    event_id = call_in(house, provider, clock, migrated, "collecting")
    fields = {} if run is None else {"run": run}
    # HTMX drops a 400 and shows its own "Not saved" banner, so only the plain post's body is seen.
    assert htmx(house, f"/events/{event_id}/cancel", fields | {"reason": "no run"}).status_code == 400
    plain = submit(house, f"/events/{event_id}/cancel", fields)
    assert plain.status_code == 400 and "<html" in plain.text and STALE_SHOWN in page(plain)
    assert state_of(migrated, event_id) == "collecting"
    assert submit(house, f"/events/{event_id}/cancel", {"run": "1"}).status_code == 303
    assert htmx(house, f"/events/{event_id}/restart", fields).status_code == 400
    plain = submit(house, f"/events/{event_id}/restart", fields)
    assert plain.status_code == 400 and "<html" in plain.text and STALE_SHOWN in page(plain)
    assert (state_of(migrated, event_id), event_row(migrated, event_id).run) == ("cancelled", 1)


def test_two_close_now_confirmations_at_once_close_one_window(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    """Pat (coordinating) and an admin both confirm Start the vote now. One closes responses;
    the other must not then close the vote nobody confirmed."""
    event_id = call_in(house, provider, clock, migrated, "collecting")
    pat = (dict(house.cookies), csrf_from(house))
    as_admin(house, provider)
    admin = (dict(house.cookies), csrf_from(house))

    async def post(cookies: dict[str, str], token: str) -> httpx.Response:
        headers = {"Origin": BASE_URL, "HX-Request": "true", "X-CSRF-Token": token}
        transport = httpx.ASGITransport(app=house.app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL, cookies=cookies, headers=headers) as c:
            return await c.post(f"/events/{event_id}/close", data={"run": "1", "window": "respond"})

    async def both() -> list[httpx.Response]:
        return list(await asyncio.gather(post(*pat), post(*admin)))

    responses = house.portal.call(both)  # type: ignore[union-attr]
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert any(STALE_SHOWN in page(r) for r in responses)
    assert state_of(migrated, event_id) == "voting"
    assert [a.payload["window"] for a in audits(migrated, "event.window_closed")] == ["respond"]


def test_close_now_after_the_deadline_changes_nothing(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    """Responses are due to close, and the next tick will start the vote: a confirmation now
    would only close the window a second time."""
    event_id = call_in(house, provider, clock, migrated, "collecting")
    clock.advance(timedelta(minutes=90))
    late = htmx(house, f"/events/{event_id}/close", {"run": "1", "window": "respond"})
    assert late.status_code == 409 and STALE_SHOWN in page(late)
    assert state_of(migrated, event_id) == "collecting" and audits(migrated, "event.window_closed") == []
    tick(house, clock)
    assert state_of(migrated, event_id) == "voting"


# Starting again ----------------------------------------------------------------------------------


def test_any_member_starts_a_called_off_call_again(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, "voting")
    open_call = htmx(house, f"/events/{event_id}/restart", {"run": "1"})
    assert open_call.status_code == 409 and "already a dinner call" in page(open_call)
    submit(house, f"/events/{event_id}/cancel", {"reason": "Rain", "run": "1"})
    sign_in_as(house, provider, CREW["other"])
    html = house.get("/").text
    assert "Start dinner again" in html and f'action="/events/{event_id}/restart"' in html
    restarted = htmx(house, f"/events/{event_id}/restart", {"run": "1", "from": "today"})
    assert restarted.status_code == 200 and "Answer by" in restarted.text and "Called off" not in restarted.text
    row = event_row(migrated, event_id)
    riley = member(migrated, CREW["other"]).id
    assert (row.state, row.run, row.created_by, row.coordinator_id) == ("collecting", 2, riley, None)
    assert offers_cancel(restarted.text, event_id)  # Riley started it, so Riley can call it off


def test_admins_see_start_again_instead_of_re_run(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, "collecting")
    submit(house, f"/events/{event_id}/cancel", {"run": "1"})
    as_admin(house, provider)
    html = house.get(f"/events/{event_id}").text
    assert "Start dinner again" in html and ">Re-run<" not in html


def test_starting_again_is_too_late_after_the_meal(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, "collecting")
    submit(house, f"/events/{event_id}/cancel", {"run": "1"})
    clock.advance(timedelta(hours=8, minutes=1))  # 21:01 in Chicago: past 18:30 plus two hours, still the 22nd
    html = house.get("/").text
    assert "Called off by Pat." in html and "Start dinner again" not in html
    late = htmx(house, f"/events/{event_id}/restart", {"run": "1"})
    assert late.status_code == 409 and "It's too late to start today's dinner call again." in page(late)
    assert state_of(migrated, event_id) == "cancelled"


# A card left open across a restart -------------------------------------------------------------


def test_a_card_left_open_cannot_act_on_a_newer_run(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    """Pat's card shows run 1. Pat calls it off and Riley starts it again as run 2; forms from
    run 1's card, Pat's and an admin's, are refused and run 2 is untouched."""
    event_id = call_in(house, provider, clock, migrated, "collecting")
    card = house.get(f"/events/{event_id}").text
    assert card.count('name="run" value="1"') == 3  # Close now, Call it off, and Invite a guest
    assert submit(house, f"/events/{event_id}/cancel", {"run": "1"}).status_code == 303
    sign_in_as(house, provider, CREW["other"])
    assert 'name="run" value="1"' in house.get(f"/events/{event_id}").text  # Start dinner again
    assert htmx(house, f"/events/{event_id}/restart", {"run": "1"}).status_code == 200
    before = (event_row(migrated, event_id).version, event_audits(migrated))
    for role, action in (("coordinator", "cancel"), ("admin", "cancel"), ("admin", "close")):
        act_as(house, provider, role)
        stale = htmx(house, f"/events/{event_id}/{action}", {"run": "1", "window": "respond", "reason": "old page"})
        assert stale.status_code == 409 and STALE_SHOWN in page(stale), (role, action)
        if role == "admin":  # an admin may act on any run, so only the run stops it
            assert 'name="run" value="2"' in stale.text  # the fresh card, so the next tap is current
    malformed = htmx(house, f"/events/{event_id}/cancel", {"run": "first"})
    assert malformed.status_code == 400 and STALE_SHOWN in page(malformed)
    row = event_row(migrated, event_id)
    assert (row.run, row.state, row.end_note) == (2, "collecting", None)
    assert (row.version, event_audits(migrated)) == before
    act_as(house, provider, "other")  # Riley started run 2, calls it off; run 1's restart is stale too
    assert submit(house, f"/events/{event_id}/cancel", {"run": "2"}).status_code == 303
    again = htmx(house, f"/events/{event_id}/restart", {"run": "1"})
    assert again.status_code == 409 and STALE_SHOWN in page(again)
    assert (event_row(migrated, event_id).run, state_of(migrated, event_id)) == (2, "cancelled")


# Skip a day --------------------------------------------------------------------------------------


def test_an_admin_skips_a_later_day(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    html = house.get("/schedule").text  # Skip a day is on the Schedule page (#149)
    assert 'action="/admin/schedule/skip"' in html and 'min="2026-10-22" max="2026-10-29"' in html
    skipped = submit(
        house, "/admin/schedule/skip", {"slot": "dinner", "day": "2026-10-24", "reason": "<i>AwayZq7x</i>"}
    )
    assert (skipped.status_code, skipped.headers["location"]) == (303, "/schedule?notice=skipped#skip")
    assert "Skipped." in house.get("/schedule?notice=skipped").text
    old = house.get("/admin/schedule?notice=skipped", follow_redirects=False)  # a link from before the move
    assert (old.status_code, old.headers["location"]) == (303, "/schedule?notice=skipped#skip")
    (row,) = rows(migrated, EventRow)
    assert (str(row.date), row.slot, row.state, row.end_note) == (
        "2026-10-24",
        "dinner",
        "cancelled",
        "<i>AwayZq7x</i>",
    )
    (audit,) = audits(migrated, "event.skipped")
    assert audit.payload == {"slot": "dinner", "date": "2026-10-24", "note": "<i>AwayZq7x</i>"}
    clock.advance(timedelta(days=2))  # the skipped day: it shows on Today, and anyone can start it
    sign_in_as(house, provider, CREW["other"])
    today = house.get("/").text
    assert "Called off by admin." in today and "&lt;i&gt;AwayZq7x&lt;/i&gt;" in today and "<i>AwayZq7x" not in today
    assert "Start dinner again" in today


@pytest.mark.parametrize(
    ("form", "status", "message"),
    [
        ({"slot": "dinner", "day": "2026-10-21"}, 409, "You can skip today's call or a later day's."),
        ({"slot": "dinner", "day": "2026-10-30"}, 409, "You can skip a call up to 7 days ahead."),
        ({"slot": "lunch", "day": "2026-10-23"}, 409, "Lunch is off in the schedule."),
        ({"slot": "brunch", "day": "2026-10-23"}, 400, "Pick breakfast, lunch, or dinner."),
        ({"slot": "dinner", "day": "Friday"}, 400, "YYYY-MM-DD"),
    ],
)
def test_skipping_a_day_explains_refusals(
    house: TestClient, form: dict[str, str], status: int, message: str, migrated: Settings
) -> None:
    refused = submit(house, "/admin/schedule/skip", form | {"reason": "TripZq7x"})
    assert refused.status_code == status and message in page(refused)
    assert 'value="TripZq7x"' in refused.text and f'value="{form["day"]}"' in refused.text  # as typed
    assert rows(migrated, EventRow) == [] and audits(migrated, "event.skipped") == []


def test_skip_a_day_offers_only_slots_with_a_daily_call(house: TestClient) -> None:
    html = house.get("/schedule").text
    picker = re.search(r'<select name="slot">(.*?)</select>', html, flags=re.S)
    assert picker is not None and re.findall(r'value="(\w+)"', picker.group(1)) == ["dinner"]  # lunch is off
    off = slot_form(Slot.DINNER)
    off.pop("dinner_enabled")
    assert submit(house, "/schedule/dinner", off).status_code == 303
    html = house.get("/schedule").text
    assert 'action="/admin/schedule/skip"' not in html and "nothing to skip" in html
    assert submit(house, "/schedule/lunch", slot_form(Slot.LUNCH, lunch_enabled="on")).status_code == 303
    again = slot_form(Slot.DINNER, dinner_seen=seen_on_page(house, Slot.DINNER))
    assert submit(house, "/schedule/dinner", again).status_code == 303
    picker = re.search(r'<select name="slot">(.*?)</select>', house.get("/schedule").text, flags=re.S)
    assert picker is not None and re.findall(r'value="(\w+)"', picker.group(1)) == ["dinner", "lunch"]


def test_skip_a_day_bounds_follow_the_household_date(house: TestClient, clock: FakeClock, migrated: Settings) -> None:
    """00:30 UTC on the 23rd is still the 22nd in Chicago."""
    clock.now = datetime(2026, 10, 23, 0, 30, tzinfo=UTC)
    html = house.get("/schedule").text
    assert 'min="2026-10-22" max="2026-10-29"' in html
    late = submit(house, "/admin/schedule/skip", {"slot": "dinner", "day": "2026-10-30"})
    assert late.status_code == 409 and "up to 7 days ahead" in page(late)
    today = submit(house, "/admin/schedule/skip", {"slot": "dinner", "day": "2026-10-22"})
    assert today.status_code == 303
    assert [str(e.date) for e in rows(migrated, EventRow)] == ["2026-10-22"]


def test_skipping_a_day_that_has_a_call(
    house: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = call_in(house, provider, clock, migrated, "collecting")
    as_admin(house, provider)
    open_call = submit(house, "/admin/schedule/skip", {"slot": "dinner", "day": "2026-10-22"})
    assert open_call.status_code == 409 and "Call it off instead." in page(open_call)
    submit(house, f"/events/{event_id}/cancel", {"run": "1"})
    off = submit(house, "/admin/schedule/skip", {"slot": "dinner", "day": "2026-10-22"})
    assert off.status_code == 409 and "already off" in page(off)


def test_members_cannot_skip_a_day(house: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in_as(house, provider, CREW["other"])
    assert house.get("/admin/schedule").status_code == 403
    refused = submit(house, "/admin/schedule/skip", {"slot": "dinner", "day": "2026-10-23"})
    assert refused.status_code == 403
    assert rows(migrated, EventRow) == [] and audits(migrated, "event.skipped") == []
