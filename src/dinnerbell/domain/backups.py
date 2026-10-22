# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backup retention: keep the newest N daily copies and the newest copy from each of the last M weeks."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

DAILY_COPIES = 14
WEEKLY_COPIES = 8


def backups_to_keep(
    backup_dates: Iterable[date], *, daily: int = DAILY_COPIES, weekly: int = WEEKLY_COPIES
) -> set[date]:
    """Return the backup dates to keep.

    Daily: the ``daily`` most recent backups. Weekly: for each of the ``weekly`` most recent ISO
    weeks that have a backup, the newest backup in that week. The two sets may overlap, as in
    other grandfather-father-son schemes, so recent weeks are covered by both.
    """
    ordered = sorted(set(backup_dates), reverse=True)
    keep = set(ordered[:daily])
    newest_per_week: dict[tuple[int, int], date] = {}
    for day in ordered:
        iso = day.isocalendar()
        newest_per_week.setdefault((iso.year, iso.week), day)
    weeks = sorted(newest_per_week, reverse=True)[:weekly]
    keep.update(newest_per_week[week] for week in weeks)
    return keep


def backups_to_delete(backup_dates: Iterable[date], **limits: int) -> set[date]:
    dates = set(backup_dates)
    return dates - backups_to_keep(dates, **limits)
