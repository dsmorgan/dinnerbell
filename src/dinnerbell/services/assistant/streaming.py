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
        end = self._end(text, len(text))
        while end > self._sent and label_may_span(text[:end], text[end:]):
            end = self._end(text, end - len("Member"))  # before the label's word
        if end <= self._sent:
            return
        piece = text[self._sent : end]
        self._sent = end
        restored = self._restore(piece)
        self._tell(lambda out: out.more(restored))

    def _end(self, text: str, before: int) -> int:
        """Where a piece may end, at most at ``before``: just before the last whitespace (a line
        break's, for lines) that starts before ``before``, with any whitespace before it left
        out; or no further than what's sent, when there's none."""
        at = before - 1
        while at >= self._sent and not (text[at] == "\n" or (self._words and text[at].isspace())):
            at -= 1
        if at < self._sent:
            return self._sent
        return len(text[:at].rstrip())

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
