# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fun mode (#153): the scene Dinnerbone's Den shows, each person's switch for it, and what the
pages carry with it on and off."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from dinnerbell import __version__
from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow
from dinnerbell.domain.fun import DEPTH_STEPS, Period, Scene, Season, period_at, scene, season_bounds, season_depth
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
    on = client.get("/").text
    audits = len(rows(migrated, AuditRow))
    assert 'name="fun" value="on" checked' in client.get("/preferences").text

    response = submit(client, "/me/fun", {"fun": "off"})
    assert response.status_code == 303
    assert response.headers["location"] == "/preferences?notice=settings"
    assert "Settings saved." in page(client.get(response.headers["location"]))
    assert not member(migrated, ADMIN_EMAIL).fun_mode
    off = client.get("/").text
    assert off == on.replace(FUN_ASSETS, "").replace(FUN_BODY, "")
    for path in ("/preferences", "/catalog", "/stats"):
        html = client.get(path).text
        assert "data-fun" not in html and "fun.css" not in html and "fun.js" not in html, path
    assert 'name="fun" value="off" checked' in client.get("/preferences").text
    assert len(rows(migrated, AuditRow)) == audits  # a personal setting, not audited

    assert submit(client, "/me/fun", {"fun": "on"}).status_code == 303
    assert client.get("/").text == on


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
