# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small helpers for reading HTML form values. Validation rules live in the domain layer."""

from __future__ import annotations

from datetime import date
from enum import StrEnum


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
