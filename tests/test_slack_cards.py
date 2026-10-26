# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Your answer card and the reminder that shows your answer (spec section 10, issue #42).

A tap on the call message posts a card only the tapper sees, with their own answer and offers
marked. A tap on the card replaces it in place through that tap's ``response_url``; a tap on the
reminder edits the reminder with ``chat.update``. The call message's own ``response_url`` is
never used, because it would rewrite the call for everyone. Every card button names its run,
and one for a call that has moved on says so instead of acting."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.api import WEBHOOK_LOG, SlackApi, SlackUnavailable
from dinnerbell.channels.slack.blocks import section
from dinnerbell.channels.slack.interactions import OUT_OF_DATE, WRONG_PLACE, Source, Tap
from dinnerbell.channels.slack.messages import MOVED_ON, Message
from dinnerbell.config import Settings
from dinnerbell.domain.events import EventState, RoleOffer, Status
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.notifications import PermanentDeliveryError
from dinnerbell.services import calls
from dinnerbell.services.database import Database, open_database
from tests.support.calls import OPENS, REMIND, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import BOT_TOKEN, CHANNEL, RESPONSE_URL, Failure, FakeSlack, block_action, send, state
from tests.support.slack import Slack, wire

pytestmark = pytest.mark.anyio


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


async def open_call(house: House) -> int:
    house.clock.now = OPENS
    await house.tick()
    return await dinner_id(house)


def selected(card: dict[str, Any]) -> list[str]:
    """The texts of a card's selected buttons."""
    return [
        e["text"]["text"] for b in card["blocks"] if b["type"] == "actions" for e in b["elements"] if e.get("style")
    ]


def action_ids(card: dict[str, Any]) -> list[str]:
    return [e["action_id"] for b in card["blocks"] if b["type"] == "actions" for e in b["elements"]]


def text_of(card: dict[str, Any]) -> str:
    return json.dumps(card["blocks"], ensure_ascii=False)


async def response(house: House, event_id: int, name: str) -> tuple[Status | None, frozenset[RoleOffer]]:
    mine = (await house.context(event_id)).response_of(house[name].id)
    return (None, frozenset()) if mine is None else (mine.status, mine.offers)


# A tap on the call message -------------------------------------------------------------------------


