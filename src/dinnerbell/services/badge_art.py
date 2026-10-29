# SPDX-License-Identifier: AGPL-3.0-or-later
"""Badge art (M4, #15): a tier frame plus a badge's icon, drawn as SVG.

The drawing is the approved design (the "Badge component" board): a 120x140 viewBox, one of
three shapes by tier (a rosette for First, a medal for Bronze, Silver and Gold, a faceted gem for
Diamond), a 24-px stroke icon scaled into its center, and a sparkle layer on Gold and Diamond.

One definition serves four uses, so the pages, the committed files, and the PNGs Slack shows never
drift apart:

- ``badge_svg`` is the inline ``<svg>`` the pages use (the ``badge`` macro in ``_badge.html``).
  Inline, because a page's CSS animates the sparkle, and CSS does not reach into an ``<img>``.
- ``standalone_svg`` is a complete, still SVG file with the stars drawn in.
- ``png_for`` renders that SVG to a PNG. It is the built-in ``ArtSource``: what
  ``services.badge_files`` uploads to Slack unless it is given another source.
- ``composite_png`` draws many badges in one grid PNG (#114), from the same standalone drawing. It is
  the built-in ``CompositeSource``: what ``services.badge_files`` uploads for App Home unless it is
  given another.
- ``icon_file`` and ``frame_file`` produce the files under ``static/badges/``. A test checks that
  the committed files match.

Nothing here takes user input. Every value is a constant from this module.
"""

from __future__ import annotations

import functools
import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import resvg_py
from markupsafe import Markup, escape

from dinnerbell.domain.badges import BADGES, BY_KEY, BadgeKey, Tier

SVG_NS = "http://www.w3.org/2000/svg"
VIEW_WIDTH, VIEW_HEIGHT = 120, 140
SPDX = "<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->"


@dataclass(frozen=True, slots=True)
class TierArt:
    shape: str
    """``ribbon``, ``medal``, or ``gem``: the tiers differ in outline, not only in color."""
    fill: str
    rim: str
    inner: str
    icon: str
    facet: str | None = None


TIER_ART = {
    Tier.FIRST: TierArt("ribbon", "#1E8C8A", "#13615F", "#4FB8B4", "#FFFFFF"),
    Tier.BRONZE: TierArt("medal", "#A86A3D", "#7A4A27", "#C98C5E", "#FFF6EE"),
    Tier.SILVER: TierArt("medal", "#C3C9D1", "#8E97A3", "#E4E8EC", "#3A414B"),
    Tier.GOLD: TierArt("medal", "#E3B23C", "#B5861F", "#F3D27E", "#5B420E"),
    Tier.DIAMOND: TierArt("gem", "#CDEFFC", "#3F94BC", "#FFFFFF", "#1C4D66", facet="#8FD3F0"),
}
STRAPS = ("#33415C", "#46587A")
ICON_TRANSFORM = {
    "ribbon": "translate(36 38) scale(2)",
    "medal": "translate(38.4 62.4) scale(1.8)",
    "gem": "translate(42 54) scale(1.5)",
}
ICON_STROKE = "1.8"

ICONS = {
    "chef": "M6 13a3.5 3.5 0 0 1 1.5-6.7A4.5 4.5 0 0 1 16 5.5a3.5 3.5 0 0 1 2 7.5V18H6z M6 15h12",
    "road_runner": "M4 15v-3l2.5-5h11l2.5 5v3z M4 12h16 M5.5 15a2 2 0 0 0 4 0 M14.5 15a2 2 0 0 0 4 0",
    "order_up": "M6 8h12l-1 12H7z M9 8V6a3 3 0 0 1 6 0v2",
    "quarterback": "M5 17h14 M7 17v-4a5 5 0 0 1 10 0v4 M12 8V5 M10 5h4 M10.5 20h3",
    "full_table": "M7 3v8 M5 3v5a2 2 0 0 0 4 0V3 M7 11v10 M16 3c-2 2-2 6 0 8v10",
    "early_bird": "M12 20a7 7 0 1 0 0-14 7 7 0 0 0 0 14z M12 10v3.5l2 2 M5 5.5l2-2 M19 5.5l-2-2",
    "bright_idea": "M9 18h6 M10 21h4 M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.5 1 2.5h6c0-1 .3-1.8 1-2.5A6 6 0 0 0 12 3z",
    "scout": "M5 4h14v16H5z M8 8h8 M8 12h8 M8 16h5",
    "explorer": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M15.5 8.5l-2 5-5 2 2-5z",
    "good_host": "M7 4v16 M7 4h3v8 M7 12h10 M17 12v8",
    "food_critic": "M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.8l6.1-.9z",
    "on_a_roll": "M12 21c-4 0-6-2.7-6-6 0-4 4-6 4-10 3 2 5 4.5 5 7 1-1 1.5-2 1.5-3 1.5 1.5 1.5 3.5 1.5 5 0 4-2.5 7-6 7z",  # noqa: E501
    "gambler": "M12 3l8 4.5v9L12 21l-8-4.5v-9z M4 7.5l8 4.5 8-4.5 M12 12v9 M7.75 7.5a.45.45 0 1 0 .9 0a.45.45 0 1 0-.9 0 M11.55 7.5a.45.45 0 1 0 .9 0a.45.45 0 1 0-.9 0 M15.35 7.5a.45.45 0 1 0 .9 0a.45.45 0 1 0-.9 0",  # noqa: E501
}
"""Each badge's icon as one stroked path on a 24x24 grid, keyed by ``BadgeKey`` value."""

