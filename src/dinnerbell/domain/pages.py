# SPDX-License-Identifier: AGPL-3.0-or-later
"""Splitting a long list into numbered pages, as History and the Catalog do (#153)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a list: ``number`` counts from 1 and is always a page that exists, so an empty
    list still has page 1 of 1."""

    number: int
    count: int
    size: int
    total: int

    @property
    def offset(self) -> int:
        """How many items come before this page."""
        return (self.number - 1) * self.size

    @property
    def first(self) -> int:
        """The position of the page's first item, counting from 1; 0 on an empty list."""
        return min(self.offset + 1, self.total)

    @property
    def last(self) -> int:
        """The position of the page's last item, counting from 1."""
        return min(self.offset + self.size, self.total)

    @property
    def has_previous(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.count


def page_of(requested: int, total: int, size: int) -> Page:
    """The page ``requested`` asks for, moved onto the nearest page that exists: below 1 is the
    first page, and past the end is the last, so an old bookmark still lands somewhere useful."""
    if size < 1:
        raise ValueError("a page holds at least one item")
    count = max(1, -(-total // size))
    return Page(number=min(max(requested, 1), count), count=count, size=size, total=total)
