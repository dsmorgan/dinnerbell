# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shards must cover every test file exactly once, or CI would skip tests without saying so."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.shard import TESTS, WEIGHTS, collectable, parse, record, shards, weigh


@pytest.mark.parametrize("total", [1, 2, 8, 200])
def test_shards_cover_every_test_file_once(total: int) -> None:
    files = collectable()
    assert Path(__file__) in files
    groups = shards(files, total)
    assert len(groups) == total
    assert sorted(file for group in groups for file in group) == files


def test_shards_are_the_same_every_time() -> None:
    assert shards(collectable(), 8) == shards(collectable(), 8)


def test_the_shards_are_balanced_by_weight() -> None:
    files = collectable()
    weights = weigh(files, json.loads(WEIGHTS.read_text()))
    loads = [sum(weights[file] for file in group) for group in shards(files, 8)]
    assert max(loads) < 1.2 * (sum(loads) / 8)


def test_a_file_without_a_recorded_time_weighs_what_its_size_predicts() -> None:
    files = collectable()
    named = {str(file.relative_to(TESTS.parent)): 10.0 for file in files[:-1]}
    weights = weigh(files, named)
    assert weights[files[0]] == 10.0
    assert weights[files[-1]] > 0
    groups = shards(files, 3, named)
    assert sorted(file for group in groups for file in group) == files


def test_a_durations_report_adds_up_per_file() -> None:
    report = """
1.50s call     tests/test_a.py::test_one
0.25s setup    tests/test_a.py::test_one[x-1]
2.00s call     tests/test_b.py::test_two
slowest 20 durations
"""
    assert record(report) == {"tests/test_a.py": 1.8, "tests/test_b.py": 2.0}


def test_a_spec_names_a_shard_and_a_total() -> None:
    assert parse("2/8") == (2, 8)


@pytest.mark.parametrize("spec", ["", "4", "0/4", "5/4", "a/4", "2/b", "-1/4", "2/4/6", "2 / 4"])
def test_a_bad_spec_is_refused(spec: str) -> None:
    with pytest.raises(ValueError, match="shard must look like"):
        parse(spec)