STAR = "M0 -7 C1 -1 1 -1 7 0 C1 1 1 1 0 7 C-1 1 -1 1 -7 0 C-1 -1 -1 -1 0 -7Z"
GEM_OUTLINE = "M34 30 L86 30 L108 60 L60 128 L12 60 Z"
CLIPS = {
    Tier.GOLD: ("badge-medal-clip", '<circle cx="60" cy="84" r="35"/>'),
    Tier.DIAMOND: ("badge-gem-clip", f'<path d="{GEM_OUTLINE}"/>'),
}
"""Each sparkling tier's clip path: its id, and the shape the sheen is cut to."""
_instances = itertools.count(1)

Star = tuple[int, int, float, str]
"""x, y, scale, and the CSS classes that time its twinkle."""
STARS = {
    Tier.GOLD: (
        "#C9971F",
        (
            (97, 50, 1.1, "badge-tw"),
            (24, 64, 0.8, "badge-tw badge-tw2"),
            (90, 118, 0.7, "badge-tw badge-tw3"),
        ),
    ),
    Tier.DIAMOND: (
        "#3F94BC",
        (
            (102, 30, 1.2, "badge-tw badge-fast"),
            (18, 40, 0.9, "badge-tw badge-fast badge-tw2"),
            (100, 96, 0.8, "badge-tw badge-fast badge-tw3"),
            (60, 14, 0.7, "badge-tw badge-fast badge-tw4"),
        ),
    ),
}
SHEEN = {
    # rotation, y, width, height, opacity
    Tier.GOLD: ("rotate(20 60 84)", 10, 14, 150, "0.55"),
    Tier.DIAMOND: ("rotate(20 60 76)", 0, 16, 160, "0.7"),
}


def _number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _rosette() -> str:
    """The First tier's 24-point rosette: 48 alternating outer and inner points."""
    points = []
    for i in range(48):
        radius = 46 if i % 2 == 0 else 41
        angle = i * math.pi / 24 - math.pi / 2
        points.append(f"{60 + radius * math.cos(angle):.1f} {62 + radius * math.sin(angle):.1f}")
    return "M" + " L".join(points) + " Z"


def _shape(art: TierArt) -> str:
    if art.shape == "ribbon":
        return (
            f'<path d="M44 88 L30 134 L42 126 L50 136 L60 96 Z" fill="{art.rim}"/>'
            f'<path d="M76 88 L90 134 L78 126 L70 136 L60 96 Z" fill="{art.rim}"/>'
            f'<path d="{_rosette()}" fill="{art.rim}"/>'
            f'<circle cx="60" cy="62" r="36" fill="{art.fill}"/>'
            f'<circle cx="60" cy="62" r="31" fill="none" stroke="{art.inner}" stroke-width="2"/>'
        )
    if art.shape == "medal":
        return (
            f'<path d="M36 4 L52 4 L66 50 L50 50 Z" fill="{STRAPS[0]}"/>'
            f'<path d="M84 4 L68 4 L54 50 L70 50 Z" fill="{STRAPS[1]}"/>'
            f'<circle cx="60" cy="84" r="42" fill="{art.rim}"/>'
            f'<circle cx="60" cy="84" r="35" fill="{art.fill}"/>'
            f'<circle cx="60" cy="84" r="30" fill="none" stroke="{art.inner}" stroke-width="2"/>'
        )
    return (
        f'<path d="{GEM_OUTLINE}" fill="{art.fill}" stroke="{art.rim}" stroke-width="3" stroke-linejoin="round"/>'
        f'<path d="M12 60 L108 60 M34 30 L46 60 L60 30 L74 60 L86 30 M46 60 L60 128 L74 60" fill="none"'
        f' stroke="{art.facet}" stroke-width="2" stroke-linejoin="round"/>'
        f'<circle cx="60" cy="72" r="24" fill="#FFFFFF" stroke="{art.rim}" stroke-width="2.5"/>'
    )


