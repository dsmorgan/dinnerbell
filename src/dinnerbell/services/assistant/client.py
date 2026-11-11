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

from dinnerbell.domain.assistant import PRICE_TABLE, TokenUsage

FALLBACK_BETA = "server-side-fallback-2026-07-01"
"""Anthropic's server-side refusal fallback, in its ``"default"`` form (Claude API only)."""


def with_fallback(request: dict[str, Any]) -> dict[str, Any]:
    """``request`` with the server-side refusal fallback when its model has one. The reservation
    covers each model the fallback can run on (``domain.assistant.reservation_micros``)."""
    card = PRICE_TABLE.get(str(request["model"]))
    if card is not None and card.falls_back_to:
        request["fallbacks"] = "default"
        request["betas"] = [FALLBACK_BETA]
    return request


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
    error that means it ran nothing; otherwise (a timeout, a dropped connection, a gateway
    error, a reply that couldn't be read) the request may have run and been billed, and its
    reservation stands. ``status``, ``error_type``, and ``request_id`` are for the log: none is
    secret."""

    def __init__(
        self, *, maybe_billed: bool, status: int | None = None, error_type: str = "", request_id: str | None = None
    ) -> None:
        super().__init__("the model call failed")
        self.maybe_billed = maybe_billed
        self.status = status
        self.error_type = error_type
        self.request_id = request_id

    def log_fields(self) -> dict[str, object]:
        return {
            "maybe_billed": self.maybe_billed,
            "status": self.status,
            "error_type": self.error_type,
            "anthropic_request_id": self.request_id,
        }


class ModelEvents(Protocol):
    """What a model call reports as the model writes, for a reply shown while it's written
    (``services.assistant.streaming``). Each method is synchronous and must not raise; a
    client that calls one ignores anything it raises. The reply ``create`` returns is the same
    either way."""

    def text_started(self) -> None:
        """A text block starts."""
        ...

    def text(self, delta: str) -> None:
        """More of the current text block."""
        ...

    def tool_started(self) -> None:
        """A tool call starts: this call is a tool round, not the final answer."""
        ...


class ModelClient(Protocol):
    async def create(
        self, request: Mapping[str, Any], *, time_left: float, events: ModelEvents | None = None
    ) -> ModelReply:
        """Send ``request``, the Messages API's keyword arguments. Raise ``ModelCallFailed``
        instead of any client error, and never retry: each request has its own reservation.
        ``time_left`` is the seconds left of the message's deadline (``conversation``), which
        also cancels the call when they run out. ``events``, when given, hears the reply's
        text as it's written; ``conversation`` passes it only to show a reply in Slack."""
        ...
