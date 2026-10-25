# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback in Slack (spec 5.9, section 10, FR-47, FR-48): **How was it?** on the done decision,
on the reminder, and on App Home opens the feedback modal; saving it goes through the same
checks as the web. Nothing in Slack says who chose "not again"."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.interactions import FEEDBACK_NONE, FEEDBACK_SAVED, STALE
from dinnerbell.config import Settings
from dinnerbell.db.models import FeedbackRow
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.feedback import NO_PLACE, NOT_A_PARTICIPANT
from dinnerbell.services.database import Database, open_database
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import CHANNEL, state
from tests.support.feedback import done_dinner, remind
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


async def rows(house: House) -> list[FeedbackRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(FeedbackRow).order_by(FeedbackRow.id)))


def buttons(payload: dict[str, Any]) -> dict[str, str]:
    """action_id to value for every button on a message or view, accessories included."""
    found: dict[str, str] = {}
    for block in payload["blocks"]:
        elements = block.get("elements", []) if block["type"] == "actions" else [block.get("accessory") or {}]
        for element in elements:
            if element.get("type") == "button":
                found[element["action_id"]] = element.get("value", "")
    return found


def modal(slack: Slack) -> dict[str, Any]:
    (opened,) = slack.fake.of("views.open")
    view: dict[str, Any] = opened.args["view"]
    return view


