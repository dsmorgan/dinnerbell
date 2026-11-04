# SPDX-License-Identifier: AGPL-3.0-or-later
"""Away (FR-63, #196): a member who'll be gone sets a window, from **Away until…** where a call
asks or from Preferences, and the calls on those days answer them Out. They're out, marked "away
until", count as answered, get no ask and no reminder, and can still say In on any call."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dinnerbell.channels.slack import ids
from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, MemberRow
from dinnerbell.domain import labels
from dinnerbell.domain.away import MAX_AWAY_DAYS, Away, AwayRuleViolation, away_from_call, check_away
from dinnerbell.domain.contributions import Kind
from dinnerbell.domain.events import EventState, Response, Status
from dinnerbell.domain.meals import Slot
from dinnerbell.services import away, call_admin, calls
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden, NotFound
from tests.conftest import invite, page
from tests.support.calls import OPENS, REMIND, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.fake_slack import CHANNEL, state
from tests.support.slack import Slack, wire
from tests.support.web import htmx, submit
from tests.test_admin import sign_in_as
from tests.test_auth_flow import member, rows
from tests.test_events_web import admin as admin
from tests.test_events_web import start

THU = date(2026, 10, 22)
"""The day the test household's dinner call is for, in Chicago."""
FRI, SAT, SUN = THU + timedelta(days=1), THU + timedelta(days=2), THU + timedelta(days=3)
pytestmark = pytest.mark.anyio


# The rules -----------------------------------------------------------------------------------------


def test_a_window_is_whole_days_upcoming_or_under_way_and_at_most_a_year() -> None:
    assert check_away(THU, SAT, THU) == Away(THU, SAT)
    assert check_away(THU - timedelta(days=3), THU, THU).under_way(THU)  # started already: fine
    assert check_away(THU, THU + timedelta(days=MAX_AWAY_DAYS - 1), THU).back == THU + timedelta(days=MAX_AWAY_DAYS)
    for first, last, code in (
        (SAT, FRI, "away_backwards"),
        (THU - timedelta(days=3), THU - timedelta(days=1), "away_past"),
        (THU, THU + timedelta(days=MAX_AWAY_DAYS), "away_too_long"),
    ):
        with pytest.raises(AwayRuleViolation) as caught:
            check_away(first, last, THU)
        assert caught.value.code == code
    window = Away(FRI, SAT)
    assert [window.covers(d) for d in (THU, FRI, SAT, SUN)] == [False, True, True, False]
    assert window.current(SAT) and not window.current(SUN) and not window.under_way(THU)


def test_away_until_runs_from_the_calls_day_through_the_day_before_the_one_picked() -> None:
    assert away_from_call(THU, SUN, THU) == Away(THU, SAT)
    assert away_from_call(THU, FRI, THU) == Away(THU, THU)  # back tomorrow: away just today
    for back in (THU, THU - timedelta(days=1)):
        with pytest.raises(AwayRuleViolation, match="after this call's day"):
            away_from_call(THU, back, THU)


def test_one_status_line_names_the_day_theyre_back() -> None:
    assert labels.away_status(Away(THU, SAT), THU) == "Away until Sun, Oct 25"
    assert labels.away_status(Away(FRI, SAT), THU) == "Away from Fri, Oct 23 until Sun, Oct 25"
    assert labels.away_end(Away(THU, SAT), THU) == labels.IM_BACK
    assert labels.away_end(Away(FRI, SAT), THU) == labels.CLEAR_AWAY  # it hasn't started
    assert labels.away_marker(SUN) == "away until Oct 25"
    assert labels.away_marker(SUN, capital=True) == "Away until Oct 25"


def test_their_own_answer_takes_the_away_mark_off() -> None:
    from dinnerbell.domain.events import Details, with_details, with_status

    out = Response(3, Status.OUT, away_until=SUN)
    assert with_status(out, 3, Status.IN, OPENS).away_until is None
    assert with_status(out, 3, Status.OUT, OPENS).away_until is None  # their Out now, not the window's
    assert with_details(out, 3, Details(), OPENS).away_until is None


