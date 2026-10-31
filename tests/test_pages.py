# SPDX-License-Identifier: AGPL-3.0-or-later
"""Splitting a list into pages (#153)."""

from __future__ import annotations

import pytest

from dinnerbell.domain.pages import page_of


def test_page_boundaries() -> None:
    first = page_of(1, 45, 20)
    assert (first.number, first.count, first.offset, first.first, first.last) == (1, 3, 0, 1, 20)
    assert not first.has_previous and first.has_next
    middle = page_of(2, 45, 20)
    assert (middle.offset, middle.first, middle.last) == (20, 21, 40)
    assert middle.has_previous and middle.has_next
    last = page_of(3, 45, 20)
    assert (last.offset, last.first, last.last) == (40, 41, 45)
    assert last.has_previous and not last.has_next
    exact = page_of(2, 40, 20)
    assert (exact.count, exact.first, exact.last, exact.has_next) == (2, 21, 40, False)


@pytest.mark.parametrize(("asked", "shown"), [(0, 1), (-5, 1), (4, 3), (10**30, 3)])
def test_a_page_out_of_range_is_the_nearest_page(asked: int, shown: int) -> None:
    assert page_of(asked, 45, 20).number == shown


def test_an_empty_list_has_one_empty_page() -> None:
    empty = page_of(3, 0, 20)
    assert (empty.number, empty.count, empty.offset, empty.first, empty.last) == (1, 1, 0, 0, 0)
    assert not empty.has_previous and not empty.has_next


def test_a_page_holds_at_least_one_item() -> None:
    with pytest.raises(ValueError, match="at least one"):
        page_of(1, 10, 0)
