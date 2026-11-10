# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``ModelClient`` that calls Anthropic's Messages API through the official SDK. The only
module that imports ``anthropic`` (tests/test_architecture.py checks)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import anthropic
from anthropic.types.beta import BetaMessage
from pydantic import SecretStr

from dinnerbell.domain.assistant import TokenUsage, costlier
from dinnerbell.services.assistant.client import ModelCallFailed, ModelReply

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

    async def create(self, request: Mapping[str, Any]) -> ModelReply:
        opened = False
        try:
            async with self._client.beta.messages.stream(**request) as stream:
                opened = True  # from here the request is running, and may bill
                message = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            # An error answer before the stream opened ran nothing, unless the status says it may have.
            raise ModelCallFailed(
                maybe_billed=opened or exc.status_code in MAYBE_BILLED_STATUSES,
                status=exc.status_code,
                error_type=type(exc).__name__,
                request_id=exc.request_id,
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


def echoed_content(blocks: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """The blocks to send back as the assistant turn, after a server-side fallback too. Before
    the last ``fallback`` block, only text goes back: the declined model's thinking and tool
    calls don't, and its text is what the fallback model continued from. The ``fallback``
    markers themselves are audit markers and stay out. Dinnerbell sends no server tools, so
    there are no server-tool pairs to keep."""
    last = max((n for n, block in enumerate(blocks) if block.get("type") == "fallback"), default=-1)
    before = [block for block in blocks[:last] if block.get("type") == "text"]
    return [*before, *blocks[last + 1 :]]


def to_reply(message: BetaMessage, *, requested: str = "") -> ModelReply:
    """The reply, with one usage entry per attempt. An attempt that names no model is priced as
    the costlier of the requested model and the message's: during a stream the SDK moves the
    message's model to the fallback's, so either may be the one that ran it."""
    blocks = [block.to_dict(mode="json") for block in message.content]
    usage = message.usage
    parts: list[TokenUsage] = []
    for attempt in usage.iterations or ():
        if attempt.type not in ("message", "fallback_message"):
            continue  # compaction and advisor iterations: Dinnerbell asks for neither
        parts.append(
            TokenUsage(
                model=str(
                    getattr(attempt, "model", None) or costlier(requested or str(message.model), str(message.model))
                ),
                input_tokens=attempt.input_tokens,
                output_tokens=attempt.output_tokens,
                cache_read_tokens=attempt.cache_read_input_tokens,
                cache_write_tokens=attempt.cache_creation_input_tokens,
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
