# SPDX-License-Identifier: AGPL-3.0-or-later
"""The **Assistant** page's reply shown as the model writes it (#370, spec 6.14): the events of
``POST /assistant`` with ``Accept: text/event-stream``, in order; the finished reply replacing
what was shown; a member leaving mid-reply; and the HTMX and plain form posts left as they were.
The model is ``FakeModel``; the browser is the real test client with HTTPS, CSRF, and Origin."""

from __future__ import annotations

import asyncio
import html
import json
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
from dinnerbell.db.models import AssistantConversationRow
from dinnerbell.services.assistant.client import ModelEvents, ModelReply
from dinnerbell.services.assistant.conversation import FAILED, MAX_MESSAGE_CHARS, TOO_LONG_ANSWER
from dinnerbell.web import assistant_stream
from dinnerbell.web.assistant_stream import STREAM_TYPE, WebStream, events
from dinnerbell.web.routes import assistant_page
from tests.conftest import BASE_URL, csrf_from, page
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, stopped, usage
from tests.support.fake_oidc import FakeProvider
from tests.test_assistant_page import HX, KEY, as_who, draft_ids, furnish, offer_tool, send
from tests.test_auth_flow import rows

STREAM = {"Accept": STREAM_TYPE}
LONG = (
    "Here are a few ideas for tonight, Member 1.\n"
    "Siam Garden has the green curry you liked last time.\n"
    "Noodle Bar is close by and quick on a weeknight.\n"
    "Either one works for the whole household."
)
"""A reply long enough to pass the hold (two lines and 160 characters)."""
LONG_SHOWN = LONG.replace("Member 1", "Kim")


@dataclass
class GatedModel(FakeModel):
    """A ``FakeModel`` whose streamed text stops halfway until ``gate`` opens, so a test can
    leave mid-reply."""

    gate: asyncio.Event | None = None

    async def write(self, reply: ModelReply, events: ModelEvents) -> None:
        for block in reply.content:
            if block.get("type") == "tool_use":
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
    client: TestClient, provider: FakeProvider, model: FakeModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
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
    texts = [data["html"] for name, data in found if name == "text"]
    assert "Let me check" in texts[0]
    last = texts[-1]
    assert "Kim" in last and "Member 1" not in last  # names put back as it's written
    assert "<p>" in last  # rendered as the finished reply is
    # Only whole lines show: the last line waits for the finished reply.
    assert "Either one works" not in last
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
    assert names(found) == ["start", "done"]  # held: under two lines
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


def test_a_failure_while_answering_is_an_error_event(
    client: TestClient, provider: FakeProvider, model: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(assistant_page, "converse", broken)
    _, found = streamed(client, "Hi")
    assert names(found) == ["start", "error"] and found[-1][1] == {"status": 500}


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


def test_leaving_mid_reply_still_stores_the_reply(
    client: TestClient, provider: FakeProvider, model: GatedModel, migrated: Settings
) -> None:
    furnish(client, provider)
    as_who(client, provider, "kim")
    model.script = [answer(LONG)]
    body = urlencode({"message": "Ideas for tonight?", "csrf_token": csrf_from(client)}).encode()
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

    async def leave_mid_reply() -> None:
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
        model.gate.set()  # the model goes on writing after the member left

    client.portal.call(leave_mid_reply)  # type: ignore[union-attr]
    body_sent = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"event: start" in body_sent and b"event: done" not in body_sent
    deadline = time.monotonic() + 10
    while not rows(migrated, AssistantConversationRow) and time.monotonic() < deadline:
        client.portal.call(asyncio.sleep, 0.01)  # type: ignore[union-attr]
    assert rows(migrated, AssistantConversationRow)
    assert "Either one works for the whole household." in page(client.get("/assistant"))


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
    """``events`` for a task that returns ``result`` after ``script``: strings for ``lines``,
    "tool" for ``tool_round``, "call" for ``call_started``."""

    async def go() -> str:
        async def work() -> Any:
            for step in script:
                if step == "tool":
                    stream.tool_round()
                elif step == "call":
                    stream.call_started(0)
                else:
                    stream.lines(step)
                await asyncio.sleep(0)
            return result

        task = assistant_stream.start(work(), stream, 1)

        async def done(value: Any) -> str:
            return f"<p>{value}</p>"

        return "".join([part async for part in events(stream, task, "<p>asked</p>", replace=False, done=done)])

    return parsed(asyncio.run(go()))


def test_pieces_that_queue_up_go_as_one_and_calls_never_join() -> None:
    stream = WebStream()
    stream.lines("one")  # both before anyone reads: they go as one event
    stream.lines("\ntwo")
    found = run_events(stream, ["tool", "call", "three"], "finished")
    assert names(found) == ["start", "text", "discard", "text", "done"]
    texts = [data["html"] for name, data in found if name == "text"]
    assert "two" in texts[0] and "one" in texts[0]
    assert "three" in texts[-1] and "one" not in texts[-1]  # the next call starts fresh
    assert found[-1][1] == {"html": "<p>finished</p>"}


def test_a_stream_nobody_listens_to_queues_nothing() -> None:
    stream = WebStream()
    stream.listening = False
    stream.lines("text")
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

        task = assistant_stream.start(work(), stream, 1)
        parts = []
        async for part in events(stream, task, "", replace=False, done=lambda v: asyncio.sleep(0, v)):
            parts.append(part)
            if part == assistant_stream.HEARTBEAT:
                release.set()
        return "".join(parts)

    body = asyncio.run(go())
    assert assistant_stream.HEARTBEAT in body and body.rstrip().endswith('"ok"}')
