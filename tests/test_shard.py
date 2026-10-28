# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shards must cover every test exactly once, or CI would skip tests without saying so."""

from __future__ import annotations

import pytest

from tests.support.shard import parse, select


@pytest.mark.parametrize("total", [1, 2, 4, 7])
@pytest.mark.parametrize("count", [0, 1, 3, 4, 101])
def test_shards_cover_every_test_once(total: int, count: int) -> None:
    ordered = list(range(count))
    shards = [select(ordered, index, total) for index in range(1, total + 1)]
    assert sorted(item for shard in shards for item in shard) == ordered
    assert max(map(len, shards)) - min(map(len, shards)) <= 1


def test_a_spec_names_a_shard_and_a_total() -> None:
    assert parse("2/4") == (2, 4)


@pytest.mark.parametrize("spec", ["", "4", "0/4", "5/4", "a/4", "2/b", "-1/4", "2/4/6", "2 / 4"])
def test_a_bad_spec_is_refused(spec: str) -> None:
    with pytest.raises(ValueError, match="DINNERBELL_TEST_SHARD"):
        parse(spec)
