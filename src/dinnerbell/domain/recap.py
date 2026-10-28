# SPDX-License-Identifier: AGPL-3.0-or-later
"""When the monthly recap is due (FR-40), and the month it covers, in the household's time zone.

The recap for a month posts on the first day of the next one, from ``POST_HOUR``. A household
setting, ``recap_posted``, holds the last month recapped (``2026-10``), so it posts once: a
restart or a second tick finds it set. If the app was down on the first, the recap posts on the
next tick up to ``WINDOW_DAYS`` into the month, and after that the month is left out: news a week
late isn't news. Only the previous month is ever recapped.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MARKER = re.compile(r"\d{4}-\d{2}")

SETTINGS_KEY = "recap_posted"
POST_HOUR = 9
"""The local hour the recap goes out from: a morning post, not a midnight one."""
WINDOW_DAYS = 7
"""How many days into a month a missed recap for the month before still posts."""

MAX_THANKS = 15
MAX_CUISINES = 12
MAX_ITEM_NAMES = 5


def month_key(first: date) -> str:
    return f"{first:%Y-%m}"


def previous_month(today: date) -> date:
    """The first day of the month before ``today``'s."""
    return (today.replace(day=1) - timedelta(days=1)).replace(day=1)


def next_month(first: date) -> date:
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1)


def due_month(local_now: datetime, posted: object) -> date | None:
    """The month to recap at ``local_now`` (a time in the household zone), or None when none is
    due: it is too early on the first, past the window, or that month is already recapped.
    ``posted`` is the stored marker; anything but a ``YYYY-MM`` string counts as none."""
    today = local_now.date()
    if today.day > WINDOW_DAYS or (today.day == 1 and local_now.hour < POST_HOUR):
        return None
    month = previous_month(today)
    if isinstance(posted, str) and MARKER.fullmatch(posted) and posted >= month_key(month):
        return None
    return month


def bounds_utc(first: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """The month starting ``first`` as UTC times: from local midnight on its first day up to local
    midnight on the next month's, so a 25-hour or 23-hour day at a DST change falls in the right month."""
    start = datetime.combine(first, time(), tzinfo=zone)
    end = datetime.combine(next_month(first), time(), tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)
