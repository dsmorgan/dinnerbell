# SPDX-License-Identifier: AGPL-3.0-or-later
"""Waiting for the app's scheduler in tests that drive it with a fake clock."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient


def settle(client: TestClient) -> None:
    """Wait until the heartbeat has ticked and the scheduler's startup jobs have finished.

    The jobs read the fake clock when they start and write ``SchedulerState`` when they finish,
    so a test that moves the clock or sets the state by hand before then can have its values
    overwritten with older ones.
    """
    jobs = client.app.state.jobs  # type: ignore[attr-defined]
    deadline = time.monotonic() + 10
    while jobs.state.last_tick is None or jobs.running:
        assert time.monotonic() < deadline, "the scheduler's startup jobs never finished"
        time.sleep(0.01)
