# SPDX-License-Identifier: AGPL-3.0-or-later
"""Jinja filters for showing call times in the household's time zone."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from jinja2 import Environment

from dinnerbell.services.badge_art import badge_name, badge_svg


def clock(moment: datetime | None, timezone: str) -> str:
    """A UTC moment as a wall-clock time in ``timezone``, such as "4:30 PM"."""
    if moment is None:
        return ""
    local = moment.astimezone(ZoneInfo(timezone))
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def wall(value: time | None) -> str:
    """A wall-clock time as a 12-hour string, such as "6:30 PM"."""
    if value is None:
        return ""
    return f"{value.hour % 12 or 12}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def hhmm(value: time | None) -> str:
    """The value of an ``<input type="time">``."""
    return "" if value is None else f"{value.hour:02d}:{value.minute:02d}"


def local_hhmm(moment: datetime | None, timezone: str) -> str:
    return "" if moment is None else hhmm(moment.astimezone(ZoneInfo(timezone)).time())


def day(value: date) -> str:
    return f"{value.strftime('%a %b')} {value.day}"


def elapsed(value: timedelta) -> str:
    """A duration in whole minutes, such as "1 hr 5 min" or "45 min"."""
    hours, minutes = divmod(int(value.total_seconds()) // 60, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} hr" if not minutes else f"{hours} hr {minutes} min"


def register(env: Environment) -> None:
    env.filters.update(clock=clock, wall=wall, hhmm=hhmm, local_hhmm=local_hhmm, day=day, elapsed=elapsed)
    env.globals.update(badge_svg=badge_svg, badge_name=badge_name)
