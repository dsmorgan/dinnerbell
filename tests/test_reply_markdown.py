# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's replies on the web (#284): a small Markdown subset, escaped first, and a reply
still being written (#372)."""

from __future__ import annotations

import re
import time

import pytest
from markupsafe import escape

from dinnerbell.web import reply_markdown as markdown_module
from dinnerbell.web.reply_markdown import reply_blocks, reply_markdown

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
        ("# C#\n# a ###  \n# ###", "<p><strong>C#</strong></p><p><strong>a</strong></p>"),
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
@pytest.mark.parametrize("unfinished", [False, True])
def test_nothing_but_the_renderers_tags_comes_out(text: str, unfinished: bool) -> None:
    """Escaped first: every tag in the result is one the renderer wrote, with no attribute but
    an ``ol``'s ``start``, and no link, finished or not, and cut anywhere."""
    for cut in range(1, len(text) + 1) if unfinished else [len(text)]:
        check_tags("".join(reply_blocks(text[:cut], unfinished=unfinished)))


def check_tags(out: str) -> None:
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


def test_a_heading_with_a_long_gap_is_quick() -> None:
    """Fifty headings of nearly ``MAX_STYLED`` characters, mostly spaces, the shape a heading
    pattern with a trailer backtracks on."""
    started = time.monotonic()
    shown = html("\n".join(["# a" + " " * 1_990 + "b"] * 50))
    assert time.monotonic() - started < 2 and shown.count("<strong>") == 50


def test_a_backslashed_backtick_opens_no_code() -> None:
    assert html("\\`x\\` and ``a ` b``") == "<p>&#96;x&#96; and <code>a ` b</code></p>"


def test_an_entity_the_model_wrote_stays_text() -> None:
    assert html("&lt;b&gt;") == "<p>&amp;lt;b&amp;gt;</p>"


# A reply still being written (#372) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("Tonight: **Siam", "<p>Tonight: <strong>Siam</strong></p>"),
        ("Run `uv", "<p>Run <code>uv</code></p>"),
        ("a *maybe", "<p>a <em>maybe</em></p>"),
        ("a _maybe", "<p>a <em>maybe</em></p>"),
        ("~~old", "<p><del>old</del></p>"),
        ("***both", "<p><strong><em>both</em></strong></p>"),
        ("- **Tacos", "<ul><li><strong>Tacos</strong></li></ul>"),
        ("1. **Tacos", "<ol><li><strong>Tacos</strong></li></ol>"),
        ("**closed** and _open", "<p><strong>closed</strong> and <em>open</em></p>"),
        ("**Earlier** line\nand **now", "<p><strong>Earlier</strong> line<br>and <strong>now</strong></p>"),
        # Nothing open: as the finished reply.
        ("2*3 is 6", "<p>2*3 is 6</p>"),
        ("x * y", "<p>x * y</p>"),
        ("snake_case", "<p>snake_case</p>"),
        ("a [link", "<p>a [link</p>"),
        # A closer that would make the line another kind of block isn't used.
        ("``", "<p>``</p>"),
        ("a ``", "<p>a ``</p>"),
        # Markers inside code don't count, and code closes first.
        ("Run `ls *.py", "<p>Run <code>ls *.py</code></p>"),
        ("Run `a **b", "<p>Run <code>a **b</code></p>"),
        # A line that ends in a marker gets no closer: it would only make a span of markers.
        ("a ***", "<p>a ***</p>"),
        ("a **", "<p>a **</p>"),
        ("Rating: *****", "".join(reply_blocks("Rating: *****"))),
        ("**bold** and ~~", "<p><strong>bold</strong> and ~~</p>"),
        # A span of markers alone that the line has already doesn't stop a closer.
        ("Press `*` to **multiply", "<p>Press <code>*</code> to <strong>multiply</strong></p>"),
        ("Use `_` and *italic words", "<p>Use <code>_</code> and <em>italic words</em></p>"),
        ("a ***** b **c", "<p>a <strong>*</strong> b <strong>c</strong></p>"),
        # A line that ends in a closed span still gets a closer for what's open before it.
        ("~~Tonight we have *pasta*", "<p><del>Tonight we have <em>pasta</em></del></p>"),
        ("*Tonight we have **pasta**", "<p><em>Tonight we have <strong>pasta</strong></em></p>"),
        ("**Tonight we have *pasta*", "".join(reply_blocks("**Tonight we have *pasta***"))),
        ("**a \\*", "<p><strong>a &#42;</strong></p>"),  # a backslashed marker is text
        # Bold never spans code, finished or not.
        ("**Note: use `uv`", "".join(reply_blocks("**Note: use `uv`"))),
        ("**bold** and **", "<p><strong>bold</strong> and **</p>"),
        # A code block shows as code, open or not, and gets nothing appended.
        ("```\nx = `a", "<pre><code>x = `a</code></pre>"),
    ],
)
def test_an_unfinished_replys_last_line_closes_what_it_opened(text: str, shown: str) -> None:
    assert "".join(reply_blocks(text, unfinished=True)) == shown


