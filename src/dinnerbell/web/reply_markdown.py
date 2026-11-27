# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's replies on the web, with their Markdown shown (#284): a small subset, built
here, not a Markdown library, so nothing but these tags can come out.

Blocks: paragraphs (a line break stays one), bulleted lists (``-``, ``*``, ``+``, or ``•``),
numbered lists (``1.`` or ``1)``), headings (``#`` to ``######``, shown as a bold line), and
fenced code (shown as it is). Inline: bold (``**`` or ``__``), italics (``*`` or ``_``),
strikethrough (``~~``), and code (`````). A backslash before a marker keeps it as typed.

Everything else stays text: links, images, tables, block quotes, and HTML. The text is escaped
first, so the only tags in the result are the fixed ones this module writes, and a link is never
clickable (ADR 0009).

A reply still being written (``reply_blocks`` with ``unfinished``, #372) can end anywhere, mid-word
included (#377). Its last line's open code span, bold, italics, or strikethrough is closed, so it
shows styled while the model writes it, not as the characters typed. What the next characters
could still make something else waits for them: markers at the line's end that show as typed, a
backslash, a last line that's only a list, heading, rule, or code block's marker so far (or a
number, which could be a numbered item's), and the start of a code block's closing fence. A
``[``, ``>``, ``|``, or ``&`` needs nothing: links, block quotes, tables, and entities always show
as typed, so a half-written one is just the text so far."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator

from markupsafe import Markup, escape

_BULLET = re.compile(r"^\s{0,3}[-*+•]\s+(\S.*)$")
_NUMBERED = re.compile(r"^\s{0,3}(\d{1,9})[.)]\s+(\S.*)$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(\S.*)$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")

MAX_STYLED = 2000
"""A longer line shows as plain text: every pattern here looks at one line, so this bounds the
work a reply the model wrote can make, however it's built (a run of backticks, of markers, of
spaces), on each load of the page that keeps it."""

_CODE = re.compile(r"(?<![\\`])(`+)(?!`)(.{1,500}?)(?<!`)\1(?!`)")
_ESCAPED = re.compile(r"\\([\\`*_~#+\-.!\[\]()>|])")
# Each span is at most 500 characters, so a marker that never closes costs a bounded look ahead.
_INLINE = (
    (re.compile(r"\*\*\*(?=\S)(.{1,500}?)(?<=\S)\*\*\*"), "<strong><em>", "</em></strong>"),
    (re.compile(r"\*\*(?=\S)(.{1,500}?)(?<=\S)\*\*"), "<strong>", "</strong>"),
    (re.compile(r"(?<!\w)__(?=\S)(.{1,500}?)(?<=\S)__(?!\w)"), "<strong>", "</strong>"),
    (re.compile(r"~~(?=\S)(.{1,500}?)(?<=\S)~~"), "<del>", "</del>"),
    (re.compile(r"(?<![\w*])\*(?=[^\s*])(.{1,500}?)(?<=[^\s*])\*(?![\w*])"), "<em>", "</em>"),
    (re.compile(r"(?<!\w)_(?=[^\s_])(.{1,500}?)(?<=[^\s_])_(?!\w)"), "<em>", "</em>"),
)


def _styled(text: str) -> str:
    """Escaped ``text`` with its bold, italics, and strikethrough as tags. A backslashed marker
    becomes a character reference first, which no pattern matches."""
    out = str(escape(text))
    out = _ESCAPED.sub(lambda m: f"&#{ord(m.group(1))};", out)
    for pattern, opening, closing in _INLINE:
        out = pattern.sub(rf"{opening}\1{closing}", out)
    return out


def inline(text: str) -> str:
    """One line as HTML: code spans as they are, the rest through ``_styled``. A line longer
    than ``MAX_STYLED`` is only escaped."""
    if len(text) > MAX_STYLED:
        return str(escape(text))
    parts: list[str] = []
    start = 0
    for match in _CODE.finditer(text):
        parts.append(_styled(text[start : match.start()]))
        parts.append(f"<code>{escape(match.group(2).strip() or match.group(2))}</code>")
        start = match.end()
    parts.append(_styled(text[start:]))
    return "".join(parts)


