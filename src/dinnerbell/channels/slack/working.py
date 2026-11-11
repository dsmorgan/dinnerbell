# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's working indicator in Slack (#265, spec 6.14): what a member sees while
Dinnerbell answers their message.

Dinnerbell first asks Slack for a status under the thread's last message, "Dinnerbell is
thinking…", with a few rotating loading messages (``assistant.threads.setStatus``). If Slack
refuses it or doesn't answer in time, Dinnerbell reacts to the member's message instead
(``REACTION``), which needs ``reactions:write`` from the assistant manifests. Once the reply is
posted, or fails, Dinnerbell clears whatever it showed.

The indicator never holds up the reply. It runs beside the answer as its own task, each Slack
call gets at most ``STEP_SECONDS``, and a failure is logged by its type and Slack's error code
only, never with the message's text. When Slack refuses a way in a manner that retrying won't
fix (``REFUSALS``), Dinnerbell stops trying it for that kind of conversation, DMs or the
household channel, for ``REFUSAL_TTL``, so a fix such as adding ``reactions:write`` takes
effect without a restart. Dinnerbell serves one workspace, so the kind is enough.

A status belongs to the thread, not the message, and Slack clears it on any reply in the
thread. So when two members' messages in one channel thread are answered at once, the first to
finish sets the status again for the other instead of clearing it, and only the last clears it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from dinnerbell.channels.slack.api import SlackApi, SlackUnavailable
from dinnerbell.domain.notifications import PermanentDeliveryError
from dinnerbell.services.clock import Clock

log = logging.getLogger(__name__)

STATUS = "is thinking…"
"""Slack shows it after the app's name: "Dinnerbell is thinking…"."""
LOADING_MESSAGES = ["Reading your message…", "Checking the catalog…", "Thinking it over…"]
"""What the status rotates through while Slack shows it (1 to 10 of them)."""
REACTION = "hourglass_flowing_sand"
"""The reaction on the member's message when Slack won't show the status."""
STEP_SECONDS = 5.0
"""The longest any one indicator call to Slack may take."""
CANCEL_SECONDS = 2.0
"""How long clearing may take once the answer is cancelled, as at shutdown."""
REFUSALS = frozenset({"method_not_supported_for_channel_type", "missing_scope", "not_allowed_token_type"})
"""Slack's refusals that the same call would get again in the same kind of conversation."""
REFUSAL_TTL = timedelta(hours=1)
"""How long Dinnerbell stops trying a way Slack refused, before it tries once more."""

Way = Literal["status", "reaction"]
"""The two ways Dinnerbell shows that it's working."""

_clearing: set[asyncio.Task[None]] = set()
"""Clears still running after a cancelled answer stopped waiting for them."""


def answered(error: Exception) -> bool:
    """Whether Slack answered the call that raised ``error``, so it certainly didn't take
    effect. After a timeout or a transport error, it may have."""
    if isinstance(error, PermanentDeliveryError):
        return True
    return isinstance(error, SlackUnavailable) and error.code is not None


@dataclass
class _Thread:
    """The indicators in one thread. It stays in ``Indicators.threads`` until its last
    message's clear is done, so a message that starts meanwhile joins it."""

    active: int = 0
    """Messages in the thread being answered now."""
    statuses: int = 0
    """Messages whose status may be showing, or may have been until a reply: when it isn't 0,
    the last message to finish clears the thread's status."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Held across every call that sets or clears the thread's status, so they land in the
    order they're decided: a status set again for a message still running can't land after
    the last one's clear, and a new message's status can't land before an old clear."""


class Indicators:
    """Starts a ``Working`` for each message the assistant answers, keeps track of the threads
    with one, and remembers which ways Slack refused, by kind of conversation."""

    def __init__(self, api: SlackApi, clock: Clock) -> None:
        self.api = api
        self.clock = clock
        self.refused: dict[tuple[Way, bool], datetime] = {}
        """Ways Slack refused (``REFUSALS``), with whether the conversation is a DM, and until
        when Dinnerbell doesn't try them there."""
        self.threads: dict[tuple[str, str], _Thread] = {}
        """Threads with a message being answered, by channel and thread root."""

    def start(self, channel: str, thread_ts: str, ts: str, *, private: bool) -> Working:
        """Show the indicator for the message ``ts`` in the thread ``thread_ts``, in the
        background. ``private`` is True for a DM."""
        return Working(self, channel, thread_ts, ts, private=private)

    def allowed(self, way: Way, private: bool) -> bool:
        until = self.refused.get((way, private))
        if until is None:
            return True
        if self.clock() < until:
            return False
        del self.refused[(way, private)]  # long enough: try it again
        return True

    def refuse(self, way: Way, private: bool) -> None:
        self.refused[(way, private)] = self.clock() + REFUSAL_TTL