def fields(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {b["block_id"]: b["element"] for b in view["blocks"] if b["type"] == "input"}


LOVED = {"selected_option": {"value": "loved"}}
NOT_AGAIN = {"selected_option": {"value": "not_again"}}


async def test_how_was_it_from_the_reminder_and_the_decision(slack: Slack, house: House) -> None:
    await remind(house, "Kim")
    event_id = await done_dinner(house)
    await slack.drain()

    # The done decision carries How was it?, and Kim, who asked for one, gets the reminder.
    (decision,) = [c for c in slack.fake.of("chat.update") if c.args["text"].startswith("Dinner on")]
    value = ids.card_value(event_id, 1)
    assert buttons(decision.args)[ids.FEEDBACK] == value
    (reminder,) = [c for c in slack.fake.of("chat.postMessage") if c.args["text"].startswith("How was")]
    assert reminder.args["channel"] == "U03KIM"
    assert reminder.args["text"] == "How was dinner on Thursday, Oct 22? Tell us by Thursday, Oct 29."
    assert buttons(reminder.args)[ids.FEEDBACK] == value

    # Kim taps it in the DM: a modal with food and the cook, since Sam cooked.
    await slack.click("Kim", ids.FEEDBACK, value, channel="D03KIM", response_url=None)
    view = modal(slack)
    assert view["callback_id"] == ids.FEEDBACK_VIEW
    assert set(fields(view)) == {"food", "cook_thanks", "cook_note"}
    origin = ids.ViewOrigin.decode(view["private_metadata"])
    assert origin == ids.ViewOrigin(event_id, "D03KIM", run=1)

    values = state(food=NOT_AGAIN, cook_thanks={"selected_options": [{"value": "thanks"}]}, cook_note={"value": "Ty!"})
    await slack.submit("Kim", ids.FEEDBACK_VIEW, values, event_id, origin=origin)
    (row,) = await rows(house)
    assert (row.member_id, row.food, row.cook_thanks, row.cook_note) == (house["Kim"].id, "not_again", True, "Ty!")
    assert slack.ephemerals("Kim")[-1] == FEEDBACK_SAVED

    # From the channel's decision message, the modal comes back with Kim's answers.
    slack.fake.clear()
    await slack.click("Kim", ids.FEEDBACK, value)
    prefilled = fields(modal(slack))
    assert prefilled["food"]["initial_option"]["value"] == "not_again"
    assert prefilled["cook_note"]["initial_value"] == "Ty!"
    # The shared decision message never changes with anyone's feedback.
    assert slack.fake.of("chat.update") == []


async def test_the_cook_and_people_who_didnt_eat(slack: Slack, house: House) -> None:
    event_id = await done_dinner(house)
    value = ids.card_value(event_id, 1)
    await slack.click("Lee", ids.FEEDBACK, value)  # Lee didn't eat
    assert slack.fake.of("views.open") == []
    assert slack.ephemerals("Lee")[-1] == NOT_A_PARTICIPANT
    await slack.click("Sam", ids.FEEDBACK, value)  # Sam cooked: food only, no thanking himself
    assert set(fields(modal(slack))) == {"food"}
    await slack.click("Kim", ids.FEEDBACK, ids.card_value(event_id, 2))  # a run that never was
    assert slack.ephemerals("Kim")[-1] == STALE


async def test_a_restaurant_meal_asks_about_the_place(slack: Slack, house: House) -> None:
    event_id = await done_dinner(house, "Siam Garden", Mode.PICKUP)
    await slack.click("Kim", ids.FEEDBACK, ids.card_value(event_id, 1))
    view = modal(slack)
    assert set(fields(view)) == {"food", "place", "place_issues"}
    values = state(place=NOT_AGAIN, place_issues={"selected_options": [{"value": "cold"}, {"value": "late"}]})
    await slack.submit("Kim", ids.FEEDBACK_VIEW, values, event_id, origin=ids.ViewOrigin(event_id, CHANNEL, run=1))
    (row,) = await rows(house)
    assert (row.food, row.place, row.place_issues) == (None, "not_again", ["cold", "late"])


async def test_meals_awaiting_feedback_on_home(slack: Slack, house: House) -> None:
    event_id = await done_dinner(house)
    await slack.open_home("Kim")
    home = slack.last_home("Kim")
    assert buttons(home)[ids.FEEDBACK] == ids.card_value(event_id, 1)
    assert home["blocks"][-1]["elements"][0]["text"].startswith("Dinnerbell ")  # the version stays last
    await slack.open_home("Lee")
    assert ids.FEEDBACK not in buttons(slack.last_home("Lee"))

    await slack.click("Kim", ids.FEEDBACK, ids.card_value(event_id, 1), home=True)
    origin = ids.ViewOrigin.decode(modal(slack)["private_metadata"])
    assert origin.home and origin.run == 1
    await slack.submit("Kim", ids.FEEDBACK_VIEW, state(food=LOVED), event_id, origin=origin)
    home = slack.last_home("Kim")
    assert FEEDBACK_SAVED in json.dumps(home)
    assert ids.FEEDBACK not in buttons(home)  # given, so no longer awaiting


async def test_saving_nothing_and_rules_the_modal_cant_check(slack: Slack, house: House) -> None:
    event_id = await done_dinner(house)
    origin = ids.ViewOrigin(event_id, CHANNEL, run=1)
    await slack.submit("Kim", ids.FEEDBACK_VIEW, state(), event_id, origin=origin)
    assert slack.ephemerals("Kim")[-1] == FEEDBACK_NONE
    await slack.submit("Kim", ids.FEEDBACK_VIEW, state(place=LOVED), event_id, origin=origin)
    assert slack.ephemerals("Kim")[-1] == NO_PLACE
    assert await rows(house) == []

    # Slack can't unselect a radio button, so "No answer" clears a rating, as on the web.
    await slack.submit("Kim", ids.FEEDBACK_VIEW, state(food=LOVED), event_id, origin=origin)
    assert len(await rows(house)) == 1
    await slack.click("Kim", ids.FEEDBACK, ids.card_value(event_id, 1))
    food = fields(modal(slack))["food"]
    assert [o["value"] for o in food["options"]] == ["loved", "fine", "not_again", "none"]
    assert food["initial_option"]["value"] == "loved"
    await slack.submit(
        "Kim", ids.FEEDBACK_VIEW, state(food={"selected_option": {"value": "none"}}), event_id, origin=origin
    )
    assert slack.ephemerals("Kim")[-1] == FEEDBACK_NONE
    assert await rows(house) == []


async def test_tampered_feedback_modals_change_nothing(slack: Slack, house: House) -> None:
    event_id = await done_dinner(house)
    origin = ids.ViewOrigin(event_id, CHANNEL, run=1)
    for values in (
        state(food={"selected_option": {"value": "meh"}}),
        state(place_issues={"selected_options": [{"value": "rude"}]}),
        state(cook_thanks={"selected_options": [{"value": "yes"}]}),
        state(place_issues={"selected_options": "cold"}),
    ):
        response = await slack.submit("Kim", ids.FEEDBACK_VIEW, values, event_id, origin=origin)
        assert response.body == ""
    # A feedback modal always names its run.
    await slack.submit("Kim", ids.FEEDBACK_VIEW, state(food=LOVED), event_id, origin=ids.ViewOrigin(event_id, CHANNEL))
    assert await rows(house) == []
    assert slack.ephemerals("Kim") == []
    too_long = state(cook_note={"value": "x" * 201})
    response = await slack.submit("Kim", ids.FEEDBACK_VIEW, too_long, event_id, origin=origin)
    assert json.loads(response.body) == {
        "response_action": "errors",
        "errors": {"cook_note": "Keep the note to 200 characters."},
    }
    await slack.click("Kim", ids.FEEDBACK, "not-a-value")
    assert slack.fake.of("views.open") == []
