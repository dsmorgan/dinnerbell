# SPDX-License-Identifier: AGPL-3.0-or-later
"""The **Assistant** page's reply shown as the model writes it (#370, #372, spec 6.14): the events
of ``POST /assistant`` with ``Accept: text/event-stream``, in order; the reply word by word, with
names put back and half-written Markdown closed; the finished reply replacing what was shown; a
member leaving mid-reply; and the HTMX and plain form posts left as they were. The model is
``FakeModel``; the browser is the real test client with HTTPS, CSRF, and Origin."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey

from dinnerbell.app_factory import create_app
from dinnerbell.config import Settings
from dinnerbell.db.models import AssistantConversationRow, AssistantUsageRow
from dinnerbell.domain.assistant import Pseudonyms
from dinnerbell.services.assistant.client import ModelEvents, ModelReply
from dinnerbell.services.assistant.conversation import FAILED, MAX_MESSAGE_CHARS, TOO_LONG_ANSWER
from dinnerbell.services.assistant.streaming import WORDS_HOLD_CHARS, CallText, Cut
from dinnerbell.services.database import Database
from dinnerbell.web import assistant_stream
from dinnerbell.web import render as render_module
from dinnerbell.web.assistant_stream import STREAM_TYPE, TEXT_SECONDS, WebStream, events, next_text_at
from dinnerbell.web.reply_markdown import reply_blocks
from dinnerbell.web.routes import assistant_page
from tests.conftest import BASE_URL, csrf_from, page
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, stopped, usage
from tests.support.fake_oidc import FakeProvider
from tests.test_assistant_page import HX, KEY, as_who, draft_ids, furnish, member_id, offer_tool, send
from tests.test_auth_flow import rows

STREAM = {"Accept": STREAM_TYPE}
LONG = (
    "Here are a few ideas for tonight, Member 1.\n"
    "Siam Garden has the green curry you liked last time.\n"
    "Noodle Bar is close by and quick on a weeknight.\n"
    "Either one works for the whole household."
)
"""A reply long enough to pass the hold (`WORDS_HOLD_CHARS`)."""
LONG_SHOWN = LONG.replace("Member 1", "Kim")


@dataclass
class GatedModel(FakeModel):
    """A ``FakeModel`` whose streamed text stops halfway until ``gate`` opens, so a test can
    leave mid-reply."""

    gate: asyncio.Event | None = None
    before_tool: asyncio.Event | None = None
    """When set, a tool call waits for it: for a test that needs the text before it sent."""

    async def write(self, reply: ModelReply, events: ModelEvents) -> None:
        for block in reply.content:
            if block.get("type") == "tool_use":
                if self.before_tool is not None:
                    await self.before_tool.wait()
                events.tool_started()
            if block.get("type") != "text":
                continue
            events.text_started()
            text = str(block.get("text", ""))
            half = len(text) // 2
            events.text(text[:half])
            await asyncio.sleep(0)
            if self.gate is not None:
                await self.gate.wait()
            events.text(text[half:])


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"anthropic_api_key": KEY}


@pytest.fixture
def model() -> FakeModel:
    return GatedModel()


@pytest.fixture
def provider(signing_key: RSAKey, clock: FakeClock, settings: Settings) -> FakeProvider:
    return FakeProvider(key=signing_key, clock=clock, issuer=settings.oidc_issuer)


@pytest.fixture
def client(migrated: Settings, clock: FakeClock, provider: FakeProvider, model: FakeModel) -> Iterator[TestClient]:
    app = create_app(migrated, clock=clock, oidc_transport=httpx.ASGITransport(app=provider.app), model_client=model)
    with TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False) as test_client:
        yield test_client


def parsed(body: str) -> list[tuple[str, dict[str, Any]]]:
    """The events of a streamed body, as ``app.js`` reads them; comment lines are skipped."""
    found = []
    for block in body.split("\n\n"):
        name, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:]
        if data:
            found.append((name, json.loads(data)))
    return found


def streamed(client: TestClient, text: str) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:
    response = send(client, text, **STREAM)
    return response, parsed(response.text)


_TAG = re.compile(r"<(/?)(\w+)[^>]*>")


def top_level(html_text: str) -> list[str]:
    """``html_text``'s top-level elements, as the browser counts a ``text`` event's ``keep``."""
    out, depth, start = [], 0, 0
    for tag in _TAG.finditer(html_text):
        closing, name = tag.group(1), tag.group(2)
        if name in ("br", "hr"):
            if depth == 0:
                out.append(html_text[start : tag.end()])
                start = tag.end()
            continue
        depth += -1 if closing else 1
        if depth == 0:
            out.append(html_text[start : tag.end()])
            start = tag.end()
    assert depth == 0 and start == len(html_text), html_text
    return out


def shown(found: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """What the placeholder shows after each ``text`` and ``discard`` event, as ``app.js`` puts
    them together."""
    blocks: list[str] = []
    out = []
    for name, data in found:
        if name == "text":
            assert 0 <= data["keep"] <= len(blocks)
            blocks = blocks[: data["keep"]] + top_level(data["html"])
        elif name == "discard":
            blocks = []
        else:
            continue
        out.append("".join(blocks))
    return out


def names(found: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Event names, with each run of ``text`` as one."""
    out: list[str] = []
    for name, _ in found:
        if not (name == "text" and out and out[-1] == "text"):
            out.append(name)
    return out


