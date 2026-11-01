# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small helpers for reading HTML form values. Validation rules live in the domain layer."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated

from fastapi import Path


class BadFormValue(ValueError):
    """A value a person typed doesn't fit its field. The message is safe to show."""


def checked(value: str | None) -> bool:
    """A checkbox is on when the browser sends its field at all."""
    return value is not None


def split_names(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated list of names, dropping blanks."""
    return tuple(part for part in (value or "").split(",") if part.strip())


def optional_int(value: str | None, message: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise BadFormValue(message) from None


def choice[E: StrEnum](enum: type[E], value: str | None) -> E | None:
    """The enum member named by ``value``, or None for a blank or unknown value."""
    try:
        return enum(value) if value else None
    except ValueError:
        return None


def iso_date(value: str | None, message: str) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        raise BadFormValue(message) from None


MAX_ID = 2**63 - 1
"""The largest row ID SQLite stores; anything bigger can't name a row and overflows the driver."""


def parse_id(value: str | None) -> int | None:
    """A positive row ID typed as ASCII digits, or None. ``str.isdigit`` alone also accepts
    digits such as "²" that ``int()`` rejects."""
    if not value or not (value.isascii() and value.isdigit()):
        return None
    number = int(value)
    return number if 1 <= number <= MAX_ID else None


PAST_THE_END = 10**30
"""The page a ``?page=`` number longer than 30 digits reads as: past the end of any list."""


def page_number(value: str) -> int:
    """A ``?page=`` number as typed, or 1 when it isn't a whole number, such as "abc" or "2.5".
    ``page_of`` moves a number past either end onto the nearest page, so any value lands on a
    page instead of an error. A number too long to bother converting is past the end: the
    first page when it's negative, else the last."""
    negative = value.startswith("-")
    digits = value.removeprefix("-")
    if not (digits.isascii() and digits.isdigit()):
        return 1
    if len(digits) > 30:
        return 1 if negative else PAST_THE_END
    return int(value)


RowId = Annotated[int, Path(ge=1, le=MAX_ID)]
"""A row ID in a URL path. Anything else, such as 0 or a 25-digit number, is a 404."""
