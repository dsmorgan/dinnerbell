# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dinnerbone's weather (#319, ADR 0011): the seasonal choice, the WMO mapping, Open-Meteo's
egress rules, Admin's setting, the scheduler's refresh, its failures and logs, and what the
pages carry. No test reaches the network: Open-Meteo is ``tests.support.weather.FakeOpenMeteo``
or a mock transport."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import LocalWeatherRow
from dinnerbell.domain.events import EventState
from dinnerbell.domain.fun import (
    SEASONAL_PATTERNS,
    Fall,
    Period,
    Pile,
    Pose,
    React,
    Scene,
    Season,
    day_pattern,
    pose,
    scene,
    season_bounds,
    seasonal_weather,
    weather_day,
    weather_draw,
)
from dinnerbell.domain.weather import (
    ALLOWED_HOSTS,
    FORECAST_HOST,
    GEOCODING_HOST,
    MAX_ANSWER_BYTES,
    REFRESH_MAX,
    REFRESH_MIN,
    WMO_SKIES,
    Feel,
    InvalidLocation,
    LocalWeather,
    Observation,
    Place,
    Sky,
    Weather,
    WeatherError,
    WeatherFailure,
    check_url,
    forecast_url,
    geocoding_url,
    is_fresh,
    next_fetch,
    normalize_location,
    parse_current,
    parse_place,
    weather_for,
)
from dinnerbell.scheduler.runner import WEATHER_INTERVAL, build_scheduler, refresh_weather
from dinnerbell.services import local_weather
from dinnerbell.services.open_meteo import HEADERS, OpenMeteo
from dinnerbell.services.safe_fetch import USER_AGENT
from tests.conftest import invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.scheduler import settle
from tests.support.weather import OTHER_POSTAL_CODE, PLACE, POSTAL_CODE, FakeOpenMeteo, current, json_response
from tests.support.web import submit
from tests.test_admin import sign_in_as
from tests.test_auth_flow import member, rows
from tests.test_web_app import audits

CHICAGO = "America/Chicago"
STATIC = Path(__file__).resolve().parents[1] / "src" / "dinnerbell" / "web" / "static"
FUN_CSS = (STATIC / "fun.css").read_text()
FUN_JS = (STATIC / "fun.js").read_text()
ROUNDED = Place(40.1, -75.2, "Elmford, Northshire")
# The test clock: 1 PM in Chicago on Oct 22, 2026, when household 1's seasonal weather is crisp
# and clear (tests/test_fun.py).
SEASONAL = 'data-weather="clear" data-feel="none" data-fall="leaves" data-pile="leaves" data-react="none"'


# The seasonal choice ------------------------------------------------------------------------------


def test_the_draw_is_stable_for_a_household_and_day_and_differs_between_them() -> None:
    day = date(2026, 10, 22)
    assert weather_draw(1, day) == weather_draw(1, day)  # a digest: every process and restart agree
    assert 0 <= weather_draw(1, day) < 1
    assert len({weather_draw(1, day + timedelta(days=n)) for n in range(30)}) == 30
    assert len({weather_draw(h, day) for h in range(1, 31)}) == 30
    # Every member sees the same: it depends on nothing but the household and the moment.
    now = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
    assert scene(now, CHICAGO, household_id=1) == scene(now, CHICAGO, household_id=1)


@pytest.mark.parametrize("season", list(Season))
def test_weather_is_spread_across_each_season(season: Season) -> None:
    """Every pattern turns up over a season, for several households, and none takes over."""
    start = {
        Season.SPRING: date(2027, 3, 20),
        Season.SUMMER: date(2027, 6, 21),
        Season.AUTUMN: date(2026, 9, 22),
        Season.WINTER: date(2026, 12, 21),
    }[season]
    _, first, end = season_bounds(start)
    days = [first + timedelta(days=n) for n in range((end - first).days)]
    for household_id in (1, 2, 7):
        counts = Counter(day_pattern(household_id, day).name for day in days)
        assert set(counts) == {p.name for p in SEASONAL_PATTERNS[season]}, household_id
        assert max(counts.values()) < 0.45 * len(days), counts
        assert len({seasonal_weather(household_id, day, Period.LUNCH) for day in days}) >= 4


def test_each_seasons_patterns_fit_it() -> None:
    skies = {season: {p.at(period).sky for p in SEASONAL_PATTERNS[season] for period in Period} for season in Season}
    assert {Sky.SNOW, Sky.HEAVY_SNOW, Sky.SLEET, Sky.FOG} <= skies[Season.WINTER]
    assert not {Sky.RAIN, Sky.THUNDERSTORM} & skies[Season.WINTER]
    assert {Sky.DRIZZLE, Sky.RAIN, Sky.FOG, Sky.CLEAR} <= skies[Season.SPRING]
    assert Sky.THUNDERSTORM in skies[Season.SUMMER] and not {Sky.SNOW, Sky.SLEET} & skies[Season.SUMMER]
    assert {Sky.RAIN, Sky.FOG, Sky.CLEAR} <= skies[Season.AUTUMN]
    feels = {season: {p.at(period).feel for p in SEASONAL_PATTERNS[season] for period in Period} for season in Season}
    assert Feel.HOT in feels[Season.SUMMER] and Feel.WINDY in feels[Season.SPRING] | feels[Season.AUTUMN]
    assert Feel.HOT not in feels[Season.WINTER] and Feel.COLD not in feels[Season.SUMMER]


def test_the_weather_can_change_within_a_day_without_changing_on_reload() -> None:
    """A morning fog clears by lunch, and a summer afternoon storms: by period, never by load."""
    fog = next(p for p in SEASONAL_PATTERNS[Season.SPRING] if p.name == "fog in the morning")
    assert fog.at(Period.BREAKFAST).sky is Sky.FOG and fog.at(Period.LUNCH).sky is Sky.CLEAR
    storm = next(p for p in SEASONAL_PATTERNS[Season.SUMMER] if p.name == "a thunderstorm in the afternoon")
    assert storm.at(Period.AFTERNOON).sky is Sky.THUNDERSTORM and storm.at(Period.LUNCH).sky is not Sky.THUNDERSTORM
    morning = datetime(2027, 4, 1, 12, 0, tzinfo=UTC)  # 7 AM in Chicago
    assert {scene(morning + timedelta(minutes=m), CHICAGO, household_id=3) for m in range(0, 180, 20)} == {
        scene(morning, CHICAGO, household_id=3)
    }


