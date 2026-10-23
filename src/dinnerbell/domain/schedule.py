# SPDX-License-Identifier: AGPL-3.0-or-later
"""Slot schedules and ranking weights, as stored in the household settings JSON (FR-1, FR-43).

Reading is forgiving: a missing or malformed value falls back to its default, so a bad edit
can't stop the scheduler. Writing is strict: ``check_schedule`` and ``check_weights`` reject
anything out of range with a message the admin can act on.

Times are wall-clock times in the household time zone. The caller passes the ``tzinfo``; this
module never reads time zone data itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

from dinnerbell.domain.catalog import Mode, sorted_modes
from dinnerbell.domain.meals import Slot

SLOTS_KEY = "slots"
WEIGHTS_KEY = "ranking_weights"
PROPOSAL_COUNT_KEY = "proposal_count"

MIN_WINDOW_MINUTES = 5
MAX_WINDOW_MINUTES = 6 * 60
MAX_REMINDER_MINUTES = 3 * 60
MIN_WEIGHT = 0.0
MAX_WEIGHT = 10.0
DEFAULT_PROPOSAL_COUNT = 3
MAX_PROPOSAL_COUNT = 5


class InvalidSchedule(ValueError):
    """A schedule or weight the admin entered that can't be saved. The message is safe to show."""


@dataclass(frozen=True, slots=True)
class SlotSchedule:
    """One slot's standing schedule. ``meal_time`` is the meal time used when nobody gives an
    eat window (FR-19 needs a fallback; the spec leaves it unset)."""

    enabled: bool
    call_time: time
    response_minutes: int
    vote_minutes: int
    reminder_minutes: int
    modes: frozenset[Mode]
    meal_time: time

    def opens_at(self, day: date, zone: tzinfo) -> datetime:
        """When this slot's call opens on ``day`` in ``zone``, in UTC. UTC, so adding a window
        to it counts elapsed time even across a daylight saving change."""
        return datetime.combine(day, self.call_time, tzinfo=zone).astimezone(UTC)

    @property
    def response_window(self) -> timedelta:
        return timedelta(minutes=self.response_minutes)

    @property
    def vote_window(self) -> timedelta:
        return timedelta(minutes=self.vote_minutes)


DEFAULT_SLOTS: dict[Slot, SlotSchedule] = {
    Slot.DINNER: SlotSchedule(
        enabled=True,
        call_time=time(15, 0),
        response_minutes=90,
        vote_minutes=45,
        reminder_minutes=15,
        modes=frozenset({Mode.PICKUP, Mode.DELIVERY}),
        meal_time=time(18, 30),
    ),
    Slot.LUNCH: SlotSchedule(
        enabled=False,
        call_time=time(10, 30),
        response_minutes=60,
        vote_minutes=30,
        reminder_minutes=15,
        modes=frozenset({Mode.PICKUP}),
        meal_time=time(12, 30),
    ),
}


@dataclass(frozen=True, slots=True)
class Weights:
    """Ranking weights (spec section 7). Each multiplies its component's raw value."""

    soft_preference: float = 1.0
    cuisine_variety: float = 3.0
    novelty: float = 1.0
    suggested: float = 2.0
    default_mode: float = 1.0
    volunteer_fit: float = 1.0


WEIGHT_NAMES = tuple(f.name for f in fields(Weights))


def _hhmm(value: object) -> time | None:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return None
    hours, minutes = value[:2], value[3:]
    if not (hours.isascii() and hours.isdigit() and minutes.isascii() and minutes.isdigit()):
        return None
    if int(hours) > 23 or int(minutes) > 59:
        return None
    return time(int(hours), int(minutes))