def reply_turn(html: str) -> str:
    match = re.search(r'<div class="turn turn-reply">.*?</div>\s*</div>', html, re.S)
    assert match, html
    return match.group(0)


def with_text_and_tool(text: str) -> ModelReply:
    """A call that writes ``text`` and then calls a tool after all."""
    blocks: list[Mapping[str, Any]] = [
        {"type": "text", "text": text},
        {"type": "tool_use", "id": "toolu_1", "name": "draft_offer", "input": offer_tool()},
    ]
    return ModelReply(tuple(blocks), "tool_use", "claude-sonnet-5-5", (usage(),))


# The events --------------------------------------------------------------------------------------


def test_the_reply_streams_in_order_and_ends_as_a_reload_shows_it(
    client: TestClient, provider: FakeProvider, model: GatedModel, migrated: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    # The tool call waits until the call's text has gone out, so the page has shown it.
    model.before_tool = asyncio.Event()
    send_event = assistant_stream.event

    def sent(name: str, data: dict[str, Any]) -> str:
        if name == "text" and model.before_tool is not None:
            model.before_tool.set()
        return send_event(name, data)

    monkeypatch.setattr(assistant_stream, "event", sent)
    model.script = [with_text_and_tool("Let me check what's open.\n" + "x" * 200 + "\nOne moment."), answer(LONG)]
    response, found = streamed(client, "Ideas for tonight?")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(STREAM_TYPE)
    # The order: the message, the tool round's text and then its discard, the reply, the card.
    assert names(found) == ["start", "text", "discard", "text", "done"]
    start = found[0][1]
    assert start["replace"] is False
    assert "Ideas for tonight?" in start["html"] and "data-stream-reply" in start["html"]
    assert 'aria-hidden="true"' in start["html"] and " hidden " in start["html"]
    texts = shown(found)
    assert "Let me check" in texts[0]
    last = texts[-1]
    assert "Kim" in last and "Member 1" not in last  # names put back as it's written
    assert "<p>" in last  # rendered as the finished reply is
    # Word by word: the last line shows too, all but the word still being written.
    assert "Either one works for the whole" in last and "household" not in last
    done = found[-1][1]["html"]
    assert "Either one works for the whole household." in done and "Ideas for tonight?" not in done
    assert len(draft_ids(done)) == 1 and 'hx-post="/assistant/drafts/' in done
    # What a reload shows is what the stream ended with.
    reloaded = client.get("/assistant").text
    assert reply_turn(done) in reloaded
    assert rows(migrated, AssistantConversationRow)


def test_a_short_reply_shows_only_when_its_done(client: TestClient, provider: FakeProvider, model: FakeModel) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [answer("Siam Garden.")]
    _, found = streamed(client, "Thai?")
    assert names(found) == ["start", "done"]  # held: under WORDS_HOLD_CHARS
    assert "Siam Garden." in found[-1][1]["html"]


def test_dinnerbells_own_line_replaces_what_was_shown(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [stopped("max_tokens", {"type": "text", "text": LONG})]
    _, found = streamed(client, "Ideas for tonight?")
    assert names(found) == ["start", "text", "done"]
    done = found[-1][1]["html"]
    assert TOO_LONG_ANSWER in done and "Siam Garden" not in done
    assert rows(migrated, AssistantConversationRow) == []  # not kept, as without streaming
    assert TOO_LONG_ANSWER not in page(client.get("/assistant"))


def test_a_message_too_long_gets_its_line_without_a_model_call(
    client: TestClient, provider: FakeProvider, model: FakeModel
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    _, found = streamed(client, "x" * (MAX_MESSAGE_CHARS + 1))
    assert names(found) == ["start", "done"] and "That message is too long for me." in found[-1][1]["html"]
    assert model.requests == []


def test_at_hours_zero_the_exchange_replaces_the_log_and_keeps_waiting_cards(
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider, conversation_hours="0")
    as_who(client, provider, "kim")
    model.script = [with_text_and_tool("Drafting it."), answer("Drafted."), answer("Anything else?")]
    _, first = streamed(client, "I'll pick up Siam Garden")
    assert first[0][1]["replace"] is True and len(draft_ids(first[-1][1]["html"])) == 1
    _, later = streamed(client, "Thanks")
    done = later[-1][1]["html"]
    assert "Anything else?" in done and len(draft_ids(done)) == 1  # the waiting card comes back
    assert rows(migrated, AssistantConversationRow) == []


def test_a_failure_while_answering_is_an_error_event_and_logs_the_member_id_only(
    client: TestClient,
    provider: FakeProvider,
    model: FakeModel,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    migrated: Settings,
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(assistant_page, "converse", broken)
    with caplog.at_level(logging.ERROR, logger="dinnerbell.web.assistant_stream"):
        _, found = streamed(client, "Hi from Kim")
    assert names(found) == ["start", "error"] and found[-1][1] == {"status": 500}
    [line] = [r for r in caplog.records if r.name == "dinnerbell.web.assistant_stream"]
    assert line.getMessage() == "the assistant broke answering on the web"
    assert line.member_id == member_id(migrated, "Kim")  # type: ignore[attr-defined]
    logged = caplog.text
    assert "Hi from Kim" not in logged and "kim@example.com" not in logged


def test_turned_off_while_answering_is_a_404_event(
    client: TestClient, provider: FakeProvider, model: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")

    async def switched_off(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(assistant_page, "converse", switched_off)
    _, found = streamed(client, "Hi")
    assert names(found) == ["start", "error"] and found[-1][1] == {"status": 404}


def test_the_cores_failure_line_is_shown_in_done(client: TestClient, provider: FakeProvider, model: FakeModel) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [RuntimeError("the client broke")]
    _, found = streamed(client, "Hi")
    assert names(found) == ["start", "done"] and FAILED in html.unescape(found[-1][1]["html"])


def test_an_empty_message_is_a_204(client: TestClient, provider: FakeProvider, model: FakeModel) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    response = send(client, "  \r\n ", **STREAM)
    assert response.status_code == 204 and model.requests == []


# Leaving mid-reply ---------------------------------------------------------------------------------


def leave_mid_reply(client: TestClient, model: GatedModel, message: str, *, release: bool = True) -> bytes:
    """Send ``message`` asking for the stream, and leave as soon as ``start`` arrives, while the
    model has written half its text. With ``release``, the model then goes on writing. Returns
    what the response sent before the member left. Raw ASGI, since the test client reads every
    response to its end."""
    body = urlencode({"message": message, "csrf_token": csrf_from(client)}).encode()
    host = urlsplit(BASE_URL).hostname or ""
    cookies = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/assistant",
        "raw_path": b"/assistant",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", host.encode()),
            (b"origin", BASE_URL.encode()),
            (b"cookie", cookies.encode()),
            (b"accept", STREAM_TYPE.encode()),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("testclient", 50000),
        "server": (host, 443),
        "state": client.app_state.copy(),
    }
    sent: list[dict[str, Any]] = []

    async def go() -> None:
        model.gate = asyncio.Event()
        started = asyncio.Event()
        asked = False

        async def receive() -> dict[str, Any]:
            nonlocal asked
            if not asked:
                asked = True
                return {"type": "http.request", "body": body, "more_body": False}
            await started.wait()
            return {"type": "http.disconnect"}

        async def send_(message: dict[str, Any]) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and b"event: start" in message.get("body", b""):
                started.set()

        await client.app(scope, receive, send_)  # type: ignore[arg-type]
        if release:
            model.gate.set()  # the model goes on writing after the member left

    client.portal.call(go)  # type: ignore[union-attr]
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_leaving_mid_reply_still_stores_the_reply(
    client: TestClient, provider: FakeProvider, model: GatedModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [answer(LONG)]
    body_sent = leave_mid_reply(client, model, "Ideas for tonight?")
    assert b"event: start" in body_sent and b"event: done" not in body_sent
    deadline = time.monotonic() + 10
    while not rows(migrated, AssistantConversationRow) and time.monotonic() < deadline:
        client.portal.call(asyncio.sleep, 0.01)  # type: ignore[union-attr]
    assert rows(migrated, AssistantConversationRow)
    assert "Either one works for the whole household." in page(client.get("/assistant"))


def test_shutdown_cancels_an_answer_still_running_before_the_database_closes(
    migrated: Settings, clock: FakeClock, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = GatedModel(script=[answer(LONG)])
    app = create_app(migrated, clock=clock, oidc_transport=httpx.ASGITransport(app=provider.app), model_client=model)
    closing: list[set[asyncio.Task[Any]]] = []
    close = Database.close

    async def closed_after_answers(self: Database) -> None:
        closing.append(set(app.state.web_answers.tasks))
        await close(self)

    monkeypatch.setattr(Database, "close", closed_after_answers)
    client = TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False)
    with client:
        furnish(client, provider)
        as_who(client, provider, "kim")
        leave_mid_reply(client, model, "Ideas for tonight?", release=False)  # the model never finishes
        deadline = time.monotonic() + 10
        while not model.requests and time.monotonic() < deadline:
            client.portal.call(asyncio.sleep, 0.01)  # type: ignore[union-attr]
        assert model.requests  # the call is reserved and running
        [task] = app.state.web_answers.tasks
    assert task.cancelled()
    assert closing == [set()]  # every answer had ended when the database closed
    # The cancelled call keeps its reservation, settled before its task ended.
    [usage_row] = rows(migrated, AssistantUsageRow)
    assert usage_row.state == "settled"
    assert rows(migrated, AssistantConversationRow) == []  # nothing of the message is kept


# Unchanged without the header, and the same gates -------------------------------------------------


def test_htmx_and_the_plain_form_post_are_unchanged(
    client: TestClient, provider: FakeProvider, model: FakeModel
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    shown = page(client.get("/assistant"))
    assert 'method="post" action="/assistant"' in shown and 'hx-post="/assistant"' in shown
    assert 'data-stream-timeout="120000"' in shown
    model.script = [answer(LONG), answer("Try Noodle Bar.")]
    fragment = send(client, "Ideas?", **HX)
    assert fragment.headers["content-type"].startswith("text/html") and "event:" not in fragment.text
    assert "Either one works for the whole household." in page(fragment)
    plain = send(client, "Something else?")
    assert plain.status_code == 303 and plain.headers["location"] == "/assistant#ask"
    script = client.get("/static/app.js").text
    assert '"data-stream-timeout"' in script and "text/event-stream" in script
    assert "htmx.swap" in script and "innerHTML" not in script  # HTMX swaps in what the server rendered


def test_streaming_needs_sign_in_and_the_csrf_token(
    client: TestClient, provider: FakeProvider, model: FakeModel
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    for data in ({"message": "Hi"}, {"message": "Hi", "csrf_token": "wrong"}):
        refused = client.post("/assistant", data=data, headers=STREAM)
        assert refused.status_code == 403 and not refused.headers["content-type"].startswith(STREAM_TYPE)
    token = csrf_from(client)
    other = client.post(
        "/assistant", data={"message": "Hi", "csrf_token": token}, headers=STREAM | {"Origin": "https://evil.example"}
    )
    assert other.status_code == 403
    client.cookies.clear()
    signed_out = client.post("/assistant", data={"message": "Hi", "csrf_token": token}, headers=STREAM)
    assert signed_out.status_code in (303, 401) and not signed_out.headers.get("content-type", "").startswith(
        STREAM_TYPE
    )
    assert model.requests == []


def test_the_stream_carries_the_security_headers(client: TestClient, provider: FakeProvider, model: FakeModel) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [answer("Hi.")]
    response = send(client, "Hello", **STREAM)
    headers = response.headers
    assert headers["content-security-policy"].startswith("default-src 'self'; script-src 'self'")
    assert "unsafe-inline" not in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
    assert headers["cache-control"] == "no-store, no-transform" and headers["x-accel-buffering"] == "no"
    assert "content-length" not in headers and "content-encoding" not in headers
    # Every other page keeps plain no-store.
    assert client.get("/assistant").headers["cache-control"] == "no-store"


# The stream itself ---------------------------------------------------------------------------------


def run_events(stream: WebStream, script: list[Any], result: Any) -> list[tuple[str, dict[str, Any]]]:
    """``events`` for a task that returns ``result`` after ``script``: strings for ``more``,
    "tool" for ``tool_round``, "call" for ``call_started``."""

    async def go() -> str:
        async def work() -> Any:
            for step in script:
                if step == "tool":
                    stream.tool_round()
                elif step == "call":
                    stream.call_started(0)
                else:
                    stream.more(step)
                await asyncio.sleep(0)
            return result

        task = assistant_stream.Answers().start(work(), stream, 1)

        async def done(value: Any) -> str:
            return f"<p>{value}</p>"

        return "".join([part async for part in events(stream, task, "<p>asked</p>", replace=False, done=done)])

    return parsed(asyncio.run(go()))


def test_pieces_that_queue_up_go_as_one_and_calls_never_join() -> None:
    stream = WebStream()
    stream.more("one")  # both before anyone reads: they go as one event
    stream.more("\ntwo")
    found = run_events(stream, ["tool", "call", "three"], "finished")
    assert names(found) == ["start", "text", "discard", "text", "done"]
    texts = shown(found)
    assert "two" in texts[0] and "one" in texts[0]
    assert "three" in texts[-1] and "one" not in texts[-1]  # the next call starts fresh
    assert [data["keep"] for name, data in found if name == "text"] == [0, 0]
    assert found[-1][1] == {"html": "<p>finished</p>"}


def test_a_stream_nobody_listens_to_queues_nothing() -> None:
    stream = WebStream()
    stream.listening = False
    stream.more("text")
    stream.tool_round()
    stream.end()
    assert stream.queue.empty()


def test_a_quiet_stream_sends_a_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_stream, "HEARTBEAT_SECONDS", 0.001)

    async def go() -> str:
        stream = WebStream()
        release = asyncio.Event()

        async def work() -> str:
            await release.wait()
            return "ok"

        task = assistant_stream.Answers().start(work(), stream, 1)
        parts = []
        async for part in events(stream, task, "", replace=False, done=lambda v: asyncio.sleep(0, v)):
            parts.append(part)
            if part == assistant_stream.HEARTBEAT:
                release.set()
        return "".join(parts)

    body = asyncio.run(go())
    assert assistant_stream.HEARTBEAT in body and body.rstrip().endswith('"ok"}')


def test_a_reply_queues_one_marker_and_text_events_are_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = WebStream()
    stream.call_started(0)
    for n in range(100):
        stream.more(f"line {n}\n")
    assert stream.queue.qsize() == 1  # one marker, not a copy of the text per line
    monkeypatch.setattr(assistant_stream, "TEXT_SECONDS", 3600.0)  # a second text event would wait an hour

    async def go() -> list[str]:
        async def work() -> str:
            stream.more("the last line")  # while the first text event goes out
            await asyncio.sleep(0)
            return "ok"

        task = assistant_stream.Answers().start(work(), stream, 1)
        parts = []
        async for part in events(stream, task, "", replace=False, done=lambda v: asyncio.sleep(0, v)):
            parts.append(part)
            if len(parts) == 2:  # start and the first text: let the second one wait
                break
        task.cancel()
        return parts

    found = parsed("".join(asyncio.run(go())))
    assert [name for name, _ in found] == ["start", "text"]
    assert "line 99" in found[1][1]["html"]


def test_text_queued_for_a_call_never_shows_after_its_discard() -> None:
    stream = WebStream()
    stream.call_started(0)
    stream.more("first call")
    stream.tool_round()
    stream.more("second call")
    found = run_events(stream, [], "finished")
    assert names(found) == ["start", "discard", "text", "done"] and "second call" in shown(found)[-1]


def test_the_streamed_fragments_skip_the_whole_page_queries(
    client: TestClient, provider: FakeProvider, model: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    asked: list[str] = []

    def counted(name: str) -> Any:
        async def query(_request: Any) -> Any:
            asked.append(name)
            return None

        return query

    for name in ("_assistant_link", "_web_app", "_fun_scene"):
        monkeypatch.setattr(render_module, name, counted(name))
    model.script = [answer(LONG)]
    token = csrf_from(client)  # a whole page, which asks
    asked.clear()
    response = client.post("/assistant", data={"message": "Ideas?", "csrf_token": token}, headers=STREAM)
    assert names(parsed(response.text)) == ["start", "text", "done"] and asked == []


# Word by word (#372) -----------------------------------------------------------------------------


class Words:
    """A ``ReplyStream`` in words that keeps what it's given."""

    cut = Cut.WORDS

    def __init__(self) -> None:
        self.pieces: list[str] = []
        self.tool_rounds = 0

    def call_started(self, drafts: int) -> None:
        pass

    def more(self, text: str) -> None:
        self.pieces.append(text)

    def tool_round(self) -> None:
        self.tool_rounds += 1

    @property
    def joined(self) -> str:
        return "".join(self.pieces)


PAST_HOLD = "Here is a plan for the week. " * 5
"""More than ``WORDS_HOLD_CHARS``, ending in whitespace."""
NAMES = Pseudonyms({7: 1, 8: 2, 9: 12})
SHOWN_NAMES = {7: "Kim", 8: "Lee", 9: "Ana"}


def restore(text: str) -> str:
    return NAMES.restore_names(text, SHOWN_NAMES)


def feed(events: CallText, deltas: list[str]) -> None:
    events.text_started()
    for delta in deltas:
        events.text(delta)


def test_past_the_hold_each_word_shows_as_its_written() -> None:
    assert len(PAST_HOLD) > WORDS_HOLD_CHARS
    out = Words()
    events = CallText(out, lambda text: text)
    feed(events, [PAST_HOLD])
    assert out.joined == PAST_HOLD.rstrip()
    shown = len(out.pieces)
    for word in ["One", " paragraph,", " no", " line", " break"]:
        events.text(word)
    # Each word shows once the next one starts: no line break needed, and the last waits.
    assert out.pieces[shown:] == [" One", " paragraph,", " no", " line"]
    for piece in out.pieces[1:]:
        assert piece[0].isspace() and not piece[-1].isspace()


def test_a_short_preface_and_then_a_tool_call_shows_nothing() -> None:
    out = Words()
    events = CallText(out, lambda text: text)
    feed(events, ["Let me check ", "this week's plan ", "for you."])
    events.tool_started()
    assert out.pieces == [] and out.tool_rounds == 0


def test_a_tool_call_after_the_hold_takes_the_text_back() -> None:
    out = Words()
    events = CallText(out, lambda text: text)
    feed(events, [PAST_HOLD, "and more"])
    events.tool_started()
    events.text(" after the tool call ")
    assert out.joined == PAST_HOLD + "and" and out.tool_rounds == 1


@pytest.mark.parametrize(
    ("deltas", "name", "ending"),
    [
        (["Ask Mem", "ber 1 ", "tonight. "], "Kim", " Ask Kim tonight."),  # two deltas, and a third after
        (["Ask Member", " 1", "2 tonight. "], "Ana", " Ask Ana tonight."),  # three: Member 1, then Member 12
        (["Ask Member 1", "2", " tonight. "], "Ana", " Ask Ana tonight."),
        (["Ask M", "ember ", "2 tonight. "], "Lee", " Ask Lee tonight."),
        (["Ask (Member", " 2) tonight. "], "Lee", " Ask (Lee) tonight."),
    ],
)
def test_a_label_split_across_deltas_never_shows_as_a_label_or_the_wrong_name(
    deltas: list[str], name: str, ending: str
) -> None:
    out = Words()
    events = CallText(out, restore)
    feed(events, [PAST_HOLD])
    before = len(out.pieces)
    for delta in deltas:
        events.text(delta)
        later = "".join(out.pieces[before:])
        assert "M" not in later  # nothing of the label, or a name in place of part of it
        assert not [other for other in SHOWN_NAMES.values() if other != name and other in later]
    assert "".join(out.pieces[before:]) == ending


def test_member_followed_by_other_text_isnt_held() -> None:
    out = Words()
    events = CallText(out, restore)
    feed(events, [PAST_HOLD, "Ask Member Services ", "now"])
    assert out.joined.endswith("Ask Member Services")


ALPHABET = ["Member", "Member ", "Mem", "ber", " ", "  ", "\n", "\n\n", "1", "2", "12", "123", "x", "(", ")", ".", "-"]


@pytest.mark.parametrize("seed", range(4))
def test_whatever_the_deltas_the_shown_text_is_a_prefix_of_the_reply_with_names(seed: int) -> None:
    """Property: after every delta, the words shown, names put back piece by piece, are a
    prefix of the whole reply with names put back once."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not a secret
    for _ in range(300):
        text = PAST_HOLD + "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 60)))
        whole = restore(text.strip())
        out = Words()
        events = CallText(out, restore)
        events.text_started()
        at = 0
        while at < len(text):
            step = rng.randint(1, 7)
            events.text(text[at : at + step])
            at += step
            assert whole.startswith(out.joined), (text, out.pieces)
        for n, piece in enumerate(out.pieces):
            assert piece and not piece[-1].isspace()
            assert n == 0 or piece[0].isspace()


def drive(script: Any) -> list[tuple[str, dict[str, Any]]]:
    """Run ``events`` one event at a time: ``script(stream, step)`` writes to the stream and
    awaits ``step()`` for each next event."""

    async def go() -> str:
        stream = WebStream()
        stream.call_started(0)
        finish = asyncio.Event()

        async def work() -> str:
            await finish.wait()
            return "finished"

        async def done(value: str) -> str:
            return f"<p>{value}</p>"

        task = assistant_stream.Answers().start(work(), stream, 1)
        body = events(stream, task, "", replace=False, done=done)
        parts = [await anext(body)]

        async def step() -> None:
            parts.append(await anext(body))

        await script(stream, step)
        finish.set()
        parts.extend([part async for part in body])
        return "".join(parts)

    return parsed(asyncio.run(go()))


def test_a_text_event_sends_only_the_blocks_that_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_stream, "TEXT_SECONDS", 0.0)

    async def script(stream: WebStream, step: Any) -> None:
        stream.more("The first paragraph.\n\nThe second")
        await step()
        stream.more(" goes on")
        await step()
        stream.more("\n- and a list")
        await step()

    found = drive(script)
    texts = [data for name, data in found if name == "text"]
    assert [data["keep"] for data in texts] == [0, 1, 2]
    assert "first" not in texts[1]["html"] + texts[2]["html"]  # sent once
    assert shown(found)[-1] == "".join(reply_blocks("The first paragraph.\n\nThe second goes on\n- and a list"))
    assert found[-1] == ("done", {"html": "<p>finished</p>"})


def test_half_written_markdown_shows_styled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_stream, "TEXT_SECONDS", 0.0)

    async def script(stream: WebStream, step: Any) -> None:
        stream.more("Tonight: **Siam")
        await step()
        stream.more(" Garden** with `rice")
        await step()

    found = drive(script)
    assert shown(found) == [
        "<p>Tonight: <strong>Siam</strong></p>",
        "<p>Tonight: <strong>Siam Garden</strong> with <code>rice</code></p>",
    ]


def test_a_big_text_event_holds_the_next_one_back_longer() -> None:
    assert next_text_at(10.0, "x" * 10) == 10.0 + TEXT_SECONDS
    big = "x" * assistant_stream.TEXT_BYTES_PER_SECOND
    assert next_text_at(10.0, big) == 11.0


def test_a_short_preface_before_a_tool_call_never_shows_on_the_page(
    client: TestClient, provider: FakeProvider, model: FakeModel
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [with_text_and_tool("Let me look at this week's plan."), answer(LONG)]
    _, found = streamed(client, "Ideas for tonight?")
    assert names(found) == ["start", "text", "done"]  # no discard: nothing of the preface showed
    assert not [text for text in shown(found) if "Let me look" in text]
    assert "Kim" in shown(found)[-1]


def test_a_one_paragraph_reply_streams_word_by_word(
    client: TestClient, provider: FakeProvider, model: FakeModel
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    one = " ".join(["Siam Garden has the green curry you liked, and it's close by."] * 4)
    model.script = [answer(one)]
    _, found = streamed(client, "Thai?")
    texts = shown(found)
    assert texts and all("<br>" not in text for text in texts)
    assert texts[-1].endswith("close</p>")  # all but the last word, before done
