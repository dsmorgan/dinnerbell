# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offers and logging in Slack (spec 5.2, 5.6, section 10): **Make an offer** on Home and
``/dinnerbell offer`` open the offer modal; posting it posts the offer message; **Count me in**,
**+1 guest**, and **Can't make it** fill and free seats; a full offer messages its offerer; a done
offer gets **How was it?**; and ``/dinnerbell log`` logs a meal."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest

from dinnerbell.channels.slack import ids
from dinnerbell.config import Settings
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import EventState
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.offers import FULL
from dinnerbell.services import offers
from dinnerbell.services.database import Database, open_database
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import CHANNEL, send, state, view_submission
from tests.support.slack import SLACK_IDS, Slack, wire

pytestmark = pytest.mark.anyio

MORNING = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
LUNCH_AT = datetime(2026, 10, 22, 17, 30, tzinfo=UTC)
DAY = date(2026, 10, 22)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    clock.now = MORNING
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


def offer_values(item_id: int, *, capacity: str = "3", **extra: dict[str, Any]) -> dict[str, Any]:
    return state(
        slot={"selected_option": {"value": "lunch"}},
        day={"selected_date": DAY.isoformat()},
        mode={"selected_option": {"value": "pickup"}},
        item={"selected_option": {"value": str(item_id)}},
        at={"selected_time": "12:30"},
        capacity={"value": capacity},
        **extra,
    )


async def submit_form(slack: Slack, who: str, callback: str, values: dict[str, Any], origin: ids.FormOrigin) -> Any:
    payload = view_submission(SLACK_IDS[who], callback, values, origin.encode())
    response = await send(slack.conn.app, "interactive", payload)
    await slack.conn.interactions.settle()
    return response


