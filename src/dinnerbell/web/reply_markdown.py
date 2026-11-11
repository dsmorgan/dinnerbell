# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's replies on the web, with their Markdown shown (#284): a small subset, built
here, not a Markdown library, so nothing but these tags can come out.

Blocks: paragraphs (a line break stays one), bulleted lists (``-``, ``*``, ``+``, or ``•``),
numbered lists (``1.`` or ``1)``), headings (``#`` to ``######``, shown as a bold line), and
fenced code (shown as it is). Inline: bold (``**`` or ``__``), italics (``*`` or ``_``),
strikethrough (``~~``), and code (`````). A backslash before a marker keeps it as typed.

Everything else stays text: links, images, tables, block quotes, and HTML. The text is escaped
first, so the only tags in the result are the fixed ones this module writes, and a link is never
clickable (ADR 0009)."""

from __future__ import annotations

import re
from collections.abc import Iterator

from markupsafe import Markup, escape

_BULLET = re.compile(r"^\s{0,3}[-*+•]\s+(\S.*)$")
_NUMBERED = re.compile(r"^\s{0,3}(\d{1,9})[.)]\s+(\S.*)$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$")
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


def _blocks(lines: list[str]) -> Iterator[str]:
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
        fence = _FENCE.match(line)
        if fence:
            yield from flush()
            body: list[str] = []
            while index < len(lines) and not lines[index].lstrip().startswith(fence.group(1)):
                body.append(lines[index])
                index += 1
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
            yield f"<p><strong>{inline(heading.group(1))}</strong></p>"
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


def reply_markdown(text: str) -> Markup:
    """The reply ``text`` as HTML, with the subset above shown and everything else as text."""
    return Markup("".join(_blocks(text.replace("\r\n", "\n").split("\n"))))  # noqa: S704 - built from escaped text