def _stars(tier: Tier, *, animated: bool) -> str:
    if tier not in STARS:
        return ""
    stroke, stars = STARS[tier]
    out = [f'<g fill="#FFFFFF" stroke="{stroke}" stroke-width="1.2" stroke-linejoin="round">']
    for x, y, scale, classes in stars:
        mark = f' class="{classes}"' if animated else ""
        out.append(f'<g transform="translate({x} {y}) scale({_number(scale)})"><path{mark} d="{STAR}"/></g>')
    out.append("</g>")
    return "".join(out)


def _frame_parts(tier: Tier, *, animated: bool, uid: str = "") -> tuple[str, str, str]:
    """(defs, shape plus sheen, stars): the icon goes between the last two. ``uid`` is added to the
    clip path's id, so a page with many inline badges never repeats an id."""
    art = TIER_ART[tier]
    clip_id, clip_shape = CLIPS.get(tier, ("", ""))
    clip_id += uid
    defs = f'<defs><clipPath id="{clip_id}">{clip_shape}</clipPath></defs>' if clip_shape else ""
    shape = _shape(art)
    if animated and tier in SHEEN:
        rotation, y, width, height, opacity = SHEEN[tier]
        shape += (
            f'<g clip-path="url(#{clip_id})"><g transform="{rotation}">'
            f'<rect class="badge-sheen" x="20" y="{y}" width="{width}" height="{height}"'
            f' fill="#FFFFFF" opacity="{opacity}"/></g></g>'
        )
    return defs, shape, _stars(tier, animated=animated)


def _icon(key: str, tier: Tier) -> str:
    art = TIER_ART[tier]
    return (
        f'<g transform="{ICON_TRANSFORM[art.shape]}"><path d="{ICONS[key]}" fill="none" stroke="{art.icon}"'
        f' stroke-width="{ICON_STROKE}" stroke-linecap="round" stroke-linejoin="round"/></g>'
    )


def badge_name(key: str) -> str:
    return BY_KEY[key].name  # type: ignore[index]


def badge_svg(
    key: str, tier: Tier | str, *, locked: bool = False, decorative: bool = False, extra_class: str = ""
) -> Markup:
    """The inline ``<svg>`` for ``key`` at ``tier``, with the sparkle classes ``app.css`` animates.

    ``locked`` draws the First-tier shape softly (CSS grays it) with the badge's icon, and names
    it "not earned yet". ``decorative`` hides it from assistive technology, for a place where the
    badge's name and tier are right beside it in text."""
    tier = Tier(tier)
    name = badge_name(key)
    drawn = Tier.FIRST if locked else tier
    classes = " ".join(
        ["badge-svg", f"badge-svg-{'locked' if locked else drawn.value}", *([extra_class] if extra_class else [])]
    )
    defs, shape, stars = _frame_parts(drawn, animated=not locked, uid=f"-{next(_instances)}")
    if decorative:
        label = 'aria-hidden="true" focusable="false"'
    else:
        text = f"{name}, not earned yet" if locked else f"{name}, {drawn.label}"
        label = f'role="img" aria-label="{escape(text)}"'
    body = f"{defs}{shape}{_icon(key, drawn)}{'' if locked else stars}"
    return Markup(  # noqa: S704 - built from this module's constants only
        f'<svg class="{classes}" viewBox="0 0 {VIEW_WIDTH} {VIEW_HEIGHT}" {label}>{body}</svg>'
    )


def _document(inner: str, view: str, title: str, size: str) -> str:
    return (
        f"{SPDX}\n"
        f'<svg xmlns="{SVG_NS}" viewBox="{view}" {size} role="img" aria-labelledby="title">'
        f'<title id="title">{escape(title)}</title>{inner}</svg>\n'
    )


def _standalone_body(key: str, tier: Tier, uid: str = "") -> str:
    """What a still badge draws inside its 120x140 box. ``uid`` keeps clip path ids apart when
    several badges share one SVG (the composite)."""
    defs, shape, stars = _frame_parts(tier, animated=False, uid=uid)
    return f"{defs}{shape}{_icon(key, tier)}{stars}"


def standalone_svg(key: str, tier: Tier | str) -> str:
    """A complete, still SVG of ``key`` at ``tier``: the stars are drawn in and nothing moves.
    This is what a PNG render for Slack starts from."""
    tier = Tier(tier)
    return _document(
        _standalone_body(key, tier),
        f"0 0 {VIEW_WIDTH} {VIEW_HEIGHT}",
        f"{badge_name(key)}, {tier.label}",
        f'width="{VIEW_WIDTH}" height="{VIEW_HEIGHT}"',
    )