def test_the_night_belongs_to_the_day_it_started() -> None:
    evening = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)  # 10 PM Oct 22 in Chicago
    small_hours = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)  # 4 AM Oct 23
    breakfast = datetime(2026, 10, 23, 11, 0, tzinfo=UTC)  # 6 AM Oct 23
    local = ZoneInfo(CHICAGO)
    assert weather_day(evening.astimezone(local)) == weather_day(small_hours.astimezone(local)) == date(2026, 10, 22)
    assert weather_day(breakfast.astimezone(local)) == date(2026, 10, 23)
    assert scene(evening, CHICAGO, household_id=5).weather == scene(small_hours, CHICAGO, household_id=5).weather


# What the den draws for a weather ----------------------------------------------------------------


def at(season: Season, sky: Sky, feel: Feel | None = None, period: Period = Period.LUNCH) -> Scene:
    return Scene(period, season, 5, Weather(sky, feel))


@pytest.mark.parametrize(
    ("scene_", "fall", "pile", "react"),
    [
        (at(Season.WINTER, Sky.SNOW), Fall.SNOW, Pile.SNOW, React.SHAKE),
        (at(Season.WINTER, Sky.HEAVY_SNOW), Fall.SNOW, Pile.SNOW, React.SHAKE),
        (at(Season.WINTER, Sky.CLEAR, Feel.COLD), None, Pile.SNOW, None),
        (at(Season.WINTER, Sky.SLEET), Fall.SLEET, Pile.SNOW, React.UMBRELLA),
        (at(Season.WINTER, Sky.RAIN), Fall.RAIN, Pile.PUDDLE, React.UMBRELLA),  # rain washes the snow away
        (at(Season.WINTER, Sky.CLEAR, Feel.HOT), None, None, React.SHADES),  # and so does heat
        (at(Season.SPRING, Sky.RAIN), Fall.RAIN, Pile.PUDDLE, React.UMBRELLA),
        (at(Season.SPRING, Sky.DRIZZLE), Fall.RAIN, Pile.PUDDLE, React.UMBRELLA),
        (at(Season.SPRING, Sky.CLEAR), None, None, None),  # no puddle on a dry day
        (at(Season.SPRING, Sky.PARTLY_CLOUDY, Feel.WINDY), Fall.PETALS, None, React.SCARF),
        (at(Season.SUMMER, Sky.CLEAR, Feel.HOT), None, None, React.SHADES),
        (at(Season.SUMMER, Sky.CLEAR, Feel.HOT, Period.DINNER), None, None, React.FAN),
        (at(Season.SUMMER, Sky.THUNDERSTORM), Fall.RAIN, Pile.PUDDLE, React.UMBRELLA),
        (at(Season.SUMMER, Sky.OVERCAST, Feel.WINDY), Fall.GUSTS, None, React.SCARF),
        (at(Season.AUTUMN, Sky.CLEAR), Fall.LEAVES, Pile.LEAVES, None),
        (at(Season.AUTUMN, Sky.FOG), None, Pile.LEAVES, None),
        (
            at(Season.AUTUMN, Sky.PARTLY_CLOUDY, Feel.WINDY),
            Fall.LEAVES,
            None,
            React.SCARF,
        ),  # the wind scatters the pile
        (at(Season.AUTUMN, Sky.RAIN), Fall.RAIN, Pile.LEAVES, React.UMBRELLA),
        (at(Season.AUTUMN, Sky.SNOW), Fall.SNOW, None, React.SHAKE),
    ],
)
def test_what_falls_lies_and_how_he_reacts(scene_: Scene, fall: Fall, pile: Pile, react: React) -> None:
    assert (scene_.fall, scene_.pile, scene_.react) == (fall, pile, react)


@pytest.mark.parametrize("period", list(Period))
def test_a_thunderstorm_sends_him_into_the_straw_unless_hes_busy_or_asleep(period: Period) -> None:
    storm = at(Season.SUMMER, Sky.THUNDERSTORM, period=period)
    assert pose(storm, None) is (Pose.SLEEP if period is Period.LATE else Pose.PEEK)
    for state, busy in (
        (EventState.COLLECTING, Pose.LAPS),
        (EventState.VOTING, Pose.WHEEL),
        (EventState.DECIDED, Pose.EAT),
    ):
        assert pose(storm, state) is busy
    assert pose(at(Season.SUMMER, Sky.RAIN, period=period), None) is not Pose.PEEK


# Open-Meteo's answer ---------------------------------------------------------------------------------


def test_every_documented_wmo_code_maps_to_a_den_weather() -> None:
    documented = {
        0,
        1,
        2,
        3,
        45,
        48,
        51,
        53,
        55,
        56,
        57,
        61,
        63,
        65,
        66,
        67,
        71,
        73,
        75,
        77,
        80,
        81,
        82,
        85,
        86,
        95,
        96,
        97,
        99,
    }
    assert set(WMO_SKIES) == documented
    assert set(WMO_SKIES.values()) == set(Sky)  # and every den weather has a code
    mild = {code: weather_for(Observation(code, 12.0, 5.0)) for code in documented}
    assert mild[0] == Weather(Sky.CLEAR, None, local=True)
    assert mild[45] and mild[45].sky is Sky.FOG
    assert mild[57] and mild[57].sky is Sky.SLEET and mild[67] and mild[67].sky is Sky.SLEET
    assert mild[65] and mild[65].sky is Sky.HEAVY_RAIN and mild[82] and mild[82].sky is Sky.HEAVY_RAIN
    assert mild[86] and mild[86].sky is Sky.HEAVY_SNOW and mild[77] and mild[77].sky is Sky.SNOW
    assert all(w and w.sky is Sky.THUNDERSTORM for c, w in mild.items() if c >= 95)
    assert weather_for(Observation(4, 12.0, 5.0)) is None  # outside the table: no sense


def test_temperature_and_wind_adjust_a_few_weathers() -> None:
    assert weather_for(Observation(0, 30.0, 5.0)) == Weather(Sky.CLEAR, Feel.HOT, local=True)
    assert weather_for(Observation(3, -2.0, 5.0)) == Weather(Sky.OVERCAST, Feel.COLD, local=True)
    assert weather_for(Observation(2, 30.0, 40.0)) == Weather(Sky.PARTLY_CLOUDY, Feel.WINDY, local=True)
    assert weather_for(Observation(61, 30.0, 5.0)) == Weather(Sky.RAIN, None, local=True)  # rain isn't hot
    assert weather_for(Observation(95, 20.0, 60.0)) == Weather(Sky.THUNDERSTORM, None, local=True)