_CLOSERS = ("**", "*", "`", "_", "__", "~~", "***", "``")
"""What ``_closed`` tries at the end of a line still being written, best first."""
_CODE_SPAN = re.compile(r"<code>.*?</code>")
_TAG = re.compile(r"<[^>]*>")
_MARKER = re.compile(r"[*_~`]")
_EMPTY_SPAN = re.compile(r"<(?:strong|em|del|code)>(?:<[^>]*>|[\s*_~`])*</")
"""A span with nothing in it but markers or whitespace: a closer made it from markers alone."""
_JUST_MARKER = re.compile(r"\s{0,3}(?:[-*+•]|\d{1,9}[.)]?|#{1,6}|([-*_])\s*\1?|`{3,}.*|~{3,}.*)?")
"""A last line that's only what starts a list item, heading, rule, or code block, so far, or
could be: a number could be a numbered item's."""
_TRAILING_MARKERS = re.compile(r"[\s*_~`]+\Z")
_OPENER = re.compile(r"(?<![\\\w])[*_~]+(?=[^\s*_~`])|(?<=\w)(?:\*\*|~~)(?=[^\s*_~`])")
"""Markers that could open bold, italics, or strikethrough: not after a backslash, and after a
word character only if they're not a single ``*`` or ``_``, as in "2*3" or "snake_case"."""


def _kind(line: str) -> tuple[bool, ...]:
    return tuple(bool(p.match(line)) for p in (_FENCE, _RULE, _HEADING, _BULLET, _NUMBERED))


def _literal_markers(styled: str) -> int:
    """How many marker characters ``styled``, a line from ``inline``, shows as typed outside
    code."""
    return len(_MARKER.findall(_TAG.sub("", _CODE_SPAN.sub("", styled))))


def _best_closer(line: str) -> tuple[str, int]:
    """``line`` with the closer from ``_CLOSERS`` that leaves the fewest markers showing as typed
    outside code, if any leaves fewer, and how many it leaves. A closer must keep the line the
    kind of block it is, and make no span of markers alone that the line doesn't have already.
    Each try styles the line once, so only closers whose characters the line has are tried, and
    the first that leaves none wins."""
    styled = inline(line)
    empty = Counter(_EMPTY_SPAN.findall(styled))
    fewest = _literal_markers(styled)
    best = line
    for closer in _CLOSERS:
        if not fewest:
            break
        if closer[0] not in line:
            continue
        candidate = line + closer
        if _kind(candidate) != _kind(line):
            continue
        styled = inline(candidate)
        if Counter(_EMPTY_SPAN.findall(styled)) - empty:
            continue
        shown = _literal_markers(styled)
        if shown < fewest:
            best, fewest = candidate, shown
    return best, fewest


def _held(line: str) -> str:
    """``line``, the last of a reply still being written, without what the next delta could still
    make something else: a backslash that could escape what comes next, and markers at the end
    that show as typed, such as the ``*`` of "a *" or "**bold*", which could be a span's start
    or end. A line that would be a rule only without them is held whole."""
    if len(line) > MAX_STYLED:
        return line
    held, before = line, None
    while held != before:  # each turn makes it shorter
        before = held
        if (len(held) - len(held.rstrip("\\"))) % 2:
            held = held[:-1]
        styled = inline(held)
        if styled and _MARKER.match(styled[-1]):
            held = _TRAILING_MARKERS.sub("", held)
    return "" if _RULE.match(held) and not _RULE.match(line) else held


