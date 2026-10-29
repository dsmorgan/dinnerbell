# SPDX-License-Identifier: AGPL-3.0-or-later
"""The composite badge picture (#114): the layout of the grid, which badges it holds, and when its
hash changes. Uploading and showing it are in tests/test_slack_badge_art.py."""

from __future__ import annotations

import hashlib
import re
import struct
from datetime import UTC, datetime

import pytest

from dinnerbell.domain.badges import BADGES, Announcement, Award, BadgeKey, Tier, highest_tiers
from dinnerbell.services import badge_art
from dinnerbell.services.badge_art import (
    CELL_GAP,
    CELL_HEIGHT,
    CELL_WIDTH,
    COMPOSITE_COLUMNS,
    PNG_SCALE,
    Held,
    cell_origin,
    composite_png,
    composite_size,
    composite_svg,
    held_list,
)

SHOWN = [b.key.value for b in BADGES]


def some(count: int, tier: Tier = Tier.FIRST) -> list[Held]:
    """``count`` badges: all thirteen badges, then repeats (the layout doesn't care)."""
    return [(SHOWN[i % len(SHOWN)], tier) for i in range(count)]


def png_size(png: bytes) -> tuple[int, int]:
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def cells(held: list[Held]) -> list[tuple[int, int]]:
    """The x and y of each nested drawing in the SVG, in order."""
    return [(int(x), int(y)) for x, y in re.findall(r'<svg x="(\d+)" y="(\d+)"', composite_svg(held))]


def test_one_badge_is_one_cell() -> None:
    assert composite_size(1) == (CELL_WIDTH, CELL_HEIGHT)
    assert cells(some(1)) == [(0, 0)]
    assert png_size(composite_png(some(1))) == (CELL_WIDTH * PNG_SCALE, CELL_HEIGHT * PNG_SCALE)


def test_seven_badges_fill_one_row_with_small_gaps() -> None:
    step = CELL_WIDTH + CELL_GAP
    assert cells(some(7)) == [(i * step, 0) for i in range(7)]
    assert composite_size(7) == (7 * CELL_WIDTH + 6 * CELL_GAP, CELL_HEIGHT)
    assert CELL_GAP < CELL_WIDTH // 4 and 60 <= CELL_WIDTH <= 70  # about 64 px, small gaps


def test_the_eighth_badge_starts_a_second_row() -> None:
    assert COMPOSITE_COLUMNS == 7
    assert cell_origin(7) == (0, CELL_HEIGHT + CELL_GAP)
    assert cells(some(8))[7] == (0, CELL_HEIGHT + CELL_GAP)
    width, height = composite_size(8)
    assert width == composite_size(7)[0] and height == 2 * CELL_HEIGHT + CELL_GAP


def test_thirteen_badges_make_two_rows_of_seven_and_six() -> None:
    placed = cells(some(13))
    assert len(placed) == 13
    rows = [[p for p in placed if p[1] == y] for y in sorted({p[1] for p in placed})]
    assert [len(r) for r in rows] == [7, 6]
    assert [x for x, _ in rows[1]] == [x for x, _ in rows[0]][:6]  # the second row lines up under the first
    width, height = composite_size(13)
    assert png_size(composite_png(some(13))) == (width * PNG_SCALE, height * PNG_SCALE)


def test_the_picture_is_a_transparent_png_with_no_text() -> None:
    png = composite_png(some(13, Tier.GOLD))
    assert png[25] == 6  # color type 6: RGBA, so the background shows through
    assert "<text" not in composite_svg(some(13)) and "<rect" not in composite_svg(some(13)).split("<svg x=")[0]


def test_every_badge_is_the_standalone_drawing_in_its_cell() -> None:
    svg = composite_svg([("chef", Tier.GOLD)])
    inner = badge_art._standalone_body("chef", Tier.GOLD, "-0")
    assert inner in svg
    assert badge_art.standalone_svg("chef", Tier.GOLD).count("<path") == inner.count("<path")  # the same art


def test_clip_paths_in_one_picture_never_share_an_id() -> None:
    svg = composite_svg([("chef", Tier.GOLD), ("scout", Tier.GOLD), ("explorer", Tier.DIAMOND)])
    ids = re.findall(r'<clipPath id="([^"]+)"', svg)
    assert len(ids) == 3 == len(set(ids))


def test_food_critic_is_in_the_picture_in_badges_order_like_the_rest() -> None:
    held = {BadgeKey.FOOD_CRITIC: Tier.DIAMOND, BadgeKey.SCOUT: Tier.FIRST, BadgeKey.CHEF: Tier.GOLD}
    assert held_list(held) == (("chef", Tier.GOLD), ("scout", Tier.FIRST), ("food_critic", Tier.DIAMOND))
    assert held_list({BadgeKey.FOOD_CRITIC: Tier.FIRST}) == (("food_critic", Tier.FIRST),)
    assert [k for k, _ in held_list(dict.fromkeys(BadgeKey, Tier.FIRST))] == SHOWN


def test_each_badge_shows_only_its_highest_tier() -> None:
    def award(n: int, key: BadgeKey, tier: Tier) -> Award:
        return Award(n, 1, 1, key, tier, datetime(2026, 10, 28, tzinfo=UTC), Announcement.PENDING)

    held = highest_tiers([award(1, BadgeKey.CHEF, Tier.BRONZE), award(2, BadgeKey.CHEF, Tier.SILVER)])
    assert held_list(held) == (("chef", Tier.SILVER),)


def hash_of(held: list[Held]) -> str:
    return hashlib.sha256(composite_png(held)).hexdigest()


def test_the_hash_is_the_same_for_the_same_badges_and_changes_with_a_new_tier_or_badge() -> None:
    held = some(3)
    assert hash_of(held) == hash_of(list(held))
    higher = [(held[0][0], Tier.BRONZE), *held[1:]]
    assert hash_of(higher) != hash_of(held)
    assert hash_of([*held, ("good_host", Tier.FIRST)]) != hash_of(held)


def test_other_art_gives_another_hash() -> None:
    def other(held: list[Held]) -> bytes:  # what a different art source would draw
        return composite_png(held) + b"redrawn"

    assert hashlib.sha256(other(some(2))).hexdigest() != hash_of(some(2))


@pytest.mark.parametrize("count", [1, 7, 8, 13])
def test_the_picture_is_drawn_once_per_set_of_badges(count: int) -> None:
    held = some(count)
    assert composite_png(held) is composite_png(list(held))  # cached
