# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers for the assistant's tool tests."""

from __future__ import annotations

from datetime import UTC, datetime

from dinnerbell.domain.assistant_drafts import MAX_DIRECT_ACTIONS, Draft, DraftKind
from dinnerbell.services.assistant.tools import ToolSession

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_ACTED = Draft(0, 0, 0, "", DraftKind.VOTE, {}, "", (), _EPOCH, _EPOCH, direct=True)


def drafting(session: ToolSession) -> ToolSession:
    """``session`` as if its message had acted at once ``MAX_DIRECT_ACTIONS`` times already
    (#374), so every tool drafts, private or not: for tests of the drafts themselves."""
    session.records.extend([_ACTED] * (MAX_DIRECT_ACTIONS - len(session.records)))
    return session