@pytest.mark.parametrize(
    "answer",
    [
        None,
        [],
        {},
        {"current": None},
        {"current": {"weather_code": 61, "temperature_2m": 12}},
        {"current": {"weather_code": 4, "temperature_2m": 12, "wind_speed_10m": 3}},
        {"current": {"weather_code": True, "temperature_2m": 12, "wind_speed_10m": 3}},
        {"current": {"weather_code": "61", "temperature_2m": 12, "wind_speed_10m": 3}},
        {"current": {"weather_code": 61, "temperature_2m": float("nan"), "wind_speed_10m": 3}},
        {"current": {"weather_code": 61, "temperature_2m": 99, "wind_speed_10m": 3}},
        {"current": {"weather_code": 61, "temperature_2m": 12, "wind_speed_10m": -1}},
        {"current": {"weather_code": 61, "temperature_2m": False, "wind_speed_10m": 3}},
    ],
)
def test_an_answer_that_makes_no_sense_is_refused(answer: Any) -> None:
    with pytest.raises(WeatherError) as caught:
        parse_current(answer)
    assert caught.value.reason is WeatherFailure.BAD_ANSWER


def test_a_sensible_answer_is_read() -> None:
    assert parse_current(current(63, 4.5, 12.0)) == Observation(63, 4.5, 12.0)
    assert parse_current({"current": {"weather_code": 3.0, "temperature_2m": 1, "wind_speed_10m": 0}}).code == 3


def test_geocoding_finds_the_place_rounded_or_says_why_not() -> None:
    assert parse_place({"results": [PLACE]}) == ROUNDED
    assert parse_place({"results": [PLACE | {"admin1": "Elmford"}]}).name == "Elmford"
    for empty in ({"generationtime_ms": 0.1}, {"results": []}):
        with pytest.raises(WeatherError) as caught:
            parse_place(empty)
        assert caught.value.reason is WeatherFailure.NOT_FOUND
    for bad in (
        None,
        {"results": "x"},
        {"results": [1]},
        {"results": [PLACE | {"latitude": 95}]},
        {"results": [{"latitude": 1, "longitude": 2}]},
    ):
        with pytest.raises(WeatherError) as caught:
            parse_place(bad)
        assert caught.value.reason is WeatherFailure.BAD_ANSWER


# The postal code -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "country", "expected"),
    [
        ("12345", "US", ("12345", "US")),
        (" 12345 ", "", ("12345", "US")),
        ("12345-6789", "us", ("12345", "US")),  # ZIP+4 sends only the ZIP
        ("123456789", "US", ("12345", "US")),
        ("k1a 0b1", "CA", ("K1A 0B1", "CA")),
        ("SW1A", "GB", ("SW1A", "GB")),
        ("10115", "DE", ("10115", "DE")),
    ],
)
def test_valid_postal_codes(code: str, country: str, expected: tuple[str, str]) -> None:
    assert normalize_location(code, country) == expected


@pytest.mark.parametrize(
    ("code", "country", "message"),
    [
        ("", "US", "Enter a ZIP or postal code"),
        ("1234", "US", "5-digit ZIP code"),
        ("ABCDE", "US", "5-digit ZIP code"),
        ("12345", "USA", "two letters"),
        ("12345", "U1", "two letters"),
        ("A", "CA", "2 to 10"),
        ("K1A_0B1", "CA", "2 to 10"),
        ("12345678901", "DE", "2 to 10"),
        ("<script>", "FR", "2 to 10"),
    ],
)
def test_invalid_postal_codes_are_refused(code: str, country: str, message: str) -> None:
    with pytest.raises(InvalidLocation, match=message):
        normalize_location(code, country)


# Egress --------------------------------------------------------------------------------------------


def test_only_https_to_the_two_hosts_on_443() -> None:
    assert {GEOCODING_HOST, FORECAST_HOST} == ALLOWED_HOSTS
    assert check_url(forecast_url(40.12, -75.24)).startswith(f"https://{FORECAST_HOST}/v1/forecast?")
    assert check_url(geocoding_url("12345", "US")).startswith(f"https://{GEOCODING_HOST}/v1/search?")
    for url in (
        f"http://{FORECAST_HOST}/v1/forecast",
        f"https://{FORECAST_HOST}:8443/v1/forecast",
        f"https://user@{FORECAST_HOST}/v1/forecast",
        "https://open-meteo.example/v1/forecast",
        "https://api.open-meteo.com.example/v1/forecast",
        "https://1.2.3.4/v1/forecast",
        "not a url",
    ):
        with pytest.raises(WeatherError) as caught:
            check_url(url)
        assert caught.value.reason is WeatherFailure.REFUSED, url


def test_the_forecast_sends_only_rounded_coordinates() -> None:
    params = httpx.URL(forecast_url(40.1234, -75.2468)).params
    assert (params["latitude"], params["longitude"]) == ("40.1", "-75.2")
    assert set(params) == {"latitude", "longitude", "current", "temperature_unit", "wind_speed_unit", "timezone"}
    assert httpx.URL(geocoding_url("12345", "US")).params["countryCode"] == "US"


def fetcher(handler: Any, **kwargs: Any) -> OpenMeteo:
    return OpenMeteo(httpx.MockTransport(handler), **kwargs)


@pytest.mark.anyio
async def test_requests_carry_no_cookies_and_a_fixed_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == GEOCODING_HOST:
            return json_response({"results": [PLACE]})
        return json_response(current(95))

    weather = fetcher(handler)
    assert await weather.geocode("12345", "US") == ROUNDED
    assert await weather.current(ROUNDED) == Observation(95, 12.5, 8.0)
    assert [r.url.scheme for r in seen] == ["https", "https"]
    for request in seen:
        assert request.headers["user-agent"] == USER_AGENT == HEADERS["User-Agent"]
        assert request.headers["accept"] == "application/json"
        assert "cookie" not in request.headers and "authorization" not in request.headers
    assert "12345" not in str(seen[1].url)


