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
far into its season a day is, in steps from 0 (the first day) to 10 (the last few days), so the
page can draw snow, leaves, or a puddle with attribute selectors instead of inline styles. It
follows the date, never the visit, so every page load that day shows the same pile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

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


@dataclass(frozen=True, slots=True)
class Scene:
    """What the den shows at one moment."""

    period: Period
    season: Season
    depth: int
    """How far into the season, from 0 to ``DEPTH_STEPS``."""

    @property
    def overnight(self) -> bool:
        """9 PM to 5 AM, when Dinnerbone sleeps unless a call keeps him up."""
        return self.period is Period.LATE


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
    """How far into its season ``day`` is: 0 on its first day, ``DEPTH_STEPS`` on its last few,
    rounded to the nearest step, half up."""
    _, start, end = season_bounds(day)
    elapsed, length = (day - start).days, (end - start).days
    return (2 * elapsed * DEPTH_STEPS + length) // (2 * length)


def scene(now: datetime, timezone: str) -> Scene:
    """The scene at ``now`` (UTC-aware) in the household's time zone, an IANA name."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(ZoneInfo(timezone))
    season, _, _ = season_bounds(local.date())
    return Scene(period=period_at(local.time()), season=season, depth=season_depth(local.date()))
