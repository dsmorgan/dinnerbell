# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backup retention: keep the newest N daily copies and the newest copy from each of the last M
weeks, and the newest few one-off copies of each kind."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date

DAILY_COPIES = 14
WEEKLY_COPIES = 8
ONE_OFF_COPIES = 5
"""How many one-off backups of each kind (``pre-upgrade``, ``pre-reset``, ``pre-migrate``) to keep."""


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


def one_offs_to_delete(one_offs: Mapping[str, tuple[str, str]], *, keep: int = ONE_OFF_COPIES) -> set[str]:
    """Return the names of the one-off backups to delete: all but the ``keep`` newest of each kind.

    ``one_offs`` maps each backup's name to its kind, such as ``"upgrade"``, and the UTC time in
    its name, such as ``"20261023T123005Z"``, which sorts in time order as text.
    """
    by_kind: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    for name, (kind, stamp) in one_offs.items():
        by_kind[kind].append((stamp, name))
    doomed: set[str] = set()
    for copies in by_kind.values():
        copies.sort(reverse=True)
        doomed.update(name for _, name in copies[keep:])
    return doomed