async def failure_of(response: httpx.Response | Exception, **kwargs: Any) -> tuple[WeatherFailure, int]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    with pytest.raises(WeatherError) as caught:
        await fetcher(handler, **kwargs).current(ROUNDED)
    return caught.value.reason, len(calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(302, headers={"location": f"https://{FORECAST_HOST}/elsewhere"}), WeatherFailure.REDIRECT),
        (httpx.Response(301, headers={"location": "https://evil.example/"}), WeatherFailure.REDIRECT),
        (json_response({"reason": "busy"}, status=503), WeatherFailure.STATUS),
        (json_response({"reason": "nope"}, status=429), WeatherFailure.STATUS),
        (json_response(current(), content_type="text/html"), WeatherFailure.CONTENT_TYPE),
        (
            httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"}),
            WeatherFailure.BAD_JSON,
        ),
        (
            httpx.Response(200, content=b"\xff\xfe", headers={"content-type": "application/json"}),
            WeatherFailure.BAD_JSON,
        ),
        (json_response({"current": {}}), WeatherFailure.BAD_ANSWER),
        (httpx.ConnectError("down"), WeatherFailure.NETWORK),
        (httpx.ReadTimeout("slow"), WeatherFailure.TIMEOUT),
    ],
)
async def test_each_failure_has_its_reason_and_is_never_followed(response: Any, reason: WeatherFailure) -> None:
    assert await failure_of(response) == (reason, 1)  # one request: no redirect followed, no retry


@pytest.mark.anyio
async def test_an_answer_past_the_size_cap_is_refused() -> None:
    big = b" " * (MAX_ANSWER_BYTES + 1)
    declared = httpx.Response(200, content=big, headers={"content-type": "application/json"})
    assert await failure_of(declared) == (WeatherFailure.TOO_BIG, 1)
    streamed = httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(big))
    assert "content-length" not in streamed.headers
    assert await failure_of(streamed) == (WeatherFailure.TOO_BIG, 1)


@pytest.mark.anyio
async def test_a_hung_answer_times_out() -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    with pytest.raises(WeatherError) as caught:
        await fetcher(hang, deadline=0.05).current(ROUNDED)
    assert caught.value.reason is WeatherFailure.TIMEOUT


@pytest.mark.anyio
async def test_a_url_outside_the_allowlist_is_never_sent() -> None:
    sent: list[httpx.Request] = []
    weather = fetcher(lambda request: sent.append(request) or json_response({}))
    with pytest.raises(WeatherError) as caught:
        await weather._get("https://evil.example/v1/forecast")
    assert caught.value.reason is WeatherFailure.REFUSED and sent == []


# When to ask -----------------------------------------------------------------------------------------


def test_the_next_fetch_is_one_to_three_hours_away_and_randomized() -> None:
    now = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
    assert next_fetch(now, 0.0) - now == REFRESH_MIN == timedelta(hours=1)
    assert next_fetch(now, 1.0) - now == REFRESH_MAX - WEATHER_INTERVAL == timedelta(hours=2, minutes=55)
    assert next_fetch(now, 0.5) - now == timedelta(hours=1, minutes=57, seconds=30)
    import random

    draws = [next_fetch(now, random.random()) - now for _ in range(200)]  # noqa: S311 - like the job's own draw
    assert all(REFRESH_MIN <= d <= REFRESH_MAX - WEATHER_INTERVAL for d in draws)
    assert len(set(draws)) > 150  # not on a fixed clock
    for bad in (-0.1, 1.1):
        with pytest.raises(ValueError, match="from 0 to 1"):
            next_fetch(now, bad)