def buttons(payload: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for block in payload["blocks"]:
        for element in block.get("elements", []) if block["type"] == "actions" else []:
            if element.get("type") == "button":
                found[element["action_id"]] = element.get("value", "")
    return found


def blocks_of(args: dict[str, Any]) -> list[dict[str, Any]]:
    raw = args["blocks"]
    found: list[dict[str, Any]] = raw if isinstance(raw, list) else json.loads(raw)
    return found


async def offer_id(house: House) -> int:
    from dinnerbell.db.events import EventRepository

    async with house.sessionmaker() as db:
        (offer,) = await EventRepository(db).offers_for(house.household.id, Slot.LUNCH, DAY)
    return offer.id


async def test_home_and_the_command_open_the_offer_modal(slack: Slack) -> None:
    await slack.click("Kim", ids.MAKE_OFFER, ids.MAKE_OFFER, home=True)
    await slack.command("Kim", "offer")
    opened = slack.fake.of("views.open")
    assert [c.args["view"]["callback_id"] for c in opened] == [ids.OFFER_VIEW, ids.OFFER_VIEW]
    home, command = (ids.FormOrigin.decode(c.args["view"]["private_metadata"]) for c in opened)
    assert home == ids.FormOrigin(None, home=True) and command == ids.FormOrigin(CHANNEL)
    view = opened[0].args["view"]
    by_block = {b.get("block_id"): b for b in view["blocks"]}
    assert by_block["slot"]["element"]["initial_option"]["value"] == "lunch"  # before lunch's usual time
    assert by_block["mode"]["element"]["initial_option"]["value"] == "pickup"  # lunch's default mode (FR-1)
    assert by_block["at"]["element"]["initial_time"] == "12:30"
    await slack.click("Kim", ids.MAKE_OFFER, "tampered", home=True)
    assert len(slack.fake.of("views.open")) == 2


async def test_a_lunch_offer_fills_from_the_channel_and_tells_the_offerer(slack: Slack, house: House) -> None:
    siam = house.items["Siam Garden"].id
    response = await submit_form(slack, "Alex", ids.OFFER_VIEW, offer_values(siam), ids.FormOrigin(CHANNEL))
    assert response.status == 200
    assert any("Posted your lunch offer" in text for text in slack.ephemerals("Alex"))
    await slack.drain()
    (posted,) = [c for c in slack.fake.of("chat.postMessage") if c.args["channel"] == CHANNEL]
    assert "Alex is picking up" in posted.args["text"] and "3 servings spare" in posted.args["text"]
    message = {"blocks": blocks_of(posted.args)}
    value = buttons(message)[ids.JOIN_OFFER]
    event_id = await offer_id(house)
    assert value == f"{event_id}:1" and buttons(message)[ids.ADD_GUEST] == value

    await slack.click("Sam", ids.JOIN_OFFER, value)
    await slack.click("Kim", ids.ADD_GUEST, value)
    assert any("You're in for Siam Garden with 1 guest" in t for t in slack.ephemerals("Kim"))
    await slack.click("Lee", ids.JOIN_OFFER, value)
    assert slack.ephemerals("Lee")[-1] == FULL
    slack.fake.clear()
    await slack.drain()
    dms = [c for c in slack.fake.of("chat.postMessage") if c.args["channel"] == SLACK_IDS["Alex"]]
    assert len(dms) == 1 and "full" in dms[0].args["text"]
    blocks = blocks_of(slack.fake.of("chat.update")[-1].args)
    assert set(buttons({"blocks": blocks})) >= {ids.LEAVE_OFFER} and ids.JOIN_OFFER not in buttons({"blocks": blocks})

    await slack.click("Sam", ids.LEAVE_OFFER, value)  # a seat opens again
    assert "not joining" in slack.ephemerals("Sam")[-1]
    await slack.click("Sam", ids.JOIN_OFFER, "999:1")  # someone else's or no offer
    assert slack.ephemerals("Sam")[-1] == "No such call."


async def test_a_join_from_home_and_a_stale_button(slack: Slack, house: House) -> None:
    async with house.sessionmaker() as db:
        posted = await offers.post_offer(
            db,
            house["Alex"],
            offers.OfferInput(
                slot=Slot.LUNCH,
                day=DAY,
                mode=Mode.PICKUP,
                at=time(12, 30),
                capacity=2,
                item_id=house.items["Siam Garden"].id,
            ),
            now=house.clock(),
        )
    await slack.open_home("Kim")
    home = slack.last_home("Kim")
    value = buttons(home)[ids.JOIN_OFFER]
    await slack.click("Kim", ids.JOIN_OFFER, value, home=True)
    home = slack.last_home("Kim")
    assert "You're in for Siam Garden" in json.dumps(home)
    assert ids.LEAVE_OFFER in buttons(home) and ids.JOIN_OFFER not in buttons(home)
    await slack.click("Kim", ids.JOIN_OFFER, f"{posted.event_id}:2", home=True)  # another run
    assert "can't happen anymore" in json.dumps(slack.last_home("Kim"))


async def test_a_done_offer_asks_how_it_was(slack: Slack, house: House) -> None:
    siam = house.items["Siam Garden"].id
    await submit_form(slack, "Alex", ids.OFFER_VIEW, offer_values(siam), ids.FormOrigin(CHANNEL))
    event_id = await offer_id(house)
    await slack.click("Kim", ids.JOIN_OFFER, f"{event_id}:1")
    house.clock.now = LUNCH_AT + timedelta(hours=2)
    await house.tick()
    assert (await house.event(event_id)).state is EventState.DONE
    slack.fake.clear()
    await slack.drain()
    edits = slack.fake.of("chat.update")
    blocks = blocks_of(edits[-1].args)
    assert buttons({"blocks": blocks})[ids.FEEDBACK] == f"{event_id}:1"
    await slack.click("Kim", ids.FEEDBACK, f"{event_id}:1")
    (opened,) = slack.fake.of("views.open")
    assert opened.args["view"]["callback_id"] == ids.FEEDBACK_VIEW


async def test_the_offer_modal_flags_fields_before_closing(slack: Slack, house: House) -> None:
    siam = house.items["Siam Garden"].id
    response = await submit_form(
        slack, "Kim", ids.OFFER_VIEW, offer_values(siam, capacity="0"), ids.FormOrigin(CHANNEL)
    )
    assert json.loads(response.body)["errors"] == {"capacity": "Enter a whole number from 1 to 20."}
    both = offer_values(siam, free_text={"value": "Pho"})
    response = await submit_form(slack, "Kim", ids.OFFER_VIEW, both, ids.FormOrigin(CHANNEL))
    assert "free_text" in json.loads(response.body)["errors"]
    payload = view_submission(SLACK_IDS["Kim"], ids.OFFER_VIEW, offer_values(siam), '{"event": 1}')
    response = await send(slack.conn.app, "interactive", payload)  # metadata this app didn't write
    await slack.conn.interactions.settle()
    assert response.status == 200 and await house.kinds() == []


async def test_an_unlinked_person_cant_offer(slack: Slack, house: House) -> None:
    siam = house.items["Siam Garden"].id
    payload = view_submission("U09STRANGER", ids.OFFER_VIEW, offer_values(siam), ids.FormOrigin(CHANNEL).encode())
    await send(slack.conn.app, "interactive", payload)
    await slack.conn.interactions.settle()
    assert await house.kinds() == []
    await slack.click("U09STRANGER", ids.MAKE_OFFER, ids.MAKE_OFFER, home=True)
    assert slack.fake.of("views.open") == []


async def test_log_opens_a_modal_and_logs_the_meal(slack: Slack, house: House) -> None:
    await slack.command("Sam", "log")
    (opened,) = slack.fake.of("views.open")
    view = opened.args["view"]
    assert view["callback_id"] == ids.LOG_VIEW
    who = next(b for b in view["blocks"] if b.get("block_id") == "who")
    assert who["element"]["initial_options"] == [
        {"text": {"type": "plain_text", "text": "Sam", "emoji": True}, "value": str(house["Sam"].id)}
    ]
    values = state(
        item={"selected_option": {"value": str(house.items["Taqueria"].id)}},
        slot={"selected_option": {"value": "dinner"}},
        day={"selected_date": (DAY - timedelta(days=1)).isoformat()},
        who={"selected_options": [{"value": str(house["Sam"].id)}, {"value": str(house["Kim"].id)}]},
    )
    await submit_form(slack, "Sam", ids.LOG_VIEW, values, ids.FormOrigin(CHANNEL))
    (meal,) = await house.meals()
    assert (meal.item_id, meal.logged_by, meal.event_id) == (house.items["Taqueria"].id, house["Sam"].id, None)
    assert any("Logged Taqueria for dinner" in t for t in slack.ephemerals("Sam"))
    future = dict(values, day={"value": {"selected_date": (DAY + timedelta(days=1)).isoformat()}})
    await submit_form(slack, "Sam", ids.LOG_VIEW, future, ids.FormOrigin(CHANNEL))
    assert "not the future" in slack.ephemerals("Sam")[-1]
    response = await submit_form(
        slack, "Sam", ids.LOG_VIEW, dict(values, who={"value": {"selected_options": []}}), ids.FormOrigin(CHANNEL)
    )
    assert "who" in json.loads(response.body)["errors"]


async def test_the_offerers_own_tap_says_its_their_offer(slack: Slack, house: House) -> None:
    siam = house.items["Siam Garden"].id
    await submit_form(slack, "Alex", ids.OFFER_VIEW, offer_values(siam), ids.FormOrigin(CHANNEL))
    event_id = await offer_id(house)
    await slack.click("Alex", ids.JOIN_OFFER, f"{event_id}:1")
    assert slack.ephemerals("Alex")[-1] == "It's your offer, so you're in. 3 spare."
    await slack.click("Alex", ids.ADD_GUEST, f"{event_id}:1")
    assert slack.ephemerals("Alex")[-1] == "It's your offer, so you're in, with 1 guest. 2 spare."


def test_home_keeps_within_100_blocks_with_the_footer_last() -> None:
    """However many calls and offers there are, Home leaves room for the version footer and
    keeps the start buttons and links; the ones that don't fit are counted and left to the web."""
    from dataclasses import replace as copy

    from dinnerbell.channels.slack.blocks import MAX_VIEW_BLOCKS, with_version_footer
    from dinnerbell.channels.slack.preview import AWAITING, OFFER_VIEW, SCHEDULED_VIEW
    from dinnerbell.channels.slack.views import HomeLinks, home_view
    from dinnerbell.services.event_views import TodayView

    offers_ = [copy(OFFER_VIEW, event=copy(OFFER_VIEW.event, id=100 + n)) for n in range(60)]
    today = TodayView(DAY, [*offers_, SCHEDULED_VIEW], tuple(Slot))
    view = with_version_footer(
        home_view(today, HomeLinks("https://dinnerbell.example.com"), notice="Hi", awaiting=AWAITING), "0.5.0"
    )
    blocks = view["blocks"]
    assert MAX_VIEW_BLOCKS - 2 <= len(blocks) <= MAX_VIEW_BLOCKS  # cards are two blocks each
    assert blocks[-1]["elements"][0]["text"].startswith("Dinnerbell 0.5.0")
    assert any(b.get("block_id") == "start" for b in blocks)
    assert "Today on the web" in json.dumps(blocks[-2])
    shown = sum(1 for b in blocks if str(b.get("block_id", "")).startswith("event-actions-"))
    assert f"…and {61 - shown} more" in json.dumps(blocks, ensure_ascii=False)