async def test_each_person_sees_only_their_own_choices(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    e = str(event_id)
    await slack.click("Kim", "respond_in", e)
    await slack.click("Sam", "respond_maybe", e)
    await slack.click("Sam", "offer_cook", e)
    (kim,) = slack.cards("Kim")
    assert selected(kim) == ["✓ In"]
    assert "Your answer: *In*" in text_of(kim)
    assert selected(slack.cards("Sam")[-1]) == ["✓ Maybe", "✓ I can cook"]
    assert "can cook" not in kim["blocks"][0]["text"]["text"]
    # Only Kim and Sam were shown anything, and only as ephemerals to themselves.
    assert {c.args["user"] for c in slack.fake.of("chat.postEphemeral")} == {"U03KIM", "U02SAM"}
    assert slack.fake.of("response_url", "chat.postMessage", "chat.update") == []


def call_buttons(blocks: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [(e["action_id"], e.get("style")) for b in blocks if b["type"] == "actions" for e in b["elements"]]


async def test_the_call_message_offers_one_answer_button_and_marks_nobody(slack: Slack, house: House) -> None:
    """The choices are on each person's card; the shared message only leads there (#52)."""
    event_id = await open_call(house)
    await slack.drain()
    (post,) = slack.fake.of("chat.postMessage")
    assert call_buttons(post.args["blocks"]) == [("answer", "primary"), ("link_event", None), ("call_menu", None)]
    assert "✓" not in json.dumps(post.args["blocks"], ensure_ascii=False)
    (answer,) = [
        e for b in post.args["blocks"] if b["type"] == "actions" for e in b["elements"] if e["action_id"] == "answer"
    ]
    assert answer["value"] == ids.card_value(event_id, 1) and answer["accessibility_label"].startswith("Answer the")
    await slack.click("Kim", "answer", answer["value"])
    await slack.tap_card("Kim", "card_in", slack.cards("Kim")[-1])
    await slack.drain()
    (update,) = slack.fake.of("chat.update")
    assert call_buttons(update.args["blocks"]) == [("answer", "primary"), ("link_event", None), ("call_menu", None)]


async def test_answer_posts_your_card_and_changes_nothing(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    async with house.sessionmaker() as db:
        await calls.respond(db, house["Kim"], event_id, Status.MAYBE, now=house.clock())
        await calls.offer_role(db, house["Kim"], event_id, RoleOffer.ORDER, True, now=house.clock())
    slack.fake.clear()
    await slack.click("Kim", "answer", ids.card_value(event_id, 1))
    (card,) = slack.cards("Kim")
    assert selected(card) == ["✓ Maybe", "✓ I'll order"]
    assert "card_offer_cook" in action_ids(card) and "card_details" in action_ids(card)
    assert slack.fake.of("response_url", "chat.postMessage") == []  # never the call message's response_url
    assert await response(house, event_id, "Kim") == (Status.MAYBE, frozenset({RoleOffer.ORDER}))
    assert slack.conn.interactions.card_locks == {}  # it went through the card lock, and let it go

    # Every change then happens on the card, which is replaced in place.
    await slack.tap_card("Kim", "card_in", card)
    assert len(slack.fake.of("chat.postEphemeral")) == 1 and len(slack.fake.of("response_url")) == 1


async def test_answer_on_a_call_message_from_before_a_restart_says_it_moved_on(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    async with house.sessionmaker() as db:
        await calls.cancel_call(db, house["Alex"], event_id, now=house.clock())
    await slack.click("Lee", ids.ACTION_FOR_START[Slot.DINNER], "dinner", home=True)
    assert (await house.event(event_id)).run == 2
    slack.fake.clear()
    await slack.click("Kim", "answer", ids.card_value(event_id, 1))  # run 1's message, not yet redrawn
    (card,) = slack.cards("Kim")
    assert f"*{MOVED_ON}* It started again" in text_of(card) and action_ids(card) == ["link_event"]
    assert slack.fake.of("response_url") == []
    await slack.click("Kim", "answer", ids.card_value(event_id, 2))  # the new message's works
    assert "card_in" in action_ids(slack.cards("Kim")[-1])


async def test_answer_after_responses_close_says_the_call_moved_on(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Alex", "offer_pickup", str(event_id))
    await slack.click("Kim", "respond_in", str(event_id))
    house.clock.now = RESPOND_BY
    await house.tick()
    slack.fake.clear()
    await slack.click("Kim", "answer", ids.card_value(event_id, 1))
    (card,) = slack.cards("Kim")
    assert f"*{MOVED_ON}* Thanks for answering!" in text_of(card) and action_ids(card) == ["link_event"]


@pytest.mark.parametrize(
    "where",
    [{"ephemeral": True}, {"home": True}, {"channel": "D03KIM"}, {"channel": "C0ELSEWHERE"}],
)
async def test_answer_works_only_on_the_call_message(slack: Slack, house: House, where: dict[str, Any]) -> None:
    """Anywhere else it's refused, rather than falling through to a card or a direct message
    with the card's text (#54)."""
    event_id = await open_call(house)
    await slack.drain()
    slack.fake.clear()
    await slack.click("Kim", "answer", ids.card_value(event_id, 1), **where)
    assert slack.cards("Kim") == []
    assert slack.fake.of("response_url") == []
    if where.get("channel") == "C0ELSEWHERE":  # refused before it's read, by a direct message
        assert [c.args["text"] for c in slack.fake.of("chat.postMessage")] == [WRONG_PLACE]
        return
    assert slack.fake.of("chat.postMessage") == []
    if where.get("home"):
        assert OUT_OF_DATE in json.dumps(slack.last_home("Kim"))
    else:
        assert slack.ephemerals("Kim") == [OUT_OF_DATE]


@pytest.mark.parametrize("value", [None, "", "7", "7:0", "x:1", "1:1:1"])
async def test_an_answer_value_this_app_didnt_write_is_refused(slack: Slack, house: House, value: str | None) -> None:
    await open_call(house)
    await slack.click("Kim", "answer", value)
    assert slack.ephemerals("Kim") == [OUT_OF_DATE]


async def test_older_call_messages_buttons_still_post_a_card(slack: Slack, house: House) -> None:
    """A call message posted before #52 keeps In, Maybe, Out, the offers, and Details until it's
    redrawn, and they still work."""
    event_id = await open_call(house)
    e = str(event_id)
    await slack.click("Kim", "respond_maybe", e)
    await slack.click("Kim", "offer_cook", e)
    assert [selected(card) for card in slack.cards("Kim")] == [["✓ Maybe"], ["✓ Maybe", "✓ I can cook"]]
    await slack.click("Kim", "coordinate", e)
    assert "You're coordinating." in text_of(slack.cards("Kim")[-1])
    await slack.click("Kim", "details", e)
    assert len(slack.fake.of("views.open")) == 1
    assert slack.fake.of("response_url") == []


async def test_a_tap_on_the_call_message_never_replaces_through_its_response_url(slack: Slack, house: House) -> None:
    """Slack sends a response_url with the call message's taps too; replacing through it would
    rewrite the call for everyone, so each tap there posts a card instead."""
    event_id = await open_call(house)
    for action in ("respond_in", "offer_order", "coordinate"):
        await slack.click("Kim", action, str(event_id))
    assert slack.fake.of("response_url") == []
    assert len(slack.cards("Kim")) == 3
    assert "You're coordinating." in text_of(slack.cards("Kim")[-1])


# A tap on the card ---------------------------------------------------------------------------------


async def test_a_tap_on_the_card_replaces_it_and_adds_no_message(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Kim", "respond_maybe", str(event_id))
    (card,) = slack.cards("Kim")
    slack.fake.clear()

    await slack.tap_card("Kim", "card_in", card)
    assert await response(house, event_id, "Kim") == (Status.IN, frozenset())
    (replaced,) = slack.fake.of("response_url")
    assert replaced.args["url"] == RESPONSE_URL
    assert replaced.args["replace_original"] is True and replaced.args["response_type"] == "ephemeral"
    assert selected(replaced.args) == ["✓ In"]
    assert slack.fake.of("chat.postEphemeral", "chat.postMessage") == []


async def test_a_role_tap_on_the_card_toggles_its_check(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Kim", "respond_in", str(event_id))
    card = slack.cards("Kim")[-1]
    assert "card_offer_cook" in action_ids(card)

    await slack.tap_card("Kim", "card_offer_cook", card)
    offered = slack.cards("Kim")[-1]
    assert selected(offered) == ["✓ In", "✓ I can cook"]
    assert "card_withdraw_cook" in action_ids(offered) and "card_offer_cook" not in action_ids(offered)
    assert await response(house, event_id, "Kim") == (Status.IN, frozenset({RoleOffer.COOK}))

    await slack.tap_card("Kim", "card_withdraw_cook", offered)
    assert selected(slack.cards("Kim")[-1]) == ["✓ In"]
    assert await response(house, event_id, "Kim") == (Status.IN, frozenset())


async def test_a_double_tap_on_the_card_offers_once(slack: Slack, house: House) -> None:
    """The card's buttons say which way they go, so the second tap of a double tap, on the
    card as it was, can't take the offer back as a toggle would."""
    event_id = await open_call(house)
    await slack.click("Kim", "respond_in", str(event_id))
    card = slack.cards("Kim")[-1]
    await slack.tap_card("Kim", "card_offer_pickup", card)
    await slack.tap_card("Kim", "card_offer_pickup", card)
    assert await response(house, event_id, "Kim") == (Status.IN, frozenset({RoleOffer.PICKUP}))
    assert selected(slack.cards("Kim")[-1]) == ["✓ In", "✓ I can pick up"]


async def test_coordinating_from_the_card(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Kim", "respond_in", str(event_id))
    await slack.click("Sam", "respond_in", str(event_id))
    kim, sam = slack.cards("Kim")[-1], slack.cards("Sam")[-1]
    await slack.tap_card("Kim", "card_coordinate", kim)
    assert (await house.event(event_id)).coordinator_id == house["Kim"].id
    mine = slack.cards("Kim")[-1]
    assert "You're coordinating." in text_of(mine) and "card_coordinate" not in action_ids(mine)

    await slack.tap_card("Sam", "card_coordinate", sam)  # drawn before Kim claimed it
    told = slack.cards("Sam")[-1]
    assert "Someone is already coordinating." in text_of(told)  # on the card, not a new message
    assert "*Coordinator:* Kim" in text_of(told) and "card_coordinate" not in action_ids(told)
    assert slack.ephemerals("Sam") == [slack.cards("Sam")[0]["text"]]


async def test_a_second_coordinate_tap_from_the_coordinator_changes_nothing(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Kim", "respond_in", str(event_id))
    card = slack.cards("Kim")[-1]
    await slack.tap_card("Kim", "card_coordinate", card)
    await slack.tap_card("Kim", "card_coordinate", card)  # the double tap, on the card as it was
    last = slack.cards("Kim")[-1]
    assert "You're coordinating." in text_of(last) and "already coordinating" not in text_of(last)
    assert len(await house.audits("event.coordinator_claimed")) == 1


async def test_two_quick_taps_land_in_order(slack: Slack, house: House) -> None:
    """The first tap's replace is held up in Slack; the second waits for it, so the card the
    person ends up with shows their last answer."""
    event_id = await open_call(house)
    await slack.click("Kim", "respond_in", str(event_id))
    value = ids.card_value(event_id, 1)
    slack.fake.clear()
    gate = slack.fake.gates["response_url"] = asyncio.Event()
    locks = slack.conn.interactions.card_locks
    key = ("U03KIM", event_id)

    async def until(condition: Callable[[], bool]) -> None:
        for _ in range(1000):  # up to 10 seconds on a slow runner
            if condition():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out")

    await send(slack.conn.app, "interactive", block_action("U03KIM", "card_maybe", value, ephemeral=True))
    await until(lambda: bool(slack.fake.of("response_url")))  # the first holds the lock, waiting on Slack
    await send(slack.conn.app, "interactive", block_action("U03KIM", "card_out", value, ephemeral=True))
    await until(lambda: key in locks and locks[key].users == 2)  # the second waits for it
    assert len(slack.fake.of("response_url")) == 1
    assert await response(house, event_id, "Kim") == (Status.MAYBE, frozenset())  # the second hasn't run
    assert list(locks) == [key]
    gate.set()
    await slack.conn.interactions.settle()
    assert [selected(r.args) for r in slack.fake.of("response_url")] == [["✓ Maybe"], ["✓ Out"]]
    assert await response(house, event_id, "Kim") == (Status.OUT, frozenset())
    assert slack.conn.interactions.card_locks == {}  # dropped once nobody holds it


async def test_send_card_replaces_only_an_ephemeral_in_the_channel(slack: Slack, house: House) -> None:
    """``_send_card``'s own guards, apart from ``_card_tap``'s: never through the response_url
    of a message everyone sees, or one outside the household channel."""
    card = Message("Your answer", [section("Your answer")])
    send_card = slack.conn.interactions._send_card
    kim = house["Kim"]
    shared = Tap(message_ts="1.000100", ephemeral=False, response_url=RESPONSE_URL)
    await send_card(Source("U03KIM", channel_id=CHANNEL), card, kim, shared)
    elsewhere = Tap(message_ts="1.000100", ephemeral=True, response_url=RESPONSE_URL)
    await send_card(Source("U03KIM", channel_id="C0ELSEWHERE"), card, kim, elsewhere)
    await send_card(Source("U03KIM", channel_id=CHANNEL, home=True), card, kim, elsewhere)
    assert slack.fake.of("response_url") == []
    await send_card(Source("U03KIM", channel_id=CHANNEL), card, kim, elsewhere)
    assert len(slack.fake.of("response_url")) == 1


async def test_a_card_slack_wont_replace_gives_way_to_a_new_one(slack: Slack, house: House) -> None:
    """Slack allows a response_url 5 uses within 30 minutes; after that, post a fresh card."""
    event_id = await open_call(house)
    await slack.click("Kim", "respond_in", str(event_id))
    card = slack.cards("Kim")[-1]
    slack.fake.clear()
    slack.fake.fail("response_url", Failure("expired_url", status=404), Failure(raises=OSError("boom")))
    await slack.tap_card("Kim", "card_maybe", card)
    await slack.tap_card("Kim", "card_out", card)
    posted = slack.fake.of("chat.postEphemeral")
    assert [selected({"blocks": c.args["blocks"]}) for c in posted] == [["✓ Maybe"], ["✓ Out"]]
    assert await response(house, event_id, "Kim") == (Status.OUT, frozenset())


async def test_a_card_that_cant_be_posted_comes_as_a_direct_message(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    slack.fake.fail("chat.postEphemeral", Failure("user_not_in_channel"))
    await slack.click("Kim", "respond_in", str(event_id))
    (dm,) = slack.fake.of("chat.postMessage")
    assert dm.args["channel"] == "U03KIM" and dm.args["text"] == "Dinner call · Thursday, Oct 22: your answer is In"
    slack.fake.clear()
    slack.fake.fail("chat.postEphemeral", Failure("user_not_in_channel"))
    slack.fake.fail("chat.postMessage", Failure("messages_tab_disabled"))
    await slack.click("Kim", "respond_maybe", str(event_id))  # nowhere to say it; the answer still counts
    assert await response(house, event_id, "Kim") == (Status.MAYBE, frozenset())


@pytest.mark.parametrize(
    "url",
    [
        None,
        "http://hooks.slack.com/actions/T0HOUSE/1/x",
        "https://hooks.slack.com.evil.example/actions/T0HOUSE/1/x",
        "https://evil.example/actions/T0HOUSE/1/x",
        "https://user@hooks.slack.com/actions/T0HOUSE/1/x",
        "https://hooks.slack.com:8443/actions/T0HOUSE/1/x",
        "https://hooks.slack.com/commands/T0HOUSE/1/x",
    ],
)
async def test_a_response_url_that_isnt_slacks_is_never_called(slack: Slack, house: House, url: str | None) -> None:
    event_id = await open_call(house)
    await slack.click("Kim", "respond_in", str(event_id))
    card = slack.cards("Kim")[-1]
    slack.fake.clear()
    await slack.tap_card("Kim", "card_maybe", card, response_url=url)
    assert slack.fake.of("response_url") == []
    (posted,) = slack.fake.of("chat.postEphemeral")
    assert posted.args["user"] == "U03KIM"


async def test_card_buttons_work_only_on_a_card(slack: Slack, house: House) -> None:
    """A card button arriving from the call message, or from Home, isn't something this app drew."""
    event_id = await open_call(house)
    value = ids.card_value(event_id, 1)
    await slack.click("Kim", "card_in", value)  # not ephemeral: the call message
    assert slack.ephemerals("Kim") == [OUT_OF_DATE]
    await slack.click("Kim", "card_in", value, home=True)
    assert OUT_OF_DATE in json.dumps(slack.last_home("Kim"))
    assert slack.fake.of("response_url") == []
    assert await response(house, event_id, "Kim") == (None, frozenset())


@pytest.mark.parametrize("value", ["7", "7:0", "0:1", "x:1", "7:1:1", "", None, "99999999999999999999:1"])
async def test_a_card_value_this_app_didnt_write_is_refused(slack: Slack, house: House, value: str | None) -> None:
    await open_call(house)
    await slack.click("Kim", "card_in", value, ephemeral=True)
    assert slack.ephemerals("Kim") == [OUT_OF_DATE]
    assert slack.fake.of("response_url") == []


# A card for a call that has moved on ----------------------------------------------------------------


async def test_a_card_after_responses_close_says_the_call_moved_on(
    slack: Slack, house: House, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="dinnerbell")
    event_id = await open_call(house)
    await slack.click("Alex", "offer_pickup", str(event_id))
    await slack.click("Kim", "respond_in", str(event_id))
    card = slack.cards("Kim")[-1]
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    slack.fake.clear()

    await slack.tap_card("Kim", "card_out", card)
    assert await response(house, event_id, "Kim") == (Status.IN, frozenset())
    (replaced,) = slack.fake.of("response_url")
    assert f"*{MOVED_ON}* Thanks for answering! Vote on the picks" in text_of(replaced.args)
    assert "Your answer: *In*" in text_of(replaced.args)
    assert action_ids(replaced.args) == ["link_event"]
    assert "closed" not in text_of(replaced.args)  # the card says it once, not with the rule's words too
    # #74: why the tap changed nothing isn't swallowed: it's a private line of its own, and logged.
    assert slack.ephemerals("Kim") == ["Responses are closed for this call."]
    (refused,) = [r for r in caplog.records if r.getMessage() == "slack card tap refused"]
    assert (refused.levelno, refused.member_id, refused.event_id, refused.reason, refused.refusal) == (  # type: ignore[attr-defined]
        logging.INFO,
        house["Kim"].id,
        event_id,
        "moved_on",
        "Responses are closed for this call.",
    )


async def test_a_card_from_before_a_restart_doesnt_answer_the_new_run(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Kim", "respond_maybe", str(event_id))
    card = slack.cards("Kim")[-1]
    async with house.sessionmaker() as db:
        await calls.cancel_call(db, house["Alex"], event_id, now=house.clock())
    await slack.click("Lee", ids.ACTION_FOR_START[Slot.DINNER], "dinner", home=True)
    assert ((await house.event(event_id)).state, (await house.event(event_id)).run) == (EventState.COLLECTING, 2)
    before = await response(house, event_id, "Kim")
    slack.fake.clear()

    for action in ("card_in", "card_offer_cook", "card_coordinate", "card_details"):
        await slack.tap_card("Kim", action, card)
    assert await response(house, event_id, "Kim") == before
    assert (await house.event(event_id)).coordinator_id is None
    assert slack.fake.of("views.open") == []
    replaced = slack.fake.of("response_url")
    assert len(replaced) == 4
    assert all("It started again" in text_of(r.args) for r in replaced)
    assert all(action_ids(r.args) == ["link_event"] for r in replaced)


async def test_a_called_off_call_shows_on_the_card(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Kim", "respond_in", str(event_id))
    card = slack.cards("Kim")[-1]
    async with house.sessionmaker() as db:
        await calls.cancel_call(db, house["Alex"], event_id, note="<!here> pizza *now*", now=house.clock())
    slack.fake.clear()
    await slack.tap_card("Kim", "card_maybe", card)
    (replaced,) = slack.fake.of("response_url")
    text = replaced.args["blocks"][0]["text"]["text"]
    assert "Called off by Alex. “&lt;!here&gt; pizza *now*”" in text
    assert "<!here>" not in json.dumps(replaced.args)


# Details from the card ---------------------------------------------------------------------------


async def test_details_from_the_card_name_the_run(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Kim", "respond_in", str(event_id))
    card = slack.cards("Kim")[-1]
    slack.fake.clear()
    await slack.tap_card("Kim", "card_details", card)
    (opened,) = slack.fake.of("views.open")
    origin = ids.ViewOrigin.decode(opened.args["view"]["private_metadata"])
    assert origin == ids.ViewOrigin(event_id, CHANNEL, run=1)
    assert slack.fake.of("response_url", "chat.postEphemeral") == []

    await slack.submit("Kim", ids.DETAILS_VIEW, state(guests={"value": "2"}), event_id, origin=origin)
    assert (await house.context(event_id)).response_of(house["Kim"].id).guests == 2  # type: ignore[union-attr]
    stale = ids.ViewOrigin(event_id, CHANNEL, run=2)
    await slack.submit("Kim", ids.DETAILS_VIEW, state(guests={"value": "3"}), event_id, origin=stale)
    assert (await house.context(event_id)).response_of(house["Kim"].id).guests == 2  # type: ignore[union-attr]
    assert slack.ephemerals("Kim") == ["That can't happen anymore."]


async def test_details_from_the_call_message_cant_save_into_a_restarted_call(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Kim", "details", str(event_id))
    origin = ids.ViewOrigin.decode(slack.fake.of("views.open")[-1].args["view"]["private_metadata"])
    assert origin.run == 1
    async with house.sessionmaker() as db:
        await calls.cancel_call(db, house["Alex"], event_id, now=house.clock())
    await slack.click("Lee", ids.ACTION_FOR_START[Slot.DINNER], "dinner", home=True)
    assert (await house.event(event_id)).run == 2
    slack.fake.clear()
    await slack.submit("Kim", ids.DETAILS_VIEW, state(guests={"value": "2"}), event_id, origin=origin)
    assert (await house.context(event_id)).response_of(house["Kim"].id) is None
    assert slack.ephemerals("Kim") == ["That can't happen anymore."]


# The reminder -------------------------------------------------------------------------------------


async def reminder_for(slack: Slack, house: House, who: str) -> tuple[int, dict[str, Any], str]:
    """Open the call, send the reminders, and return the call, ``who``'s reminder, and its ts."""
    event_id = await open_call(house)
    house.clock.now = REMIND
    await house.tick()
    await slack.drain()
    user = {"Lee": "U04LEE", "Kim": "U03KIM"}[who]
    (post,) = [c for c in slack.fake.of("chat.postMessage") if c.args["channel"] == user]
    index = slack.fake.calls.index(post)
    ts = f"{sum(1 for c in slack.fake.calls[: index + 1] if c.method == 'chat.postMessage')}.000100"
    return event_id, post.args, ts


async def test_a_tap_on_the_reminder_edits_the_reminder(slack: Slack, house: House) -> None:
    event_id, reminder, ts = await reminder_for(slack, house, "Lee")
    assert selected(reminder) == []  # nothing looks chosen before Lee answers
    slack.fake.clear()
    await slack.tap_card("Lee", "card_maybe", reminder, channel="D04LEE", ephemeral=False, message_ts=ts)
    assert await response(house, event_id, "Lee") == (Status.MAYBE, frozenset())
    (update,) = slack.fake.of("chat.update")
    assert (update.args["channel"], update.args["ts"]) == ("D04LEE", ts)
    assert selected(update.args) == ["✓ Maybe"]
    assert "Your answer for dinner on Thursday, Oct 22 is *Maybe*" in text_of(update.args)
    assert slack.fake.of("chat.postEphemeral", "response_url", "chat.postMessage") == []


async def test_a_reminder_tapped_after_the_call_moved_on_says_so(slack: Slack, house: House) -> None:
    event_id, reminder, ts = await reminder_for(slack, house, "Lee")
    await slack.click("Alex", "respond_in", str(event_id))
    house.clock.now = RESPOND_BY
    await house.tick()
    slack.fake.clear()
    await slack.tap_card("Lee", "card_in", reminder, channel="D04LEE", ephemeral=False, message_ts=ts)
    assert await response(house, event_id, "Lee") == (None, frozenset())
    (update,) = slack.fake.of("chat.update")
    assert f"*{MOVED_ON}*" in text_of(update.args) and action_ids(update.args) == ["link_event"]
    assert slack.ephemerals("Lee") == ["Responses are closed for this call."]  # not swallowed (#74)


async def test_a_reminder_slack_wont_edit_still_answers(slack: Slack, house: House) -> None:
    event_id, reminder, ts = await reminder_for(slack, house, "Lee")
    slack.fake.clear()
    slack.fake.fail("chat.update", Failure("message_not_found"))
    await slack.tap_card("Lee", "card_in", reminder, channel="D04LEE", ephemeral=False, message_ts=ts)
    assert await response(house, event_id, "Lee") == (Status.IN, frozenset())
    assert slack.ephemerals("Lee") == ["Dinner call · Thursday, Oct 22: your answer is In"]


async def test_a_reminder_tap_without_a_message_ts_answers_in_text(slack: Slack, house: House) -> None:
    event_id, reminder, _ = await reminder_for(slack, house, "Lee")
    slack.fake.clear()
    await slack.tap_card("Lee", "card_out", reminder, channel="D04LEE", ephemeral=False, message_ts="nope")
    assert await response(house, event_id, "Lee") == (Status.OUT, frozenset())
    assert slack.fake.of("chat.update") == []
    assert slack.ephemerals("Lee") == ["Dinner call · Thursday, Oct 22: your answer is Out"]


async def test_a_reminder_button_in_someone_elses_dm_is_refused(slack: Slack, house: House) -> None:
    event_id, reminder, ts = await reminder_for(slack, house, "Lee")
    slack.fake.clear()
    await slack.tap_card("Kim", "card_in", reminder, channel="D04LEE", ephemeral=False, message_ts=ts)
    assert await response(house, event_id, "Kim") == (None, frozenset())
    assert slack.fake.of("chat.update", "response_url") == []


# A labelled instance ------------------------------------------------------------------------------


async def test_cards_and_reminders_carry_the_instance_label(house: House) -> None:
    slack = await wire(house, instance_label="TEST")
    slack.conn.audience.members = frozenset({"U01ALEX", "U02SAM", "U03KIM", "U04LEE"})
    try:
        event_id, reminder, ts = await reminder_for(slack, house, "Lee")
        banner = {"type": "context", "elements": [{"type": "mrkdwn", "text": "*TEST* instance"}]}
        assert reminder["blocks"][0] == banner
        await slack.click("Kim", "respond_in", str(event_id))
        card = slack.cards("Kim")[-1]
        assert card["blocks"][0] == banner
        await slack.tap_card("Kim", "card_out", card)
        assert slack.cards("Kim")[-1]["blocks"][0] == banner
        await slack.tap_card("Lee", "card_in", reminder, channel="D04LEE", ephemeral=False, message_ts=ts)
        assert slack.fake.of("chat.update")[-1].args["blocks"][0] == banner
    finally:
        await slack.conn.stop()


# The response_url call itself ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (Failure("expired_url", status=404), PermanentDeliveryError),
        (Failure("used_url", status=410), PermanentDeliveryError),
        (Failure("<html>oops</html>", status=502), SlackUnavailable),
        (Failure("ratelimited", status=429), SlackUnavailable),
        (Failure(raises=OSError(f"{RESPONSE_URL} {BOT_TOKEN}")), SlackUnavailable),
    ],
)
async def test_response_url_errors_never_carry_the_url(failure: Failure, error: type[Exception]) -> None:
    fake = FakeSlack()
    api = SlackApi(fake, webhook=fake.webhook)
    fake.fail("response_url", failure)
    with pytest.raises(error) as raised:
        await api.replace_original(RESPONSE_URL, {"text": "x", "blocks": []})
    assert "hooks.slack.com" not in str(raised.value) and BOT_TOKEN not in str(raised.value)
    assert "<html>" not in str(raised.value)


def test_the_webhook_logger_never_logs_request_urls() -> None:
    """The SDK logs each request's URL and body at DEBUG when its logger's own level allows."""
    assert WEBHOOK_LOG.level == logging.INFO


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("https://hooks.slack.com/actions/T0HOUSE/1/abc", True),
        ("https://hooks.slack-gov.com/actions/T0HOUSE/1/abc", True),
        ("https://hooks.slack.com/actions/T0HOUSE/1/abc?x=1", False),
        ("https://hooks.slack.com/actions/T0HOUSE/1/abc#x", False),
        ("https://HOOKS.slack.com/actions/T0HOUSE/1/abc", False),
        ("https://hooks.slack.com/actions/" + "a" * 500, False),
        ("https://hooks.slack.com:/actions/T0HOUSE/1/abc", False),
        ("https://[::1/actions/x", False),
        ("https://hooks.slack.com/actions/é", False),
        ("https://hooks.slack.com/actions/T0HOUSE/1/abc\n", False),
        ("https://hooks\t.slack.com/actions/T0HOUSE/1/abc", False),
        ("https://hooks.slack.com\t/actions/T0HOUSE/1/abc", False),
        ("https://hooks.slack.com/actions/../commands/T0HOUSE/1/abc", False),
        ("https://hooks.slack.com/actions/T0HOUSE/../1/abc", False),
        ("\x00https://hooks.slack.com/actions/T0HOUSE/1/abc", False),
        (" https://hooks.slack.com/actions/T0HOUSE/1/abc", False),
        ("https://hooks.slack.com/actions/T0HOUSE/1/abc/extra", False),
        ("https://hooks.slack.com/actions/T0HOUSE/x/abc", False),
        (42, False),
    ],
)
def test_response_url_parsing(value: object, ok: bool) -> None:
    assert (ids.response_url(value) == value) is ok


def test_card_values_round_trip() -> None:
    assert ids.parse_card(ids.card_value(7, 2)) == (7, 2)
    assert ids.message_ts("1712345678.000200") == "1712345678.000200"
    assert ids.message_ts("1.2.3") is None and ids.message_ts(None) is None
