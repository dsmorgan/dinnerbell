# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fun mode (#153): the scene Dinnerbone's Den shows, each person's switch for it, and what the
pages carry with it on and off."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dinnerbell import __version__
from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow
from dinnerbell.domain.events import Event, EventKind, EventState
from dinnerbell.domain.fun import (
    DEPTH_STEPS,
    FIT_STEPS,
    Period,
    Pose,
    Scene,
    Season,
    fit_step,
    followed_state,
    period_at,
    pose,
    scene,
    season_bounds,
    season_depth,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.web.middleware import STATIC_CONTENT_SECURITY_POLICY
from tests.conftest import ADMIN_EMAIL, invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import submit
from tests.test_auth_flow import member, rows

# The scene ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("local", "period"),
    [
        (time(0, 0), Period.LATE),
        (time(4, 59, 59), Period.LATE),
        (time(5, 0), Period.BREAKFAST),
        (time(10, 29, 59), Period.BREAKFAST),
        (time(10, 30), Period.LUNCH),
        (time(13, 59, 59), Period.LUNCH),
        (time(14, 0), Period.AFTERNOON),
        (time(16, 59, 59), Period.AFTERNOON),
        (time(17, 0), Period.DINNER),
        (time(20, 59, 59), Period.DINNER),
        (time(21, 0), Period.LATE),
        (time(23, 59, 59), Period.LATE),
    ],
)
def test_period_edges(local: time, period: Period) -> None:
    assert period_at(local) is period


@pytest.mark.parametrize(
    ("day", "season", "start", "end"),
    [
        (date(2027, 3, 19), Season.WINTER, date(2026, 12, 21), date(2027, 3, 20)),
        (date(2027, 3, 20), Season.SPRING, date(2027, 3, 20), date(2027, 6, 21)),
        (date(2027, 6, 20), Season.SPRING, date(2027, 3, 20), date(2027, 6, 21)),
        (date(2027, 6, 21), Season.SUMMER, date(2027, 6, 21), date(2027, 9, 22)),
        (date(2027, 9, 21), Season.SUMMER, date(2027, 6, 21), date(2027, 9, 22)),
        (date(2027, 9, 22), Season.AUTUMN, date(2027, 9, 22), date(2027, 12, 21)),
        (date(2027, 12, 20), Season.AUTUMN, date(2027, 9, 22), date(2027, 12, 21)),
        (date(2027, 12, 21), Season.WINTER, date(2027, 12, 21), date(2028, 3, 20)),
        (date(2027, 12, 31), Season.WINTER, date(2027, 12, 21), date(2028, 3, 20)),
        (date(2028, 1, 1), Season.WINTER, date(2027, 12, 21), date(2028, 3, 20)),  # the year wraps
    ],
)
def test_seasons_start_on_the_equinoxes_and_solstices(day: date, season: Season, start: date, end: date) -> None:
    assert season_bounds(day) == (season, start, end)


@pytest.mark.parametrize(
    ("day", "depth"),
    [
        # Every season starts at 0 and ends at the deepest step.
        (date(2027, 3, 20), 0),
        (date(2027, 6, 20), DEPTH_STEPS),
        (date(2027, 6, 21), 0),
        (date(2027, 9, 21), DEPTH_STEPS),
        (date(2027, 9, 22), 0),
        (date(2027, 12, 20), DEPTH_STEPS),
        (date(2027, 12, 21), 0),
        (date(2028, 3, 19), DEPTH_STEPS),
        # The middle: spring has 93 days, so day 46 of it (May 5) is 4.95 steps in, rounded up.
        (date(2027, 5, 5), 5),
        (date(2027, 5, 4), 5),  # 4.84
        (date(2027, 5, 3), 5),  # 4.73
        (date(2027, 4, 29), 4),  # 4.30
        # Winter across the new year: 89 days from Dec 21, 2026, and 90 into the leap year 2028.
        (date(2027, 1, 1), 1),  # 11 days in: 1.24
        (date(2027, 2, 4), 5),  # 45 days in: 5.06
        (date(2027, 3, 19), DEPTH_STEPS),
        (date(2028, 2, 4), 5),  # 45 of 90: exactly halfway
    ],
)
def test_season_depth(day: date, depth: int) -> None:
    assert season_depth(day) == depth


