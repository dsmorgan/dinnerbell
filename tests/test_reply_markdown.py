# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's replies on the web (#284): a small Markdown subset, escaped first."""

from __future__ import annotations

import re
import time

import pytest
from markupsafe import escape

from dinnerbell.web.reply_markdown import reply_markdown

ALLOWED_TAGS = {"p", "br", "ul", "ol", "li", "strong", "em", "del", "code", "pre", "hr"}


def html(text: str) -> str:
    return str(reply_markdown(text))


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (
            "Yes, you have two vetoes set.\n\n**Tag**\n- fast food (cuisine)\n\n"
            "**Restaurants**\n- Pho Corner\n- Taco Stop",
            "<p>Yes, you have two vetoes set.</p><p><strong>Tag</strong></p><ul><li>fast food (cuisine)</li></ul>"
            "<p><strong>Restaurants</strong></p><ul><li>Pho Corner</li><li>Taco Stop</li></ul>",
        ),
        ("line one\nline two", "<p>line one<br>line two</p>"),
        ("* a\n+ b\n• c", "<ul><li>a</li><li>b</li><li>c</li></ul>"),
        (
            "1. one\n2. two\n   more\n3) three\nafter",
            "<ol><li>one</li><li>two<br>more</li><li>three</li></ol><p>after</p>",
        ),
        ("5. five\n6. six", '<ol start="5"><li>five</li><li>six</li></ol>'),
        ("- a\n1. b", "<ul><li>a</li></ul><ol><li>b</li></ol>"),
        ("## Places ##\ntext", "<p><strong>Places</strong></p><p>text</p>"),
        (
            "__bold__ *it* _it_ ~~gone~~ `x*y*`",
            "<p><strong>bold</strong> <em>it</em> <em>it</em> <del>gone</del> <code>x*y*</code></p>",
        ),
        ("***both***", "<p><strong><em>both</em></strong></p>"),
        ("snake_case_name and 2*3*4", "<p>snake_case_name and 2*3*4</p>"),
        ("\\*kept\\* and \\_kept\\_", "<p>&#42;kept&#42; and &#95;kept&#95;</p>"),
        ("```\n<b>code</b> **no**\n```\nafter", "<pre><code>&lt;b&gt;code&lt;/b&gt; **no**</code></pre><p>after</p>"),
        ("```\nnever closed", "<pre><code>never closed</code></pre>"),
        ("---", "<hr>"),
        ("", ""),
        ("\n\n  \n", ""),
    ],
)
def test_the_subset_renders(text: str, shown: str) -> None:
    assert html(text) == shown


@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "[tap me](https://evil.example)",
        "![pic](https://evil.example/a.png)",
        "<https://evil.example>",
        "**<a href='https://evil.example'>x</a>**",
        "`<script>`",
        "```\n</code></pre><script>x</script>\n```",
        "- <b>x</b>\n1. <i>y</i>",
        "# <h1>z</h1>",
        '*" onmouseover="x*',
        "&lt;script&gt; &#60;b&#62;",
    ],
)
def test_nothing_but_the_renderers_tags_comes_out(text: str) -> None:
    """Escaped first: every tag in the result is one the renderer wrote, with no attribute but
    an ``ol``'s ``start``, and no link."""
    out = html(text)
    for tag in re.findall(r"<(/?)([a-zA-Z0-9]+)([^>]*)>", out):
        assert tag[1] in ALLOWED_TAGS, out
        assert tag[2] == "" or re.fullmatch(r' start="\d+"', tag[2]), out
    assert "<a" not in out and "<img" not in out and "<script" not in out


@pytest.mark.parametrize(
    "line",
    [
        "a" + "`" * 50_000,
        "**a " * 12_500,
        "_a " * 16_000,
        "#" + " " * 50_000 + "x",
        "- " + "-  " * 16_000 + "x",
        "~~a " * 12_500,
    ],
)
def test_a_long_line_shows_as_text_without_running_long(line: str) -> None:
    """A reply the model wrote can be built to make a pattern backtrack; each line's work is
    bounded by ``MAX_STYLED``. The bound is loose: it catches cubic work, not a slow machine."""
    started = time.monotonic()
    assert html(line + "\n" + line) == f"<p>{escape(line.strip())}<br>{escape(line.strip())}</p>"
    assert time.monotonic() - started < 2


def test_a_backslashed_backtick_opens_no_code() -> None:
    assert html("\\`x\\` and ``a ` b``") == "<p>&#96;x&#96; and <code>a ` b</code></p>"


def test_an_entity_the_model_wrote_stays_text() -> None:
    assert html("&lt;b&gt;") == "<p>&amp;lt;b&amp;gt;</p>"
