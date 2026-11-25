# SPDX-License-Identifier: AGPL-3.0-or-later
"""The **Assistant** page's reply while the model writes it (#370, spec 6.14).

``app.js`` posts the message form with ``fetch`` and ``Accept: text/event-stream``; the same
``POST /assistant`` then answers with a stream of events in Server-Sent Events framing, which
every proxy on the way recognizes as a stream (``docs/troubleshooting.md``). Without that
header, HTMX and the plain form post get what they always got.

``WebStream`` is a ``services.assistant.streaming.ReplyStream`` in words (#372): ``CallText``
holds each call's first ``WORDS_HOLD_CHARS`` characters, passes on the rest a word at a time
with names put back, and drops or marks a tool round's text. Its methods only put onto a queue,
so they never block the model's events. The answer runs in a task of its own
(``Answers.start``) with its own database session, never the request's: when the member leaves
the page, Starlette cancels only the response, and the reply still finishes and is stored.
Shutdown cancels what's still running (``Answers.settle``) before the database closes.

The events, in order:

- ``start``: the member's message and a hidden placeholder for the reply. ``replace`` says the
  exchange replaces the turns shown (Conversation hours 0).
- ``text``: the current model call's text so far, rendered as the finished reply is, with its
  last line's open bold, italics, strikethrough, or code closed. Each is the blocks that changed:
  the page keeps its first ``keep`` blocks (top-level elements) and puts ``html`` after them, so
  a long reply costs about its length once, not once per event. At most every ``TEXT_SECONDS``,
  and less often when an event is large (``TEXT_BYTES_PER_SECOND``).
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
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from dinnerbell.services.assistant.streaming import Cut
from dinnerbell.web.reply_markdown import reply_blocks

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


TEXT_SECONDS = 0.05
"""The least time between two ``text`` events: 20 a second, a word or two each at the model's
usual pace. Text that comes meanwhile goes in the next one."""
TEXT_BYTES_PER_SECOND = 16_000
"""The most ``text`` events carry a second: after a large one, the next waits longer than
``TEXT_SECONDS``. A reply that's one long block goes whole in each event, so this keeps its cost
from growing with every word."""


class _Mark(enum.Enum):
    DISCARD = "discard"
    END = "end"


@dataclass(frozen=True, slots=True)
class _Text:
    """The call's text changed. ``events`` reads the text when it sends, so a reply queues one of
    these at a time, never a copy of its text per line."""

    call: int


def wants_stream(request: Request) -> bool:
    """Whether the page asked for the reply as it's written: ``app.js`` does, HTMX never does."""
    return STREAM_TYPE in request.headers.get("accept", "")


def event(name: str, data: dict[str, Any]) -> str:
    """One event, in Server-Sent Events framing: JSON on one ``data`` line."""
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class WebStream:
    """A ``ReplyStream`` into a queue that ``events`` reads. Once ``events`` stops listening, as
    when the member leaves the page, nothing more is queued."""

    cut = Cut.WORDS
    """Words: the page shows the reply as it's written, not a line at a time (#372)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[_Text | _Mark] = asyncio.Queue()
        self.listening = True
        self.call = 0
        """Counts each call's text, so text queued for one call never shows as another's."""
        self.shown = ""
        """The current call's text so far, as ``more`` gave it."""
        self._queued = False
        """A ``_Text`` for this call waits in the queue."""

    # ReplyStream ---------------------------------------------------------------------------------

    def call_started(self, drafts: int) -> None:
        self._new_call()  # a call's text never joins another's

    def more(self, text: str) -> None:
        self.shown += text
        if not self._queued:
            self._queued = True
            self._put(_Text(self.call))

    def tool_round(self) -> None:
        self._new_call()
        self._put(_Mark.DISCARD)

    # ---------------------------------------------------------------------------------------------

    def take(self, item: _Text) -> str | None:
        """The text to send for ``item``: all of its call's text so far, or None once another
        call's has begun or there's none."""
        if item.call != self.call:
            return None
        self._queued = False
        return self.shown or None

    def end(self) -> None:
        """The answer is over, with or without a reply."""
        self._put(_Mark.END)

    def _new_call(self) -> None:
        self.call += 1
        self.shown = ""
        self._queued = False

    def _put(self, item: _Text | _Mark) -> None:
        if self.listening:
            self.queue.put_nowait(item)


