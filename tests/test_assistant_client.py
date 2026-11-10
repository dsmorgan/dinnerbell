# SPDX-License-Identifier: AGPL-3.0-or-later
"""``AnthropicModelClient`` against a mock transport: what it sends, how it reads a reply (a
server-side fallback included), and which failures may have been billed. Nothing here reaches
the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from dinnerbell.domain.assistant import TokenUsage
from dinnerbell.services.assistant.anthropic_client import AnthropicModelClient
from dinnerbell.services.assistant.client import ModelCallFailed
from dinnerbell.services.assistant.conversation import build_request
from dinnerbell.services.assistant.tools import TOOLS

pytestmark = pytest.mark.anyio

KEY = "sk-ant-test-not-a-real-key"


def message(
    content: list[dict[str, Any]], usage: dict[str, Any], *, model: str = "claude-sonnet-5-5"
) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage,
    }


def client_for(handler: Callable[[httpx2.Request], httpx2.Response]) -> AnthropicModelClient:
    return AnthropicModelClient(SecretStr(KEY), http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


REQUEST = build_request(
    "claude-sonnet-5-5", TOOLS, [{"role": "user", "content": "From Member 1: hi"}], keeps_history=True
)


async def test_it_sends_the_request_once_with_the_fallback_beta() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = message(
            [{"type": "text", "text": "Hello."}],
            {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2},
        )
        return httpx2.Response(200, json=body)

    client = client_for(handler)
    reply = await client.create(REQUEST)
    await client.close()
    [sent] = seen
    assert sent.url.path == "/v1/messages"
    assert KEY in sent.headers.get("x-api-key", "") + sent.headers.get("authorization", "")
    assert "server-side-fallback-2026-07-01" in sent.headers["anthropic-beta"]
    body = json.loads(sent.content)
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {"effort": "medium"}
    assert body["tools"][0]["strict"] is True and "betas" not in body
    assert reply.content == ({"type": "text", "text": "Hello."},)
    assert reply.stop_reason == "end_turn"
    assert reply.usage == (TokenUsage("claude-sonnet-5-5", 10, 5, cache_read_tokens=3, cache_write_tokens=2),)


async def test_a_fallback_reports_each_attempt_and_echoes_only_what_followed_it() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = message(
            [
                {"type": "text", "text": "partial"},
                {"type": "fallback", "from": {"model": "claude-sonnet-5-5"}, "to": {"model": "claude-sonnet-5"}},
                {"type": "text", "text": "Rescued."},
            ],
            {
                "input_tokens": 7,
                "output_tokens": 9,
                "iterations": [
                    {
                        "type": "message",
                        "input_tokens": 7,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                    {
                        "type": "fallback_message",
                        "model": "claude-sonnet-5",
                        "input_tokens": 7,
                        "output_tokens": 9,
                        "cache_read_input_tokens": 1,
                        "cache_creation_input_tokens": 0,
                    },
                ],
            },
            model="claude-sonnet-5",
        )
        return httpx2.Response(200, json=body)

    reply = await client_for(handler).create(REQUEST)
    assert reply.content == ({"type": "text", "text": "Rescued."},)
    assert reply.model == "claude-sonnet-5"
    assert reply.usage == (
        TokenUsage("claude-sonnet-5", 7, 2),  # the declined attempt names no model; the message's
        TokenUsage("claude-sonnet-5", 7, 9, cache_read_tokens=1),
    )


@pytest.mark.parametrize("status", [400, 401, 429, 529])
async def test_an_error_from_the_api_wasnt_billed(status: int) -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "no"}})

    with pytest.raises(ModelCallFailed) as failed:
        await client_for(handler).create(REQUEST)
    assert failed.value.maybe_billed is False
    assert calls == [1]  # never retried


async def test_a_dropped_connection_may_have_been_billed() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("gone", request=request)

    with pytest.raises(ModelCallFailed) as failed:
        await client_for(handler).create(REQUEST)
    assert failed.value.maybe_billed is True
