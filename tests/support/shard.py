# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run one shard of the suite, so CI can run the shards in parallel jobs (.github/workflows/ci.yml).

``DINNERBELL_TEST_SHARD=2/6`` keeps every sixth test, starting at the second, in sorted order.
Sorted order puts the tests of one module side by side, so a module's slow tests land in
different shards. Without the variable, every test runs."""

from __future__ import annotations

import os
from collections.abc import Sequence

import pytest


def parse(spec: str) -> tuple[int, int]:
    """``"2/6"`` is shard 2 of 4. Raises ``ValueError`` for anything else."""
    index, slash, total = spec.partition("/")
    if not (slash and index.isdecimal() and total.isdecimal()) or not 1 <= int(index) <= int(total):
        raise ValueError(f"DINNERBELL_TEST_SHARD must look like 2/6 (shard, then total), not {spec!r}")
    return int(index), int(total)


def select[T](ordered: Sequence[T], index: int, total: int) -> list[T]:
    """Shard ``index`` of ``total`` (counting from 1) of ``ordered``."""
    return list(ordered[index - 1 :: total])


@pytest.hookimpl(trylast=True)  # after tests/conftest.py checks the flag marks on every test
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spec = os.environ.get("DINNERBELL_TEST_SHARD")
    if not spec:
        return
    ordered = sorted(items, key=lambda item: item.nodeid)
    keep = {id(item) for item in select(ordered, *parse(spec))}
    config.hook.pytest_deselected(items=[item for item in items if id(item) not in keep])
    items[:] = [item for item in items if id(item) in keep]
