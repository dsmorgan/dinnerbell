# SPDX-License-Identifier: AGPL-3.0-or-later
"""A reply shown while the model writes it (spec 6.14): Slack streams it into a DM's thread (#266),
and the web's Assistant page into its log (#370, #372, #377, ``web.assistant_stream``).

``converse`` gives each model call a ``CallText``, which hears the call's text through the
client's ``ModelEvents`` and passes it on to a ``ReplyStream``, with members' names put back. It
builds the text exactly as the finished reply joins it (``_reply_text``: each text block
stripped, blocks joined by a blank line), so what's shown is always a prefix of the reply, unless
the reply turns out to be one of Dinnerbell's own lines, such as running out of time.

The stream's ``cut`` says where its pieces may end. ``Cut.LINES`` (Slack, whose pipeline and
update limits work in whole lines) ends them just before the whitespace that ends a line.
``Cut.DELTAS`` (the web page, which shows the reply as the Claude app does) ends them after any
delta, mid-word included, and holds back only trailing whitespace, which the reply may strip,
and the tail of a member's label that the next delta could still change
(``domain.assistant.label_across``): a piece never ends inside "Member 1", or just after a
"Member 1" that could still become "Member 12". So names put back into each piece are the names
the whole reply gets.

A call can write a little text and then call tools, and only the end of the call says which it
was. In lines, a call's text is held until it has ``HOLD_LINES`` whole lines and ``HOLD_CHARS``
characters, and a tool call before then drops it unseen. In deltas, nothing is held. Either way,
a tool call after some of the text was shown tells the stream it wasn't the reply
(``tool_round``), and the receiver takes it back. The prompt says to write no text before a tool
call, so that's rare.

Nothing here changes the reply, what's stored, or what's sent to Anthropic.
"""

from __future__ import annotations

import enum
import logging
import re
from collections.abc import Callable
from typing import Protocol

from dinnerbell.domain.assistant import label_across

log = logging.getLogger(__name__)

HOLD_LINES = 2
"""Whole lines a call writes before any of its text is shown in lines."""
HOLD_CHARS = 160
"""Characters a call writes before any of its text is shown in lines."""
SEPARATOR = "\n\n"
"""What joins a reply's text blocks (``conversation._reply_text``)."""
_WORD = re.compile(r"\w")
"""A character a label can't start just after: ``\\b`` in the label's pattern."""
_AFTER_WORD = "_"
"""A word character that's never part of a label: put before a piece whose text before it ends
in a word character, so that putting names back finds no label at the piece's start, as in the
whole reply."""


def _last_break(text: str, start: int, stop: int) -> int:
    """The index of the last line break in ``text[start:stop]``, or -1."""
    return text.rfind("\n", start, stop)


def _ink(text: str, start: int) -> int:
    """The index just after the last character in ``text[start:]`` that isn't whitespace, or -1."""
    kept = len(text[start:].rstrip())
    return start + kept if kept else -1


class Cut(enum.Enum):
    """Where ``CallText`` may end a piece of a call's text."""

    LINES = "lines"
    """Just before the whitespace that ends a line."""
    DELTAS = "deltas"
    """After any delta, before any whitespace at its end."""


class ReplyStream(Protocol):
    """Where a reply's text goes while it's written. Each method is synchronous and must not
    block; the receiver does its own I/O. ``CallText`` ignores anything one raises."""

    cut: Cut
    """Where the stream's pieces end."""

    def call_started(self, drafts: int) -> None:
        """A model call starts. ``drafts`` is how many drafts the message has so far."""
        ...

    def more(self, text: str) -> None:
        """More of the call's text, with names put back, carrying on from the call's last
        ``more``. It ends with a character that isn't whitespace. In lines, it's whole lines:
        anything but the call's first starts with whitespace that holds a line break."""
        ...

    def tool_round(self) -> None:
        """The call whose text was shown calls tools after all, so that text isn't the reply."""
        ...


class CallText:
    """One model call's text, for ``ReplyStream``: a ``ModelEvents``. ``restore`` puts members'
    names back into text in pseudonym form: it replaces each label, ``Member`` and a number at a
    word's start, and nothing else."""

    def __init__(self, out: ReplyStream, restore: Callable[[str], str]) -> None:
        self._out: ReplyStream | None = out
        self._deltas = out.cut is Cut.DELTAS
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
        """In lines, no break in the joined text before here gives more to send; in deltas, the
        joined text before here has been looked at for where its whitespace ends. Each delta
        looks only at what's new. A closed block's stripped whitespace can move a break, so in
        lines it starts over."""
        self._end = 0
        """In deltas, the index just after the joined text's last character that isn't
        whitespace."""
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
        if self._deltas:
            self._pump_deltas()
        else:
            self._pump_lines()

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
        # The joined text is the same up to ``_end``; only whitespace after it changes.
        self._clear = min(self._clear, self._end) if self._deltas else 0

    def _pump_deltas(self) -> None:
        text = self._joined_open()
        ink = _ink(text, self._clear)
        if ink >= 0:
            self._end = ink
        self._clear = len(text)
        end = self._end
        while end > self._sent and (start := label_across(text, end)) is not None:
            end = start  # before the label, and any whitespace before it
            while end > self._sent and text[end - 1].isspace():
                end -= 1
        if end <= self._sent:
            return  # a label held there shows once the next delta settles it
        piece = text[self._sent : end]
        if not self._showing:
            piece = piece.lstrip()  # whitespace the reply strips from its start
        if self._sent and _WORD.match(text[self._sent - 1]):
            restored = self._restore(_AFTER_WORD + piece)[len(_AFTER_WORD) :]
        else:
            restored = self._restore(piece)
        self._sent = end
        self._showing = True
        self._tell(lambda out: out.more(restored))

    def _pump_lines(self) -> None:
        text = self._joined_open()
        if not self._showing:
            shown = text.lstrip()
            if shown.count("\n") < HOLD_LINES or len(shown) < HOLD_CHARS:
                return
            self._showing = True
        at = _last_break(text, max(self._sent, self._clear), len(text))
        if at < 0:
            self._clear = len(text)
            return
        end = self._line_end(text, at)
        while end > self._sent and (start := label_across(text, end)) is not None:
            # Before the label's line: the last break before it.
            end = self._line_end(text, _last_break(text, self._sent, start))
        if end <= self._sent:
            # No break up to ``at`` gives anything now. A label held there shows with the line
            # after the next break, a line late at most.
            self._clear = at + 1
            return
        piece = text[self._sent : end]
        self._sent = end
        restored = self._restore(piece)
        self._tell(lambda out: out.more(restored))

    def _line_end(self, text: str, at: int) -> int:
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
