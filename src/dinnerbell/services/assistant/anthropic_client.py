# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``ModelClient`` that calls Anthropic's Messages API through the official SDK. The only
module that imports ``anthropic`` (tests/test_architecture.py checks)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import anthropic
from anthropic.types.beta import BetaMessage
from pydantic import SecretStr

from dinnerbell.domain.assistant import TokenUsage
from dinnerbell.services.assistant.client import ModelCallFailed, ModelReply

TIMEOUT_SECONDS = 90.0


class AnthropicModelClient:
    """Sends each request once. The SDK's own retries are off: a timed-out request may still
    run and bill, and each request is covered by one reservation, so a retry would spend
    outside the cap."""

    def __init__(self, api_key: SecretStr, *, http_client: Any = None) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value(), max_retries=0, timeout=TIMEOUT_SECONDS, http_client=http_client
        )

    async def create(self, request: Mapping[str, Any]) -> ModelReply:
        try:
            message = await self._client.beta.messages.create(**request)
        except anthropic.APIStatusError:
            # The API answered with an error (bad request, rate limit, overloaded), so it ran nothing.
            raise ModelCallFailed(maybe_billed=False) from None
        except anthropic.APIError:
            # A timeout, a dropped connection, or a reply the SDK couldn't read: it may have run.
            raise ModelCallFailed(maybe_billed=True) from None
        return to_reply(message)

    async def close(self) -> None:
        await self._client.close()


def _after_last_fallback(blocks: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """The blocks to send back as the assistant turn. After a server-side fallback, what came
    before the last ``fallback`` block belongs to the model that declined, so only the blocks
    after it are echoed; the ``fallback`` marker itself is only an audit marker."""
    last = max((n for n, block in enumerate(blocks) if block.get("type") == "fallback"), default=-1)
    return list(blocks[last + 1 :])


def to_reply(message: BetaMessage) -> ModelReply:
    blocks = [block.to_dict(mode="json") for block in message.content]
    usage = message.usage
    parts: list[TokenUsage] = []
    for attempt in usage.iterations or ():
        if attempt.type not in ("message", "fallback_message"):
            continue  # compaction and advisor iterations: Dinnerbell asks for neither
        parts.append(
            TokenUsage(
                model=str(getattr(attempt, "model", None) or message.model),
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
        content=tuple(_after_last_fallback(blocks)),
        stop_reason=message.stop_reason,
        model=str(message.model),
        usage=tuple(parts),
    )