class Working:
    """One message's indicator. Call ``stop`` once the reply is posted or has failed, or
    ``cancel`` when the answer itself is cancelled."""

    def __init__(self, indicators: Indicators, channel: str, thread_ts: str, ts: str, *, private: bool) -> None:
        self.indicators = indicators
        self.channel = channel
        self.thread_ts = thread_ts
        self.ts = ts
        self.private = private
        self.thread = indicators.threads.setdefault((channel, thread_ts), _Thread())
        self.thread.active += 1
        self._left = False
        self.reaction = False
        """Whether the reaction may be showing: Slack added it, or may have before an error."""
        self._task = asyncio.create_task(self._show())

    async def _show(self) -> None:
        api = self.indicators.api
        if self.indicators.allowed("status", self.private):
            async with self.thread.lock:
                self.thread.statuses += 1  # may be showing from the moment the call starts
                error = await self._attempt(
                    "assistant.threads.setStatus",
                    lambda: api.set_status(self.channel, self.thread_ts, STATUS, LOADING_MESSAGES),
                )
                if error is not None and answered(error):
                    self.thread.statuses -= 1
            if error is None:
                return
            self._remember("status", error)
        if self.indicators.allowed("reaction", self.private):
            self.reaction = True
            error = await self._attempt("reactions.add", lambda: api.add_reaction(self.channel, self.ts, REACTION))
            if error is not None:
                self.reaction = not answered(error)
                self._remember("reaction", error)

    async def resume(self) -> None:
        """Show the status again, as when a streamed message turned out not to be the reply:
        any message in the thread clears it. Best effort and bounded, as every call is."""
        if not self.indicators.allowed("status", self.private):
            return
        api = self.indicators.api
        async with self.thread.lock:
            self.thread.statuses += 1
            await self._attempt(
                "assistant.threads.setStatus",
                lambda: api.set_status(self.channel, self.thread_ts, STATUS, LOADING_MESSAGES),
            )

    async def stop(self) -> None:
        """Wait for the indicator to finish showing, which ``STEP_SECONDS`` bounds, then clear
        what it showed. A reply clears a status already, but an ephemeral or a failed post
        doesn't, so the last message in the thread always clears it. A cancel meanwhile still
        clears, briefly (``cancel``), before it ends the task."""
        try:
            await self._task
            await self._finish()
        except asyncio.CancelledError:
            await self.cancel()
            raise

    async def cancel(self) -> None:
        """Stop showing the indicator, as when the answer is cancelled at shutdown, and clear
        it within ``CANCEL_SECONDS``, so no reaction is left on the message. The clear runs on
        even if this task is cancelled again; Slack drops a status after 2 minutes anyway."""
        self._task.cancel()
        clear = asyncio.create_task(self._clear_after_cancel())
        _clearing.add(clear)
        clear.add_done_callback(_clearing.discard)
        # Cancelled again: the clear goes on, bounded, and the caller re-raises its own cancel.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(clear)

    async def _clear_after_cancel(self) -> None:
        await asyncio.wait([self._task])
        try:
            async with asyncio.timeout(CANCEL_SECONDS):
                await self._finish()
        except TimeoutError:
            log.info("slack working indicator not cleared in time after a cancel")

    async def _finish(self) -> None:
        """Leave the thread and clear what may be showing. Safe to call again after a cancel."""
        api = self.indicators.api
        thread = self.thread
        if not self._left:
            self._left = True
            thread.active -= 1
        async with thread.lock:  # who's still active is read under the lock, after any call before
            if thread.statuses:
                if thread.active == 0:
                    await self._attempt(
                        "assistant.threads.setStatus", lambda: api.set_status(self.channel, self.thread_ts, "")
                    )
                    thread.statuses = 0
                elif self.indicators.allowed("status", self.private):
                    # This reply cleared the status for the whole thread; another is still coming.
                    await self._attempt(
                        "assistant.threads.setStatus",
                        lambda: api.set_status(self.channel, self.thread_ts, STATUS, LOADING_MESSAGES),
                    )
            key = (self.channel, self.thread_ts)
            if thread.active == 0 and self.indicators.threads.get(key) is thread:
                del self.indicators.threads[key]  # only now: a message that started meanwhile kept it
        for _ in range(2):  # once more if Slack fails, so the reaction isn't stranded
            if not self.reaction:
                break
            error = await self._attempt(
                "reactions.remove", lambda: api.remove_reaction(self.channel, self.ts, REACTION)
            )
            if error is None:
                self.reaction = False

    def _remember(self, way: Way, error: Exception) -> None:
        if getattr(error, "code", None) in REFUSALS:
            self.indicators.refuse(way, self.private)
            where = "direct messages" if self.private else "the channel"
            log.info("slack refused the assistant's working %s in %s; not trying it there for now", way, where)

    @staticmethod
    async def _attempt(method: str, call: Callable[[], Awaitable[None]]) -> Exception | None:
        """Make one indicator call, at most ``STEP_SECONDS`` long. Returns what went wrong, if
        anything, after logging its type and Slack's error code."""
        try:
            async with asyncio.timeout(STEP_SECONDS):
                await call()
        except Exception as exc:
            extra = {"slack_method": method, "error": type(exc).__name__, "slack_error": getattr(exc, "code", None)}
            log.info("slack working indicator call failed", extra=extra)
            return exc
        return None