class Answers:
    """The web's answers in flight, one set per app. The loop keeps only a weak reference to a
    task, so this holds them; at shutdown, ``settle`` cancels what's left before the database and
    the model client close."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task[Any]] = set()

    def start[T](self, work: Coroutine[Any, Any, T], stream: WebStream, member_id: int) -> asyncio.Task[T]:
        """Run ``work``, the answer, in a task of its own, so the response going away doesn't
        cancel it. ``stream`` hears when it ends; a failure is logged here, with the member's ID
        only, whether or not anyone listens."""
        task = asyncio.create_task(work)
        self.tasks.add(task)

        def finished(done: asyncio.Task[T]) -> None:
            self.tasks.discard(done)
            stream.end()
            if not done.cancelled() and (exc := done.exception()) is not None:
                log.error("the assistant broke answering on the web", exc_info=exc, extra={"member_id": member_id})

        task.add_done_callback(finished)
        return task

    async def settle(self, limit: float) -> None:
        """At shutdown: cancel every answer still running, and wait up to ``limit`` seconds
        for each to unwind. A cancelled model call keeps its reservation, settled before its task
        ends (``conversation._shielded``), so this waits for that write too."""
        pending = set(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            log.info("assistant answers on the web cancelled at shutdown", extra={"answers": len(pending)})
            await asyncio.wait(pending, timeout=limit)


def next_text_at(now: float, sent: str) -> float:
    """When the ``text`` event after ``sent``, which went at ``now``, may go."""
    return now + max(TEXT_SECONDS, len(sent) / TEXT_BYTES_PER_SECOND)


def _kept(before: list[str], after: list[str]) -> int:
    """How many blocks ``after`` starts with that the page shows already, as ``before``."""
    kept = 0
    for old, new in zip(before, after, strict=False):
        if old != new:
            break
        kept += 1
    return kept


async def events[T](
    stream: WebStream,
    task: asyncio.Task[T | None],
    pending: str,
    *,
    replace: bool,
    done: Callable[[T], Awaitable[str]],
) -> AsyncIterator[str]:
    """The response body: ``start`` with ``pending`` (the member's message and the reply's
    placeholder), the shown text as it changes, at most every ``TEXT_SECONDS``, and then
    ``done`` with what ``done`` renders from the task's result, or ``error``."""
    loop = asyncio.get_running_loop()
    ready_at = loop.time()
    """When the next ``text`` event may go."""
    blocks: list[str] = []
    """The blocks the page shows of the current call's text."""
    call = 0
    """The call whose text they are."""
    try:
        yield event("start", {"html": pending, "replace": replace})
        waiting: _Text | None = None
        """Text to send once ``ready_at`` comes, unless the answer ends first: ``done`` then
        replaces it, so it never holds ``done`` up."""
        while True:
            timeout = HEARTBEAT_SECONDS if waiting is None else ready_at - loop.time()
            item: _Text | _Mark | None = None
            if timeout > 0:
                try:
                    item = await asyncio.wait_for(stream.queue.get(), timeout)
                except TimeoutError:
                    if waiting is None:
                        yield HEARTBEAT
                        continue
            elif not stream.queue.empty():
                item = stream.queue.get_nowait()  # an end that came meanwhile goes first
            if item is None:  # the wait for ``waiting`` is over
                item, waiting = waiting, None
            elif isinstance(item, _Text) and item is not waiting and loop.time() < ready_at:
                waiting = item  # the latest call's text; more words may come meanwhile
                continue
            if isinstance(item, _Text):
                waiting = None
                text = stream.take(item)
                if text is None:
                    continue
                if item.call != call:  # another call's text replaces what's shown
                    call, blocks = item.call, []
                now = reply_blocks(text, unfinished=True)
                keep = _kept(blocks, now)
                if keep == len(now) == len(blocks):
                    continue
                blocks = now
                sent = event("text", {"keep": keep, "html": "".join(now[keep:])})
                ready_at = next_text_at(loop.time(), sent)
                yield sent
            elif item is _Mark.DISCARD:
                waiting, blocks = None, []
                ready_at = loop.time()  # the next call's first text never waits for this one's
                yield event("discard", {})
            else:
                break
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
