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

What Dinnerbone does follows the call he's watching (``followed_state``) and the time of day
(``pose``). The server renders the pose, so the den is right with no script and under reduced
motion.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from dinnerbell.domain.events import Event, EventKind, EventState

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
    """How far into its season ``day`` is, rounded to the nearest step, half up: 0 for its first
    few days (about 4 of a 90-day season) and ``DEPTH_STEPS`` for its last few."""
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
    at night."""
    if call in CALL_POSES:
        return CALL_POSES[call]
    return CLOCK_POSES[scene.period]


def followed_state(events: Iterable[Event], now: datetime, timezone: str) -> EventState | None:
    """The state of the call he follows among ``events``, the calls on Today in their order.

    It's the first call that's taking answers, voting, or decided. With none, a call marked done
    today keeps him asleep for the rest of the time of day it was done in, so a dinner done at
    7 PM puts him to bed, and a breakfast done at 9 AM lets him up for lunch. Offers and calls
    still to be posted don't count."""
    calls = [e for e in events if e.kind is EventKind.CALL]
    for event in calls:
        if event.state in FOLLOWED_STATES:
            return event.state
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    for event in calls:
        if event.state is EventState.DONE and event.done_at is not None:
            done = event.done_at.astimezone(zone)
            if done.date() == local.date() and period_at(done.time()) is period_at(local.time()) and done <= local:
                return EventState.DONE
    return None


FIT_STEPS = (8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 40)
"""The greeting's width buckets, in narrow characters, for the CSS that shrinks it to fit."""


def fit_step(text: str) -> int:
    """How wide ``text`` reads, rounded up to a step in ``FIT_STEPS`` (the last for anything
    longer): a wide character, such as CJK or most emoji, counts as two narrow ones."""
    width = sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)
    return next((step for step in FIT_STEPS if width <= step), FIT_STEPS[-1])
