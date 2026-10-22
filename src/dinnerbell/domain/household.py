# SPDX-License-Identifier: AGPL-3.0-or-later
"""The household that owns everything in a deployment."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Household:
    id: int
    name: str
    timezone: str
