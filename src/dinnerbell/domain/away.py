# SPDX-License-Identifier: AGPL-3.0-or-later
"""Away (FR-63, #196): a member who'll be gone skips the calls on those days.

A member has at most one window: a first and a last day away, whole days in the household's
time zone, both included. Calls on those days answer them Out, marked as away, so nobody waits
on them, they get no ask and no reminder, and voting never picks them to coordinate. They can
still answer In, or join late, on any call; that answer is theirs and holds for that call only.

The window is upcoming or under way; once its last day has passed it means nothing, and the
next one replaces it. **Away until…** where a call asks sets one from the call's day through the
day before the day they're back; Preferences sets the first and last day directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from dinnerbell.domain.events import EventRuleViolation

MAX_AWAY_DAYS = 365
"""The longest window, in days. A member away longer is better deactivated."""


class AwayRuleViolation(EventRuleViolation):
    code = "away"


@dataclass(frozen=True, slots=True)
class Away:
    """A member's away window: ``first`` through ``last``, both days included."""

    first: date
    last: date

    @property
    def back(self) -> date:
        """The day they're back: the day after the last one away. "Away until" names it."""
        return self.last + timedelta(days=1)

    def covers(self, day: date) -> bool:
        return self.first <= day <= self.last

    def current(self, today: date) -> bool:
        """Upcoming or under way: its last day hasn't passed."""
        return self.last >= today

    def under_way(self, today: date) -> bool:
        return self.covers(today)


def check_away(first: date, last: date, today: date) -> Away:
    """A window the member may set: the last day on or after the first and not in the past,
    and at most ``MAX_AWAY_DAYS`` long. The first day may be in the past (a window under way)."""
    if last < first:
        raise AwayRuleViolation("The last day away can't be before the first.", code="away_backwards")
    if last < today:
        raise AwayRuleViolation("The last day away has passed already. Pick a later one.", code="away_past")
    if (last - first).days + 1 > MAX_AWAY_DAYS:
        raise AwayRuleViolation(f"Pick at most {MAX_AWAY_DAYS} days away.", code="away_too_long")
    return Away(first, last)


def away_from_call(call_day: date, back: date, today: date, current: Away | None = None) -> Away:
    """**Away until…** on a call: away from the call's day through the day before ``back``. A
    ``current`` window that overlaps or touches those days, on either side, is joined with them
    into one stretch, from the earliest first day to the latest last day: away Monday to
    Wednesday, **Away until** Sunday on Thursday's call is Monday to Saturday, and away Friday
    to Sunday, **Away until** Friday on Thursday's call is Thursday to Sunday. A window apart
    from them is replaced. Shortening one is **I'm back**, or Preferences."""
    if back <= call_day:
        raise AwayRuleViolation("Pick a day after this call's day.", code="away_backwards")
    first, last = call_day, back - timedelta(days=1)
    day = timedelta(days=1)
    if current is not None and current.first <= last + day and first <= current.last + day:
        first, last = min(first, current.first), max(last, current.last)
    return check_away(first, last, today)
