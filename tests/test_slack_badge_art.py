# SPDX-License-Identifier: AGPL-3.0-or-later
"""Badge art in Slack (#105): the PNG for each badge tier is uploaded once, remembered by file ID and
hash, and shown in the badge announcement and on App Home. Anything that goes wrong shows text."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from dinnerbell.channels.slack.badge_text import TIER_MARKS, tag
from dinnerbell.channels.slack.blocks import MAX_MESSAGE_BLOCKS
from dinnerbell.channels.slack.messages import Directory, RenderContext, render
from dinnerbell.channels.slack.views import HomeLinks, _badges, art_wanted
from dinnerbell.config import Settings
from dinnerbell.db.badge_files import BadgeFileRepository
from dinnerbell.db.models import SlackBadgeCompositeRow
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.badges import BADGES, TIERS, BadgeKey, Tier
from dinnerbell.domain.notifications import PermanentDeliveryError
from dinnerbell.services import badge_art, badge_files
from dinnerbell.services.badge_files import (
    COMPOSITE_SETTLE,
    COOLDOWN,
    PREWARM_FAILURES,
    PREWARM_SPACING,
    RETRY_PAUSES,
    BadgeFiles,
)
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.stats import my_stats, picture_badges
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import Failure
from tests.support.slack import SLACK_IDS, Slack, connection, wire
from tests.test_badge_news import announce, award
from tests.test_contributions import dinner

pytestmark = pytest.mark.anyio

CHEF = ("chef", "gold")
SCOUT = ("scout", "first")


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


class Uploads:
    """An uploader that records its calls and numbers its files."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, str]] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def __call__(self, filename: str, png: bytes, alt: str) -> str:
        self.calls.append((filename, png, alt))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return f"F{len(self.calls)}"


def files(house: House, uploads: Uploads, *, art: Any = None, composite: Any = None, **options: Any) -> BadgeFiles:
    return BadgeFiles(
        house.sessionmaker,
        art_for=art or (lambda k, t: f"{k}-{t.value}".encode()),
        composite_for=composite or (lambda held: ("grid:" + ",".join(f"{k}-{t.value}" for k, t in held)).encode()),
        upload=uploads,
        clock=house.clock,
        **options,
    )


# The cache -----------------------------------------------------------------------------------------


async def test_a_new_tier_uploads_once_and_a_second_ask_reuses_the_file(house: House) -> None:
    uploads = Uploads()
    badges = files(house, uploads)
    assert await badges.file_ids(house.household.id, [CHEF]) == {CHEF: "F1"}
    assert await badges.file_ids(house.household.id, [CHEF]) == {CHEF: "F1"}
    assert [c[0] for c in uploads.calls] == ["chef-gold.png"]
    assert uploads.calls[0][2] == "Chef, Gold"


async def test_a_restart_reuses_the_remembered_file(house: House) -> None:
    first = Uploads()
    await files(house, first).file_ids(house.household.id, [CHEF, SCOUT])
    second = Uploads()
    again = files(house, second)  # a new object, as after a restart: nothing but the table remembers
    assert await again.file_ids(house.household.id, [CHEF, SCOUT]) == {CHEF: "F1", SCOUT: "F2"}
    assert second.calls == []


async def test_changed_art_uploads_again_and_replaces_the_row(house: House) -> None:
    uploads = Uploads()
    await files(house, uploads).file_ids(house.household.id, [CHEF])
    redrawn = files(house, uploads, art=lambda k, t: b"another drawing")
    assert await redrawn.file_ids(house.household.id, [CHEF]) == {CHEF: "F2"}
    async with house.sessionmaker() as db:
        rows = await BadgeFileRepository(db).all(house.household.id)
    assert [r.file_id for r in rows.values()] == ["F2"]


async def test_two_asks_at_once_share_one_upload(house: House) -> None:
    uploads = Uploads()
    uploads.gate = asyncio.Event()
    badges = files(house, uploads)

    async def release() -> dict[tuple[str, str], str]:
        await uploads.started.wait()
        assert uploads.gate is not None
        uploads.gate.set()
        return {}

    first, second, _ = await asyncio.gather(
        badges.file_ids(house.household.id, [CHEF]), badges.file_ids(house.household.id, [CHEF]), release()
    )
    assert first == second == {CHEF: "F1"}
    assert len(uploads.calls) == 1


async def test_food_critic_is_uploaded_like_any_badge_but_unknown_art_is_not(house: House) -> None:
    uploads = Uploads()
    asked = [(BadgeKey.FOOD_CRITIC.value, "gold"), ("nonsense", "gold"), ("chef", "platinum")]
    assert await files(house, uploads).file_ids(house.household.id, asked) == {("food_critic", "gold"): "F1"}
    assert len(uploads.calls) == 1


async def test_forgetting_a_file_uploads_it_again(house: House) -> None:
    uploads = Uploads()
    badges = files(house, uploads)
    await badges.file_ids(house.household.id, [CHEF])
    await badges.forget(house.household.id, ["F1"])
    assert await badges.file_ids(house.household.id, [CHEF]) == {CHEF: "F2"}


async def test_the_built_in_art_is_a_png_and_a_different_source_is_used_as_given() -> None:
    png = badge_art.png_for("chef", Tier.GOLD)
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and badge_art.png_for("chef", Tier.GOLD) is png
    assert png != badge_art.png_for("chef", Tier.SILVER)


# Failure ------------------------------------------------------------------------------------------


