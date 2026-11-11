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
only, never with the message's text. When Slack refuses a path in a way that retrying won't
fix (``REFUSALS``), Dinnerbell stops trying it for that kind of conversation, DMs or the
household channel, until it restarts. It serves one workspace, so the kind is enough.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from dinnerbell.channels.slack.api import SlackApi

log = logging.getLogger(__name__)

STATUS = "is thinking…"
"""Slack shows it after the app's name: "Dinnerbell is thinking…"."""
LOADING_MESSAGES = ["Reading your message…", "Checking the catalog…", "Thinking it over…"]
"""What the status rotates through while Slack shows it (1 to 10 of them)."""
REACTION = "hourglass_flowing_sand"
"""The reaction on the member's message when Slack won't show the status."""
STEP_SECONDS = 5.0
"""The longest any one indicator call to Slack may take."""
REFUSALS = frozenset(
    {
        "method_not_supported_for_channel_type",
        "missing_scope",
        "not_allowed_token_type",
        "no_permission",
        "access_denied",
    }
)
"""Slack's refusals that the same call would get again in the same kind of conversation."""

Way = Literal["status", "reaction"]
"""The two ways Dinnerbell shows that it's working."""


class Indicators:
    """Starts a ``Working`` for each message the assistant answers, and remembers which paths
    Slack refused, by kind of conversation, until Dinnerbell restarts."""

    def __init__(self, api: SlackApi) -> None:
        self.api = api
        self.refused: set[tuple[Way, bool]] = set()
        """Ways Slack refused (``REFUSALS``), with whether the conversation is a DM."""

    def start(self, channel: str, thread_ts: str, ts: str, *, private: bool) -> Working:
        """Show the indicator for the message ``ts`` in the thread ``thread_ts``, in the
        background. ``private`` is True for a DM."""
        return Working(self, channel, thread_ts, ts, private=private)


class Working:
    """One message's indicator. Call ``stop`` once the reply is posted or has failed, or
    ``cancel`` when the answer itself is cancelled."""

    def __init__(self, indicators: Indicators, channel: str, thread_ts: str, ts: str, *, private: bool) -> None:
        self.indicators = indicators
        self.channel = channel
        self.thread_ts = thread_ts
        self.ts = ts
        self.private = private
        self.status = False
        """Whether a status may be showing: Slack set it, or the call timed out and may have."""
        self.reaction = False
        """Whether the reaction may be showing, in the same sense."""
        self._task = asyncio.create_task(self._show())

    async def _show(self) -> None:
        api = self.indicators.api
        if self._allowed("status"):
            error = await self._attempt(
                "assistant.threads.setStatus",
                lambda: api.set_status(self.channel, self.thread_ts, STATUS, LOADING_MESSAGES),
            )
            self.status = error is None or isinstance(error, TimeoutError)
            if error is None:
                return
            self._remember("status", error)
        if self._allowed("reaction"):
            error = await self._attempt("reactions.add", lambda: api.add_reaction(self.channel, self.ts, REACTION))
            self.reaction = error is None or isinstance(error, TimeoutError)
            if error is not None:
                self._remember("reaction", error)

    async def stop(self) -> None:
        """Wait for the indicator to finish showing, which ``STEP_SECONDS`` bounds, then clear
        what it showed. A reply clears a status already, but an ephemeral or a failed post
        doesn't, so the status is always cleared."""
        await self._task
        api = self.indicators.api
        if self.status:
            await self._attempt("assistant.threads.setStatus", lambda: api.set_status(self.channel, self.thread_ts, ""))
        if self.reaction:
            await self._attempt("reactions.remove", lambda: api.remove_reaction(self.channel, self.ts, REACTION))

    def cancel(self) -> None:
        """Stop showing the indicator without clearing it, when the answer is cancelled, as at
        shutdown. Slack drops a status after 2 minutes; a reaction stays."""
        self._task.cancel()

    def _allowed(self, path: Way) -> bool:
        return (path, self.private) not in self.indicators.refused

    def _remember(self, path: Way, error: Exception) -> None:
        if getattr(error, "code", None) in REFUSALS:
            self.indicators.refused.add((path, self.private))
            where = "direct messages" if self.private else "the channel"
            log.info("slack refused the assistant's working %s in %s; not trying it there again", path, where)

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