def _closed(line: str) -> str:
    """``line``, the last of a reply still being written (and ``_held``), with its open bold,
    italics, strikethrough, or code closed, as ``_best_closer`` finds. When markers still show as
    typed, as in "*a* and **b _c", where no one closer does, the line ends instead before the
    last marker that could open a span, if that shows fewer and cuts off no code: that span
    shows once it closes."""
    if len(line) > MAX_STYLED or not line:
        return line
    best, fewest = _best_closer(line)
    if fewest:
        openers = [opener.start() for opener in _OPENER.finditer(line)]
        if openers and "`" not in line[openers[-1] :]:
            shorter = line[: openers[-1]].rstrip()
            if shorter and _kind(shorter) == _kind(line):
                closed, left = _best_closer(shorter)
                if left < fewest:
                    return closed
    return best


def _blocks(lines: list[str], *, unfinished: bool = False) -> Iterator[str]:
    paragraph: list[str] = []
    items: list[str] = []
    kind = ""  # "ul" or "ol" while ``items`` holds a list
    first = 1

    def flush() -> Iterator[str]:
        nonlocal kind
        if paragraph:
            yield "<p>" + "<br>".join(inline(line.strip()) for line in paragraph) + "</p>"
            paragraph.clear()
        if items:
            opening = f'<ol start="{first}">' if kind == "ol" and first != 1 else f"<{kind}>"
            yield opening + "".join(f"<li>{item}</li>" for item in items) + f"</{kind}>"
            items.clear()
        kind = ""

    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if unfinished and index == len(lines):  # a fence's last line never comes here
            line = _held(line)
            if _JUST_MARKER.fullmatch(line):
                continue  # it shows once its first word does, as the block it starts
            line = _closed(line)
        fence = _FENCE.match(line)
        if fence:
            yield from flush()
            body: list[str] = []
            while index < len(lines) and not lines[index].lstrip().startswith(fence.group(1)):
                body.append(lines[index])
                index += 1
            tail = body[-1].strip() if unfinished and index == len(lines) and body else ""
            if tail and fence.group(1).startswith(tail):
                body.pop()  # the start of the closing fence, so far
            index += 1  # the closing fence, or past the end
            yield f"<pre><code>{escape(chr(10).join(body))}</code></pre>"
            continue
        if not line.strip():
            yield from flush()
            continue
        if len(line) > MAX_STYLED:  # plain text, matched against nothing
            if items:
                yield from flush()
            paragraph.append(line)
            continue
        bullet, numbered, heading = _BULLET.match(line), _NUMBERED.match(line), _HEADING.match(line)
        if _RULE.match(line) and not bullet:
            yield from flush()
            yield "<hr>"
        elif heading:
            yield from flush()
            words = heading.group(1).rstrip()
            trailer = words.rstrip("#")  # a closing run of #, after a space, isn't part of the heading
            if trailer != words and (not trailer or trailer[-1].isspace()):
                words = trailer.rstrip()
            if words:
                yield f"<p><strong>{inline(words)}</strong></p>"
        elif bullet or numbered:
            wanted = "ul" if bullet else "ol"
            if kind != wanted:
                yield from flush()
                kind = wanted
                first = int(numbered.group(1)) if numbered else 1
            items.append(inline(bullet.group(1) if bullet else numbered.group(2) if numbered else ""))
        elif items and line[:1].isspace():
            items[-1] += "<br>" + inline(line.strip())  # an indented line goes on with the item
        else:
            if items:
                yield from flush()
            paragraph.append(line)
    yield from flush()


def reply_blocks(text: str, *, unfinished: bool = False) -> list[str]:
    """The reply ``text`` as HTML, one element per block. With ``unfinished``, ``text`` is a
    reply still being written, and its last line's open spans are closed."""
    return list(_blocks(text.replace("\r\n", "\n").split("\n"), unfinished=unfinished))


def reply_markdown(text: str) -> Markup:
    """The reply ``text`` as HTML, with the subset above shown and everything else as text."""
    return Markup("".join(reply_blocks(text)))  # noqa: S704 - built from escaped text