def realized_gap(lag: timedelta, draw: float) -> timedelta:
    """The time between two fetches as they happen. The scheduler checks every interval from a
    tick; the check that claims a fetch reads the clock ``lag`` after its tick, and the next fetch
    runs at the first tick at or after its drawn time."""
    tick = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
    claimed = tick + lag
    due = next_fetch(claimed, draw)
    checks = -(-(due - tick) // WEATHER_INTERVAL)  # ceiling division
    return tick + checks * WEATHER_INTERVAL - claimed


LAGS = [timedelta(0), timedelta(seconds=1), timedelta(seconds=30), WEATHER_INTERVAL - timedelta(seconds=1)]


@pytest.mark.parametrize("draw", [0.0, 1e-9, 0.5, 1 - 1e-9, 1.0])
@pytest.mark.parametrize("lag", LAGS)
def test_fetches_as_they_happen_are_one_to_three_hours_apart(draw: float, lag: timedelta) -> None:
    """A drawn time is reached up to one check late, and the draw leaves room for that."""
    assert REFRESH_MIN <= realized_gap(lag, draw) <= REFRESH_MAX


def test_the_extremes_reach_one_and_three_hours() -> None:
    assert min(realized_gap(lag, 0.0) for lag in LAGS) == REFRESH_MIN
    assert max(realized_gap(lag, 1.0) for lag in LAGS) == REFRESH_MAX - timedelta(seconds=1)
    assert max(realized_gap(lag, 1.0) for lag in LAGS) > REFRESH_MAX - WEATHER_INTERVAL


def test_an_answer_is_good_for_six_hours() -> None:
    now = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
    assert is_fresh(now - timedelta(hours=6), now) and not is_fresh(now - timedelta(hours=6, seconds=1), now)
    assert not is_fresh(None, now) and not is_fresh(now + timedelta(minutes=1), now)
    stored = LocalWeather(1, True, "12345", "US", ROUNDED, Observation(61, 12, 3), now, None, None, None)
    assert stored.weather(now) == Weather(Sky.RAIN, None, local=True)
    assert stored.weather(now + timedelta(hours=7)) is None
    assert LocalWeather(1, False, "12345", "US", None, None, None, None, None, None).weather(now) is None


def test_the_scheduler_looks_every_five_minutes_and_does_nothing_without_a_source() -> None:
    assert timedelta(minutes=5) == WEATHER_INTERVAL
    from dinnerbell.scheduler.runner import JobContext

    ctx = JobContext.__new__(JobContext)
    object.__setattr__(ctx, "weather", None)
    asyncio.run(refresh_weather(ctx))  # returns at once, touching nothing


def test_the_job_is_scheduled(client: TestClient) -> None:
    scheduler = build_scheduler(client.app.state.jobs)  # type: ignore[attr-defined]
    job = scheduler.get_job("refresh_weather")
    assert job is not None and job.trigger.interval == WEATHER_INTERVAL


# Admin's setting ---------------------------------------------------------------------------------------


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    sign_in(client, provider)
    settle(client)
    return client


def save(client: TestClient, on: bool = True, postal_code: str = POSTAL_CODE, country: str = "US") -> httpx.Response:
    data = {"postal_code": postal_code, "country": country} | ({"on": "on"} if on else {})
    return submit(client, "/admin/web-app/weather", data)


def stored(settings: Settings) -> LocalWeatherRow | None:
    found = rows(settings, LocalWeatherRow)
    return found[0] if found else None


def refresh(client: TestClient, draw: float = 0.5) -> int:
    jobs = client.app.state.jobs  # type: ignore[attr-defined]
    run = functools.partial(local_weather.refresh, jobs.sessionmaker, jobs.weather, jobs.clock(), draw=lambda: draw)
    asked: int = client.portal.call(run)  # type: ignore[union-attr]
    return asked


def weather_card(html: str) -> str:
    return html.split('id="weather"', 1)[1].split("</form>", 1)[0]


def test_off_by_default_nothing_is_fetched(admin: TestClient, open_meteo: FakeOpenMeteo, migrated: Settings) -> None:
    card = weather_card(page(admin.get("/admin/web-app")))
    assert "Dinnerbone's weather" in card and 'name="on" value="on" aria-describedby="weather-privacy">' in card
    assert 'name="country" value="US"' in card and 'id="weather-status"' not in card
    assert refresh(admin) == 0
    assert stored(migrated) is None and open_meteo.requests == []
    assert SEASONAL in admin.get("/").text and "Open-Meteo" not in admin.get("/").text


def test_the_admin_turns_it_on_and_it_is_saved_rounded_and_audited(
    admin: TestClient, open_meteo: FakeOpenMeteo, migrated: Settings, clock: FakeClock
) -> None:
    response = save(admin, postal_code="12345-6789")
    assert (response.status_code, response.headers["location"]) == (303, "/admin/web-app?notice=weather-on#weather")
    row = stored(migrated)
    assert row is not None and row.enabled and (row.postal_code, row.country) == ("12345", "US")
    assert (row.latitude, row.longitude, row.place) == (40.1, -75.2, "Elmford, Northshire")
    assert row.next_fetch_at == clock.now and row.code is None
    assert len(open_meteo.lookups()) == 1 and open_meteo.forecasts() == []
    admin_id = member(migrated, "admin@example.com").id
    assert [(a.actor_member_id, a.target, a.payload) for a in audits(migrated, "household.weather_updated")] == [
        (admin_id, f"household:{row.household_id}", {"on": True, "postal_code_changed": True})
    ]
    card = weather_card(page(admin.get("/admin/web-app?notice=weather-on")))
    assert "Local weather is on." in card and "checked>" in card and 'value="12345"' in card
    assert "Showing the weather near Elmford, Northshire. Waiting for the first update" in card

    assert save(admin).status_code == 303  # unchanged: no lookup, no audit
    assert len(open_meteo.lookups()) == 1 and len(audits(migrated, "household.weather_updated")) == 1

    assert save(admin, on=False).headers["location"] == "/admin/web-app?notice=weather-off#weather"
    row = stored(migrated)
    assert row is not None and not row.enabled and row.postal_code == "12345"
    assert (row.latitude, row.longitude, row.place, row.next_fetch_at) == (None, None, None, None)
    assert audits(migrated, "household.weather_updated")[-1].payload == {"on": False, "postal_code_changed": False}
    card = weather_card(page(admin.get("/admin/web-app")))
    assert 'value="12345"' in card and "checked>" not in card and 'id="weather-status"' not in card
    assert refresh(admin) == 0 and open_meteo.forecasts() == []  # off: nothing fetched

    assert save(admin).status_code == 303  # on again: looked up again
    assert len(open_meteo.lookups()) == 2


def test_no_audit_row_or_log_line_names_the_place(
    admin: TestClient, migrated: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="dinnerbell")
    save(admin)
    refresh(admin)
    for audit in audits(migrated, "household.weather_updated"):
        assert set(audit.payload) == {"on", "postal_code_changed"}
    for record in caplog.records:
        if record.levelno >= logging.INFO:
            text = record.getMessage() + repr(record.__dict__)
            for secret in ("12345", "40.1", "-75.2", "Elmford"):
                assert secret not in text, (record.getMessage(), secret)


@pytest.mark.parametrize(
    ("postal_code", "country", "message"),
    [
        ("", "US", "Enter a ZIP or postal code to show local weather."),
        ("1234", "US", "Enter a 5-digit ZIP code, such as 12345."),
        ("12345", "USA", "Enter the country as two letters, such as US or CA."),
        ("54321", "US", "Dinnerbell couldn't find that postal code."),
    ],
)
def test_a_code_that_cant_be_one_or_finds_nothing_saves_nothing(
    admin: TestClient, open_meteo: FakeOpenMeteo, migrated: Settings, postal_code: str, country: str, message: str
) -> None:
    response = save(admin, postal_code=postal_code, country=country)
    assert response.status_code == 400
    card = weather_card(page(response))
    assert message in card and f'value="{postal_code}"' in card and "checked>" in card  # what they typed stays
    assert stored(migrated) is None and audits(migrated, "household.weather_updated") == []
    assert len(open_meteo.lookups()) == (1 if postal_code == "54321" else 0)


def test_a_failed_lookup_says_to_try_again(admin: TestClient, open_meteo: FakeOpenMeteo, migrated: Settings) -> None:
    open_meteo.respond = lambda request: httpx.Response(503)
    response = save(admin)
    assert response.status_code == 400
    assert "couldn't reach the weather service to look up that postal code" in weather_card(page(response))
    assert stored(migrated) is None


def test_only_an_admin_with_a_csrf_token_changes_it(
    admin: TestClient, provider: FakeProvider, migrated: Settings, open_meteo: FakeOpenMeteo
) -> None:
    assert admin.post("/admin/web-app/weather", data={"on": "on", "postal_code": POSTAL_CODE}).status_code == 403
    assert invite(admin, "sam@example.com", "Sam").status_code == 303
    sign_in_as(admin, provider, "subject-sam", "sam@example.com")
    assert save(admin).status_code == 403
    assert stored(migrated) is None and open_meteo.requests == []


def test_the_card_uses_the_admin_style(admin: TestClient) -> None:
    html = admin.get("/admin/web-app").text
    assert '<main class="page admin-page">' in html
    card = weather_card(html)
    assert 'class="button admin"' in card and "primary" not in card


# The refresh ----------------------------------------------------------------------------------------


def test_the_refresh_asks_once_then_waits_one_to_three_hours(
    admin: TestClient, open_meteo: FakeOpenMeteo, migrated: Settings, clock: FakeClock
) -> None:
    save(admin)
    assert refresh(admin, draw=0.0) == 1
    row = stored(migrated)
    assert row is not None and (row.code, row.temperature_c, row.wind_kmh) == (61, 12.5, 8.0)
    assert row.observed_at == clock.now and row.next_fetch_at == clock.now + timedelta(hours=1)
    params = open_meteo.forecasts()[0].url.params
    assert (params["latitude"], params["longitude"]) == ("40.1", "-75.2")
    assert refresh(admin) == 0  # not due again for an hour
    clock.advance(timedelta(minutes=55))
    assert refresh(admin) == 0
    clock.advance(timedelta(minutes=5))
    assert refresh(admin, draw=1.0) == 1
    assert len(open_meteo.forecasts()) == 2
    row = stored(migrated)
    assert row is not None and row.next_fetch_at == clock.now + timedelta(hours=2, minutes=55)
    clock.advance(timedelta(hours=2, minutes=50))
    assert refresh(admin) == 0
    clock.advance(WEATHER_INTERVAL)  # the next check, on schedule: 3 hours after the last fetch
    assert refresh(admin) == 1


def test_a_change_of_code_asks_again_at_the_next_check(admin: TestClient, open_meteo: FakeOpenMeteo) -> None:
    """Turning it off and on, or another code, asks at the next check, whenever the last fetch was."""
    save(admin)
    assert refresh(admin) == 1
    save(admin, on=False)
    save(admin)
    assert refresh(admin) == 1
    assert len(open_meteo.forecasts()) == 2
    save(admin, postal_code=OTHER_POSTAL_CODE)  # another code, with it on all along
    assert refresh(admin) == 1
    assert len(open_meteo.forecasts()) == 3


def test_a_source_that_never_answers_cant_hold_the_job(
    admin: TestClient, migrated: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh gives any source its own deadline plus a second, then counts a timeout."""
    save(admin)
    monkeypatch.setattr(local_weather, "DEADLINE_SECONDS", -0.95)  # a 50 ms limit in all

    class Silent:
        async def geocode(self, postal_code: str, country: str) -> Place:  # pragma: no cover - not asked
            raise AssertionError

        async def current(self, place: Place) -> Observation:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

    jobs = admin.app.state.jobs  # type: ignore[attr-defined]
    run = functools.partial(local_weather.refresh, jobs.sessionmaker, Silent(), jobs.clock())
    assert admin.portal.call(run) == 1  # type: ignore[union-attr]
    row = stored(migrated)
    assert row is not None and row.failure == "timeout"


def test_a_fetch_that_ends_after_it_was_turned_off_changes_nothing(
    admin: TestClient, open_meteo: FakeOpenMeteo, migrated: Settings
) -> None:
    save(admin)
    jobs = admin.app.state.jobs  # type: ignore[attr-defined]

    class OffWhileAsking:
        async def geocode(self, postal_code: str, country: str) -> Place:  # pragma: no cover - not asked
            raise AssertionError

        async def current(self, place: Place) -> Observation:
            async with jobs.sessionmaker() as db:
                from dinnerbell.db.local_weather import LocalWeatherRepository

                await LocalWeatherRepository(db).save_setting(
                    1, postal_code=POSTAL_CODE, country="US", place=None, now=jobs.clock()
                )
                await db.commit()
            return Observation(95, 20, 3)

    run = functools.partial(local_weather.refresh, jobs.sessionmaker, OffWhileAsking(), jobs.clock())
    assert admin.portal.call(run) == 1  # type: ignore[union-attr]
    row = stored(migrated)
    assert row is not None and not row.enabled and row.code is None


def test_the_page_shows_the_local_weather_with_its_credit(
    admin: TestClient, open_meteo: FakeOpenMeteo, clock: FakeClock
) -> None:
    open_meteo.answer = current(95, 24.0, 10.0)  # a thunderstorm at 1 PM, with no call
    save(admin)
    assert SEASONAL in admin.get("/").text  # nothing fetched yet: still the season's
    refresh(admin)
    html = admin.get("/").text
    storm = 'data-weather="thunderstorm" data-feel="none" data-fall="rain" data-pile="leaves" data-react="umbrella"'
    assert re.search(f'<body[^>]* {storm} data-weather-local="on">', html)
    assert re.search(f'<div id="den-state" hidden data-pose="peek"[^>]* {storm} data-weather-local="on"', html)
    assert '<div class="den" data-pose="peek" aria-hidden="true">' in html
    credit = (
        '<p class="small muted weather-credit">Weather data by '
        '<a href="https://open-meteo.com/" rel="noopener noreferrer">Open-Meteo.com</a></p>'
    )
    assert html.count(credit) == 1 and "style=" not in html
    assert storm in admin.get("/catalog").text and credit not in admin.get("/catalog").text
    status = weather_card(page(admin.get("/admin/web-app")))
    assert "Updated Thu Oct 22 at 1:00 PM." in status

    clock.advance(timedelta(hours=6, minutes=1))  # no answer since: too old, so the season's again
    later = admin.get("/").text
    assert 'data-weather-local="on"' not in later and credit in later


def test_a_calls_page_shows_it_too(admin: TestClient, open_meteo: FakeOpenMeteo) -> None:
    open_meteo.answer = current(71, -3.0, 4.0)
    save(admin)
    refresh(admin)
    response = submit(admin, "/events", {"slot": "dinner", "day": "2026-10-22", "post_now": "1"})
    html = admin.get(response.headers["location"]).text
    snow = 'data-weather="snow" data-feel="none" data-fall="snow" data-pile="none" data-react="shake"'
    assert snow in html and "weather-credit" in html and 'data-pose="laps"' in html


def test_fun_mode_off_carries_no_weather(admin: TestClient, open_meteo: FakeOpenMeteo) -> None:
    save(admin)
    refresh(admin)
    assert submit(admin, "/me/fun").status_code == 303
    html = admin.get("/").text
    assert "data-weather" not in html and "weather-credit" not in html


# Failure ------------------------------------------------------------------------------------------


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("slow", request=request)


def down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("down", request=request)


def broken(request: httpx.Request) -> httpx.Response:
    raise RuntimeError("a bug in the transport")


FAILURES = {
    WeatherFailure.TIMEOUT: timeout,
    WeatherFailure.NETWORK: down,
    WeatherFailure.STATUS: lambda request: httpx.Response(500),
    WeatherFailure.REDIRECT: lambda request: httpx.Response(302, headers={"location": "https://evil.example/"}),
    WeatherFailure.CONTENT_TYPE: lambda request: httpx.Response(
        200, text="<html>", headers={"content-type": "text/html"}
    ),
    WeatherFailure.TOO_BIG: lambda request: json_response({"pad": "x" * MAX_ANSWER_BYTES}),
    WeatherFailure.BAD_JSON: lambda request: httpx.Response(
        200, content=b"{", headers={"content-type": "application/json"}
    ),
    WeatherFailure.BAD_ANSWER: lambda request: json_response(current(code=12)),
    WeatherFailure.ERROR: broken,
}


@pytest.mark.parametrize("reason", list(FAILURES))
def test_each_failure_falls_back_to_the_seasonal_weather(
    admin: TestClient, open_meteo: FakeOpenMeteo, migrated: Settings, clock: FakeClock, reason: WeatherFailure
) -> None:
    save(admin)
    open_meteo.respond = FAILURES[reason]
    assert refresh(admin) == 1
    row = stored(migrated)
    assert row is not None and (row.failure, row.failing_since, row.code) == (reason.value, clock.now, None)
    assert row.next_fetch_at == next_fetch(clock.now, 0.5)  # failing, it still waits
    html = admin.get("/").text
    assert SEASONAL in html and 'data-weather-local="on"' not in html  # the rest of the page is untouched
    assert 'class="den" data-pose="potter"' in html
    status = weather_card(page(admin.get("/admin/web-app")))
    assert "Couldn't reach the weather service since Thu Oct 22 at 1:00 PM." in status
    assert "The den shows the season's weather until it can." in status


def test_a_failure_keeps_the_last_good_answer_while_its_fresh(
    admin: TestClient, open_meteo: FakeOpenMeteo, clock: FakeClock
) -> None:
    open_meteo.answer = current(61)
    save(admin)
    refresh(admin)
    clock.advance(timedelta(hours=2))
    open_meteo.respond = down
    refresh(admin)
    assert 'data-weather="rain"' in admin.get("/").text
    assert "The den shows the last update until it's 6 hours old" in weather_card(page(admin.get("/admin/web-app")))


def test_logs_warn_once_stay_quiet_then_say_it_recovered(
    admin: TestClient, open_meteo: FakeOpenMeteo, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="dinnerbell.services.local_weather")
    save(admin)

    def lines(level: int) -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.name == "dinnerbell.services.local_weather" and r.levelno == level]

    open_meteo.respond = timeout
    refresh(admin)
    [warning] = lines(logging.WARNING)
    assert warning.getMessage() == "local weather failed; the den shows the seasonal weather"
    assert (warning.household_id, warning.reason) == (1, "timeout")  # type: ignore[attr-defined]
    for _ in range(3):
        clock.advance(timedelta(hours=3))
        open_meteo.respond = down
        refresh(admin)
    assert len(lines(logging.WARNING)) == 1  # quiet while it keeps failing
    assert len([r for r in lines(logging.DEBUG) if r.getMessage() == "local weather still failing"]) == 3
    assert not lines(logging.ERROR)
    clock.advance(timedelta(hours=3))
    open_meteo.respond = None
    refresh(admin)
    [recovered] = [r for r in lines(logging.INFO) if r.getMessage() == "local weather recovered"]
    assert recovered.failing_minutes == 4 * 180  # type: ignore[attr-defined]
    clock.advance(timedelta(hours=3))
    refresh(admin)
    assert len([r for r in lines(logging.INFO) if r.getMessage() == "local weather recovered"]) == 1
    clock.advance(timedelta(hours=3))
    open_meteo.respond = down
    refresh(admin)
    assert len(lines(logging.WARNING)) == 2  # failing again starts with a warning again


def test_a_hung_fetch_never_holds_up_a_page(admin: TestClient, open_meteo: FakeOpenMeteo) -> None:
    """The refresh waits on the service in the scheduler; pages read only what's stored."""
    save(admin)
    jobs = admin.app.state.jobs  # type: ignore[attr-defined]
    started = asyncio.Event()

    class Hung:
        async def geocode(self, postal_code: str, country: str) -> Place:  # pragma: no cover - not asked
            raise AssertionError

        async def current(self, place: Place) -> Observation:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

    portal = admin.portal  # type: ignore[union-attr]
    task = portal.start_task_soon(local_weather.refresh, jobs.sessionmaker, Hung(), jobs.clock())
    portal.call(started.wait)
    try:
        for path in ("/", "/admin/web-app"):
            response = admin.get(path)
            assert response.status_code == 200, path
        assert SEASONAL in admin.get("/").text
        assert not task.done()
    finally:
        task.cancel()


def test_a_database_error_reading_it_shows_the_seasonal_weather(
    admin: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from dinnerbell.db.local_weather import LocalWeatherRepository

    async def broken_get(self: Any, household_id: int) -> None:
        raise RuntimeError("disk")

    monkeypatch.setattr(LocalWeatherRepository, "get", broken_get)
    response = admin.get("/")
    assert response.status_code == 200 and SEASONAL in response.text
    assert any(r.getMessage() == "could not read local weather" for r in caplog.records)


def test_a_refresh_that_cant_read_the_database_returns_quietly(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> Any:
        raise RuntimeError("disk")

    assert asyncio.run(local_weather.refresh(broken, OpenMeteo(), datetime.now(UTC))) == 0  # type: ignore[arg-type]
    assert any(r.getMessage() == "could not read which local weather is due" for r in caplog.records)


# The art ----------------------------------------------------------------------------------------------


def test_fun_css_draws_every_weather_fall_pile_and_reaction() -> None:
    for sky in Sky:
        assert f'[data-weather="{sky}"]' in FUN_CSS or sky is Sky.CLEAR, sky
    for feel in Feel:
        assert f'[data-feel="{feel}"]' in FUN_CSS or feel is Feel.HOT, feel  # hot shows through data-react
    for fall in Fall:
        assert f'body[data-fall="{fall}"]' in FUN_CSS, fall
    for pile in Pile:
        assert f'body[data-pile="{pile}"]' in FUN_CSS, pile
    for react in React:
        assert f'body[data-react="{react}"]' in FUN_CSS, react
    assert '.den[data-pose="peek"]' in FUN_CSS


def css_rules(css: str) -> list[tuple[list[str], dict[str, str]]]:
    """Every innermost rule, inside @media too, as its selectors and declarations, with comments
    dropped and whitespace normalized, so a test doesn't depend on how the file is laid out."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    found = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = [" ".join(part.split()) for part in selector.split(",") if part.strip()]
        declarations = {}
        for declaration in body.split(";"):
            name, colon, value = declaration.partition(":")
            if colon:
                declarations[name.strip()] = " ".join(value.split())
        found.append((selectors, declarations))
    return found


FUN_RULES = css_rules(FUN_CSS)


def declared(selector: str, prop: str) -> list[str]:
    """What every rule naming exactly ``selector`` sets ``prop`` to."""
    return [d[prop] for selectors, d in FUN_RULES if selector in selectors and prop in d]


def test_only_he_takes_taps_in_the_den() -> None:
    """The den lets taps through; only Dinnerbone, once fun.js is live, takes them back."""
    taking = [
        selectors
        for selectors, d in FUN_RULES
        if "pointer-events" in d and d["pointer-events"].replace("!important", "").strip() != "none"
    ]
    assert taking == [[".den.den-live .den-ham"]]
    assert declared(".den", "pointer-events") == ["none"]
    for layer in (".den-wx", ".den-fog", ".den-clouds", ".edge-wx", ".den-track", ".ham .acc"):
        assert declared(layer, "pointer-events") == ["none"], layer


def test_the_weather_sits_behind_him() -> None:
    (ham,) = declared(".den-ham", "z-index")
    for layer in (".den-wx", ".den-fog", ".den-clouds", ".den-snow", ".den-puddle", ".den-leaves"):
        levels = declared(layer, "z-index")
        assert levels and all(int(z) < int(ham) for z in levels), layer


def test_everything_he_wears_is_inside_him() -> None:
    """Every accessory is drawn in his own svg, so it moves with him and stays under his tap."""
    den = (Path(__file__).resolve().parents[1] / "src/dinnerbell/web/templates/_den.html").read_text()
    inside = den.split('<svg class="ham"', 1)[1].split("</svg>", 1)[0]
    outside = den.replace(inside, "")
    assert len(re.findall(r'class="acc ', inside)) == len(re.findall(r'class="acc ', den)) >= 12
    assert 'class="acc ' not in outside


def test_he_is_hidden_only_while_away_on_a_lap() -> None:
    hidden = [selectors for selectors, d in FUN_RULES if d.get("visibility") == "hidden"]
    assert hidden == [[".den-ham.den-away"]]
    for selectors, d in FUN_RULES:
        if d.get("display") == "none" or d.get("opacity") == "0":
            for selector in selectors:
                assert not re.search(r"\.den-ham$|\.ham$", selector), selector


def test_lightning_only_brightens_and_only_with_motion() -> None:
    start = FUN_CSS.index("@media (prefers-reduced-motion: no-preference) {")
    end = FUN_CSS.index("\n}\n", start)
    assert "den-lightning" in FUN_CSS[start:end] and "den-lightning" not in FUN_CSS[:start]
    keyframes = FUN_CSS.split("@keyframes den-lightning", 1)[1].split("\n", 1)[0]
    levels = [float(x) for x in re.findall(r"brightness\(([\d.]+)\)", keyframes)]
    assert levels and max(levels) <= 1.15
    stops = [float(x) for x in re.findall(r"([\d.]+)%", keyframes)]
    peak = float(re.search(r"([\d.]+)% \{ filter: brightness", keyframes).group(1))  # type: ignore[union-attr]
    rise = min(peak - s for s in stops if s < peak) * 9 / 100  # seconds, in the 9-second loop
    assert rise >= 0.9


def test_fun_js_follows_the_weather() -> None:
    assert 'var WEATHER = ["weather", "feel", "fall", "pile", "react"];' in FUN_JS
    assert '["period", "season", "season-depth"].concat(WEATHER).forEach' in FUN_JS
    assert "flashTimers" in FUN_JS  # a second tap restarts the reaction in full
    tap = FUN_JS.split('ham.addEventListener("click"', 1)[1].split("});", 1)[0]
    asleep = tap.index('if (pose === "sleep" || pose === "doze")')
    assert tap.index('flash(heart, "den-pop"') < asleep  # asleep, the heart pops too


def test_the_lap_runner_never_shakes_off_snow() -> None:
    """The runner is a copy of him; the shake on the den's own would leak onto it unless the
    runner's rule wins."""
    for part in (".ham-shake", ".acc-snowcap"):
        for who in (".den-runner", ".den[data-walking]"):  # nor while he walks
            rule = f"body[data-react] {who} .ham {part}"
            assert declared(rule, "animation") == ["none"], rule


def test_the_reactions_are_in_the_sprite() -> None:
    sprite = (STATIC / "den.svg").read_text()
    for symbol in ("acc-umbrella", "acc-shades", "acc-fan", "acc-snowcap", "acc-scarf-tail"):
        assert f'<symbol id="{symbol}"' in sprite, symbol
    assert "style=" not in sprite and "<style" not in sprite


def test_a_new_member_sees_the_same_weather(
    admin: TestClient, provider: FakeProvider, open_meteo: FakeOpenMeteo
) -> None:
    mine = re.search(r"<body[^>]*>", admin.get("/").text)
    assert invite(admin, "sam@example.com", "Sam").status_code == 303
    sign_in_as(admin, provider, "subject-sam", "sam@example.com")
    theirs = re.search(r"<body[^>]*>", admin.get("/").text)
    assert mine and theirs
    attributes = re.compile(r' data-(?:period|season|season-depth|weather|feel|fall|pile|react)="[^"]*"')
    assert attributes.findall(mine.group(0)) == attributes.findall(theirs.group(0))


def test_a_lookup_that_raises_logs_no_traceback_above_debug(
    admin: TestClient, open_meteo: FakeOpenMeteo, migrated: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """An exception's message can quote the URL, and so the postal code: its traceback is debug only."""

    def leaky(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"could not open {request.url}")

    caplog.set_level(logging.DEBUG, logger="dinnerbell")
    open_meteo.respond = leaky
    assert save(admin).status_code == 400
    open_meteo.respond = None  # and on a refresh: turn it on with a working lookup, then fail the fetch
    assert save(admin).status_code == 303
    open_meteo.respond = leaky
    refresh(admin)
    loud = [r for r in caplog.records if r.levelno >= logging.INFO and r.name.startswith("dinnerbell.services")]
    assert any(r.getMessage() == "postal code lookup failed" for r in loud)
    for record in loud:
        assert record.exc_info is None, record.getMessage()
        assert "12345" not in record.getMessage() + repr(record.__dict__)
    assert any(r.exc_info and r.levelno == logging.DEBUG for r in caplog.records)


def test_an_off_save_never_stores_a_country_that_cant_be_one(admin: TestClient, migrated: Settings) -> None:
    save(admin)
    assert save(admin, on=False, postal_code="", country="XYZ").status_code == 303
    row = stored(migrated)
    assert row is not None and (row.postal_code, row.country) == ("", "US")
    assert save(admin, on=False, postal_code="99999", country="1A").status_code == 303
    row = stored(migrated)
    assert row is not None and (row.postal_code, row.country) == ("", "US")  # kept what was saved
    assert save(admin, on=False, postal_code="", country="ca").status_code == 303
    row = stored(migrated)
    assert row is not None and row.country == "CA"