def test_an_away_out_isnt_a_quick_answer() -> None:
    from dinnerbell.domain.contributions import DoneFacts, credits_for_done
    from dinnerbell.domain.events import Event, EventKind

    event = Event(
        id=1,
        household_id=1,
        slot=Slot.DINNER,
        day=THU,
        kind=EventKind.CALL,
        state=EventState.DONE,
        run=1,
        opens_at=OPENS,
        respond_deadline=RESPOND_BY,
        reminder_minutes=15,
        vote_minutes=45,
    )
    quick = Response(1, Status.OUT, responded_at=OPENS)
    away_out = Response(2, Status.OUT, responded_at=OPENS, away_until=SUN)
    facts = DoneFacts(event=event, assignments=(), responses=(quick, away_out), guest_ids=frozenset())
    on_time = {c.member_id for c in credits_for_done(facts) if c.kind is Kind.RESPONDED_ON_TIME}
    assert on_time == {1}


# Calls, through the services ---------------------------------------------------------------------


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def act(house: House, use_case: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


async def set_away(house: House, name: str, first: date, last: date) -> None:
    await act(house, away.set_my_away, await house.refresh(name), first, last)
    await house.refresh(name)


async def answer(house: House, event_id: int, name: str) -> Response | None:
    return (await house.context(event_id)).response_of(house[name].id)


async def open_dinner(house: House) -> int:
    house.clock.now = OPENS
    await house.tick()
    return await dinner_id(house)


async def test_a_call_on_a_day_away_answers_them_out_with_no_ask_and_no_reminder(house: House) -> None:
    await set_away(house, "Kim", THU, FRI)
    event_id = await open_dinner(house)
    kims = await answer(house, event_id, "Kim")
    assert kims is not None and kims.status is Status.OUT and kims.away_until == SAT
    (opened,) = [e for e in await house.outbox() if e.kind == "call_opened"]
    assert "Kim" not in [p["name"] for p in opened.payload["mentions"]]  # no ask (FR-63)
    assert {p["name"] for p in opened.payload["waiting_on"]} == {"Alex", "Lee", "Sam"}
    assert [r["away_until"] for r in opened.payload["roster"]] == ["2026-10-24"]
    house.clock.now = REMIND
    await house.tick()
    reminded = {e.payload["member"]["name"] for e in await house.outbox() if e.kind == "response_reminder"}
    assert reminded == {"Alex", "Lee", "Sam"}  # FR-11: Kim answered, by being away


async def test_calls_before_and_after_the_window_still_ask(house: House) -> None:
    await set_away(house, "Kim", FRI, SAT)
    today = await open_dinner(house)
    assert await answer(house, today, "Kim") is None  # Thursday is before it
    fri = await act(house, calls.start_call, house["Sam"], Slot.LUNCH, FRI)
    sun = await act(house, calls.start_call, house["Sam"], Slot.LUNCH, SUN)
    for event in (fri, sun):  # posted early (issue #59): they open now
        await act(house, calls.post_now, house["Sam"], event.id)
    in_window = await answer(house, fri.id, "Kim")
    assert in_window is not None and in_window.status is Status.OUT and in_window.away_until == SUN
    assert await answer(house, sun.id, "Kim") is None  # Sunday is the day she's back


async def test_setting_a_window_answers_open_calls_but_never_changes_an_answer(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    await set_away(house, "Sam", THU, FRI)
    await set_away(house, "Kim", THU, FRI)
    sams = await answer(house, event_id, "Sam")
    assert sams is not None and sams.status is Status.IN and sams.away_until is None  # his own answer stays
    kims = await answer(house, event_id, "Kim")
    assert kims is not None and kims.status is Status.OUT and kims.away_until == SAT
    assert (await house.kinds()).count("call_updated") >= 1  # the roster shows it


async def test_an_away_member_can_still_say_in_for_that_call_only(house: House) -> None:
    await set_away(house, "Kim", THU, FRI)
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Kim"], event_id, Status.IN)
    kims = await answer(house, event_id, "Kim")
    assert kims is not None and kims.is_in and kims.away_until is None
    assert (await house.refresh("Kim")).away == Away(THU, FRI)  # the window stays
    await act(house, calls.respond, house["Kim"], event_id, Status.OUT)
    kims = await answer(house, event_id, "Kim")
    assert kims is not None and kims.away_until is None  # her own Out now
    await set_away(house, "Kim", THU, SAT)  # a new window doesn't take her answer over either
    assert (await answer(house, event_id, "Kim")) == kims


async def test_replacing_and_clearing_a_window_follows_open_calls_and_is_audited(house: House) -> None:
    event_id = await open_dinner(house)
    await set_away(house, "Kim", THU, FRI)
    await set_away(house, "Kim", THU, SAT)  # replaced: back on Sunday now
    kims = await answer(house, event_id, "Kim")
    assert kims is not None and kims.away_until == SUN
    await set_away(house, "Kim", FRI, SAT)  # no longer covers Thursday: asked again
    assert await answer(house, event_id, "Kim") is None
    await set_away(house, "Kim", THU, SAT)
    await act(house, away.clear_my_away, house["Kim"])
    assert await answer(house, event_id, "Kim") is None and (await house.refresh("Kim")).away is None
    audits = await house.audits("member.away")
    assert [a.action for a in audits] == ["member.away_set"] * 4 + ["member.away_cleared"]
    kim = f"member:{house['Kim'].id}"
    assert {(a.actor_member_id, a.target) for a in audits} == {(house["Kim"].id, kim)}
    assert audits[0].payload == {"first": "2026-10-22", "last": "2026-10-23", "from": "preferences"}
    assert audits[-1].payload == {"first": "2026-10-22", "last": "2026-10-24"}
    await act(house, away.clear_my_away, house["Kim"])  # nothing to end: no second row
    assert len(await house.audits("member.away")) == 5


async def test_away_outs_count_as_answered_so_the_call_closes_early(house: House) -> None:
    await set_away(house, "Kim", THU, THU)
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    await house.tick()
    assert (await house.event(event_id)).state is EventState.ABANDONED  # everyone out (FR-56)


async def test_a_rerun_answers_them_again_in_the_new_run(house: House) -> None:
    await set_away(house, "Kim", THU, THU)
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    await house.tick()
    house.clock.advance(timedelta(minutes=5))
    await act(house, call_admin.rerun, house["Alex"], event_id)
    event = await house.event(event_id)
    kims = await answer(house, event_id, "Kim")
    assert event.run == 2 and kims is not None and kims.away_until == FRI
    assert kims.updated_at is not None and kims.updated_at >= event.opens_at  # counts in this run (FR-56)


async def test_admin_sets_and_clears_a_members_window_and_only_an_admin_may(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, away.set_member_away, house["Alex"], house["Kim"].id, THU, FRI)
    kims = await answer(house, event_id, "Kim")
    assert kims is not None and kims.away_until == SAT
    await act(house, away.clear_member_away, house["Alex"], house["Kim"].id)
    assert await answer(house, event_id, "Kim") is None
    audits = await house.audits("member.away")
    assert [(a.action, a.actor_member_id, a.target) for a in audits] == [
        ("member.away_set", house["Alex"].id, f"member:{house['Kim'].id}"),
        ("member.away_cleared", house["Alex"].id, f"member:{house['Kim'].id}"),
    ]
    assert audits[0].payload["from"] == "admin"
    with pytest.raises(Forbidden):
        await act(house, away.set_member_away, house["Sam"], house["Kim"].id, THU, FRI)


async def test_the_day_ends_in_the_households_time_zone(house: House) -> None:
    """Chicago is UTC-5 in October: 04:30 UTC on Friday is still Thursday evening there."""
    house.clock.now = datetime(2026, 10, 23, 4, 30, tzinfo=UTC)
    await set_away(house, "Kim", THU, THU)  # Thursday hasn't passed in Chicago
    house.clock.now = datetime(2026, 10, 23, 5, 30, tzinfo=UTC)  # 00:30 Friday in Chicago
    with pytest.raises(AwayRuleViolation, match="passed already"):
        await set_away(house, "Kim", THU, THU)
    await act(house, away.clear_my_away, house["Kim"])  # over by itself: nothing to clear
    assert [a.action for a in await house.audits("member.away")] == ["member.away_set"]


async def test_away_until_on_a_call_needs_a_call_in_the_household(house: House) -> None:
    event_id = await open_dinner(house)
    updated = await act(house, away.away_until, house["Kim"], event_id, SAT)
    assert updated.away == Away(THU, FRI)
    assert (await house.audits("member.away"))[0].payload["from"] == "call"
    with pytest.raises(NotFound):
        await act(house, away.away_until, house["Kim"], event_id + 100, SAT)
    with pytest.raises(NotFound):
        await act(house, away.back_on_call, house["Kim"], event_id + 100)


# Slack ----------------------------------------------------------------------------------------------


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


async def test_away_until_in_slack_opens_a_datepicker_and_saves_the_window(slack: Slack, house: House) -> None:
    event_id = await open_dinner(house)
    await slack.click("Kim", ids.AWAY, ids.card_value(event_id, 1))  # on the call message
    (opened,) = slack.fake.of("views.open")
    view = opened.args["view"]
    assert view["callback_id"] == ids.AWAY_VIEW and view["title"]["text"] == labels.AWAY_UNTIL
    (picker,) = [b for b in view["blocks"] if b.get("block_id") == "back"]
    assert picker["element"]["type"] == "datepicker" and picker["element"]["initial_date"] == "2026-10-23"
    origin = ids.ViewOrigin(event_id, CHANNEL, run=1)
    await slack.submit("Kim", ids.AWAY_VIEW, state(back={"selected_date": "2026-10-25"}), event_id, origin=origin)
    assert (await house.refresh("Kim")).away == Away(THU, SAT)
    assert slack.ephemerals("Kim")[-1].startswith("Away until Sun, Oct 25.")
    kims = await answer(house, event_id, "Kim")
    assert kims is not None and kims.away_until == SUN


async def test_a_day_before_the_call_is_refused_in_slack(slack: Slack, house: House) -> None:
    event_id = await open_dinner(house)
    origin = ids.ViewOrigin(event_id, CHANNEL, run=1)
    await slack.submit("Kim", ids.AWAY_VIEW, state(back={"selected_date": "2026-10-22"}), event_id, origin=origin)
    assert (await house.refresh("Kim")).away is None
    assert "after this call's day" in slack.ephemerals("Kim")[-1]


async def test_im_back_on_the_card_ends_the_window_and_asks_again(slack: Slack, house: House) -> None:
    await set_away(house, "Kim", THU, FRI)
    event_id = await open_dinner(house)
    await slack.click("Kim", ids.ANSWER, ids.card_value(event_id, 1))
    card = slack.cards("Kim")[-1]
    text = str(card["blocks"])
    assert "Away until Sat, Oct 24" in text
    await slack.tap_card("Kim", ids.AWAY_BACK, card)
    assert (await house.refresh("Kim")).away is None
    assert await answer(house, event_id, "Kim") is None
    again = slack.cards("Kim")[-1]
    shown = [e["action_id"] for b in again["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert ids.AWAY in shown and ids.AWAY_BACK not in shown and "You haven't answered yet." in str(again["blocks"])


async def test_a_guest_has_no_away_window(house: House) -> None:
    from dinnerbell.domain.members import Capabilities, Member, Role

    guest = Member(99, house.household.id, "Pat", None, Role.GUEST, Capabilities(), active=True)
    with pytest.raises(Forbidden):
        await act(house, away.set_my_away, guest, THU, FRI)


# The web --------------------------------------------------------------------------------------------


def window(settings: Settings, email: str) -> tuple[date | None, date | None]:
    (row,) = [m for m in rows(settings, MemberRow) if m.email == email]
    return row.away_first, row.away_last


def test_away_until_on_the_event_page_and_im_back(admin: TestClient, migrated: Settings) -> None:
    event_id = start(admin)
    card = htmx(admin, f"/events/{event_id}/away", {"back": "2026-10-24", "from": "event"})
    assert card.status_code == 200
    text = page(card)
    assert "Away until Sat, Oct 24" in text and f'action="/events/{event_id}/back"' in text and "I'm back" in text
    assert "Away until Oct 24" in text  # the roster's chip: the admin is out, away
    assert window(migrated, "admin@example.com") == (THU, FRI)
    refused = htmx(admin, f"/events/{event_id}/away", {"back": "2026-10-22", "from": "event"})
    assert refused.status_code == 409 and "after this call's day" in page(refused)
    back = htmx(admin, f"/events/{event_id}/back", {"from": "event"})
    assert back.status_code == 200 and window(migrated, "admin@example.com") == (None, None)
    assert f'action="/events/{event_id}/away"' in back.text and 'type="date" name="back"' in back.text


def test_preferences_sets_and_clears_away_days(admin: TestClient, migrated: Settings) -> None:
    saved = submit(admin, "/preferences/away", {"first": "2026-10-23", "last": "2026-10-24"})
    assert saved.status_code == 303 and saved.headers["location"] == "/preferences?notice=away#away"
    text = page(admin.get("/preferences"))
    assert "Away from Fri, Oct 23 until Sun, Oct 25" in text and ">Clear</button>" in text
    assert 'name="first" value="2026-10-23"' in text and 'name="last" min="2026-10-22" value="2026-10-24"' in text
    for first, last, why in (
        ("2026-10-24", "2026-10-23", "can't be before the first"),
        ("2026-10-20", "2026-10-21", "passed already"),
        ("", "2026-10-21", "Pick the first day away."),
    ):
        refused = submit(admin, "/preferences/away", {"first": first, "last": last})
        assert refused.status_code == 400 and why in page(refused), why
    assert window(migrated, "admin@example.com") == (FRI, SAT)
    assert submit(admin, "/preferences/away/clear").status_code == 303
    assert window(migrated, "admin@example.com") == (None, None)
    actions = [a.action for a in rows(migrated, AuditRow) if a.action.startswith("member.away")]
    assert actions == ["member.away_set", "member.away_cleared"]


def test_admin_sets_a_members_away_days_and_a_member_cant(
    admin: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    invite(admin, "sam@example.com", "Sam")
    sign_in_as(admin, provider, "subject-sam", "sam@example.com")
    sign_in_as(admin, provider, "subject-admin", "admin@example.com")
    sam = member(migrated, "sam@example.com").id
    base = f"/admin/members/{sam}/preferences"
    assert f'action="{base}/away"' in admin.get(base).text
    saved = submit(admin, f"{base}/away", {"first": "2026-10-22", "last": "2026-10-23"})
    assert saved.status_code == 303 and saved.headers["location"] == f"{base}?notice=away#away"
    assert window(migrated, "sam@example.com") == (THU, FRI)
    assert "Away until Sat, Oct 24" in page(admin.get(base)) and "I'm back" in page(admin.get(base))
    assert submit(admin, f"{base}/away/clear").status_code == 303
    audits = [a for a in rows(migrated, AuditRow) if a.action.startswith("member.away")]
    admin_id = member(migrated, "admin@example.com").id
    assert [(a.action, a.actor_member_id, a.target) for a in audits] == [
        ("member.away_set", admin_id, f"member:{sam}"),
        ("member.away_cleared", admin_id, f"member:{sam}"),
    ]
    sign_in_as(admin, provider, "subject-sam", "sam@example.com")  # Sam can't set the admin's
    days = {"first": "2026-10-22", "last": "2026-10-23"}
    refused = submit(admin, f"/admin/members/{admin_id}/preferences/away", days)
    assert refused.status_code == 403 and window(migrated, "admin@example.com") == (None, None)