def test_only_the_last_line_is_closed() -> None:
    text = "an **open line\n\nand **the last"
    assert "".join(reply_blocks(text, unfinished=True)) == "<p>an **open line</p><p>and <strong>the last</strong></p>"
    assert "".join(reply_blocks(text)) == html(text)


def test_a_finished_replys_blocks_are_its_markdown() -> None:
    text = "One.\n\n- a\n- b\n\n---\n\n```\ncode\n```\n\n# Head"
    blocks = reply_blocks(text)
    assert "".join(blocks) == html(text)
    assert [block[:4] for block in blocks] == ["<p>O", "<ul>", "<hr>", "<pre", "<p><"]


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        # A list item's marker waits for its first word, so it never shows as a paragraph.
        ("- **Tacos** on Monday\n-", "<ul><li><strong>Tacos</strong> on Monday</li></ul>"),
        (
            "- **Tacos** on Monday\n- **Pasta",
            "<ul><li><strong>Tacos</strong> on Monday</li><li><strong>Pasta</strong></li></ul>",
        ),
        ("1. Tacos\n2.", "<ol><li>Tacos</li></ol>"),
        ("1. Tacos\n2)", "<ol><li>Tacos</li></ol>"),
        ("Plan:\n\n3.", "<p>Plan:</p>"),
        ("Plan:\n•", "<p>Plan:</p>"),
        ("- a\n  -", "<ul><li>a</li></ul>"),
        # A heading's, a rule's, or a code block's start waits too.
        ("Plan:\n\n##", "<p>Plan:</p>"),
        ("Plan:\n--", "<p>Plan:</p>"),
        ("Plan:\n**", "<p>Plan:</p>"),
        ("Plan:\n```py", "<p>Plan:</p>"),
        ("Plan:\n~~~", "<p>Plan:</p>"),
        ("-", ""),
        # Once it's more than a marker, it shows as the block it is.
        ("Plan:\n---", "<p>Plan:</p><hr>"),
        ("Plan:\n```py\nx", "<p>Plan:</p><pre><code>x</code></pre>"),
        ("Plan:\n## Monday", "<p>Plan:</p><p><strong>Monday</strong></p>"),
        # A block quote's marker is text, as in the finished reply.
        ("Plan:\n>", "<p>Plan:<br>&gt;</p>"),
    ],
)
def test_an_unfinished_replys_last_line_that_is_only_a_marker_waits(text: str, shown: str) -> None:
    assert "".join(reply_blocks(text, unfinished=True)) == shown


def test_a_marker_line_waits_only_while_its_last() -> None:
    text = "- a\n-\n- b"
    assert reply_blocks(text, unfinished=True) == reply_blocks(text)


def test_closing_a_line_styles_it_only_as_often_as_it_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each closer tried styles the whole line again, so only closers whose characters the line
    has are tried, and the first that leaves no marker showing ends the search."""
    calls = []
    styled = markdown_module.inline

    def counted(text: str) -> str:
        calls.append(text)
        return styled(text)

    monkeypatch.setattr(markdown_module, "inline", counted)
    assert markdown_module._closed("plain words") == "plain words"
    assert len(calls) == 1
    calls.clear()
    assert markdown_module._closed("Tonight: **Siam") == "Tonight: **Siam**"
    assert len(calls) == 2  # the line, then ** leaves none
    calls.clear()
    markdown_module._closed("*a " * 600 + "*a")  # nothing closes it all: only the * closers
    assert len(calls) == 4
