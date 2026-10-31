# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where members act in Slack (#149): App Home follows the call through the vote and the
decision, direct messages carry the buttons they mention, and each page is linked by one name."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from dinnerbell.channels.slack import ids, preview
from dinnerbell.channels.slack.interactions import GUEST_TAPS
from dinnerbell.channels.slack.messages import RenderContext, render
from dinnerbell.config import Settings
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.events import EventState
from dinnerbell.services.database import Database, open_database
from tests.support.calls import OPENS, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import CHANNEL
from tests.support.slack import Slack, wire

pytestmark = pytest.mark.anyio


def _buttons(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


def _ids(blocks: list[dict[str, Any]]) -> list[str]:
    return [e["action_id"] for e in _buttons(blocks)]


def _text(blocks: list[dict[str, Any]]) -> str:
    found: list[str] = []
    for block in blocks:
        if block["type"] == "section":
            found.append(block["text"]["text"])
        elif block["type"] == "context":
            found += [e.get("text", "") for e in block["elements"]]
    return "\n".join(found)


def _rendered(kind: str) -> dict[str, Any]:
    return preview.preview(kind)


# Rendering ---------------------------------------------------------------------------------------


def test_the_reminder_has_the_cards_rows_and_says_how_to_stop_reminders() -> None:
    blocks = _rendered("response_reminder")["blocks"]
    rows = {b["block_id"]: [e["action_id"] for e in b["elements"]] for b in blocks if b["type"] == "actions"}
    assert rows["participation"] == ["card_in", "card_maybe", "card_out"]  # a row of their own (Q4)
    assert rows["roles"][:3] == ["card_offer_cook", "card_offer_pickup", "card_offer_order"]  # Q6
    assert rows["more"] == ["link_event"]
    assert "/preferences|your preferences>" in _text(blocks)  # gap 10
    assert "/preferences" not in _text(_rendered("response_reminder_guest")["blocks"])  # guests have no such page
    assert "/preferences|your preferences>" in _text(_rendered("feedback_reminder")["blocks"])


def test_the_maybe_turned_out_message_has_the_button_it_mentions() -> None:
    blocks = _rendered("maybe_resolved")["blocks"]
    assert "I'm in (late)" in _text(blocks) and _ids(blocks) == [ids.JOIN_LATE, "link_event"]


def test_a_called_off_call_offers_to_start_again_where_its_told() -> None:
    called_off = _rendered("call_updated_cancelled")["blocks"]
    (restart,) = [e for e in _buttons(called_off) if e["action_id"] == ids.RESTART]
    assert restart["text"]["text"] == "Start dinner again" and restart["value"] == "7:1"
    assert ids.RESTART in _ids(_rendered("started_call_cancelled")["blocks"])
    for kind in ("call_opened", "call_updated_voting", "call_updated_decided", "offer_cancelled"):
        assert ids.RESTART not in _ids(_rendered(kind)["blocks"]), kind


def test_every_link_to_an_event_has_one_name() -> None:
    names = set()
    for kind in preview.KINDS:
        for block in _rendered(kind).get("blocks", []):
            if block["type"] == "actions":
                names |= {e["text"]["text"] for e in block["elements"] if e["action_id"] == "link_event"}
    assert names == {"View on web"}
    for kind in ("call_abandoned", "event_cancelled"):
        assert "|View on web>" in _text(_rendered(kind)["blocks"])


def test_badge_posts_and_the_recap_link_stats() -> None:
    for kind in ("badges_earned", "badges_earned_many", "monthly_recap"):
        assert "/stats|Stats on the web>" in _text(_rendered(kind)["blocks"]), kind


def test_home_links_stats_and_schedule_without_badge_art() -> None:
    footer = _text(_rendered("home_badges_none")["blocks"])
    assert "/stats|Stats>" in footer and "/schedule|Schedule>" in footer


def test_homes_voting_card_says_how_you_voted_and_links_the_picks() -> None:
    text = _text(_rendered("home_voting")["blocks"])
    assert "You voted for *Sam's Tacos*." in text
    assert f"<{preview.PICKS_LINK}|See the picks and vote in the channel>" in text
    unlinked = _text(_rendered("home_voting_no_coordinator")["blocks"])
    assert "You haven't voted yet." in unlinked and "The picks are posted in the channel." in unlinked


def test_home_offers_to_coordinate_while_voting_with_nobody_coordinating() -> None:
    assert ids.CARD_COORDINATE in _ids(_rendered("home_voting_no_coordinator")["blocks"])
    assert ids.CARD_COORDINATE not in _ids(_rendered("home_voting")["blocks"])  # Alex coordinates


def test_homes_decided_card_says_when_and_your_role() -> None:
    text = _text(_rendered("home_decided_cook")["blocks"])
    assert "The plan: *Sam's Tacos* at 6:45 PM" in text and "You're cooking." in text
    assert "You're cooking." not in _text(_rendered("home_decided")["blocks"])  # not Alex's role


def test_permalinks_take_only_what_slack_sends() -> None:
    assert ids.permalink("https://home.slack.com/", "C0DINNER", "1712345678.000200") == (
        "https://home.slack.com/archives/C0DINNER/p1712345678000200"
    )
    assert ids.permalink("https://acme.enterprise.slack.com/", "C0DINNER", "1.2") is not None
    for url, channel, ts in [
        ("https://evil.example/", "C0DINNER", "1.2"),
        ("http://home.slack.com/", "C0DINNER", "1.2"),
        ("https://home.slack.com.evil.example/", "C0DINNER", "1.2"),
        ("https://home.slack.com/", "not a channel", "1.2"),
        ("https://home.slack.com/", "C0DINNER", "1.2x"),
        ("", "C0DINNER", "1.2"),
    ]:
        assert ids.permalink(url, channel, ts) is None, url


def test_a_recap_links_stats_on_the_instances_own_address() -> None:
    recap = preview.NOTIFICATIONS["monthly_recap"]()
    assert isinstance(recap, note.MonthlyRecap)
    message = render(recap, RenderContext("https://food.example.org", preview.DIRECTORY))
    assert "<https://food.example.org/stats|Stats on the web>" in str(message.blocks)


# Taps --------------------------------------------------------------------------------------------


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


async def _daily_call(house: House) -> int:
    house.clock.now = OPENS
    await house.tick()
    return await dinner_id(house)


async def test_start_dinner_again_from_the_called_off_call_message(slack: Slack, house: House) -> None:
    event_id = await _daily_call(house)
    await slack.drain()
    await slack.command("Alex", "cancel dinner")
    await slack.drain()
    (update,) = [c.args for c in slack.fake.of("chat.update") if ids.RESTART in _ids(c.args["blocks"])]
    (restart,) = [e for e in _buttons(update["blocks"]) if e["action_id"] == ids.RESTART]
    await slack.click("Kim", ids.RESTART, restart["value"])
    event = await house.event(event_id)
    assert (event.state, event.run) == (EventState.COLLECTING, 2)
    assert any("Started the dinner call" in text for text in slack.ephemerals("Kim"))
    await slack.click("Kim", ids.RESTART, restart["value"])  # the old message, after it started again
    assert (await house.event(event_id)).run == 2


def test_a_guest_cant_start_a_call_again() -> None:
    assert ids.RESTART not in GUEST_TAPS  # refused before any service runs; the service refuses too (#38)


async def test_a_role_on_the_reminder_edits_it_in_place(slack: Slack, house: House) -> None:
    event_id = await _daily_call(house)
    await slack.click("Lee", "card_offer_order", ids.card_value(event_id, 1), channel="D04LEE", message_ts="2.000100")
    (edited,) = [c.args for c in slack.fake.of("chat.update") if c.args["channel"] == "D04LEE"]
    rows = {b["block_id"]: b for b in edited["blocks"] if b["type"] == "actions"}
    (order,) = [e for e in rows["roles"]["elements"] if e["action_id"] == "card_withdraw_order"]
    assert order["text"]["text"] == "✓ I'll order" and order["style"] == "primary"
    assert "*In*" in edited["blocks"][0]["text"]["text"]  # offering counts you in


async def test_home_links_the_picks_post_while_voting(slack: Slack, house: House) -> None:
    event_id = await _daily_call(house)
    await slack.click("Alex", "respond_in", str(event_id))
    await slack.click("Alex", "offer_pickup", str(event_id))
    await slack.click("Sam", "respond_in", str(event_id))
    house.clock.now = RESPOND_BY
    await house.tick()
    await slack.drain()
    async with house.sessionmaker() as db:
        ref = await OutboxRepository(db).message_ref(house.household.id, f"event:{event_id}:run1:proposals")
    assert ref is not None and ref.channel_id == CHANNEL
    ts = ref.message_id
    slack.conn.interactions.team_url = "https://home.slack.com/"
    await slack.open_home("Sam")
    text = _text(slack.last_home("Sam")["blocks"])
    assert f"<https://home.slack.com/archives/{CHANNEL}/p{ts.replace('.', '')}|See the picks" in text
    assert "You haven't voted yet." in text


async def test_the_workspace_address_comes_from_auth_test(slack: Slack) -> None:
    assert slack.conn.interactions.team_url == "https://home.slack.com/"


def test_the_samples_cover_the_new_home_states() -> None:
    assert {"home_voting", "home_voting_no_coordinator", "home_decided_cook"} <= set(preview.KINDS)