def test_depth_only_grows_within_a_season_and_stays_in_range() -> None:
    day, previous = date(2026, 1, 1), None
    while day < date(2029, 1, 1):
        _, start, _ = season_bounds(day)
        depth = season_depth(day)
        assert 0 <= depth <= DEPTH_STEPS
        if previous is not None and day != start:
            assert depth >= previous, day
        if day == start:
            assert depth == 0, day
        previous, day = depth, day + timedelta(days=1)


def test_scene_is_in_the_household_time_zone() -> None:
    # 03:00 UTC on Dec 21 is still 9 PM on Dec 20 in Chicago: autumn's last evening.
    now = datetime(2026, 12, 21, 3, 0, tzinfo=UTC)
    assert scene(now, "America/Chicago") == Scene(Period.LATE, Season.AUTUMN, DEPTH_STEPS)
    assert scene(now, "UTC") == Scene(Period.LATE, Season.WINTER, 0)
    assert scene(now, "Asia/Tokyo") == Scene(Period.LUNCH, Season.WINTER, 0)  # noon on Dec 21
    assert scene(now, "America/Chicago").overnight
    assert not scene(now, "Asia/Tokyo").overnight


def test_scene_needs_an_aware_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        scene(datetime(2026, 10, 22, 18, 0), "UTC")  # noqa: DTZ001


# What he does ----------------------------------------------------------------------------------------

AUTUMN = Season.AUTUMN


@pytest.mark.parametrize(
    ("period", "call", "expected"),
    [
        # A call outranks the clock, overnight too.
        (Period.LUNCH, EventState.COLLECTING, Pose.LAPS),
        (Period.LUNCH, EventState.VOTING, Pose.WHEEL),
        (Period.LUNCH, EventState.DECIDED, Pose.EAT),
        (Period.LUNCH, EventState.DONE, Pose.SLEEP),
        (Period.LATE, EventState.COLLECTING, Pose.LAPS),
        (Period.LATE, EventState.VOTING, Pose.WHEEL),
        (Period.LATE, EventState.DECIDED, Pose.EAT),  # decided after 9 PM: up to eat
        (Period.LATE, EventState.DONE, Pose.SLEEP),
        # No call: the clock.
        (Period.BREAKFAST, None, Pose.NIBBLE),
        (Period.LUNCH, None, Pose.POTTER),
        (Period.AFTERNOON, None, Pose.DOZE),
        (Period.DINNER, None, Pose.WAIT),
        (Period.LATE, None, Pose.SLEEP),
        # A state he never follows reads as no call.
        (Period.DINNER, EventState.SCHEDULED, Pose.WAIT),
        (Period.DINNER, EventState.CANCELLED, Pose.WAIT),
    ],
)
def test_pose(period: Period, call: EventState | None, expected: Pose) -> None:
    assert pose(Scene(period, AUTUMN, 3), call) is expected


CHICAGO = "America/Chicago"
NOW = datetime(2026, 10, 22, 23, 0, tzinfo=UTC)  # 6 PM in Chicago: dinner


def event(
    event_id: int,
    state: EventState,
    *,
    kind: EventKind = EventKind.CALL,
    slot: Slot = Slot.DINNER,
    day: date = date(2026, 10, 22),
    done_at: datetime | None = None,
) -> Event:
    return Event(
        id=event_id,
        household_id=1,
        kind=kind,
        slot=slot,
        day=day,
        state=state,
        run=1,
        opens_at=NOW,
        respond_deadline=NOW,
        reminder_minutes=15,
        vote_minutes=45,
        done_at=done_at,
    )


