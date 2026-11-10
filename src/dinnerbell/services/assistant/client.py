# SPDX-License-Identifier: AGPL-3.0-or-later
"""The seam between the assistant and a language model.

``converse`` builds each request as the exact keyword arguments of Anthropic's Messages API
(``model``, ``system``, ``messages``, ``tools``, and the rest), measures it, reserves its worst
case, and hands it to a ``ModelClient``. Production uses ``anthropic_client``; tests use a fake
that records each request and never reaches the network.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from dinnerbell.domain.assistant import TokenUsage


@dataclass(frozen=True, slots=True)
class ModelReply:
    """What a model call returned. ``content`` is the response's content blocks as plain dicts,
    ready to send back as the assistant turn; ``usage`` has one entry per attempt the API
    reports (a refused attempt and its fallback are two)."""

    content: tuple[Mapping[str, Any], ...]
    stop_reason: str | None
    model: str
    usage: tuple[TokenUsage, ...]


class ModelCallFailed(Exception):
    """The call returned no reply. ``maybe_billed`` is False only when the API answered with an
    error, so it ran nothing; otherwise (a timeout, a dropped connection) the request may have
    run and been billed, and its reservation stands."""

    def __init__(self, *, maybe_billed: bool) -> None:
        super().__init__("the model call failed")
        self.maybe_billed = maybe_billed


class ModelClient(Protocol):
    async def create(self, request: Mapping[str, Any]) -> ModelReply:
        """Send ``request``, the Messages API's keyword arguments. Raise ``ModelCallFailed``
        instead of any client error, and never retry: each request has its own reservation."""
        ...
