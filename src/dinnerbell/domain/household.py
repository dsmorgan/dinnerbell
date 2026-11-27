# SPDX-License-Identifier: AGPL-3.0-or-later
"""The household that owns everything in a deployment."""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NAME_MAX_LENGTH = 80
"""As ``DINNERBELL_HOUSEHOLD_NAME`` allows."""


class InvalidHouseholdInput(ValueError):
    """A household name or time zone that can't be stored. The message says what to enter."""


@dataclass(frozen=True, slots=True)
class Household:
    id: int
    name: str
    timezone: str


def is_timezone(value: str) -> bool:
    """Whether ``value`` names an IANA time zone, such as ``America/Chicago``."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def check_timezone(value: str) -> str:
    zone = value.strip()
    if not zone:
        raise InvalidHouseholdInput("Enter an IANA time zone name, such as America/Chicago.")
    if not is_timezone(zone):
        raise InvalidHouseholdInput(f"{zone} isn't an IANA time zone name. Enter one such as America/Chicago.")
    return zone


def check_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise InvalidHouseholdInput("Enter a household name.")
    if len(name) > NAME_MAX_LENGTH:
        raise InvalidHouseholdInput(f"Enter a household name of at most {NAME_MAX_LENGTH} characters.")
    return name
