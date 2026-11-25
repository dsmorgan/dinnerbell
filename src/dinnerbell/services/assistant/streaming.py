# SPDX-License-Identifier: AGPL-3.0-or-later
"""A reply shown while the model writes it (spec 6.14): Slack streams it into a DM's thread (#266),
and the web's Assistant page into its log (#370, #372, ``web.assistant_stream``).

``converse`` gives each model call a ``CallText``, which hears the call's text through the
client's ``ModelEvents`` and passes it on to a ``ReplyStream``, with members' names put back. It
builds the text exactly as the finished reply joins it (``_reply_text``: each text block
stripped, blocks joined by a blank line), so what's shown is always a prefix of the reply, unless
the reply turns out to be one of Dinnerbell's own lines, such as running out of time.

The stream's ``cut`` says where its pieces may end: just before the whitespace that ends a line
(``Cut.LINES``, Slack, whose pipeline and update limits work in whole lines), or before any
whitespace (``Cut.WORDS``, the web page, which shows the reply word by word). A word still being
written waits for the whitespace after it, and so does a member's label that the next delta could
still finish (``domain.assistant.label_may_span``): a piece never ends inside "Member 1", or just
after a "Member 1" that could still become "Member 12". So names put back into each piece are the
names the whole reply gets.

A call can write a little text and then call tools, and only the end of the call says which it
was. So a call's text is held: for lines, until it has ``HOLD_LINES`` whole lines and
``HOLD_CHARS`` characters; for words, until it has ``WORDS_HOLD_CHARS`` characters. A tool call
before then drops the held text unseen, and the working indicator stays. A tool call after it
tells the stream the shown text wasn't the reply (``tool_round``).

Nothing here changes the reply, what's stored, or what's sent to Anthropic.
"""

from __future__ import annotations

import enum
import logging
import re
from collections.abc import Callable
from typing import Protocol

from dinnerbell.domain.assistant import label_may_span

log = logging.getLogger(__name__)

HOLD_LINES = 2
"""Whole lines a call writes before any of its text is shown in lines."""
HOLD_CHARS = 160
"""Characters a call writes before any of its text is shown in lines."""
WORDS_HOLD_CHARS = 120
"""Characters a call writes before any of its text is shown in words: about half a second of the
model's writing, and more than the sentence or two a model writes before a tool call when it
ignores the prompt's "Write no text before a tool call"."""
SEPARATOR = "\n\n"
"""What joins a reply's text blocks (``conversation._reply_text``)."""
_SPACE = re.compile(r"\s")
"""Whitespace, as ``str.isspace`` and ``str.rstrip`` see it."""


def _last_break(text: str, start: int, stop: int, *, words: bool) -> int:
    """The index of the last break in ``text[start:stop]``, or -1: a line break, or with
    ``words`` any whitespace. It looks back from ``stop`` in doubling steps, so it costs about
    the distance to the break, at C speed."""
    if not words:
        return text.rfind("\n", start, stop)
    high, size = stop, 64
    while high > start:
        low = max(start, high - size)
        found = _SPACE.search(text[low:high][::-1])
        if found is not None:
            return high - 1 - found.start()
        high, size = low, size * 2
    return -1


class Cut(enum.Enum):
    """Where ``CallText`` may end a piece of a call's text."""

    LINES = "lines"
    """Just before the whitespace that ends a line."""
    WORDS = "words"
    """Just before any whitespace."""


class ReplyStream(Protocol):
    """Where a reply's text goes while it's written. Each method is synchronous and must not
    block; the receiver does its own I/O. ``CallText`` ignores anything one raises."""

    cut: Cut
    """Where the stream's pieces end."""

    def call_started(self, drafts: int) -> None:
        """A model call starts. ``drafts`` is how many drafts the message has so far."""
        ...

    def more(self, text: str) -> None:
        """More of the call's text: whole lines or words, as ``cut`` says, with names put back,
        carrying on from the call's last ``more``. It ends with a character that isn't
        whitespace, and anything but the call's first starts with whitespace, which for
        ``Cut.LINES`` holds a line break."""
        ...

    def tool_round(self) -> None:
        """The call whose text was shown calls tools after all, so that text isn't the reply."""
        ...


