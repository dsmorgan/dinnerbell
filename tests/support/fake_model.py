# SPDX-License-Identifier: AGPL-3.0-or-later
"""A ``ModelClient`` for tests: it records every request, exactly as ``converse`` built it,
and answers from a script. It never reaches the network; no test calls Anthropic."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from dinnerbell.domain.assistant import TokenUsage
from dinnerbell.services.assistant.client import ModelCallFailed, ModelEvents, ModelReply
from dinnerbell.services.assistant.prompt import ROSTER_HEADING

MODEL = "claude-sonnet-5-5"


def usage(input_tokens: int = 1_000, output_tokens: int = 200, *, model: str = MODEL, **more: int) -> TokenUsage:
    return TokenUsage(model=model, input_tokens=input_tokens, output_tokens=output_tokens, **more)


def answer(text: str, *, parts: tuple[TokenUsage, ...] | None = None, model: str = MODEL) -> ModelReply:
    """A final answer: a thinking block, as adaptive thinking sends, then the text."""
    content = ({"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": text})
    return ModelReply(content, "end_turn", model, parts or (usage(model=model),))


def tool_call(name: str, tool_input: Mapping[str, Any], *, call_id: str = "toolu_1", **calls: Any) -> ModelReply:
    """A turn that calls ``name`` with ``tool_input``. More calls in the same turn come as
    ``calls``: ``{"toolu_2": ("name", {...})}``."""
    blocks: list[Mapping[str, Any]] = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": call_id, "name": name, "input": dict(tool_input)},
    ]
    for other_id, (other_name, other_input) in calls.items():
        blocks.append({"type": "tool_use", "id": other_id, "name": other_name, "input": dict(other_input)})
    return ModelReply(tuple(blocks), "tool_use", MODEL, (usage(),))


def stopped(reason: str, *blocks: Mapping[str, Any]) -> ModelReply:
    return ModelReply(tuple(blocks), reason, MODEL, (usage(),))


@dataclass
class FakeModel:
    """Answers each request with the next item of ``script`` (a reply, or an exception to
    raise), then with ``answer("OK")``. ``time_left`` records each call's ``time_left``: the
    seconds the message had left."""

    script: list[ModelReply | Exception] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    time_left: list[float] = field(default_factory=list)
    delta: int = 5
    """How many characters of text each ``text`` event carries, when the caller listens."""

    async def create(
        self, request: Mapping[str, Any], *, time_left: float, events: ModelEvents | None = None
    ) -> ModelReply:
        self.requests.append(copy.deepcopy(dict(request)))
        self.time_left.append(time_left)
        if not self.script:
            reply = answer("OK")
        else:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            reply = step
        if events is not None:
            await self.write(reply, events)
        return reply

    async def write(self, reply: ModelReply, events: ModelEvents) -> None:
        """Tell ``events`` about ``reply`` as a stream would: each text block in ``delta``-sized
        pieces, and each tool call as it starts. A fallback block, as in a real stream, says
        nothing. Yields between pieces, as a stream does."""
        for block in reply.content:
            if block.get("type") == "text":
                events.text_started()
                text = str(block.get("text", ""))
                for start in range(0, len(text), self.delta):
                    events.text(text[start : start + self.delta])
                    await asyncio.sleep(0)
            elif block.get("type") == "tool_use":
                events.tool_started()

    @property
    def sent(self) -> str:
        """Every request so far as one JSON string, for checking what left the house, without the
        roster, the one place members' names go on purpose (#277): ``rosters`` has those."""
        return json.dumps([without_roster(r) for r in self.requests], ensure_ascii=False)

    @property
    def sent_data(self) -> str:
        """``sent`` without the tool definitions, the registry's own text, which names what tools
        take, such as a "veto" level (#253). A test that scans this checks the definitions apart."""
        return json.dumps(
            [{k: v for k, v in without_roster(r).items() if k != "tools"} for r in self.requests], ensure_ascii=False
        )

    @property
    def rosters(self) -> list[list[tuple[str, str]]]:
        """Each conversation request's roster, as ``(label, name)`` pairs."""
        return [roster_in(r) for r in self.requests]


def roster_in(request: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The roster block of a conversation request (``build_request``), as ``(label, name)``
    pairs, checking its form: the heading, then one ``Member N: "name"`` line per member."""
    fixed, roster = request["system"]
    assert fixed["type"] == roster["type"] == "text"
    heading, *lines = roster["text"].split("\n")
    assert heading == ROSTER_HEADING
    pairs = []
    for line in lines:
        label, _, quoted = line.partition(": ")
        assert re.fullmatch(r"Member [1-9]\d*", label), line
        pairs.append((label, json.loads(quoted)))
    return pairs


def without_roster(request: Mapping[str, Any]) -> dict[str, Any]:
    """``request`` with its roster block dropped: a conversation request's system prompt is the
    fixed block alone. A menu link's drafting call, whose system prompt is one string, is as it was."""
    system = request.get("system")
    if not isinstance(system, list):
        return dict(request)
    roster_in(request)  # it has the roster's form, so nothing else hides there
    return {**request, "system": system[:1]}


def failure(*, maybe_billed: bool) -> ModelCallFailed:
    return ModelCallFailed(maybe_billed=maybe_billed)