async def test_a_failed_upload_gives_nothing_logs_one_line_and_waits_before_trying_again(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    uploads = Uploads()
    uploads.error = RuntimeError("Slack files.upload failed: missing_scope")
    badges = files(house, uploads)
    with caplog.at_level(logging.INFO):
        assert await badges.file_ids(house.household.id, [CHEF, SCOUT]) == {}
        assert await badges.file_ids(house.household.id, [CHEF]) == {}
    (line,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "missing_scope" in line.error and "files:write" in line.getMessage()  # type: ignore[attr-defined]
    assert len(uploads.calls) == 1  # the second ask didn't try
    uploads.error = None
    house.clock.advance(COOLDOWN + timedelta(seconds=1))
    assert await badges.file_ids(house.household.id, [CHEF]) == {CHEF: "F2"}


async def test_a_cooldown_pauses_new_uploads_but_not_files_already_uploaded(house: House) -> None:
    uploads = Uploads()
    badges = files(house, uploads)
    await badges.file_ids(house.household.id, [CHEF])
    uploads.error = RuntimeError("x" * 500)
    assert await badges.file_ids(house.household.id, [SCOUT]) == {}  # fails, and starts the cooldown
    assert await badges.file_ids(house.household.id, [CHEF, SCOUT]) == {CHEF: "F1"}
    assert len(uploads.calls) == 2  # no third try while cooling


async def test_the_logged_error_is_cut_short(house: House, caplog: pytest.LogCaptureFixture) -> None:
    uploads = Uploads()
    uploads.error = RuntimeError("x" * 500)
    with caplog.at_level(logging.WARNING):
        await files(house, uploads).file_ids(house.household.id, [CHEF])
    (line,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(line.error) == 200  # type: ignore[attr-defined]


async def test_a_slow_upload_gives_up_instead_of_holding_the_caller(house: House) -> None:
    uploads = Uploads()
    uploads.gate = asyncio.Event()  # never set
    assert await files(house, uploads, timeout=0.05).file_ids(house.household.id, [CHEF]) == {}


async def test_what_was_uploaded_before_a_failure_is_kept(house: House) -> None:
    uploads = Uploads()
    badges = files(house, uploads)
    await badges.file_ids(house.household.id, [CHEF])
    uploads.error = RuntimeError("boom")
    assert await badges.file_ids(house.household.id, [CHEF, SCOUT]) == {CHEF: "F1"}


# The announcement ---------------------------------------------------------------------------------


async def earn(house: House) -> None:
    await award(house, "Kim", BadgeKey.CHEF, Tier.GOLD)
    await award(house, "Kim", BadgeKey.SCOUT, Tier.FIRST)
    assert await announce(house) == 1


def art_uploads(slack: Slack) -> list[Any]:
    """The uploads of single badge tiers: not the composite pictures (``badges-*.png``)."""
    return [c for c in slack.fake.of("files.getUploadURLExternal") if not c.args["filename"].startswith("badges-")]


def announcement(slack: Slack) -> list[dict[str, Any]]:
    (call,) = slack.fake.of("chat.postMessage")
    blocks: list[dict[str, Any]] = call.args["blocks"]
    return blocks


async def test_the_announcement_shows_each_badge_as_art(house: House) -> None:
    slack = await wire(house)
    await earn(house)
    await slack.drain()
    blocks = announcement(slack)
    art = [b["accessory"] for b in blocks if "accessory" in b]
    assert [a["alt_text"] for a in art] == ["Chef, Gold", "Scout, First"]
    assert all(a["type"] == "image" and set(a["slack_file"]) == {"id"} for a in art)
    assert {a["slack_file"]["id"] for a in art} <= set(slack.fake.files)
    text = json.dumps(blocks, ensure_ascii=False)
    assert f"<@{SLACK_IDS['Kim']}> earned *Chef* · Gold" in text and "and *Scout* · First." in text
    assert len(art_uploads(slack)) == 2 and all(f["filename"].endswith(".png") for f in slack.fake.files.values())


async def test_food_critic_is_never_in_an_announcement_even_beside_other_badges(house: House) -> None:
    slack = await wire(house)
    await award(house, "Kim", BadgeKey.CHEF, Tier.GOLD)
    await award(house, "Kim", BadgeKey.FOOD_CRITIC, Tier.FIRST)
    await award(house, "Sam", BadgeKey.FOOD_CRITIC, Tier.FIRST)
    assert await announce(house) == 1
    await slack.drain()
    text = json.dumps(announcement(slack), ensure_ascii=False)
    assert "Food Critic" not in text and f"<@{SLACK_IDS['Sam']}>" not in text


async def test_a_second_announcement_reuses_the_uploads(house: House) -> None:
    slack = await wire(house)
    await earn(house)
    await slack.drain()
    slack.fake.clear()
    await award(house, "Alex", BadgeKey.CHEF, Tier.GOLD)
    await announce(house)
    await slack.drain()
    assert art_uploads(slack) == []
    assert any("accessory" in b for b in announcement(slack))


async def test_a_missing_scope_posts_the_text_badges_and_does_not_fail_the_outbox(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    slack = await wire(house)
    slack.fake.fail("files.getUploadURLExternal", Failure("missing_scope"))
    await earn(house)
    with caplog.at_level(logging.INFO):
        result = await slack.drain()
    assert (result.delivered, result.failed, result.retrying) == (1, 0, 0)
    blocks = announcement(slack)
    assert not any("accessory" in b for b in blocks)
    text = json.dumps(blocks, ensure_ascii=False)
    assert tag("*Chef*", "Gold") in text and tag("*Scout*", "First") in text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "badge art" in r.getMessage()]
    assert len(warnings) == 1 and "missing_scope" in warnings[0].error  # type: ignore[attr-defined]


async def test_slack_refusing_a_file_posts_text_and_forgets_the_file(house: House) -> None:
    slack = await wire(house)
    slack.fake.fail("chat.postMessage", Failure("invalid_blocks"), Failure("invalid_blocks"), Failure("invalid_blocks"))
    await earn(house)
    await slack.drain()
    *tried, text = slack.fake.of("chat.postMessage")
    assert len(tried) == 3 and all(any("accessory" in b for b in c.args["blocks"]) for c in tried)
    assert not any("accessory" in b for b in text.args["blocks"])
    async with house.sessionmaker() as db:
        assert await BadgeFileRepository(db).all(house.household.id) == {}


async def test_a_refusal_that_is_not_about_blocks_keeps_the_cached_files(house: House) -> None:
    slack = await wire(house)
    slack.fake.fail("chat.postMessage", Failure("channel_not_found"))
    await earn(house)
    result = await slack.drain()
    assert result.failed == 1 and len(slack.fake.of("chat.postMessage")) == 1  # no text retry
    async with house.sessionmaker() as db:
        assert len(await BadgeFileRepository(db).all(house.household.id)) == 2


async def test_an_announcement_with_only_unknown_badges_has_no_art(house: House) -> None:
    slack = await wire(house)
    post = note.BadgesEarned(
        house.household.id,
        1,
        (note.BadgeNews(note.PersonInfo(house["Kim"].id, "Kim"), (note.BadgeTier("Nonsense", "Gold"),)),),
    )
    await slack.conn.channel.deliver(post)
    assert slack.fake.of("files.getUploadURLExternal") == []
    assert not any("accessory" in b for b in announcement(slack))


# App Home -----------------------------------------------------------------------------------------


def home_icons(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The small icons of Your badges: the image elements in context blocks."""
    return [e for row in icons(blocks) for e in row]


async def test_home_shows_the_members_own_badges_as_art(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    await slack.open_home("Alex")
    blocks = slack.last_home("Alex")["blocks"]
    art = home_icons(blocks)
    assert art and all(e["slack_file"]["id"] in slack.fake.files for e in art)
    text = json.dumps(blocks)
    assert "Your badges" in text and "Next:" in text and "Quarterback" not in text


async def test_home_shows_text_tags_when_uploads_fail(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    slack.fake.fail("files.getUploadURLExternal", Failure("missing_scope"))
    await slack.open_home("Alex")
    text = json.dumps(slack.last_home("Alex"), ensure_ascii=False)
    assert "slack_file" not in text and tag("Full Table", "First") in text


async def test_home_shows_food_critic_as_art_like_the_rest(house: House) -> None:
    await award(house, "Alex", BadgeKey.CHEF, Tier.GOLD)
    await award(house, "Alex", BadgeKey.FOOD_CRITIC, Tier.FIRST)
    slack = await wire(house)
    await slack.open_home("Alex")
    blocks = slack.last_home("Alex")["blocks"]
    assert [e["alt_text"] for e in home_icons(blocks)] == ["Chef, Gold", "Food Critic, First"]
    assert "Also:" not in json.dumps(blocks, ensure_ascii=False)


async def test_slack_refusing_a_file_on_home_publishes_text_and_forgets_the_file(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    slack.fake.fail("views.publish", Failure("invalid_blocks"), Failure("invalid_blocks"), Failure("invalid_blocks"))
    await slack.open_home("Alex")
    *tried, last = slack.fake.of("views.publish")
    assert len(tried) == 3 and all("slack_file" in json.dumps(c.args["view"]) for c in tried)
    text = json.dumps(last.args["view"], ensure_ascii=False)
    assert "slack_file" not in text and tag("Full Table", "First") in text
    async with house.sessionmaker() as db:
        assert await BadgeFileRepository(db).all(house.household.id) == {}
    await slack.open_home("Alex")  # cooling down: text, no upload
    assert "slack_file" not in json.dumps(slack.last_home("Alex"))
    house.clock.advance(COOLDOWN + timedelta(seconds=1))
    await slack.open_home("Alex")  # the next view uploads again
    assert "slack_file" in json.dumps(slack.last_home("Alex"))


async def test_a_home_refusal_that_is_not_about_blocks_is_not_retried_and_keeps_the_files(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    slack.fake.fail("views.publish", Failure("user_not_found"))
    await slack.open_home("Alex")
    assert len(slack.fake.of("views.publish")) == 1
    async with house.sessionmaker() as db:
        assert await BadgeFileRepository(db).all(house.household.id)


# Why Slack refused (#110) --------------------------------------------------------------------------


def refusals(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and "refused badge art" in r.getMessage()]


class Pauses:
    """A pause that records how long it was asked to wait and returns at once."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        await asyncio.sleep(0)


async def test_a_refusal_logs_slacks_error_code_and_explanation(house: House, caplog: pytest.LogCaptureFixture) -> None:
    slack = await wire(house)
    why = Failure("invalid_blocks", messages=["[ERROR] invalid file [json-pointer:/blocks/1/accessory]", "x" * 500])
    slack.fake.fail("chat.postMessage", why, why, why)
    await earn(house)
    with caplog.at_level(logging.INFO):
        await slack.drain()
    first, again, last = refusals(caplog)
    assert first.slack_error == "invalid_blocks"  # type: ignore[attr-defined]
    assert first.slack_messages.startswith("[ERROR] invalid file")  # type: ignore[attr-defined]
    assert len(first.slack_messages) == 300  # type: ignore[attr-defined]
    assert "invalid_blocks" in first.getMessage() and "retrying" in first.getMessage()
    assert again.slack_error == last.slack_error == "invalid_blocks"  # type: ignore[attr-defined]
    assert "again" in again.getMessage() and "again" in last.getMessage()


async def test_a_home_refusal_logs_slacks_error_code_and_explanation(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    await dinner(house)
    slack = await wire(house)
    why = Failure("invalid_blocks", messages=["[ERROR] invalid file"])
    slack.fake.fail("views.publish", why, why, why)
    with caplog.at_level(logging.INFO):
        await slack.open_home("Alex")
    first, again, last = refusals(caplog)
    assert first.slack_error == "invalid_blocks" and first.slack_messages == "[ERROR] invalid file"  # type: ignore[attr-defined]
    assert "again" in again.getMessage() and "again" in last.getMessage()


async def test_a_refusal_without_an_explanation_logs_the_code_alone(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    slack = await wire(house)
    slack.fake.fail("chat.postMessage", Failure("invalid_arguments"))
    await earn(house)
    with caplog.at_level(logging.INFO):
        await slack.drain()
    (first,) = refusals(caplog)
    assert first.slack_error == "invalid_arguments" and first.slack_messages is None  # type: ignore[attr-defined]


async def test_a_refused_post_with_a_new_file_is_retried_after_a_pause(house: House) -> None:
    slack = await wire(house)
    pauses = Pauses()
    slack.conn.badge_files.pause = pauses
    slack.fake.fail("chat.postMessage", Failure("invalid_blocks"))
    await earn(house)
    result = await slack.drain()
    assert (result.delivered, result.failed) == (1, 0)
    first, second = slack.fake.of("chat.postMessage")
    assert first.args["blocks"] == second.args["blocks"] and any("accessory" in b for b in second.args["blocks"])
    assert pauses.waits == [RETRY_PAUSES[0]]
    async with house.sessionmaker() as db:
        assert len(await BadgeFileRepository(db).all(house.household.id)) == 2  # the cache stays


async def test_a_second_retry_follows_a_refused_first_one_after_a_longer_pause(house: House) -> None:
    slack = await wire(house)
    pauses = Pauses()
    slack.conn.badge_files.pause = pauses
    slack.fake.fail("chat.postMessage", Failure("invalid_blocks"), Failure("invalid_blocks_format"))
    await earn(house)
    result = await slack.drain()
    assert (result.delivered, result.failed) == (1, 0)
    *_, last = slack.fake.of("chat.postMessage")
    assert len(slack.fake.of("chat.postMessage")) == 3 and any("accessory" in b for b in last.args["blocks"])
    assert pauses.waits == list(RETRY_PAUSES)
    async with house.sessionmaker() as db:
        assert len(await BadgeFileRepository(db).all(house.household.id)) == 2  # the cache stays


async def test_three_refusals_forget_the_files_and_post_text(house: House) -> None:
    slack = await wire(house)
    pauses = Pauses()
    slack.conn.badge_files.pause = pauses
    slack.fake.fail("chat.postMessage", *[Failure("invalid_blocks") for _ in range(3)])
    await earn(house)
    result = await slack.drain()
    assert (result.delivered, result.failed) == (1, 0)
    *_, last = slack.fake.of("chat.postMessage")
    assert len(slack.fake.of("chat.postMessage")) == 4 and not any("accessory" in b for b in last.args["blocks"])
    assert pauses.waits == list(RETRY_PAUSES)
    async with house.sessionmaker() as db:
        assert await BadgeFileRepository(db).all(house.household.id) == {}
    # The cooldown: the next badge post shows text without uploading or pausing.
    slack.fake.clear()
    await award(house, "Alex", BadgeKey.CHEF, Tier.GOLD)
    await announce(house)
    await slack.drain()
    assert slack.fake.of("files.getUploadURLExternal") == [] and len(slack.fake.of("chat.postMessage")) == 1
    assert pauses.waits == list(RETRY_PAUSES)


async def test_a_retry_that_fails_for_another_reason_is_not_hidden(house: House) -> None:
    slack = await wire(house)
    slack.fake.fail("chat.postMessage", Failure("invalid_blocks"), Failure("channel_not_found"))
    await earn(house)
    result = await slack.drain()
    assert result.failed == 1 and len(slack.fake.of("chat.postMessage")) == 2
    async with house.sessionmaker() as db:
        assert len(await BadgeFileRepository(db).all(house.household.id)) == 2


async def test_a_refusal_with_only_cached_files_is_not_retried_and_forgets_them(house: House) -> None:
    slack = await wire(house)
    await earn(house)
    await slack.drain()
    slack.fake.clear()
    house.clock.advance(timedelta(seconds=11))  # past the time a file counts as new
    pauses = Pauses()
    slack.conn.badge_files.pause = pauses
    slack.fake.fail("chat.postMessage", Failure("invalid_blocks"))
    await award(house, "Alex", BadgeKey.CHEF, Tier.GOLD)
    await announce(house)
    await slack.drain()
    first, second = slack.fake.of("chat.postMessage")
    assert any("accessory" in b for b in first.args["blocks"]) and not any(
        "accessory" in b for b in second.args["blocks"]
    )
    assert pauses.waits == []
    async with house.sessionmaker() as db:
        assert list(await BadgeFileRepository(db).all(house.household.id)) == [SCOUT]  # only the one shown is forgotten


async def test_a_home_refusal_with_a_new_file_is_retried_up_to_twice(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    pauses = Pauses()
    slack.conn.badge_files.pause = pauses
    slack.fake.fail("views.publish", Failure("invalid_blocks"), Failure("invalid_blocks"))
    await slack.open_home("Alex")
    first, _, third = slack.fake.of("views.publish")
    assert first.args["view"] == third.args["view"] and "slack_file" in json.dumps(third.args["view"])
    assert pauses.waits == list(RETRY_PAUSES)
    async with house.sessionmaker() as db:
        assert await BadgeFileRepository(db).all(house.household.id)


async def test_a_home_retry_that_fails_for_another_reason_is_not_hidden(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    slack.fake.fail("views.publish", Failure("invalid_blocks"), Failure("user_not_found"))
    await slack.open_home("Alex")
    assert len(slack.fake.of("views.publish")) == 2


async def test_a_file_a_concurrent_caller_just_uploaded_is_retried_for_both(house: House) -> None:
    slack = await wire(house)
    slack.fake.fail("chat.postMessage", Failure("invalid_blocks"), Failure("invalid_blocks"))
    kim = note.PersonInfo(house["Kim"].id, "Kim")
    posts = [
        note.BadgesEarned(house.household.id, n, (note.BadgeNews(kim, (note.BadgeTier("Chef", "Gold"),)),))
        for n in (1, 2)
    ]
    await asyncio.gather(*(slack.conn.channel.deliver(p) for p in posts))
    assert len(slack.fake.of("files.getUploadURLExternal")) == 1  # one upload, shared
    calls = slack.fake.of("chat.postMessage")
    assert len(calls) == 4 and all(any("accessory" in b for b in c.args["blocks"]) for c in calls)
    async with house.sessionmaker() as db:
        assert len(await BadgeFileRepository(db).all(house.household.id)) == 1


async def test_a_file_stays_fresh_for_ten_seconds_then_counts_as_cached(house: House) -> None:
    badges = files(house, Uploads())
    await badges.file_ids(house.household.id, [CHEF])
    house.clock.advance(timedelta(seconds=9))
    again: set[str] = set()
    assert await badges.file_ids(house.household.id, [CHEF], again) == {CHEF: "F1"} and again == {"F1"}
    house.clock.advance(timedelta(seconds=2))  # 11 s since the upload
    later: set[str] = set()
    assert await badges.file_ids(house.household.id, [CHEF], later) == {CHEF: "F1"} and later == set()


# Prewarm (#112) -----------------------------------------------------------------------------------

EVERY_TIER = 13 * 5


async def test_prewarm_uploads_every_tier_once_with_a_gap_between(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    uploads, pauses = Uploads(), Pauses()
    badges = files(house, uploads, pause=pauses)
    with caplog.at_level(logging.INFO):
        assert await badges.prewarm(house.household.id) is True
    names = [name for name, _, _ in uploads.calls]
    assert len(names) == len(set(names)) == EVERY_TIER
    assert sum(n.startswith("food_critic") for n in names) == 5
    assert pauses.waits == [PREWARM_SPACING] * EVERY_TIER
    done = [r for r in caplog.records if r.getMessage() == "badge art prewarmed"]
    assert len(done) == 1 and done[0].levelno == logging.INFO
    assert (done[0].uploaded, done[0].cached) == (EVERY_TIER, 0)  # type: ignore[attr-defined]


async def test_prewarm_skips_what_is_cached_and_a_first_use_afterwards_uploads_nothing(house: House) -> None:
    uploads = Uploads()
    await files(house, uploads, pause=Pauses()).prewarm(house.household.id)
    assert len(uploads.calls) == EVERY_TIER
    restarted = files(house, uploads, pause=Pauses())  # a new process, same database
    assert await restarted.prewarm(house.household.id) is True
    assert len(uploads.calls) == EVERY_TIER
    fresh: set[str] = set()
    found = await restarted.file_ids(house.household.id, [CHEF], fresh)
    assert found == {CHEF: f"F{[n for n, _, _ in uploads.calls].index('chef-gold.png') + 1}"}
    assert fresh == set() and len(uploads.calls) == EVERY_TIER


async def test_prewarm_resumes_where_a_stopped_run_left_off(house: House) -> None:
    uploads = Uploads()
    stop, ten = asyncio.Event(), asyncio.Event()

    async def stops_after_ten(seconds: float) -> None:
        if len(uploads.calls) >= 10:
            ten.set()
            await stop.wait()  # the process is going away
        await asyncio.sleep(0)

    task = asyncio.ensure_future(files(house, uploads, pause=stops_after_ten).prewarm(house.household.id))
    await asyncio.wait_for(ten.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(uploads.calls) == 10
    assert await files(house, uploads, pause=Pauses()).prewarm(house.household.id) is True
    names = [name for name, _, _ in uploads.calls]
    assert len(names) == len(set(names)) == EVERY_TIER


async def test_prewarm_stops_on_a_missing_scope_with_one_log_line(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    uploads = Uploads()
    uploads.error = PermanentDeliveryError("missing_scope", code="missing_scope")
    with caplog.at_level(logging.INFO):
        assert await files(house, uploads, pause=Pauses()).prewarm(house.household.id) is False
    assert len(uploads.calls) == 1
    lines = [r for r in caplog.records if r.levelno >= logging.WARNING or "prewarm" in r.getMessage()]
    assert len(lines) == 1 and "files:write" in lines[0].getMessage()


async def test_prewarm_waits_out_the_cooldown_after_a_failure_and_gives_up_after_a_few(house: House) -> None:
    uploads, pauses = Uploads(), Pauses()
    uploads.error = RuntimeError("boom")
    assert await files(house, uploads, pause=pauses).prewarm(house.household.id) is False
    assert len(uploads.calls) == PREWARM_FAILURES
    cooling = [w for w in pauses.waits if w > 1]
    assert len(cooling) == PREWARM_FAILURES - 1 and all(w == COOLDOWN.total_seconds() for w in cooling)


async def test_prewarm_does_not_clear_the_failure_cooldown_for_first_use(house: House) -> None:
    uploads = Uploads()
    uploads.error = RuntimeError("boom")
    badges = files(house, uploads, pause=Pauses())
    await badges.prewarm(house.household.id)
    uploads.error = None
    calls = len(uploads.calls)
    assert await badges.file_ids(house.household.id, [CHEF]) == {}  # still cooling
    assert len(uploads.calls) == calls


async def test_the_connection_prewarms_once_connected_and_not_before(house: House) -> None:
    from tests.support.fake_slack import FakeSlack

    fake = FakeSlack()
    conn = connection(house, fake)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if not await conn.is_connected() and len(sleeps) > 3:
            raise asyncio.CancelledError  # never connected: the test ends the wait
        await asyncio.sleep(0)

    conn.sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        await conn._prewarm(house.household.id)
    assert fake.files == {}  # no connection, nothing uploaded
    assert await conn.run_once()
    await conn._prewarm(house.household.id)
    assert len(fake.files) == EVERY_TIER


# Compact layouts (#112) ---------------------------------------------------------------------------

FOUR = [
    (BadgeKey.CHEF, Tier.GOLD),
    (BadgeKey.SCOUT, Tier.FIRST),
    (BadgeKey.ROAD_RUNNER, Tier.FIRST),
    (BadgeKey.QUARTERBACK, Tier.FIRST),
]


async def earn_for_kim(house: House, badges: list[tuple[BadgeKey, Tier]]) -> None:
    for key, tier in badges:
        await award(house, "Kim", key, tier)
    assert await announce(house) == 1


def icons(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [
        b["elements"] for b in blocks if b["type"] == "context" and all(e["type"] == "image" for e in b["elements"])
    ]


async def test_three_badges_keep_their_thumbnail_rows(house: House) -> None:
    slack = await wire(house)
    await earn_for_kim(house, FOUR[:3])
    await slack.drain()
    blocks = announcement(slack)
    assert len([b for b in blocks if "accessory" in b]) == 3 and icons(blocks) == []


async def test_four_badges_become_one_line_and_one_row_of_small_icons(house: House) -> None:
    slack = await wire(house)
    await earn_for_kim(house, FOUR)
    await slack.drain()
    blocks = announcement(slack)
    assert not any("accessory" in b for b in blocks)
    (row,) = icons(blocks)
    assert [e["alt_text"] for e in row] == ["Chef, Gold", "Road Runner, First", "Quarterback, First", "Scout, First"]
    line = next(b["text"]["text"] for b in blocks if "earned 4 badges:" in b.get("text", {}).get("text", ""))
    assert line.startswith(f"<@{SLACK_IDS['Kim']}> earned 4 badges: ")
    assert line.endswith("earned 4 badges: *Chef*, *Road Runner*, *Quarterback*, and *Scout*.")
    assert "🥇" not in line and "·" not in line  # the icons below show the tiers


async def test_the_badges_over_the_cap_are_counted_in_the_line_and_all_stay_in_blocks(house: House) -> None:
    slack = await wire(house)
    seven = [(k, Tier.FIRST) for k in list(BadgeKey)[:6]] + [(BadgeKey.GAMBLER, Tier.FIRST)]
    await earn_for_kim(house, seven)
    await slack.drain()
    blocks = announcement(slack)
    text = json.dumps(blocks, ensure_ascii=False)
    assert "earned 7 badges: " in text and "more badge" not in text  # every badge is named, and drawn
    assert [len(r) for r in icons(blocks)] == [7] and len(blocks) <= 50


async def test_four_badges_without_art_post_the_same_text_as_before(house: House) -> None:
    slack = await wire(house)
    slack.fake.fail("files.getUploadURLExternal", Failure("missing_scope"))
    await earn_for_kim(house, FOUR)
    await slack.drain()
    (section,) = [b for b in announcement(slack) if "earned" in json.dumps(b)]
    assert section["text"]["text"].splitlines()[1] == (
        f"<@{SLACK_IDS['Kim']}> earned {tag('*Chef*', 'Gold')}, {tag('*Road Runner*', 'First')}, "
        f"{tag('*Quarterback*', 'First')}, and {tag('*Scout*', 'First')}."
    )


async def test_a_member_with_many_badges_and_a_member_with_few_each_get_their_own_layout(house: House) -> None:
    slack = await wire(house)
    for key, tier in FOUR:
        await award(house, "Kim", key, tier)
    await award(house, "Sam", BadgeKey.EXPLORER, Tier.FIRST)
    assert await announce(house) == 1
    await slack.drain()
    blocks = announcement(slack)
    assert len(icons(blocks)) == 1 and [b["accessory"]["alt_text"] for b in blocks if "accessory" in b] == [
        "Explorer, First"
    ]


async def earn_everything(house: House) -> None:
    for key in BadgeKey:
        await award(house, "Alex", key, Tier.SILVER if key is BadgeKey.CHEF else Tier.FIRST)


async def test_home_fits_thirteen_badges_in_two_icon_blocks_with_a_link_and_the_next_icon(house: House) -> None:
    await earn_everything(house)
    slack = await wire(house)
    await slack.open_home("Alex")
    blocks = slack.last_home("Alex")["blocks"]
    rows = icons(blocks)
    assert [len(r) for r in rows[:2]] == [10, 3]  # all 13 have art
    assert len(blocks) <= 100
    text = json.dumps(blocks, ensure_ascii=False)
    assert "<https://dinnerbell.test/stats|All badges on the web>" in text
    assert "Also:" not in text
    nxt = next(b for b in blocks if b["type"] == "context" and "Next:" in json.dumps(b))
    assert nxt["elements"][0]["type"] == "image" and nxt["elements"][1]["type"] == "mrkdwn"
    assert all("accessory" not in b for b in blocks)


async def test_home_leaves_out_the_link_without_a_web_address(house: House) -> None:
    await earn_everything(house)
    async with house.sessionmaker() as db:
        mine = await my_stats(db, house["Alex"])
    art = dict.fromkeys(art_wanted(mine), "F1")
    assert any("All badges on the web" in json.dumps(b) for b in _badges(mine, art, HomeLinks("https://x.example")))
    assert not any("All badges on the web" in json.dumps(b) for b in _badges(mine, art, HomeLinks("")))


async def test_home_without_art_keeps_the_text_layout_byte_for_byte(house: House) -> None:
    await earn_everything(house)
    slack = await wire(house)
    slack.fake.fail("files.getUploadURLExternal", Failure("missing_scope"))
    await slack.open_home("Alex")
    blocks = slack.last_home("Alex")["blocks"]
    start = next(i for i, b in enumerate(blocks) if "*Your badges*" in json.dumps(b))
    assert blocks[start]["type"] == "section" and blocks[start]["text"]["text"].startswith("*Your badges*\n")
    assert blocks[start + 1]["type"] == "context" and len(blocks[start + 1]["elements"]) == 1
    assert "slack_file" not in json.dumps(blocks) and "All badges on the web" not in json.dumps(blocks)


# Shutdown (#112) ----------------------------------------------------------------------------------


async def test_a_failed_connection_look_does_not_end_the_prewarm(house: House) -> None:
    from tests.support.fake_slack import FakeSlack

    conn = connection(house, FakeSlack())
    looks = 0

    async def flaky() -> bool:
        nonlocal looks
        looks += 1
        if looks == 1:
            raise RuntimeError("socket gone")
        return True

    conn.is_connected = flaky  # type: ignore[method-assign]
    conn.sleep = no_wait
    await conn._prewarm(house.household.id)
    assert looks == 2 and len(conn.api.client.files) == EVERY_TIER  # type: ignore[attr-defined]


async def no_wait(seconds: float) -> None:
    await asyncio.sleep(0)


async def test_prewarm_failures_count_in_a_row_not_in_all(house: House) -> None:
    uploads = Uploads()
    calls = 0
    original = uploads.__call__

    async def every_other(filename: str, png: bytes, alt: str) -> str:
        nonlocal calls
        calls += 1
        if calls % 2:
            raise RuntimeError("blip")
        return await original(filename, png, alt)

    badges = files(house, uploads, pause=Pauses())
    badges.upload = every_other
    assert await badges.prewarm(house.household.id) is True


async def test_stopping_during_an_upload_still_saves_its_row(house: House) -> None:
    uploads = Uploads()
    badges = files(house, uploads, pause=Pauses())
    save = badges._save
    release, saving = asyncio.Event(), asyncio.Event()

    async def slow_save(household_id: int, file: Any) -> None:
        saving.set()
        await release.wait()
        await save(household_id, file)

    badges._save = slow_save  # type: ignore[method-assign]
    task = asyncio.ensure_future(badges.prewarm(house.household.id))
    await asyncio.wait_for(saving.wait(), 5)  # the first upload is in flight, its save waiting
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await badges.settle(5)
    async with house.sessionmaker() as db:
        assert len(await BadgeFileRepository(db).all(house.household.id)) == 1
    assert not badges._saves


# The composite picture on App Home (#114) ------------------------------------------------------------

BOTH = (("chef", Tier.GOLD), ("scout", Tier.FIRST))


async def stored(house: House, member: str = "Kim") -> Any:
    async with house.sessionmaker() as db:
        return await BadgeFileRepository(db).composite(house.household.id, house[member].id)


async def make_picture(badges: BadgeFiles, house: House, held: Any = BOTH, member: str = "Kim") -> str | None:
    """Ask for the member's picture and let the background draw finish."""
    found = await badges.composite(house.household.id, house[member].id, held)
    await badges.idle()
    return found


async def test_a_missing_picture_is_made_in_the_background_and_asked_for_again_later(house: House) -> None:
    uploads = Uploads()
    badges = files(house, uploads)
    assert await make_picture(badges, house) is None  # not there yet: the caller shows the small icons
    ((name, png, alt),) = uploads.calls
    assert name.startswith("badges-") and name.endswith(".png") and png == b"grid:chef-gold,scout-first"
    assert alt == "Badges: Chef (Gold), Scout (First)"
    house.clock.advance(timedelta(seconds=31))
    assert await badges.composite(house.household.id, house["Kim"].id, BOTH) == "F1"
    assert len(uploads.calls) == 1  # uploaded once, reused


async def test_a_picture_is_shown_only_once_it_has_been_in_slack_for_thirty_seconds(house: House) -> None:
    badges = files(house, Uploads())
    await make_picture(badges, house)
    fresh: set[str] = set()
    assert await badges.composite(house.household.id, house["Kim"].id, BOTH, fresh) is None  # just uploaded
    house.clock.advance(timedelta(seconds=29))
    assert await badges.composite(house.household.id, house["Kim"].id, BOTH, fresh) is None
    house.clock.advance(timedelta(seconds=1))  # 30 s exactly
    assert await badges.composite(house.household.id, house["Kim"].id, BOTH, fresh) == "F1"
    assert fresh == set()  # long past FRESH_FOR: a picture that shows is never new


async def test_the_picture_is_looked_up_without_uploading_when_it_is_current(house: House) -> None:
    uploads = Uploads()
    badges = files(house, uploads)
    await make_picture(badges, house)
    house.clock.advance(COMPOSITE_SETTLE)
    for _ in range(3):
        assert await make_picture(badges, house) == "F1"
    assert len(uploads.calls) == 1


async def test_a_restart_reuses_the_remembered_picture(house: House) -> None:
    await make_picture(files(house, Uploads()), house)
    house.clock.advance(COMPOSITE_SETTLE)
    later = Uploads()
    assert await make_picture(files(house, later), house) == "F1"
    assert later.calls == []


async def test_a_new_tier_replaces_the_members_one_row_and_waits_again(house: House) -> None:
    uploads = Uploads()
    badges = files(house, uploads)
    await make_picture(badges, house)
    house.clock.advance(COMPOSITE_SETTLE)
    higher = (("chef", Tier.DIAMOND), BOTH[1])
    assert await make_picture(badges, house, higher) is None  # stale: the small icons show meanwhile
    assert len(uploads.calls) == 2
    row = await stored(house)
    assert row.file_id == "F2" and row.uploaded_at == house.clock()
    async with house.sessionmaker() as db:
        assert (await db.scalar(select(func.count()).select_from(SlackBadgeCompositeRow))) == 1  # one per member
    assert await badges.composite(house.household.id, house["Kim"].id, higher) is None  # under 30 s old
    house.clock.advance(COMPOSITE_SETTLE)
    assert await badges.composite(house.household.id, house["Kim"].id, higher) == "F2"
    assert await badges.composite(house.household.id, house["Kim"].id, BOTH) is None  # the old badges: stale


async def test_a_different_art_source_draws_another_picture(house: House) -> None:
    uploads = Uploads()
    await make_picture(files(house, uploads), house)
    redrawn = files(house, uploads, composite=lambda held: b"another drawing")
    house.clock.advance(COMPOSITE_SETTLE)
    assert await make_picture(redrawn, house) is None
    assert [c[1] for c in uploads.calls] == [b"grid:chef-gold,scout-first", b"another drawing"]


async def test_each_member_has_their_own_picture(house: House) -> None:
    uploads = Uploads()
    badges = files(house, uploads)
    await make_picture(badges, house, member="Kim")
    await make_picture(badges, house, BOTH[:1], member="Sam")
    house.clock.advance(COMPOSITE_SETTLE)
    assert await badges.composite(house.household.id, house["Kim"].id, BOTH) == "F1"
    assert await badges.composite(house.household.id, house["Sam"].id, BOTH[:1]) == "F2"


async def test_nothing_is_drawn_for_no_badges_or_for_a_member_who_gets_no_picture(house: House) -> None:
    uploads = Uploads()
    badges = files(house, uploads)
    assert await make_picture(badges, house, ()) is None

    async def nobody() -> None:
        return None  # a guest, or a member who left

    await badges.composites_soon(house.household.id, [(house["Kim"].id, nobody)])
    await badges.idle()
    assert uploads.calls == [] and await stored(house) is None


async def test_two_asks_for_one_picture_at_once_share_one_upload(house: House) -> None:
    uploads = Uploads()
    uploads.gate = asyncio.Event()
    badges = files(house, uploads)
    for _ in range(2):
        await badges.composite(house.household.id, house["Kim"].id, BOTH)
    await uploads.started.wait()
    uploads.gate.set()
    await badges.idle()
    assert len(uploads.calls) == 1


async def test_a_failed_picture_logs_one_line_and_pauses_all_uploads_for_the_cooldown(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    uploads = Uploads()
    uploads.error = RuntimeError("Slack files.upload failed: missing_scope")
    badges = files(house, uploads)
    with caplog.at_level(logging.INFO):
        assert await make_picture(badges, house) is None  # never raises
        assert await make_picture(badges, house) is None  # cooling: not tried again
    (line,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "files:write" in line.getMessage() and len(uploads.calls) == 1
    assert await badges.file_ids(house.household.id, [CHEF]) == {}  # the same cooldown holds the art too
    uploads.error = None
    house.clock.advance(COOLDOWN + timedelta(seconds=1))
    await make_picture(badges, house)
    assert len(uploads.calls) == 2 and (await stored(house)).file_id == "F2"


async def test_a_slow_picture_gives_up(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(badge_files, "COMPOSITE_TIMEOUT", 0.05)
    uploads = Uploads()
    uploads.gate = asyncio.Event()  # never set
    badges = files(house, uploads)
    await make_picture(badges, house)
    assert await stored(house) is None


async def test_a_picture_that_cannot_be_drawn_never_raises(house: House) -> None:
    def broken(held: Any) -> bytes:
        raise ValueError("no drawing")

    badges = files(house, Uploads(), composite=broken)
    assert await make_picture(badges, house) is None


async def test_settling_at_shutdown_cancels_a_picture_still_being_made(house: House) -> None:
    uploads = Uploads()
    uploads.gate = asyncio.Event()
    badges = files(house, uploads)
    await badges.composite(house.household.id, house["Kim"].id, BOTH)
    await uploads.started.wait()
    await badges.settle(0.05)
    await badges.idle()
    assert await stored(house) is None


async def test_forgetting_a_file_forgets_the_picture_that_uses_it(house: House) -> None:
    badges = files(house, Uploads())
    await make_picture(badges, house)
    await badges.forget(house.household.id, ["F1"])
    assert await stored(house) is None


# ... in the announcement and on Home ---------------------------------------------------------------


def home_blocks(slack: Slack, who: str = "Alex") -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = slack.last_home(who)["blocks"]
    return blocks


def picture(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((b for b in blocks if b["type"] == "image"), None)


async def test_a_badge_post_makes_each_mentioned_members_picture_for_home(house: House) -> None:
    slack = await wire(house)
    await earn(house)
    await slack.drain()
    row = await stored(house, "Kim")
    assert row is not None and row.file_id in slack.fake.files
    assert await stored(house, "Sam") is None  # Sam is not in the post, so no picture
    (upload,) = [c for c in slack.fake.of("files.getUploadURLExternal") if c.args["filename"].startswith("badges-")]
    assert upload.args["alt_txt"] == "Badges: Chef (Gold), Scout (First)"


async def test_a_picture_that_fails_never_fails_or_delays_the_announcement(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    slack = await wire(house)

    def broken(held: Any) -> bytes:
        raise RuntimeError("the drawing broke")

    monkeypatch.setattr(slack.conn.badge_files, "composite_for", broken)
    await earn(house)
    result = await slack.drain()
    assert (result.delivered, result.failed, result.retrying) == (1, 0, 0)
    assert any("accessory" in b for b in announcement(slack))
    assert await stored(house, "Kim") is None


async def test_starting_the_pictures_never_fails_the_delivery(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    slack = await wire(house)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("could not start")

    monkeypatch.setattr(slack.conn.badge_files, "composites_soon", broken)
    await earn(house)
    result = await slack.drain()
    assert (result.delivered, result.failed) == (1, 0)


async def test_the_pictures_the_post_starts_wait_for_a_missing_scope_like_the_art(house: House) -> None:
    slack = await wire(house)
    slack.fake.fail("files.getUploadURLExternal", Failure("missing_scope"))
    await earn(house)
    result = await slack.drain()
    assert (result.delivered, result.failed) == (1, 0)
    assert await stored(house, "Kim") is None and len(slack.fake.of("files.getUploadURLExternal")) == 1


async def test_home_shows_the_small_icons_until_the_picture_has_settled_and_then_the_picture(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    await slack.open_home("Alex")  # nothing yet: the small icons, and the picture is made
    first = home_blocks(slack)
    assert picture(first) is None and home_icons(first)
    row = await stored(house, "Alex")
    assert row is not None
    house.clock.advance(COMPOSITE_SETTLE - timedelta(seconds=1))
    await slack.open_home("Alex")
    assert picture(home_blocks(slack)) is None  # 29 s: still the icons
    house.clock.advance(timedelta(seconds=1))
    await slack.open_home("Alex")
    blocks = home_blocks(slack)
    image = picture(blocks)
    assert image is not None and image["slack_file"] == {"id": row.file_id} and row.file_id in slack.fake.files
    assert image["alt_text"].startswith("Your badges: ") and "(First)" in image["alt_text"]
    assert not any(b["type"] == "context" and all(e["type"] == "image" for e in b["elements"]) for b in blocks)
    assert (
        len([c for c in slack.fake.of("files.getUploadURLExternal") if c.args["filename"].startswith("badges-")]) == 1
    )


async def test_home_never_shows_the_picture_in_the_view_that_uploads_it(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    await slack.open_home("Alex")
    assert all(picture(call.args["view"]["blocks"]) is None for call in slack.fake.of("views.publish"))


async def test_home_draws_food_critic_in_the_picture_like_the_rest(house: House) -> None:
    await award(house, "Alex", BadgeKey.CHEF, Tier.GOLD)
    await award(house, "Alex", BadgeKey.FOOD_CRITIC, Tier.FIRST)
    slack = await wire(house)
    await slack.open_home("Alex")
    house.clock.advance(COMPOSITE_SETTLE)
    await slack.open_home("Alex")
    blocks = home_blocks(slack)
    image = picture(blocks)
    assert image is not None and image["alt_text"] == "Your badges: Chef (Gold), Food Critic (First)"
    assert "Also:" not in json.dumps(blocks, ensure_ascii=False)
    uploaded = [c for c in slack.fake.of("files.getUploadURLExternal") if c.args["filename"].startswith("badges-")]
    assert "Food Critic (First)" in uploaded[0].args["alt_txt"]


async def test_a_new_tier_goes_back_to_the_icons_until_the_new_picture_settles(house: House) -> None:
    await award(house, "Alex", BadgeKey.CHEF, Tier.BRONZE)
    slack = await wire(house)
    await slack.open_home("Alex")
    house.clock.advance(COMPOSITE_SETTLE)
    await slack.open_home("Alex")
    old = picture(home_blocks(slack))
    assert old is not None
    await award(house, "Alex", BadgeKey.CHEF, Tier.SILVER)
    await slack.open_home("Alex")  # stale: icons, and a new picture
    assert picture(home_blocks(slack)) is None
    house.clock.advance(COMPOSITE_SETTLE)
    await slack.open_home("Alex")
    new = picture(home_blocks(slack))
    assert new is not None and new["slack_file"] != old["slack_file"]
    assert "(Silver)" in new["alt_text"]


async def test_home_shows_text_tags_when_neither_the_picture_nor_the_icons_can_be_had(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    slack.fake.fail("files.getUploadURLExternal", Failure("missing_scope"))
    await slack.open_home("Alex")
    house.clock.advance(COMPOSITE_SETTLE)
    await slack.open_home("Alex")
    text = json.dumps(home_blocks(slack), ensure_ascii=False)
    assert "slack_file" not in text and tag("Full Table", "First") in text


async def test_a_refused_picture_is_forgotten_and_home_shows_text(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    await slack.open_home("Alex")
    house.clock.advance(COMPOSITE_SETTLE)  # the picture and the icons are all cached and settled
    slack.fake.clear()
    slack.fake.fail("views.publish", Failure("invalid_blocks"))
    await slack.open_home("Alex")
    tried, last = slack.fake.of("views.publish")
    assert picture(tried.args["view"]["blocks"]) is not None
    assert "slack_file" not in json.dumps(last.args["view"])
    assert await stored(house, "Alex") is None  # forgotten, so the next view makes it again
    async with house.sessionmaker() as db:
        assert await BadgeFileRepository(db).all(house.household.id) == {}


async def test_a_picture_that_counts_as_new_is_retried_after_a_pause_and_then_forgotten(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(badge_files, "COMPOSITE_SETTLE", timedelta(0))  # shown at once, so fresh
    await dinner(house)
    slack = await wire(house)
    pauses = Pauses()
    slack.conn.badge_files.pause = pauses
    await slack.open_home("Alex")  # makes the picture
    slack.fake.clear()
    slack.fake.fail("views.publish", *[Failure("invalid_blocks")] * 3)
    await slack.open_home("Alex")
    *tried, last = slack.fake.of("views.publish")
    assert len(tried) == 3 and all(picture(c.args["view"]["blocks"]) is not None for c in tried)
    assert pauses.waits == list(RETRY_PAUSES)
    assert picture(last.args["view"]["blocks"]) is None
    assert await stored(house, "Alex") is None
    slack.fake.clear()  # the cooldown: text again, and no new picture is made
    await slack.open_home("Alex")
    assert slack.fake.of("files.getUploadURLExternal") == []
    house.clock.advance(COOLDOWN + timedelta(seconds=1))
    await slack.open_home("Alex")
    assert any(c.args["filename"].startswith("badges-") for c in slack.fake.of("files.getUploadURLExternal"))


async def test_a_member_who_left_gets_no_new_picture_and_the_old_row_is_harmless(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    await slack.open_home("Alex")
    old = await stored(house, "Alex")
    async with house.sessionmaker() as db:
        await MemberRepository(db).update(house.household.id, house["Alex"].id, house.clock(), active=False)
        await db.commit()
        assert await picture_badges(db, house.household.id, house["Alex"].id) is None
        assert await picture_badges(db, house.household.id, 9999) is None
    slack.fake.clear()
    await award(house, "Alex", BadgeKey.CHEF, Tier.GOLD)
    await announce(house)  # not announced: Alex is no longer active
    await slack.drain()
    pictures = [c for c in slack.fake.of("files.getUploadURLExternal") if c.args["filename"].startswith("badges-")]
    assert not any("Chef (Gold)" in c.args["alt_txt"] for c in pictures)  # others' pictures are made; Alex's isn't
    assert await stored(house, "Alex") == old


# Polish (#114) -------------------------------------------------------------------------------------

NAMES = [b.name for b in BADGES]


def ten_badge_post(first: int = 0, members: int = 1) -> note.BadgesEarned:
    gold = [note.BadgeTier(name, "Gold") for name in NAMES]
    news = tuple(
        note.BadgeNews(note.PersonInfo(100 + n, f"Member {n}"), tuple(gold[:6]), more=7, rest=tuple(gold[6:]))
        for n in range(members)
    )
    return note.BadgesEarned(1, first, news)


def every_tier_art() -> dict[tuple[str, str], str]:
    return {(b.key.value, t.value): f"F-{b.key.value}-{t.value}" for b in BADGES for t in TIERS}


def test_the_icon_row_shows_every_badge_and_the_line_names_them_all_plainly() -> None:
    message = render(ten_badge_post(), RenderContext("https://x.example", Directory(), art=every_tier_art()))
    blocks = message.blocks
    assert [len(r) for r in icons(blocks)] == [10, 3]  # all 13, not the 6 the text names
    line = next(b["text"]["text"] for b in blocks if "earned 13 badges:" in b.get("text", {}).get("text", ""))
    assert line.count("*") == 26 and "more badge" not in line and "🥇" not in line and "·" not in line
    assert all(f"*{name}*" in line for name in NAMES)


def test_without_art_the_text_post_is_unchanged_and_still_counts_the_rest() -> None:
    message = render(ten_badge_post(), RenderContext("https://x.example", Directory()))
    (block,) = [b for b in message.blocks if "earned" in json.dumps(b)]
    assert block["text"]["text"].splitlines()[1].endswith(", and 7 more badges.")
    assert tag("*Chef*", "Gold") in block["text"]["text"] and "🥇" in block["text"]["text"]


def test_a_badge_with_no_art_keeps_its_tag_in_the_plain_line() -> None:
    art = {k: v for k, v in every_tier_art().items() if k != ("chef", "gold")}
    message = render(ten_badge_post(), RenderContext("https://x.example", Directory(), art=art))
    line = next(b["text"]["text"] for b in message.blocks if "earned 13 badges:" in b.get("text", {}).get("text", ""))
    assert tag("*Chef*", "Gold") in line and "*Scout*" in line and tag("*Scout*", "Gold") not in line
    assert [len(r) for r in icons(message.blocks)] == [10, 2]


def test_a_post_queued_before_the_rest_existed_still_reads_right() -> None:
    old = note.BadgesEarned(1, 1, ten_badge_post().news[:1])
    news = old.news[0]
    queued = note.BadgesEarned(1, 1, (note.BadgeNews(news.person, news.badges, more=7),))
    message = render(queued, RenderContext("https://x.example", Directory(), art=every_tier_art()))
    line = next(b["text"]["text"] for b in message.blocks if "earned 13 badges:" in b.get("text", {}).get("text", ""))
    assert line.endswith("and 7 more badges.") and [len(r) for r in icons(message.blocks)] == [6]


def test_the_worst_post_stays_within_fifty_blocks() -> None:
    """Every member takes at most 3 blocks (a row-of-icons member: a line and two rows; any other: at
    most three badges as a block each, or a line for those without art), and 3 are fixed: 33 at most."""
    gold = [note.BadgeTier(name, "Gold") for name in NAMES]
    ctx = RenderContext("https://x.example", Directory(), art=every_tier_art())
    for many in range(11):
        news = tuple(
            note.BadgeNews(note.PersonInfo(n, f"Member {n}"), tuple(gold[:6]), more=7, rest=tuple(gold[6:]))
            if n < many
            else note.BadgeNews(note.PersonInfo(n, f"Few {n}"), tuple(gold[:3]))
            for n in range(10)
        )
        message = render(note.BadgesEarned(1, 1, news, more_people=5), ctx)
        assert len(message.blocks) <= 33 < MAX_MESSAGE_BLOCKS
    all_many = note.BadgesEarned(1, 1, ten_badge_post(members=10).news, more_people=5)
    assert len(render(all_many, ctx).blocks) == 33


async def test_news_for_carries_every_badge_beyond_the_six_named(house: House) -> None:
    from dinnerbell.services import announcements

    for key in list(BadgeKey)[:9]:
        await award(house, "Kim", key, Tier.FIRST)
    assert await announce(house) == 1
    (post,) = [note.from_payload(e.kind, e.payload) for e in await house.outbox() if e.kind == "badges_earned"]
    assert isinstance(post, note.BadgesEarned)
    (news,) = post.news
    assert len(news.badges) == announcements.MAX_BADGES and news.more == 3 and len(news.rest) == 3


def test_the_next_line_leaves_the_tier_emoji_off_beside_its_icon_and_keeps_it_without() -> None:
    from dinnerbell.channels.slack.preview import KIM_BADGES

    art = dict.fromkeys(art_wanted(KIM_BADGES), "F1")
    with_icon = next(b for b in _badges(KIM_BADGES, art, HomeLinks("https://x.example")) if "Next:" in json.dumps(b))
    assert with_icon["elements"][0]["type"] == "image"
    text = with_icon["elements"][1]["text"]
    assert text.startswith("Next: *") and " · " in text and not any(m in text for m in TIER_MARKS.values())
    plain = next(b for b in _badges(KIM_BADGES, {}, HomeLinks("https://x.example")) if "Next:" in json.dumps(b))
    assert any(m in plain["elements"][0]["text"] for m in TIER_MARKS.values())


def test_the_home_block_budget_holds_with_the_picture() -> None:
    from dinnerbell.channels.slack.preview import KIM_BADGES

    art = dict.fromkeys(art_wanted(KIM_BADGES), "F1")
    with_picture = _badges(KIM_BADGES, art, HomeLinks("https://x.example"), "FPIC")
    assert len(with_picture) <= 6 and [b["type"] for b in with_picture][:2] == ["section", "image"]
    assert len(_badges(KIM_BADGES, art, HomeLinks("https://x.example"))) <= 6


# Review fixes (#115) -------------------------------------------------------------------------------


async def test_a_failure_that_is_not_slacks_logs_its_own_line_and_starts_no_cooldown(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    uploads = Uploads()
    badges = files(house, uploads)

    async def broken() -> None:
        raise RuntimeError("the database is gone")

    with caplog.at_level(logging.INFO):
        await badges.composites_soon(house.household.id, [(house["Kim"].id, broken)])
        await badges.idle()
    (line,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert line.getMessage() == "badge composite could not be made" and "files:write" not in line.getMessage()
    assert await badges.file_ids(house.household.id, [CHEF]) == {CHEF: "F1"}  # the art still uploads
    assert await make_picture(badges, house) is None and (await stored(house)).file_id == "F2"


async def test_a_drawing_that_fails_starts_no_cooldown(house: House) -> None:
    def broken(held: Any) -> bytes:
        raise ValueError("no drawing")

    uploads = Uploads()
    badges = files(house, uploads, composite=broken)
    await make_picture(badges, house)
    assert await badges.file_ids(house.household.id, [CHEF]) == {CHEF: "F1"}


async def test_settling_waits_for_the_cancelled_pictures_to_finish(house: House) -> None:
    uploads = Uploads()
    uploads.gate = asyncio.Event()
    badges = files(house, uploads)
    await badges.composite(house.household.id, house["Kim"].id, BOTH)
    await uploads.started.wait()
    tasks = set(badges._composite_tasks)
    await badges.settle(0.05)
    assert all(t.done() for t in tasks)  # none runs on against a closing database


async def test_stopping_settles_interactions_before_badge_files(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.support.fake_slack import FakeSlack

    conn = connection(house, FakeSlack())
    order: list[str] = []

    async def record(name: str, within: float = 0) -> None:
        order.append(name)

    monkeypatch.setattr(conn.badge_files, "settle", lambda within: record("badge_files", within))
    monkeypatch.setattr(conn.interactions, "settle", lambda within=0: record("interactions", within))
    await conn.stop()
    assert order == ["interactions", "badge_files"]
