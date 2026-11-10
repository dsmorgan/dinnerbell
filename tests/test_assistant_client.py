# SPDX-License-Identifier: AGPL-3.0-or-later
"""``AnthropicModelClient`` against a mock transport that streams server-sent events: what it
sends, how it reads a reply (a server-side fallback included), and which failures may have been
billed. Nothing here reaches the network."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from dinnerbell.domain.assistant import TokenUsage
from dinnerbell.services.assistant.anthropic_client import AnthropicModelClient, echoed_content
from dinnerbell.services.assistant.client import ModelCallFailed
from dinnerbell.services.assistant.conversation import build_request
from dinnerbell.services.assistant.tools import TOOLS

pytestmark = pytest.mark.anyio

KEY = "sk-ant-test-not-a-real-key"
REQUEST = build_request(
    "claude-sonnet-5-5", TOOLS, [{"role": "user", "content": "From Member 1: hi"}], keeps_history=True
)


def _event(kind: str, data: dict[str, Any]) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"


def sse(
    blocks: Iterable[dict[str, Any]],
    *,
    start_usage: dict[str, Any],
    end_usage: dict[str, Any],
    model: str = "claude-sonnet-5-5",
    stop_reason: str = "end_turn",
) -> str:
    """A streamed message: each text block arrives as one delta, any other block whole."""
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": start_usage,
    }
    events = [_event("message_start", {"message": message})]
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            events.append(
                _event("content_block_start", {"index": index, "content_block": {"type": "text", "text": ""}})
            )
            delta = {"type": "text_delta", "text": block["text"]}
            events.append(_event("content_block_delta", {"index": index, "delta": delta}))
        else:
            events.append(_event("content_block_start", {"index": index, "content_block": block}))
        events.append(_event("content_block_stop", {"index": index}))
    delta = {"stop_reason": stop_reason, "stop_sequence": None}
    events.append(_event("message_delta", {"delta": delta, "usage": end_usage}))
    events.append(_event("message_stop", {}))
    return "".join(events)


def streamed(body: str) -> httpx2.Response:
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def client_for(handler: Callable[[httpx2.Request], httpx2.Response]) -> AnthropicModelClient:
    return AnthropicModelClient(SecretStr(KEY), http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


async def test_it_streams_the_request_once_with_the_fallback_beta() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return streamed(
            sse(
                [{"type": "text", "text": "Hello."}],
                start_usage={
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 2,
                },
                end_usage={"output_tokens": 5},
            )
        )

    client = client_for(handler)
    reply = await client.create(REQUEST)
    await client.close()
    [sent] = seen
    assert sent.url.path == "/v1/messages"
    assert KEY in sent.headers.get("x-api-key", "") + sent.headers.get("authorization", "")
    assert "server-side-fallback-2026-07-01" in sent.headers["anthropic-beta"]
    body = json.loads(sent.content)
    assert body["stream"] is True and body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {"effort": "medium"}
    assert body["tools"][0]["strict"] is True and "betas" not in body
    assert reply.content == ({"type": "text", "text": "Hello."},)
    assert reply.stop_reason == "end_turn"
    assert reply.usage == (TokenUsage("claude-sonnet-5-5", 10, 5, cache_read_tokens=3, cache_write_tokens=2),)


async def test_a_mid_stream_fallback_reports_each_attempt_and_echoes_the_partial_text() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        attempt = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        return streamed(
            sse(
                [
                    {"type": "thinking", "thinking": "", "signature": "sig-declined"},
                    {"type": "text", "text": "Taco Spot is "},
                    {"type": "fallback", "from": {"model": "claude-sonnet-5-5"}, "to": {"model": "claude-sonnet-5"}},
                    {"type": "text", "text": "open late."},
                ],
                start_usage={"input_tokens": 7, "output_tokens": 1},
                end_usage={
                    "output_tokens": 9,
                    "iterations": [
                        {"type": "message", "input_tokens": 7, "output_tokens": 2, **attempt},
                        {
                            "type": "fallback_message",
                            "model": "claude-sonnet-5",
                            "input_tokens": 7,
                            "output_tokens": 9,
                            **attempt | {"cache_read_input_tokens": 1},
                        },
                    ],
                },
            )
        )

    reply = await client_for(handler).create(REQUEST)
    assert reply.content == ({"type": "text", "text": "Taco Spot is "}, {"type": "text", "text": "open late."})
    assert reply.usage == (
        TokenUsage("claude-sonnet-5-5", 7, 2),  # names no model: the costlier of the two, here the requested
        TokenUsage("claude-sonnet-5", 7, 9, cache_read_tokens=1),
    )


def test_the_echo_keeps_only_text_before_the_boundary_and_everything_after() -> None:
    blocks: list[dict[str, object]] = [
        {"type": "thinking", "thinking": "", "signature": "a"},
        {"type": "tool_use", "id": "toolu_0", "name": "x", "input": {}},
        {"type": "text", "text": "Partial "},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "text", "text": "more "},
        {"type": "fallback", "from": {"model": "b"}, "to": {"model": "c"}},
        {"type": "thinking", "thinking": "", "signature": "c"},
        {"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}},
    ]
    assert echoed_content(blocks) == [blocks[2], blocks[4], blocks[6], blocks[7]]
    assert echoed_content(blocks[6:]) == blocks[6:]


@pytest.mark.parametrize(
    ("status", "billed"),
    [(400, False), (401, False), (429, False), (529, False), (408, True), (502, True), (504, True)],
)
async def test_an_error_answer_is_billed_only_when_it_may_have_run(status: int, billed: bool) -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        body = {"type": "error", "error": {"type": "x", "message": "no"}}
        return httpx2.Response(status, json=body, headers={"request-id": "req_test_123"})

    with pytest.raises(ModelCallFailed) as failed:
        await client_for(handler).create(REQUEST)
    assert failed.value.maybe_billed is billed
    assert failed.value.status == status and failed.value.request_id == "req_test_123"
    assert failed.value.log_fields()["anthropic_request_id"] == "req_test_123"
    assert calls == [1]  # never retried


async def test_an_error_mid_stream_may_have_been_billed() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        start = sse([], start_usage={"input_tokens": 7, "output_tokens": 1}, end_usage={"output_tokens": 1})
        broken = start.split("event: message_delta")[0] + _event(
            "error", {"error": {"type": "overloaded_error", "message": "Overloaded"}}
        )
        return streamed(broken)

    with pytest.raises(ModelCallFailed) as failed:
        await client_for(handler).create(REQUEST)
    assert failed.value.maybe_billed is True


async def test_a_dropped_connection_may_have_been_billed() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("gone", request=request)

    with pytest.raises(ModelCallFailed) as failed:
        await client_for(handler).create(REQUEST)
    assert failed.value.maybe_billed is True and failed.value.error_type == "APIConnectionError"


async def test_a_reply_that_cant_be_read_may_have_been_billed(monkeypatch: pytest.MonkeyPatch) -> None:
    from dinnerbell.services.assistant import anthropic_client

    def unreadable(message: object) -> object:
        raise ValueError("unexpected shape")

    monkeypatch.setattr(anthropic_client, "to_reply", unreadable)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return streamed(sse([], start_usage={"input_tokens": 1, "output_tokens": 1}, end_usage={"output_tokens": 1}))

    with pytest.raises(ModelCallFailed) as failed:
        await client_for(handler).create(REQUEST)
    assert failed.value.maybe_billed is True and failed.value.error_type == "unreadable reply"