def test_he_follows_the_first_open_call() -> None:
    lunch_done = event(1, EventState.DONE, slot=Slot.LUNCH, done_at=NOW - timedelta(minutes=5))
    dinner = event(2, EventState.VOTING)
    tomorrow = event(3, EventState.COLLECTING, day=date(2026, 10, 23))
    assert followed_state([lunch_done, dinner, tomorrow], NOW, CHICAGO) is EventState.VOTING
    assert followed_state([lunch_done, tomorrow], NOW, CHICAGO) is EventState.COLLECTING
    assert followed_state([event(4, EventState.DECIDED), dinner], NOW, CHICAGO) is EventState.DECIDED


def test_offers_and_calls_not_posted_or_ended_dont_count() -> None:
    others = [
        event(1, EventState.COLLECTING, kind=EventKind.OFFER),
        event(2, EventState.SCHEDULED),
        event(3, EventState.CANCELLED),
        event(4, EventState.ABANDONED),
    ]
    assert followed_state(others, NOW, CHICAGO) is None
    assert followed_state([], NOW, CHICAGO) is None


@pytest.mark.parametrize(
    ("done_at", "expected"),
    [
        (NOW - timedelta(minutes=30), EventState.DONE),  # 5:30 PM, in dinner time: he's asleep
        (NOW - timedelta(hours=1, minutes=1), None),  # 4:59 PM, in the afternoon: dinner time wakes him
        (NOW - timedelta(days=1), None),  # yesterday's
        (NOW + timedelta(minutes=1), None),  # never ahead of now
    ],
)
def test_a_done_call_keeps_him_asleep_for_the_rest_of_its_time_of_day(
    done_at: datetime, expected: EventState | None
) -> None:
    assert followed_state([event(1, EventState.DONE, done_at=done_at)], NOW, CHICAGO) is expected
    assert followed_state([event(1, EventState.DONE)], NOW, CHICAGO) is None  # no time recorded


def test_overnight_a_dinner_done_at_eight_still_has_him_asleep() -> None:
    late = datetime(2026, 10, 23, 3, 30, tzinfo=UTC)  # 10:30 PM in Chicago
    dinner = event(1, EventState.DONE, done_at=late - timedelta(hours=2, minutes=30))
    state = followed_state([dinner], late, CHICAGO)
    assert state is None and pose(scene(late, CHICAGO), state) is Pose.SLEEP


@pytest.mark.parametrize(
    ("text", "step"),
    [
        ("Hi, Al", 8),
        ("Hi, David", 10),
        ("Hi, Alexandria-Marguerite", 28),
        ("Hi, " + "x" * 36, 40),
        ("Hi, " + "x" * 80, 40),
        ("Hi, 山田太郎", 12),  # four wide characters count as eight
    ],
)
def test_fit_step(text: str, step: int) -> None:
    assert fit_step(text) == step
    assert step in FIT_STEPS


# The pages ----------------------------------------------------------------------------------------

FUN_ASSETS = (
    f'\n  <link rel="stylesheet" href="/static/fun.css?v={__version__}">'
    f'\n  <script src="/static/fun.js?v={__version__}" defer></script>'
)
# The test clock starts at 18:00 UTC on Thursday, Oct 22, 2026: 1 PM in Chicago, 30 days into autumn.
FUN_BODY = ' data-fun="on" data-period="lunch" data-season="autumn" data-season-depth="3"'