def format_hhmm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _minutes(value: object, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def _modes(value: object) -> frozenset[Mode] | None:
    if not isinstance(value, list) or not value:
        return None
    try:
        return frozenset(Mode(v) for v in value)
    except (ValueError, TypeError):
        return None


def _slot_from(raw: object, default: SlotSchedule) -> SlotSchedule:
    if not isinstance(raw, Mapping):
        return default
    enabled = raw.get("enabled")
    response = _minutes(raw.get("response_minutes"), MIN_WINDOW_MINUTES, MAX_WINDOW_MINUTES)
    vote = _minutes(raw.get("vote_minutes"), MIN_WINDOW_MINUTES, MAX_WINDOW_MINUTES)
    reminder = _minutes(raw.get("reminder_minutes"), 0, MAX_REMINDER_MINUTES)
    return SlotSchedule(
        enabled=enabled if isinstance(enabled, bool) else default.enabled,
        call_time=_hhmm(raw.get("call_time")) or default.call_time,
        response_minutes=response if response is not None else default.response_minutes,
        vote_minutes=vote if vote is not None else default.vote_minutes,
        reminder_minutes=reminder if reminder is not None else default.reminder_minutes,
        modes=_modes(raw.get("modes")) or default.modes,
        meal_time=_hhmm(raw.get("meal_time")) or default.meal_time,
    )


def slot_schedules(settings: Mapping[str, Any]) -> dict[Slot, SlotSchedule]:
    """Every slot's schedule from the household settings, with defaults for anything missing."""
    raw = settings.get(SLOTS_KEY)
    stored = raw if isinstance(raw, Mapping) else {}
    return {slot: _slot_from(stored.get(slot.value), default) for slot, default in DEFAULT_SLOTS.items()}


def slot_schedule(settings: Mapping[str, Any], slot: Slot) -> SlotSchedule:
    return slot_schedules(settings)[slot]


def _weight(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value) if MIN_WEIGHT <= value <= MAX_WEIGHT else default


def weights(settings: Mapping[str, Any]) -> Weights:
    raw = settings.get(WEIGHTS_KEY)
    stored = raw if isinstance(raw, Mapping) else {}
    defaults = Weights()
    return Weights(**{name: _weight(stored.get(name), getattr(defaults, name)) for name in WEIGHT_NAMES})


def proposal_count(settings: Mapping[str, Any]) -> int:
    value = _minutes(settings.get(PROPOSAL_COUNT_KEY), 1, MAX_PROPOSAL_COUNT)
    return DEFAULT_PROPOSAL_COUNT if value is None else value


def check_schedule(schedule: SlotSchedule) -> SlotSchedule:
    """Raise InvalidSchedule unless every field is in range."""
    if not MIN_WINDOW_MINUTES <= schedule.response_minutes <= MAX_WINDOW_MINUTES:
        raise InvalidSchedule(f"The response window runs {MIN_WINDOW_MINUTES} to {MAX_WINDOW_MINUTES} minutes.")
    if not MIN_WINDOW_MINUTES <= schedule.vote_minutes <= MAX_WINDOW_MINUTES:
        raise InvalidSchedule(f"The vote window runs {MIN_WINDOW_MINUTES} to {MAX_WINDOW_MINUTES} minutes.")
    if not 0 <= schedule.reminder_minutes < schedule.response_minutes:
        raise InvalidSchedule("The reminder must come before the response window closes.")
    if not schedule.modes:
        raise InvalidSchedule("Pick at least one default way to get the meal.")
    return schedule


def check_weights(values: Weights, count: int) -> tuple[Weights, int]:
    for name in WEIGHT_NAMES:
        if not MIN_WEIGHT <= getattr(values, name) <= MAX_WEIGHT:
            raise InvalidSchedule(f"Weights run from {MIN_WEIGHT:g} to {MAX_WEIGHT:g}.")
    if not 1 <= count <= MAX_PROPOSAL_COUNT:
        raise InvalidSchedule(f"Show 1 to {MAX_PROPOSAL_COUNT} proposals.")
    return values, count


def slot_to_json(schedule: SlotSchedule) -> dict[str, Any]:
    return {
        "enabled": schedule.enabled,
        "call_time": format_hhmm(schedule.call_time),
        "response_minutes": schedule.response_minutes,
        "vote_minutes": schedule.vote_minutes,
        "reminder_minutes": schedule.reminder_minutes,
        "modes": [m.value for m in sorted_modes(schedule.modes)],
        "meal_time": format_hhmm(schedule.meal_time),
    }


def weights_to_json(values: Weights) -> dict[str, float]:
    return {name: getattr(values, name) for name in WEIGHT_NAMES}


def default_settings() -> dict[str, Any]:
    """The schedule and ranking settings a household starts with."""
    return {
        SLOTS_KEY: {slot.value: slot_to_json(schedule) for slot, schedule in DEFAULT_SLOTS.items()},
        WEIGHTS_KEY: weights_to_json(Weights()),
        PROPOSAL_COUNT_KEY: DEFAULT_PROPOSAL_COUNT,
    }


def parse_hhmm(value: str, what: str) -> time:
    parsed = _hhmm(value.strip())
    if parsed is None:
        raise InvalidSchedule(f"Enter the {what} as HH:MM, such as 15:00.")
    return parsed