def frame_file(tier: Tier | str) -> str:
    """``static/badges/frames/<tier>.svg``: the tier's shape and stars, without an icon."""
    tier = Tier(tier)
    defs, shape, stars = _frame_parts(tier, animated=False)
    return _document(
        f"{defs}{shape}{stars}",
        f"0 0 {VIEW_WIDTH} {VIEW_HEIGHT}",
        tier.label,
        f'width="{VIEW_WIDTH}" height="{VIEW_HEIGHT}"',
    )


def icon_file(key: str) -> str:
    """``static/badges/icons/<key>.svg``: the icon alone, in the page's text color."""
    path = (
        f'<path d="{ICONS[key]}" fill="none" stroke="currentColor" stroke-width="{ICON_STROKE}"'
        ' stroke-linecap="round" stroke-linejoin="round"/>'
    )
    return _document(path, "0 0 24 24", badge_name(key), 'width="24" height="24"')


ArtSource = Callable[[str, Tier], bytes]
"""Where a badge tier's art comes from: a badge key and a tier to PNG bytes. ``png_for`` is the
built-in one. ``services.badge_files`` takes any, so art from another source needs no change there:
a different PNG hashes differently, so it uploads again."""
PNG_SCALE = 2
"""Slack shows the art small, but on sharp screens it needs twice the drawing's 120x140 pixels."""


@functools.cache
def png_for(key: str, tier: Tier) -> bytes:
    """The built-in art, ``standalone_svg`` drawn as a transparent PNG. Cached: it takes a few
    milliseconds, and there are only 65."""
    return bytes(
        resvg_py.svg_to_bytes(
            svg_string=standalone_svg(key, tier),
            width=VIEW_WIDTH * PNG_SCALE,
            height=VIEW_HEIGHT * PNG_SCALE,
            skip_system_fonts=True,  # the art has no text; this skips scanning the system's fonts
        )
    )


Held = tuple[str, Tier]
"""A badge key and the highest tier a member holds of it."""
CompositeSource = Callable[[Sequence[Held]], bytes]
"""Where App Home's picture of a member's badges comes from: the held badges, in order, to one PNG.
``composite_png`` is the built-in one. A source that draws from other art changes the PNG, so the
hash changes and the picture uploads again."""
COMPOSITE_COLUMNS = 7
"""Badges to a row. There are 13 badges, so at most two rows of 7 and 6."""
CELL_WIDTH, CELL_HEIGHT = 64, 75
"""One badge's cell, in the composite's own units: the 120x140 drawing at about 64 px wide."""
CELL_GAP = 6


def held_list(held: Mapping[BadgeKey, Tier]) -> tuple[Held, ...]:
    """The composite's badges for ``held`` (each badge's highest tier): in ``BADGES`` order."""
    return tuple((b.key.value, held[b.key]) for b in BADGES if b.key in held)


def cell_origin(index: int) -> tuple[int, int]:
    """Where the ``index``-th badge's cell starts: left to right, ``COMPOSITE_COLUMNS`` to a row."""
    row, column = divmod(index, COMPOSITE_COLUMNS)
    return column * (CELL_WIDTH + CELL_GAP), row * (CELL_HEIGHT + CELL_GAP)


def composite_size(count: int) -> tuple[int, int]:
    """The composite's width and height for ``count`` badges: only as wide as its first row."""
    columns = min(count, COMPOSITE_COLUMNS)
    rows = -(-count // COMPOSITE_COLUMNS)
    return columns * CELL_WIDTH + (columns - 1) * CELL_GAP, rows * CELL_HEIGHT + (rows - 1) * CELL_GAP


def composite_svg(held: Sequence[Held]) -> str:
    """The badges in ``held`` as one still SVG: each is the standalone drawing, nested in its cell,
    on a transparent background."""
    width, height = composite_size(len(held))
    cells = []
    for index, (key, tier) in enumerate(held):
        x, y = cell_origin(index)
        cells.append(
            f'<svg x="{x}" y="{y}" width="{CELL_WIDTH}" height="{CELL_HEIGHT}"'
            f' viewBox="0 0 {VIEW_WIDTH} {VIEW_HEIGHT}">{_standalone_body(key, Tier(tier), f"-{index}")}</svg>'
        )
    return (
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 {width} {height}" width="{width}" height="{height}">{"".join(cells)}</svg>'
    )


def composite_png(held: Sequence[Held]) -> bytes:
    """The built-in ``CompositeSource``: ``composite_svg`` drawn as a transparent PNG."""
    return _composite_png(tuple(held))


@functools.lru_cache(maxsize=64)
def _composite_png(held: tuple[Held, ...]) -> bytes:
    width, height = composite_size(len(held))
    return bytes(
        resvg_py.svg_to_bytes(
            svg_string=composite_svg(held),
            width=width * PNG_SCALE,
            height=height * PNG_SCALE,
            skip_system_fonts=True,
        )
    )