def test_fun_mode_is_on_by_default_on_every_signed_in_page(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    for path in ("/", "/preferences", "/catalog", "/history", "/stats", "/schedule", "/admin/members"):
        html = client.get(path).text
        assert FUN_ASSETS in html, path
        assert FUN_BODY + ">" in html, path
    assert member(migrated, ADMIN_EMAIL).fun_mode


def test_signed_out_pages_carry_no_fun(client: TestClient) -> None:
    html = client.get("/").text
    assert "data-fun" not in html and "fun.css" not in html and "fun.js" not in html


def test_the_scene_follows_the_household_clock(client: TestClient, provider: FakeProvider, clock: FakeClock) -> None:
    clock.now = datetime(2027, 1, 10, 3, 0, tzinfo=UTC)  # 9 PM on Jan 9 in Chicago, 19 days into winter
    sign_in(client, provider)
    assert ' data-period="late" data-season="winter" data-season-depth="2">' in client.get("/").text
    clock.advance(timedelta(hours=9))  # 6 AM
    assert ' data-period="breakfast" data-season="winter" data-season-depth="2">' in client.get("/").text


def test_turning_it_off_leaves_the_page_as_it_was(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    on, today_on = client.get("/catalog").text, client.get("/").text
    audits = len(rows(migrated, AuditRow))
    assert 'name="fun" value="on" checked' in client.get("/preferences").text
    assert 'class="den"' in today_on and 'class="greeting"' in today_on and 'class="edge-wx"' in today_on

    response = submit(client, "/me/fun", {"fun": "off"})
    assert response.status_code == 303
    assert response.headers["location"] == "/preferences?notice=fun#fun-mode"
    card = page(client.get(response.headers["location"])).split('id="fun-mode"', 1)[1].split("</form>", 1)[0]
    assert '<p class="alert ok" role="status">Saved.</p>' in card  # the notice is at the card
    assert "Settings saved." not in page(client.get(response.headers["location"]))
    assert not member(migrated, ADMIN_EMAIL).fun_mode
    assert client.get("/catalog").text == on.replace(FUN_ASSETS, "").replace(FUN_BODY, "")
    today_off = client.get("/").text
    for html in [today_off] + [client.get(path).text for path in ("/preferences", "/stats")]:
        assert "data-fun" not in html and "fun.css" not in html and "fun.js" not in html
        assert 'class="den' not in html and "den.svg" not in html and "edge-wx" not in html
    assert f"<h1>Hi, {member(migrated, ADMIN_EMAIL).display_name}</h1>\n" in today_off
    assert 'name="fun" value="off" checked' in client.get("/preferences").text
    assert len(rows(migrated, AuditRow)) == audits  # a personal setting, not audited

    assert submit(client, "/me/fun", {"fun": "on"}).status_code == 303
    assert client.get("/catalog").text == on
    assert client.get("/").text == today_on


def test_it_changes_only_your_own(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    assert invite(client, "sam@example.com", "Sam").status_code == 303
    submit(client, "/me/fun", {"fun": "off", "member_id": str(member(migrated, "sam@example.com").id)})
    assert not member(migrated, ADMIN_EMAIL).fun_mode
    assert member(migrated, "sam@example.com").fun_mode


def test_the_switch_needs_csrf_and_a_choice(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    assert client.post("/me/fun", data={"fun": "off"}).status_code == 403
    assert member(migrated, ADMIN_EMAIL).fun_mode
    for value in ("", "yes", "OFF"):
        response = submit(client, "/me/fun", {"fun": value})
        assert response.status_code == 400, value
        assert "Choose On or Off." in page(response)
    assert member(migrated, ADMIN_EMAIL).fun_mode


def test_signed_out_the_switch_sends_you_to_sign_in(client: TestClient) -> None:
    response = client.post("/me/fun", data={"fun": "off"})
    assert (response.status_code, response.headers["location"]) == (303, "/")


def test_fun_assets_are_static_files_with_the_static_policy(client: TestClient) -> None:
    for name, kind in (("fun.css", "text/css"), ("fun.js", "javascript")):
        response = client.get(f"/static/{name}")
        assert response.status_code == 200, name
        assert kind in response.headers["content-type"], name
        assert response.headers["content-security-policy"] == STATIC_CONTENT_SECURITY_POLICY, name
        assert response.headers.get("cache-control") != "no-store", name
        assert "SPDX-License-Identifier: AGPL-3.0-or-later" in response.text.splitlines()[0], name


@pytest.mark.parametrize("settings_overrides", [{"build": "abc1234"}])
def test_fun_assets_carry_the_cache_buster(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    html = client.get("/").text
    assert f"/static/fun.css?v={__version__}%2Babc1234" in html
    assert f"/static/fun.js?v={__version__}%2Babc1234" in html


# The den on Today ------------------------------------------------------------------------------------

STATIC = Path(__file__).resolve().parents[1] / "src" / "dinnerbell" / "web" / "static"
FUN_CSS = (STATIC / "fun.css").read_text()


def den(html: str) -> str:
    return html.split('<div class="den"', 1)[1].split('<div class="den-wx">', 1)[0]


def test_today_has_the_den_beside_the_greeting(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    html = client.get("/").text
    # 1 PM in Chicago with no call: he potters about.
    assert '<div class="greeting" data-fit="10">\n  <h1>Hi, admin</h1>' in html
    assert '<div class="den" data-pose="potter" aria-hidden="true">' in html
    assert html.count('<div class="edge-wx" aria-hidden="true">') == 1
    assert "style=" not in html
    for path in ("/catalog", "/preferences", "/stats"):
        assert 'class="den' not in client.get(path).text, path  # Today only


def test_the_den_follows_the_call(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    response = submit(client, "/events", {"slot": "dinner", "day": "2026-10-22", "post_now": "1"})
    assert response.status_code == 303, page(response)
    assert 'data-pose="laps"' in client.get("/").text


def test_every_part_the_den_uses_is_in_the_sprite(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    html = client.get("/").text
    used = set(re.findall(r'<use (?:class="[^"]*" )?href="/static/den\.svg\?v=[^"#]+#([\w-]+)"', html))
    sprite = ElementTree.fromstring((STATIC / "den.svg").read_text())  # noqa: S314 - our own file
    symbols = {el.get("id") for el in sprite.iter("{http://www.w3.org/2000/svg}symbol")}
    assert used and used == symbols
    assert html.count("<use ") == len(re.findall(r"<use (?:class=\"[^\"]*\" )?href=\"/static/den\.svg\?v=", html))
    response = client.get("/static/den.svg")
    assert response.status_code == 200 and response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["content-security-policy"] == STATIC_CONTENT_SECURITY_POLICY


def test_fun_css_draws_every_pose_period_and_season() -> None:
    for value in Pose:
        assert f'[data-pose="{value}"]' in FUN_CSS, value
    for value in Period:
        assert f'body[data-period="{value}"]' in FUN_CSS, value
    for value in Season:
        assert f'body[data-season="{value}"]' in FUN_CSS, value
    for depth in range(DEPTH_STEPS + 1):
        assert f'body[data-season-depth="{depth}"]' in FUN_CSS, depth
    for step in FIT_STEPS:
        assert f'.greeting[data-fit="{step}"]' in FUN_CSS, step


def test_nothing_moves_unless_motion_is_allowed() -> None:
    """Every animation is inside the one prefers-reduced-motion: no-preference block, so under
    reduced motion the den is a still picture."""
    start = FUN_CSS.index("@media (prefers-reduced-motion: no-preference) {")
    end = FUN_CSS.index("\n}\n", start)
    outside = FUN_CSS[:start] + FUN_CSS[end:]
    assert "animation" not in outside.replace("@keyframes", "")
    assert "transition" not in FUN_CSS


@pytest.mark.parametrize("settings_overrides", [{"build": "abc1234"}])
def test_the_sprite_carries_the_cache_buster(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    assert f'href="/static/den.svg?v={__version__}%2Babc1234#nook"' in client.get("/").text


def test_the_den_stays_under_the_top_bar_and_fits_a_narrow_row() -> None:
    """The den isolates its layers, or they paint over the sticky top bar when Today scrolls
    (checked in Chromium, WebKit, and Firefox on #164). It's a container, so a narrow den drops
    the wheel; and without container units the greeting keeps its usual size."""
    rule = FUN_CSS.split("\n.den {", 1)[1].split("}", 1)[0]
    assert "isolation: isolate;" in rule and "container: den / inline-size;" in rule
    assert "@container den (max-width: 130px)" in FUN_CSS
    assert "@supports not (width: 1cqi) {\n  .greeting h1 { font-size: 1.6rem; }" in FUN_CSS
