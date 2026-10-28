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

from dinnerbell.channels.slack.badge_text import tag
from dinnerbell.config import Settings
from dinnerbell.db.badge_files import BadgeFileRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.badges import BadgeKey, Tier
from dinnerbell.services import badge_art
from dinnerbell.services.badge_files import COOLDOWN, RETRY_PAUSE_SECONDS, BadgeFiles, is_ready
from dinnerbell.services.database import Database, open_database
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import Failure
from tests.support.slack import SLACK_IDS, Slack, wire
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


def files(house: House, uploads: Uploads, *, art: Any = None, **options: Any) -> BadgeFiles:
    return BadgeFiles(
        house.sessionmaker,
        art_for=art or (lambda k, t: f"{k}-{t.value}".encode()),
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


async def test_a_quiet_badge_is_never_uploaded(house: House) -> None:
    uploads = Uploads()
    asked = [(BadgeKey.FOOD_CRITIC.value, "gold"), ("nonsense", "gold"), ("chef", "platinum")]
    assert await files(house, uploads).file_ids(house.household.id, asked) == {}
    assert uploads.calls == []


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
    await award(house, "Sam", BadgeKey.FOOD_CRITIC, Tier.FIRST)  # quiet
    assert await announce(house) == 1


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
    assert "Food Critic" not in text
    assert len(slack.fake.of("upload_url")) == 2 and all(
        f["filename"].endswith(".png") for f in slack.fake.files.values()
    )


async def test_a_second_announcement_reuses_the_uploads(house: House) -> None:
    slack = await wire(house)
    await earn(house)
    await slack.drain()
    slack.fake.clear()
    await award(house, "Alex", BadgeKey.CHEF, Tier.GOLD)
    await announce(house)
    await slack.drain()
    assert slack.fake.of("files.getUploadURLExternal") == []
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
    slack.fake.fail("chat.postMessage", Failure("invalid_blocks"), Failure("invalid_blocks"))  # and again on the retry
    await earn(house)
    await slack.drain()
    first, second, third = slack.fake.of("chat.postMessage")
    assert any("accessory" in b for b in first.args["blocks"]) and any("accessory" in b for b in second.args["blocks"])
    assert not any("accessory" in b for b in third.args["blocks"])
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


async def test_an_announcement_with_only_quiet_or_unknown_badges_has_no_art(house: House) -> None:
    slack = await wire(house)
    post = note.BadgesEarned(
        house.household.id,
        1,
        (note.BadgeNews(note.PersonInfo(house["Kim"].id, "Kim"), (note.BadgeTier("Food Critic", "Gold"),)),),
    )
    await slack.conn.channel.deliver(post)
    assert slack.fake.of("files.getUploadURLExternal") == []
    assert not any("accessory" in b for b in announcement(slack))


# App Home -----------------------------------------------------------------------------------------


async def test_home_shows_the_members_own_badges_as_art(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    await slack.open_home("Alex")
    blocks = slack.last_home("Alex")["blocks"]
    art = [b for b in blocks if b.get("accessory", {}).get("type") == "image"]
    assert art and all(b["accessory"]["slack_file"]["id"] in slack.fake.files for b in art)
    text = json.dumps(blocks)
    assert "Your badges" in text and "Next:" in text and "Quarterback" not in text


async def test_home_shows_text_tags_when_uploads_fail(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    slack.fake.fail("files.getUploadURLExternal", Failure("missing_scope"))
    await slack.open_home("Alex")
    text = json.dumps(slack.last_home("Alex"), ensure_ascii=False)
    assert "slack_file" not in text and tag("Full Table", "First") in text


async def test_home_keeps_a_quiet_badge_as_text(house: House) -> None:
    await award(house, "Alex", BadgeKey.CHEF, Tier.GOLD)
    await award(house, "Alex", BadgeKey.FOOD_CRITIC, Tier.FIRST)
    slack = await wire(house)
    await slack.open_home("Alex")
    blocks = slack.last_home("Alex")["blocks"]
    assert [b["accessory"]["alt_text"] for b in blocks if "accessory" in b] == ["Chef, Gold"]
    assert tag("Food Critic", "First") in json.dumps(blocks, ensure_ascii=False)


async def test_slack_refusing_a_file_on_home_publishes_text_and_forgets_the_file(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    slack.fake.fail("views.publish", Failure("invalid_blocks"), Failure("invalid_blocks"))  # and again on the retry
    await slack.open_home("Alex")
    first, second, third = slack.fake.of("views.publish")
    assert "slack_file" in json.dumps(first.args["view"]) and "slack_file" in json.dumps(second.args["view"])
    text = json.dumps(third.args["view"], ensure_ascii=False)
    assert "slack_file" not in text and tag("Full Table", "First") in text
    async with house.sessionmaker() as db:
        assert await BadgeFileRepository(db).all(house.household.id) == {}
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
    slack.fake.fail("chat.postMessage", why, why)
    await earn(house)
    with caplog.at_level(logging.INFO):
        await slack.drain()
    first, again = refusals(caplog)
    assert first.slack_error == "invalid_blocks"  # type: ignore[attr-defined]
    assert first.slack_messages.startswith("[ERROR] invalid file")  # type: ignore[attr-defined]
    assert len(first.slack_messages) == 300  # type: ignore[attr-defined]
    assert "invalid_blocks" in first.getMessage() and "retrying" in first.getMessage()
    assert again.slack_error == "invalid_blocks" and "posting text badges" in again.getMessage()  # type: ignore[attr-defined]


async def test_a_home_refusal_logs_slacks_error_code_and_explanation(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    await dinner(house)
    slack = await wire(house)
    why = Failure("invalid_blocks", messages=["[ERROR] invalid file"])
    slack.fake.fail("views.publish", why, why)
    with caplog.at_level(logging.INFO):
        await slack.open_home("Alex")
    first, again = refusals(caplog)
    assert first.slack_error == "invalid_blocks" and first.slack_messages == "[ERROR] invalid file"  # type: ignore[attr-defined]
    assert "publishing text badges" in again.getMessage()


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


async def test_a_refused_post_with_a_new_file_is_retried_once_after_a_pause(house: House) -> None:
    slack = await wire(house)
    pauses = Pauses()
    slack.conn.badge_files.pause = pauses
    slack.fake.fail("chat.postMessage", Failure("invalid_blocks"))
    await earn(house)
    result = await slack.drain()
    assert (result.delivered, result.failed) == (1, 0)
    first, second = slack.fake.of("chat.postMessage")
    assert first.args["blocks"] == second.args["blocks"] and any("accessory" in b for b in second.args["blocks"])
    assert pauses.waits[-1] == RETRY_PAUSE_SECONDS
    async with house.sessionmaker() as db:
        assert len(await BadgeFileRepository(db).all(house.household.id)) == 2  # the cache stays


async def test_a_retry_that_is_refused_too_forgets_the_files_and_posts_text(house: House) -> None:
    slack = await wire(house)
    slack.fake.fail("chat.postMessage", Failure("invalid_blocks"), Failure("invalid_blocks_format"))
    await earn(house)
    result = await slack.drain()
    assert (result.delivered, result.failed) == (1, 0)
    *_, last = slack.fake.of("chat.postMessage")
    assert len(slack.fake.of("chat.postMessage")) == 3 and not any("accessory" in b for b in last.args["blocks"])
    async with house.sessionmaker() as db:
        assert await BadgeFileRepository(db).all(house.household.id) == {}


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


async def test_a_home_refusal_with_a_new_file_is_retried_once(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    slack.fake.fail("views.publish", Failure("invalid_blocks"))
    await slack.open_home("Alex")
    first, second = slack.fake.of("views.publish")
    assert first.args["view"] == second.args["view"] and "slack_file" in json.dumps(second.args["view"])
    async with house.sessionmaker() as db:
        assert await BadgeFileRepository(db).all(house.household.id)


async def test_a_home_retry_that_fails_for_another_reason_is_not_hidden(house: House) -> None:
    await dinner(house)
    slack = await wire(house)
    slack.fake.fail("views.publish", Failure("invalid_blocks"), Failure("user_not_found"))
    await slack.open_home("Alex")
    assert len(slack.fake.of("views.publish")) == 2


# Waiting until Slack has processed a new file ----------------------------------------------------


def info_after(polls: int, asked: list[str]) -> Any:
    """A ``files.info`` that says "not processed yet" for the first ``polls`` calls per file."""
    seen: dict[str, int] = {}

    async def info(file_id: str) -> dict[str, Any]:
        asked.append(file_id)
        seen[file_id] = seen.get(file_id, 0) + 1
        if seen[file_id] <= polls:
            return {"id": file_id, "url_private": "https://example.com/f"}
        return {"id": file_id, "mimetype": "image/png", "thumb_64": "https://example.com/t"}

    return info


def test_a_file_is_ready_with_a_type_and_dimensions_or_a_thumbnail() -> None:
    assert is_ready({"mimetype": "image/png", "original_w": 512})
    assert is_ready({"mimetype": "image/png", "thumb_360": "https://example.com/t"})
    assert not is_ready({"mimetype": "image/png"})
    assert not is_ready({"url_private": "https://example.com/f", "thumb_64": "https://example.com/t"})
    assert not is_ready({"mimetype": "image/png", "thumb_64": ""})
    assert not is_ready({})


async def test_a_new_file_is_returned_only_once_slack_has_processed_it(house: House) -> None:
    pauses, asked = Pauses(), []
    badges = files(house, Uploads(), file_info=info_after(2, asked), pause=pauses)
    fresh: set[str] = set()
    assert await badges.file_ids(house.household.id, [CHEF], fresh) == {CHEF: "F1"}
    assert fresh == {"F1"} and asked == ["F1"] * 3 and len(pauses.waits) == 2


async def test_several_new_files_wait_together(house: House) -> None:
    pauses, asked = Pauses(), []
    badges = files(house, Uploads(), file_info=info_after(1, asked), pause=pauses)
    fresh: set[str] = set()
    assert await badges.file_ids(house.household.id, [CHEF, SCOUT], fresh) == {CHEF: "F1", SCOUT: "F2"}
    assert fresh == {"F1", "F2"} and len(asked) == 4


async def test_a_file_that_never_gets_ready_times_out_to_text_but_stays_cached(house: House) -> None:
    block = asyncio.Event()  # never set: the wait for the next check ends only at the timeout

    async def stuck(seconds: float) -> None:
        await block.wait()

    asked: list[str] = []
    badges = files(house, Uploads(), file_info=info_after(10**6, asked), pause=stuck, timeout=0.05)
    fresh: set[str] = set()
    assert await badges.file_ids(house.household.id, [CHEF], fresh) == {}
    assert fresh == set()
    async with house.sessionmaker() as db:
        assert len(await BadgeFileRepository(db).all(house.household.id)) == 1  # a later ask finds it cached


async def test_cached_files_skip_the_readiness_check(house: House) -> None:
    asked: list[str] = []
    uploads = Uploads()
    badges = files(house, uploads, file_info=info_after(0, asked), pause=Pauses())
    await badges.file_ids(house.household.id, [CHEF])
    asked.clear()
    fresh: set[str] = set()
    assert await badges.file_ids(house.household.id, [CHEF], fresh) == {CHEF: "F1"}
    assert asked == [] and fresh == set()


async def test_an_unreadable_file_status_trusts_the_file_and_warns_once(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    async def denied(file_id: str) -> dict[str, Any]:
        raise RuntimeError("Slack files.info failed: missing_scope")

    badges = files(house, Uploads(), file_info=denied, pause=Pauses())
    with caplog.at_level(logging.INFO):
        assert await badges.file_ids(house.household.id, [CHEF, SCOUT]) == {CHEF: "F1", SCOUT: "F2"}
    (line,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "files:read" in line.getMessage() and "missing_scope" in line.error  # type: ignore[attr-defined]


async def test_the_announcement_waits_for_slack_to_process_the_new_files(house: House) -> None:
    slack = await wire(house)
    slack.fake.processing_polls = 2
    pauses = Pauses()
    slack.conn.badge_files.pause = pauses
    await earn(house)
    await slack.drain()
    assert len(slack.fake.of("files.info")) == 6  # two files, three checks each
    assert len(pauses.waits) == 4 and any("accessory" in b for b in announcement(slack))
    slack.fake.clear()
    await award(house, "Alex", BadgeKey.CHEF, Tier.GOLD)
    await announce(house)
    await slack.drain()
    assert slack.fake.of("files.info") == []  # cached files aren't checked again
