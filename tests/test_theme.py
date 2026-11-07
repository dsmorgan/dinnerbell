# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Hearth theme (#230): one look for a choice, one radius scale, and dark colors a page can
force (#231) as well as take from the device."""

from __future__ import annotations

import re
from importlib.resources import files

CSS = files("dinnerbell").joinpath("web", "static", "app.css").read_text()
BASE = files("dinnerbell").joinpath("web", "templates", "base.html").read_text()
FILL = "background: var(--accent); border-color: var(--accent); color: var(--accent-text);"


def rules(css: str) -> list[tuple[str, str]]:
    """Every innermost rule, as (selector, body), with comments dropped."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [(selector.strip(), body.strip()) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def tokens(body: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body))


def block(start: str) -> str:
    """The body of the first rule whose selector is exactly `start`."""
    return next(body for selector, body in rules(CSS) if selector == start)


def test_every_chosen_control_is_the_accent_fill() -> None:
    chosen = [
        '.segmented .answer-option[aria-current="true"]',
        ".chip-toggle input:checked + span",
        '.button.choice[aria-pressed="true"]',
        ".segment input:checked + span",
    ]
    for selector in chosen:
        assert FILL in block(selector), selector
    quiet = block('.button.quiet.choice[aria-pressed="true"]')
    assert "background: var(--accent);" in quiet and "color: var(--accent-text);" in quiet
    # Nothing chosen is filled with the text color any more.
    assert [s for s, body in rules(CSS) if "background: var(--text)" in body] == []


def test_the_current_page_is_a_tint_not_a_fill() -> None:
    for selector in ('.nav a[aria-current="page"]', '.tabs a[aria-current="page"]'):
        body = block(selector)
        assert "background: var(--accent-soft);" in body and "color: var(--accent-soft-text);" in body, selector


def test_preference_levels_keep_their_meaning_colors() -> None:
    assert "background: var(--danger); border-color: var(--danger); color: var(--surface);" in block(
        ".level-veto input:checked + span"
    )
    assert "background: var(--danger-soft);" in block(".level-neg input:checked + span")
    assert "background: var(--line);" in block(".level-zero input:checked + span")
    assert "background: var(--ok-soft);" in block(".level-pos input:checked + span")


def test_a_chosen_segment_comes_to_the_front_and_shows_all_four_edges() -> None:
    # Each segment overlaps the one before by a pixel; the chosen one must sit above its neighbor.
    assert "margin-left: -1px;" in block(".segment span")
    assert "position: relative; z-index: 1;" in block(".segment input:checked + span")
    assert "position: relative; z-index: 1;" in block('.segmented .answer-option[aria-current="true"]')


def test_corners_come_from_the_four_radii() -> None:
    scale = next(tokens(body) for selector, body in rules(CSS) if selector == ":root" and "--radius-card" in body)
    assert {k: scale[k] for k in ("--radius-card", "--radius-control", "--radius-input", "--radius-pill")} == {
        "--radius-card": "14px",
        "--radius-control": "12px",
        "--radius-input": "10px",
        "--radius-pill": "999px",
    }
    corners = re.findall(r"border-radius:\s*([^;]+);", re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL))
    for value in corners:
        parts = value.split()
        assert all(p == "0" or re.fullmatch(r"var\(--radius-(card|control|input|pill)\)", p) for p in parts), value


def test_dark_colors_follow_the_device_or_a_forced_theme() -> None:
    light = tokens(CSS[CSS.index(":root {") : CSS.index("}", CSS.index(":root {"))])
    media = CSS[CSS.index("@media (prefers-color-scheme: dark) {") :]
    assert media.startswith('@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {')
    device_dark = tokens(media[: media.index("}")])
    forced_dark = tokens(block(':root[data-theme="dark"]'))
    assert device_dark == forced_dark  # the same dark colors either way
    assert set(device_dark) <= set(light)  # every dark color has a light one
    assert "color-scheme: light;" in CSS[CSS.index(":root {") : CSS.index("}", CSS.index(":root {"))]
    assert "color-scheme: dark;" in media[: media.index("}")] and "color-scheme: dark;" in block(
        ':root[data-theme="dark"]'
    )
    assert CSS.count("@media (prefers-color-scheme") == 1


def test_the_browser_bar_matches_the_page_background() -> None:
    light = tokens(CSS[CSS.index(":root {") : CSS.index("}", CSS.index(":root {"))])
    dark = tokens(block(':root[data-theme="dark"]'))
    assert f'<meta name="theme-color" content="{light["--bg"]}" media="(prefers-color-scheme: light)">' in BASE
    assert f'<meta name="theme-color" content="{dark["--bg"]}" media="(prefers-color-scheme: dark)">' in BASE
