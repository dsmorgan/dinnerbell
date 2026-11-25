# SPDX-License-Identifier: AGPL-3.0-or-later
"""The **Assistant** page's reply while the model writes it (#370, spec 6.14).

``app.js`` posts the message form with ``fetch`` and ``Accept: text/event-stream``; the same
``POST /assistant`` then answers with a stream of events in Server-Sent Events framing, which
every proxy on the way recognizes as a stream (``docs/troubleshooting.md``). Without that
header, HTMX and the plain form post get what they always got.

``WebStream`` is a ``services.assistant.streaming.ReplyStream``: ``CallText`` holds each call's
first lines, drops or marks a tool round's text, and puts names back, as for Slack. Its methods
only put onto a queue, so they never block the model's events. The answer runs in a task of its
own (``start``) with its own database session, never the request's: when the member leaves the
page, Starlette cancels only the response, and the reply still finishes and is stored.

The events, in order:

- ``start``: the member's message and a hidden placeholder for the reply. ``replace`` says the
  exchange replaces the turns shown (Conversation hours 0).
- ``text``: the current model call's text so far, rendered as the finished reply is.
- ``discard``: the call whose text was shown calls tools after all, so that text isn't the reply.
- ``done``: the reply and its draft cards, as the HTMX response has them. It always replaces
  what was shown, so the page ends with what's stored and what a reload shows.
- ``error``: no reply to show, with the status the HTMX response would have had.

A comment line goes out every ``HEARTBEAT_SECONDS`` while nothing else does, so no proxy times
the response out while the model thinks or a tool runs.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import Any

from fastapi import Request

from dinnerbell.web.reply_markdown import reply_markdown

log = logging.getLogger(__name__)

STREAM_TYPE = "text/event-stream"
STREAM_HEADERS = {
    # no-transform asks proxies not to compress or rewrite the body; no-store, as on every page.
    "Cache-Control": "no-store, no-transform",
    # nginx buffers a proxied response unless it says not to.
    "X-Accel-Buffering": "no",
}
HEARTBEAT_SECONDS = 15.0
"""The longest the stream goes without sending anything: well under any proxy's idle timeout,
such as Cloudflare's 125 seconds."""
HEARTBEAT = ": still working\n\n"


class _Mark(enum.Enum):
    DISCARD = "discard"
    END = "end"


def wants_stream(request: Request) -> bool:
    """Whether the page asked for the reply as it's written: ``app.js`` does, HTMX never does."""
    return STREAM_TYPE in request.headers.get("accept", "")


def event(name: str, data: dict[str, Any]) -> str:
    """One event, in Server-Sent Events framing: JSON on one ``data`` line."""
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class WebStream:
    """A ``ReplyStream`` into a queue that ``events`` reads. Once ``events`` stops listening, as
    when the member leaves the page, nothing more is queued."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | _Mark] = asyncio.Queue()
        self.listening = True
        self._shown = ""
        """The current call's text so far, as ``lines`` gave it."""

    # ReplyStream ---------------------------------------------------------------------------------

    def call_started(self, drafts: int) -> None:
        self._shown = ""  # a call's text never joins another's

    def lines(self, text: str) -> None:
        self._shown += text
        self._put(self._shown)

    def tool_round(self) -> None:
        self._shown = ""
        self._put(_Mark.DISCARD)

    # ---------------------------------------------------------------------------------------------

    def end(self) -> None:
        """The answer is over, with or without a reply."""
        self._put(_Mark.END)

    def _put(self, item: str | _Mark) -> None:
        if self.listening:
            self.queue.put_nowait(item)


_running: set[asyncio.Task[Any]] = set()
"""Answers in flight: the loop keeps only a weak reference to a task."""


def start[T](work: Coroutine[Any, Any, T], stream: WebStream, member_id: int) -> asyncio.Task[T]:
    """Run ``work``, the answer, in a task of its own, so the response going away doesn't cancel
    it. ``stream`` hears when it ends; a failure is logged here, whether or not anyone listens."""
    task = asyncio.create_task(work)
    _running.add(task)

    def finished(done: asyncio.Task[T]) -> None:
        _running.discard(done)
        stream.end()
        if not done.cancelled() and (exc := done.exception()) is not None:
            log.error("the assistant broke answering on the web", exc_info=exc, extra={"member_id": member_id})

    task.add_done_callback(finished)
    return task


async def events[T](
    stream: WebStream,
    task: asyncio.Task[T | None],
    pending: str,
    *,
    replace: bool,
    done: Callable[[T], Awaitable[str]],
) -> AsyncIterator[str]:
    """The response body: ``start`` with ``pending`` (the member's message and the reply's
    placeholder), the shown text as it changes, and then ``done`` with what ``done`` renders
    from the task's result, or ``error``. Pieces that queue up while one is sent go as one."""
    try:
        yield event("start", {"html": pending, "replace": replace})
        ended = False
        while not ended:
            try:
                first = await asyncio.wait_for(stream.queue.get(), HEARTBEAT_SECONDS)
            except TimeoutError:
                yield HEARTBEAT
                continue
            batch = [first]
            while not stream.queue.empty():
                batch.append(stream.queue.get_nowait())
            text: str | None = None
            for item in batch:
                if isinstance(item, str):
                    text = item  # each is the whole of the call's text so far
                    continue
                if text is not None:
                    yield event("text", {"html": str(reply_markdown(text))})
                    text = None
                if item is _Mark.DISCARD:
                    yield event("discard", {})
                else:
                    ended = True
                    break
            if text is not None:
                yield event("text", {"html": str(reply_markdown(text))})
        await asyncio.wait([task])  # done already: END comes from its done callback
        if task.cancelled() or task.exception() is not None:
            yield event("error", {"status": 500})
            return
        result = task.result()
        if result is None:  # turned off just now: a 404, as every route here is
            yield event("error", {"status": 404})
            return
        yield event("done", {"html": await done(result)})
    finally:
        stream.listening = False