class CallText:
    """One model call's text, for ``ReplyStream``: a ``ModelEvents``. ``restore`` puts members'
    names back into text in pseudonym form."""

    def __init__(self, out: ReplyStream, restore: Callable[[str], str]) -> None:
        self._out: ReplyStream | None = out
        self._words = out.cut is Cut.WORDS
        self._restore = restore
        self._done = ""
        """The closed text blocks, joined and stripped as the reply joins them."""
        self._done_has_text = False
        """Whether a closed block had any text, so the next one comes after ``SEPARATOR``."""
        self._current: str | None = None
        """The open text block, as written so far."""
        self._sent = 0
        """How much of the joined text (``_joined_open``) has gone to ``more``."""
        self._clear = 0
        """No break in the joined text before here gives more to send: each delta looks for one
        only in what's new. A closed block's stripped whitespace can move a break, so it starts
        over."""
        self._showing = False
        """Past the hold: this call's text is being shown."""
        self._tools = False

    # ModelEvents --------------------------------------------------------------------------------

    def text_started(self) -> None:
        self._close()
        self._current = ""

    def text(self, delta: str) -> None:
        if self._tools:
            return  # the reply is a later call's
        self._current = (self._current or "") + delta
        self._pump()

    def tool_started(self) -> None:
        self._close()
        if self._tools:
            return
        self._tools = True
        if self._showing:
            self._tell(lambda out: out.tool_round())

    # ---------------------------------------------------------------------------------------------

    def _close(self) -> None:
        if self._current is None:
            return
        if self._current:
            separator = SEPARATOR if self._done_has_text else ""
            self._done = self._done + separator + self._current.strip()
            self._done_has_text = True
        self._current = None
        self._clear = 0

    def _pump(self) -> None:
        text = self._joined_open()
        if not self._showing:
            shown = text.lstrip()
            if self._words:
                if len(shown) < WORDS_HOLD_CHARS:
                    return
            elif shown.count("\n") < HOLD_LINES or len(shown) < HOLD_CHARS:
                return
            self._showing = True
        at = _last_break(text, max(self._sent, self._clear), len(text), words=self._words)
        if at < 0:
            self._clear = len(text)
            return
        end = self._end(text, at)
        while end > self._sent and label_may_span(text, end):
            # Before the label's word: the last break before it.
            end = self._end(text, _last_break(text, self._sent, end - len("Member"), words=self._words))
        if end <= self._sent:
            # No break up to ``at`` gives anything now. A label held there shows with the words
            # after the next break, a word late at most.
            self._clear = at + 1
            return
        piece = text[self._sent : end]
        self._sent = end
        restored = self._restore(piece)
        self._tell(lambda out: out.more(restored))

    def _end(self, text: str, at: int) -> int:
        """Where a piece may end for the break at ``at``: just before it, with any whitespace
        before it left out. No further than what's sent, when there's no break (-1)."""
        if at < self._sent:
            return self._sent
        return self._sent + len(text[self._sent : at].rstrip())

    def _joined_open(self) -> str:
        """The call's text so far as the reply would join it. The open block keeps its trailing
        whitespace, so its line breaks count; whatever the reply strips is never sent."""
        if not self._current:
            return self._done
        separator = SEPARATOR if self._done_has_text else ""
        return self._done + separator + self._current.lstrip()

    def _tell(self, call: Callable[[ReplyStream], None]) -> None:
        if self._out is None:
            return
        try:
            call(self._out)
        except Exception:
            log.exception("assistant reply stream broke; the reply goes on without it")
            self._out = None

    def started(self, drafts: int) -> None:
        """Tell the stream this call starts."""
        self._tell(lambda out: out.call_started(drafts))
