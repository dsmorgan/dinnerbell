# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fun mode (#153): the time of day and season Dinnerbone's Den shows.

The den's light follows the time of day and its weather follows the season, both in the
household's time zone. Every window is fixed, so the same moment always gives the same scene:
the server renders it on the page, and the browser needs no clock.

The periods run breakfast 5:00 to 10:30, lunch to 14:00, afternoon to 17:00, dinner to 21:00,
and late night from 21:00 to 5:00. Late night is when Dinnerbone sleeps unless a call wakes him.

Seasons start on the northern hemisphere's equinoxes and solstices, taken as fixed dates: spring
on March 20, summer on June 21, autumn on September 22, and winter on December 21. The real dates
move by a day or so from year to year, which no one can see in a pile of snow. ``depth`` says how
far into its season a day is, in steps from 0 (its first few days) to 10 (its last few), so the
page can draw snow, leaves, or a puddle with attribute selectors instead of inline styles. It
follows the date, never the visit, so every page load that day shows the same pile.

The weather (#319) is the household's local weather when an admin turned it on and a fresh
answer is stored (ADR 0011), and otherwise the season's: one of a few weighted day patterns,
seeded by the household and the day, some of which change through the day, such as a morning
fog that clears by lunch. The night from 9 PM to 5 AM belongs to the day it started, like the
late period. ``Scene`` derives what falls, what lies on the floor (snow, leaves, or a puddle,
only on days whose weather allows it), and how Dinnerbone reacts.

What Dinnerbone does follows the call he's watching (``followed_state``), the time of day, and a
thunderstorm (``pose``). The server renders the pose, so the den is right with no script and
under reduced motion.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from dinnerbell.domain.events import Event, EventKind, EventState
from dinnerbell.domain.weather import SNOWY, WET, Feel, Sky, Weather

DEPTH_STEPS = 10
"""The deepest pile: ``depth`` runs from 0 to this."""


class Period(StrEnum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    AFTERNOON = "afternoon"
    DINNER = "dinner"
    LATE = "late"


class Season(StrEnum):
    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"


PERIOD_STARTS = (
    (time(5, 0), Period.BREAKFAST),
    (time(10, 30), Period.LUNCH),
    (time(14, 0), Period.AFTERNOON),
    (time(17, 0), Period.DINNER),
    (time(21, 0), Period.LATE),
)
"""Each period and the local time it starts, in order. Late night runs past midnight to 5:00."""

SEASON_STARTS = (
    ((3, 20), Season.SPRING),
    ((6, 21), Season.SUMMER),
    ((9, 22), Season.AUTUMN),
    ((12, 21), Season.WINTER),
)
"""Each season and the (month, day) it starts, in calendar order."""


class Fall(StrEnum):
    """What falls in the den and down the page's edges."""

    RAIN = "rain"
    SNOW = "snow"
    SLEET = "sleet"
    LEAVES = "leaves"
    PETALS = "petals"
    GUSTS = "gusts"


class Pile(StrEnum):
    """What lies on the den's floor, as deep as ``depth`` says."""

    SNOW = "snow"
    LEAVES = "leaves"
    PUDDLE = "puddle"


class React(StrEnum):
    """How Dinnerbone reacts to the weather. A thunderstorm also sends him into the straw to
    peek out (``Pose.PEEK``), unless a call keeps him busy or he's asleep."""

    UMBRELLA = "umbrella"
    SHAKE = "shake"
    """He shakes the snow off his head now and then."""
    SCARF = "scarf"
    """His scarf blows in the wind."""
    SHADES = "shades"
    FAN = "fan"


DAYTIME = frozenset({Period.BREAKFAST, Period.LUNCH, Period.AFTERNOON})


@dataclass(frozen=True, slots=True)
class Scene:
    """What the den shows at one moment."""

    period: Period
    season: Season
    depth: int
    """How far into the season, from 0 to ``DEPTH_STEPS``."""
    weather: Weather = field(default=Weather(Sky.CLEAR))

    @property
    def overnight(self) -> bool:
        """9 PM to 5 AM, when Dinnerbone sleeps unless a call keeps him up."""
        return self.period is Period.LATE

    @property
    def fall(self) -> Fall | None:
        """What falls: rain, snow, or both in sleet; in wind, blossom in spring, leaves in
        autumn, and gusts otherwise; and leaves on autumn's other dry days."""
        sky, feel = self.weather.sky, self.weather.feel
        if sky in SNOWY:
            return Fall.SNOW
        if sky is Sky.SLEET:
            return Fall.SLEET
        if sky in WET:
            return Fall.RAIN
        if feel is Feel.WINDY:
            return {Season.SPRING: Fall.PETALS, Season.AUTUMN: Fall.LEAVES}.get(self.season, Fall.GUSTS)
        if self.season is Season.AUTUMN and sky is not Sky.FOG:
            return Fall.LEAVES
        return None

    @property
    def pile(self) -> Pile | None:
        """What lies on the floor, only when the weather allows it: winter's snow, unless rain or
        heat has washed it away; autumn's leaves, unless the wind has scattered them or snow is
        falling on them; and a puddle on any other wet day."""
        sky, feel = self.weather.sky, self.weather.feel
        if self.season is Season.WINTER and sky not in WET - {Sky.SLEET} and feel is not Feel.HOT:
            return Pile.SNOW
        if self.season is Season.AUTUMN and feel is not Feel.WINDY and sky not in SNOWY:
            return Pile.LEAVES
        if sky in WET:
            return Pile.PUDDLE
        return None

    @property
    def react(self) -> React | None:
        """An umbrella under rain, sleet, or a storm; shaking off snow; his scarf in the wind;
        and in the heat, sunglasses by day and a fan in the evening."""
        sky, feel = self.weather.sky, self.weather.feel
        if sky in WET:
            return React.UMBRELLA
        if sky in SNOWY:
            return React.SHAKE
        if feel is Feel.WINDY:
            return React.SCARF
        if feel is Feel.HOT:
            return React.SHADES if self.period in DAYTIME else React.FAN
        return None


def period_at(local: time) -> Period:
    """The period a local wall-clock time falls in."""
    current = Period.LATE  # before 5:00, still last night's
    for start, period in PERIOD_STARTS:
        if local >= start:
            current = period
    return current


def season_bounds(day: date) -> tuple[Season, date, date]:
    """The season ``day`` falls in, the day it started, and the day the next one starts."""
    years = (day.year - 1, day.year, day.year + 1)
    starts = [(date(year, month, d), season) for year in years for (month, d), season in SEASON_STARTS]
    index = max(i for i, (start, _) in enumerate(starts) if start <= day)
    (start, season), (end, _) = starts[index], starts[index + 1]
    return season, start, end


def season_depth(day: date) -> int:
    """How far into its season ``day`` is, rounded to the nearest step, half up: 0 for its first
    few days (about 4 of a 90-day season) and ``DEPTH_STEPS`` for its last few."""
    _, start, end = season_bounds(day)
    elapsed, length = (day - start).days, (end - start).days
    return (2 * elapsed * DEPTH_STEPS + length) // (2 * length)


def scene(now: datetime, timezone: str, *, household_id: int, local: Weather | None = None) -> Scene:
    """The scene at ``now`` (UTC-aware) in the household's time zone, an IANA name. The weather
    is ``local``, the household's fresh local weather, or else the seasonal choice."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    at = now.astimezone(ZoneInfo(timezone))
    season, _, _ = season_bounds(at.date())
    period = period_at(at.time())
    weather = local or seasonal_weather(household_id, weather_day(at), period)
    return Scene(period=period, season=season, depth=season_depth(at.date()), weather=weather)


# Seasonal weather (#319) -------------------------------------------------------------------------

LATE_ENDS = PERIOD_STARTS[0][0]


def weather_day(local: datetime) -> date:
    """The day whose weather a local moment shows: before 5 AM, still last night's."""
    return (local - timedelta(days=1)).date() if local.time() < LATE_ENDS else local.date()


@dataclass(frozen=True, slots=True)
class DayPattern:
    """A day's weather: ``base`` all day, except the periods ``changes`` names."""

    name: str
    weight: float
    base: Weather
    changes: Mapping[Period, Weather] = field(default_factory=dict)

    def at(self, period: Period) -> Weather:
        return self.changes.get(period, self.base)


_B, _L, _A, _D, _N = Period.BREAKFAST, Period.LUNCH, Period.AFTERNOON, Period.DINNER, Period.LATE


def _w(sky: Sky, feel: Feel | None = None) -> Weather:
    return Weather(sky, feel)


SEASONAL_PATTERNS: Mapping[Season, tuple[DayPattern, ...]] = {
    Season.WINTER: (
        DayPattern("light snow", 3, _w(Sky.SNOW)),
        DayPattern("heavy snow", 1.5, _w(Sky.HEAVY_SNOW), {_B: _w(Sky.SNOW)}),
        DayPattern("clear and cold", 2.5, _w(Sky.CLEAR, Feel.COLD)),
        DayPattern("overcast", 2, _w(Sky.OVERCAST, Feel.COLD)),
        DayPattern("sleet", 1, _w(Sky.SLEET)),
        DayPattern("fog, then overcast", 1, _w(Sky.OVERCAST, Feel.COLD), {_B: _w(Sky.FOG), _N: _w(Sky.FOG)}),
        DayPattern(
            "snow by evening", 1, _w(Sky.SNOW), {_B: _w(Sky.OVERCAST, Feel.COLD), _L: _w(Sky.OVERCAST, Feel.COLD)}
        ),
    ),
    Season.SPRING: (
        DayPattern("showers", 3, _w(Sky.PARTLY_CLOUDY), {_B: _w(Sky.DRIZZLE), _A: _w(Sky.RAIN), _N: _w(Sky.DRIZZLE)}),
        DayPattern("rain", 2, _w(Sky.RAIN), {_A: _w(Sky.HEAVY_RAIN)}),
        DayPattern("clear", 2, _w(Sky.CLEAR)),
        DayPattern("windy with blossom", 1.5, _w(Sky.PARTLY_CLOUDY, Feel.WINDY)),
        DayPattern("fog in the morning", 1.5, _w(Sky.CLEAR), {_B: _w(Sky.FOG)}),
        DayPattern("overcast", 1, _w(Sky.OVERCAST)),
    ),
    Season.SUMMER: (
        DayPattern("clear and hot", 3, _w(Sky.CLEAR, Feel.HOT), {_B: _w(Sky.CLEAR), _N: _w(Sky.CLEAR)}),
        DayPattern("partly cloudy", 2, _w(Sky.PARTLY_CLOUDY)),
        DayPattern(
            "a thunderstorm in the afternoon",
            1.5,
            _w(Sky.PARTLY_CLOUDY, Feel.HOT),
            {_B: _w(Sky.CLEAR), _A: _w(Sky.THUNDERSTORM), _D: _w(Sky.DRIZZLE), _N: _w(Sky.PARTLY_CLOUDY)},
        ),
        DayPattern("a warm evening", 1.5, _w(Sky.CLEAR), {_A: _w(Sky.CLEAR, Feel.HOT), _D: _w(Sky.CLEAR, Feel.HOT)}),
        DayPattern("summer rain", 1, _w(Sky.RAIN), {_B: _w(Sky.OVERCAST)}),
    ),
    Season.AUTUMN: (
        DayPattern("falling leaves", 3, _w(Sky.PARTLY_CLOUDY), {_B: _w(Sky.CLEAR), _L: _w(Sky.CLEAR)}),
        DayPattern("windy", 2, _w(Sky.PARTLY_CLOUDY, Feel.WINDY)),
        DayPattern("rain", 2, _w(Sky.RAIN), {_N: _w(Sky.DRIZZLE)}),
        DayPattern(
            "fog",
            1.5,
            _w(Sky.OVERCAST),
            {_B: _w(Sky.FOG), _L: _w(Sky.FOG), _N: _w(Sky.FOG)},
        ),
        DayPattern("crisp and clear", 2, _w(Sky.CLEAR), {_B: _w(Sky.CLEAR, Feel.COLD), _N: _w(Sky.CLEAR, Feel.COLD)}),
    ),
}
"""Each season's day patterns and their weights (spec 11, "Fun mode")."""


def weather_draw(household_id: int, day: date) -> float:
    """The household's draw for ``day``, from 0 up to 1: a digest, not ``hash()`` or ``random``,
    so it's the same in every process, on every restart, and for every member."""
    digest = hashlib.sha256(f"dinnerbell weather {household_id} {day.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def day_pattern(household_id: int, day: date) -> DayPattern:
    """The household's weather pattern for ``day``, drawn by weight from the season's."""
    season, _, _ = season_bounds(day)
    patterns = SEASONAL_PATTERNS[season]
    point = weather_draw(household_id, day) * sum(p.weight for p in patterns)
    for pattern in patterns:
        point -= pattern.weight
        if point < 0:
            return pattern
    return patterns[-1]  # pragma: no cover - only if rounding leaves a sliver past the last weight


def seasonal_weather(household_id: int, day: date, period: Period) -> Weather:
    """The seasonal weather for the household on ``day`` (``weather_day``) in ``period``."""
    return day_pattern(household_id, day).at(period)


class Pose(StrEnum):
    """What Dinnerbone is doing, and where in the den."""

    LAPS = "laps"
    """Answers are open: he runs laps around the call's card (fun.js); still, he waits at the
    den's front edge, looking down at it."""
    WHEEL = "wheel"
    """Voting: he runs in his wheel."""
    EAT = "eat"
    """Decided: he eats at his bowl."""
    SLEEP = "sleep"
    """Done, or late at night with no call keeping him up: asleep in the straw."""
    NIBBLE = "nibble"
    """Breakfast time, no call: a nibble at the bowl."""
    POTTER = "potter"
    """Lunchtime, no call: pottering about the floor."""
    DOZE = "doze"
    """Afternoon, no call: dozing beside the straw."""
    WAIT = "wait"
    """Dinnertime, no call: waiting at his bowl, looking up at the bell."""
    PEEK = "peek"
    """A thunderstorm, no call, awake: down in the straw, peeking out (#319)."""


CALL_POSES = {
    EventState.COLLECTING: Pose.LAPS,
    EventState.VOTING: Pose.WHEEL,
    EventState.DECIDED: Pose.EAT,
    EventState.DONE: Pose.SLEEP,
}
CLOCK_POSES = {
    Period.BREAKFAST: Pose.NIBBLE,
    Period.LUNCH: Pose.POTTER,
    Period.AFTERNOON: Pose.DOZE,
    Period.DINNER: Pose.WAIT,
    Period.LATE: Pose.SLEEP,
}
FOLLOWED_STATES = (EventState.COLLECTING, EventState.VOTING, EventState.DECIDED)
"""The states of a call he follows: open and posted."""


def pose(scene: Scene, call: EventState | None) -> Pose:
    """What he does at ``scene`` while following a call in state ``call``, or none.

    A call outranks the clock, overnight too: answers or voting wake him, and a call still
    decided after 9 PM keeps him up to eat. With no call he follows the clock, and sleeps late
    at night. A thunderstorm sends him into the straw to peek out, unless he's asleep."""
    if call in CALL_POSES:
        return CALL_POSES[call]
    clock = CLOCK_POSES[scene.period]
    if scene.weather.sky is Sky.THUNDERSTORM and clock is not Pose.SLEEP:
        return Pose.PEEK
    return clock


def followed_call(events: Iterable[Event], now: datetime, timezone: str) -> Event | None:
    """The call he follows among ``events``, the calls on Today in their order, or None.

    It's the first call that's taking answers, voting, or decided. With none, a call marked done
    today keeps him asleep for the rest of the time of day it was done in, so a dinner done at
    7 PM puts him to bed, and a breakfast done at 9 AM lets him up for lunch. Offers and calls
    still to be posted don't count."""
    calls = [e for e in events if e.kind is EventKind.CALL]
    for event in calls:
        if event.state in FOLLOWED_STATES:
            return event
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    for event in calls:
        if event.state is EventState.DONE and event.done_at is not None:
            done = event.done_at.astimezone(zone)
            if done.date() == local.date() and period_at(done.time()) is period_at(local.time()) and done <= local:
                return event
    return None


def followed_state(events: Iterable[Event], now: datetime, timezone: str) -> EventState | None:
    """The state of ``followed_call``, or None."""
    event = followed_call(events, now, timezone)
    return None if event is None else event.state


DONE_CAPTION = "All done. So full."


def decided_caption(item_name: str | None, slot: str) -> str:
    """What the den says when a call is decided, such as "Pad thai it is!"."""
    return f"{item_name} it is!" if item_name else f"{slot.capitalize()} is decided!"


def badge_caption(badge_name: str, tier_label: str) -> str:
    """What the den says for a badge, such as "Road Runner · Silver"."""
    return f"{badge_name} \u00b7 {tier_label}"


FIT_STEPS = (8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 40)
"""The greeting's width buckets, in narrow characters, for the CSS that shrinks it to fit."""


def fit_step(text: str) -> int:
    """How wide ``text`` reads, rounded up to a step in ``FIT_STEPS`` (the last for anything
    longer): a wide character, such as CJK or most emoji, counts as two narrow ones."""
    width = sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)
    return next((step for step in FIT_STEPS if width <= step), FIT_STEPS[-1])
