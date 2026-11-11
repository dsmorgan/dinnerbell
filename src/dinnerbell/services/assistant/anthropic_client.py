# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``ModelClient`` that calls Anthropic's Messages API through the official SDK. The only
module that imports ``anthropic`` (tests/test_architecture.py checks)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import anthropic
from anthropic.types.beta import BetaMessage
from pydantic import SecretStr

from dinnerbell.domain.assistant import PRICE_TABLE, TokenUsage, costlier
from dinnerbell.services.assistant.client import ModelCallFailed, ModelEvents, ModelReply

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 300.0
"""Each read on the stream, and the connection. A stream sends events as the model writes, so a
long answer never waits this long for one."""
MAYBE_BILLED_STATUSES = frozenset({408, 502, 504})
"""Errors that can come after the API ran the request: a timeout, or a gateway that lost the reply."""


class AnthropicModelClient:
    """Streams each request once and returns the final message (``get_final_message``), so a
    long answer can't hit a request timeout. The SDK's own retries are off: a timed-out request
    may still run and bill, and each request is covered by one reservation, so a retry would
    spend outside the cap."""

    def __init__(self, api_key: SecretStr, *, http_client: Any = None) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value(), max_retries=0, timeout=TIMEOUT_SECONDS, http_client=http_client
        )

    async def create(
        self, request: Mapping[str, Any], *, time_left: float, events: ModelEvents | None = None
    ) -> ModelReply:
        """``time_left`` bounds each connection and read of this request, as ``TIMEOUT_SECONDS``
        does, but never past the message's deadline. The SDK's timeout bounds each phase, not
        the whole stream: ``conversation`` cancels the call when the deadline passes. With
        ``events``, each text block, text delta, and tool call is reported as it arrives."""
        opened = False
        try:
            limit = max(0.0, min(time_left, TIMEOUT_SECONDS))
            async with self._client.beta.messages.stream(**request, timeout=limit) as stream:
                opened = True  # from here the request is running, and may bill
                if events is not None:
                    hearing: ModelEvents | None = events
                    async for event in stream:
                        hearing = _report(hearing, event)
                message = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            # An error answer before the stream opened ran nothing, unless the status says it may have.
            raise ModelCallFailed(
                maybe_billed=opened or exc.status_code in MAYBE_BILLED_STATUSES,
                status=exc.status_code,
                error_type=type(exc).__name__,
                request_id=exc.request_id,
                detail=error_detail(exc.body),
            ) from None
        except anthropic.APIError as exc:
            # A timeout, a dropped connection, or a reply the SDK couldn't read: it may have run.
            raise ModelCallFailed(maybe_billed=True, error_type=type(exc).__name__) from None
        try:
            return to_reply(message, requested=str(request.get("model", "")))
        except Exception:
            raise ModelCallFailed(
                maybe_billed=True, error_type="unreadable reply", request_id=getattr(message, "_request_id", None)
            ) from None

    async def close(self) -> None:
        await self._client.close()


MAX_DETAIL_CHARS = 300


def error_detail(body: object) -> str:
    """The API's error type and message from an error answer's body, on one line and clipped, so
    the log says why a request was refused, such as which field a 400 names."""
    error = body.get("error", body) if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    kind, message = error.get("type"), error.get("message")
    text = ": ".join(str(part) for part in (kind, message) if isinstance(part, str) and part)
    return " ".join(text.split())[:MAX_DETAIL_CHARS]


def _report(events: ModelEvents | None, event: Any) -> ModelEvents | None:
    """Tell ``events`` about one stream event. If it raises, it hears nothing more of this
    call, and the call goes on: the reply never depends on who's listening."""
    if events is None:
        return None
    try:
        if event.type == "content_block_start":
            kind = event.content_block.type
            if kind == "text":
                events.text_started()
            elif kind == "tool_use":
                events.tool_started()
        elif event.type == "content_block_delta" and event.delta.type == "text_delta":
            events.text(event.delta.text)
    except Exception:
        log.exception("assistant reply listener broke; the reply goes on without it")
        return None
    return events


def echoed_content(blocks: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """The blocks to send back as the assistant turn. Without a server-side fallback, exactly
    the blocks the model sent, unchanged, as preserved thinking requires. After one, only text
    goes back from before the last ``fallback`` block: the declined model's thinking and tool
    calls don't, and its text is what the fallback model continued from. The ``fallback``
    markers themselves are audit markers and stay out. Dinnerbell sends no server tools, so
    there are no server-tool pairs to keep."""
    last = max((n for n, block in enumerate(blocks) if block.get("type") == "fallback"), default=-1)
    if last < 0:
        return list(blocks)
    before = [block for block in blocks[:last] if block.get("type") == "text"]
    return [*before, *blocks[last + 1 :]]


KNOWN_ITERATIONS = ("message", "fallback_message")
"""Iteration types Dinnerbell expects. It asks for no compaction and no advisor, so anything
else is unexpected: it's still priced and counted, and logged."""


def _tokens(attempt: object, name: str) -> int:
    value = getattr(attempt, name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def to_reply(message: BetaMessage, *, requested: str = "") -> ModelReply:
    """The reply, with one usage entry per billed attempt. An attempt that names no model is
    priced as the costliest of the requested model, the models its fallback can run on, and the
    message's model: during a stream the SDK moves the message's model to the fallback's, so
    any of them may be the one that ran it."""
    blocks = [block.to_dict(mode="json") for block in message.content]
    usage = message.usage
    card = PRICE_TABLE.get(requested)
    unnamed = costlier(*(requested or str(message.model),), *(card.falls_back_to if card else ()), str(message.model))
    parts: list[TokenUsage] = []
    for attempt in usage.iterations or ():
        kind = str(getattr(attempt, "type", ""))
        if kind not in KNOWN_ITERATIONS:
            log.error("assistant reply has an unexpected iteration", extra={"iteration_type": kind[:40]})
        named = getattr(attempt, "model", None) if kind in KNOWN_ITERATIONS else None
        parts.append(
            TokenUsage(
                model=str(named or unnamed),
                input_tokens=_tokens(attempt, "input_tokens"),
                output_tokens=_tokens(attempt, "output_tokens"),
                cache_read_tokens=_tokens(attempt, "cache_read_input_tokens"),
                cache_write_tokens=_tokens(attempt, "cache_creation_input_tokens"),
            )
        )
    if not parts:
        parts.append(
            TokenUsage(
                model=str(message.model),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
            )
        )
    return ModelReply(
        content=tuple(echoed_content(blocks)),
        stop_reason=message.stop_reason,
        model=str(message.model),
        usage=tuple(parts),
    )
