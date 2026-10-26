# SPDX-License-Identifier: AGPL-3.0-or-later
"""Slack interactions through Bolt with fake Socket Mode requests: every button, both modals,
the Home tab, and /dinnerbell, each mapped to one service call (docs/channels.md)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import time

import pytest

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.interactions import (
    BROKEN,
    CONFIRM,
    JOIN_OFFER,
    STALE,
    UNVOTED,
    VOTED,
    parse_call_args,
)
from dinnerbell.config import Settings
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import EventRuleViolation, EventState, IllegalTransition, RoleOffer, Status
from dinnerbell.domain.meals import Slot
from dinnerbell.services import calls
from dinnerbell.services.database import Database, open_database
from tests.support.calls import OPENS, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import Failure, state
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


async def to_voting(slack: Slack) -> int:
    event_id = await open_call(slack.house)
    await slack.click("Alex", "respond_in", str(event_id))
    await slack.click("Alex", "offer_pickup", str(event_id))
    await slack.click("Sam", "respond_in", str(event_id))
    slack.house.clock.now = RESPOND_BY
    await slack.house.tick()
    assert (await slack.house.event(event_id)).state is EventState.VOTING
    return event_id


async def status_of(house: House, event_id: int, name: str) -> Status | None:
    response = (await house.context(event_id)).response_of(house[name].id)
    return None if response is None else response.status


def selected(card: dict[str, object]) -> list[str]:
    """The texts of a card's selected buttons."""
    blocks: list[dict[str, object]] = card["blocks"]  # type: ignore[assignment]
    return [
        e["text"]["text"]
        for b in blocks
        if b["type"] == "actions"
        for e in b["elements"]  # type: ignore[attr-defined]
        if e.get("style") == "primary"
    ]


def body(response: object) -> dict[str, object]:
    raw = getattr(response, "body", "")
    return json.loads(raw) if raw else {}


# Responding --------------------------------------------------------------------------------------


async def test_in_maybe_out_answer_for_the_person_who_tapped(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    for action, status in (("respond_maybe", Status.MAYBE), ("respond_out", Status.OUT), ("respond_in", Status.IN)):
        response = await slack.click("Kim", action, str(event_id))
        assert response.status == 200
        assert await status_of(house, event_id, "Kim") is status
    assert await status_of(house, event_id, "Sam") is None
    # Each tap on the call message posts a Your answer card with Kim's answer marked.
    assert [selected(card) for card in slack.cards("Kim")] == [["✓ Maybe"], ["✓ Out"], ["✓ In"]]
    assert slack.fake.of("response_url") == []  # never the call message's response_url


async def test_answering_from_an_older_reminder_dm_confirms_privately(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    # A reminder sent before cards existed has the call message's buttons, without a run.
    await slack.click("Lee", "respond_in", str(event_id), channel="D04LEE")
    assert await status_of(house, event_id, "Lee") is Status.IN
    assert slack.ephemerals("Lee") == [CONFIRM["respond_in"].format(slot="dinner")]
    assert slack.fake.of("chat.postEphemeral")[0].args["channel"] == "D04LEE"


async def test_role_buttons_toggle(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Sam", "offer_cook", str(event_id))
    response = (await house.context(event_id)).response_of(house["Sam"].id)
    assert response is not None and response.offers == {RoleOffer.COOK} and response.status is Status.IN
    await slack.click("Sam", "offer_cook", str(event_id))
    response = (await house.context(event_id)).response_of(house["Sam"].id)
    assert response is not None and response.offers == frozenset()
    assert [selected(card) for card in slack.cards("Sam")] == [["✓ In", "✓ I can cook"], ["✓ In"]]


async def test_first_to_coordinate_wins_and_the_next_is_told_kindly(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Kim", "coordinate", str(event_id))
    await slack.click("Sam", "coordinate", str(event_id))
    assert (await house.event(event_id)).coordinator_id == house["Kim"].id
    assert slack.ephemerals("Sam") == ["Someone is already coordinating. They can hand it off to you."]


async def test_rule_errors_are_shown_privately(slack: Slack, house: House) -> None:
    event_id = await to_voting(slack)
    slack.fake.clear()
    await slack.click("Kim", "respond_in", str(event_id))
    assert slack.ephemerals("Kim") == ["Responses are closed for this call."]
    assert slack.fake.of("chat.postEphemeral")[0].args["channel"] == "C0DINNER"


# The Details modal -------------------------------------------------------------------------------


async def test_details_opens_a_prefilled_modal(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Sam", "offer_cook", str(event_id))
    slack.fake.clear()
    await slack.click("Sam", "details", str(event_id))
    (call,) = slack.fake.of("views.open")
    view = call.args["view"]
    assert call.args["trigger_id"] == "1234.5678.trigger"
    assert view["callback_id"] == ids.DETAILS_VIEW
    assert ids.ViewOrigin.decode(view["private_metadata"]) == ids.ViewOrigin(event_id, "C0DINNER", run=1)
    assert [b.get("block_id") for b in view["blocks"][1:]] == [
        "guests",
        "eat_from",
        "eat_until",
        "suggest_item",
        "suggest_text",
        "note",
    ]


async def test_saving_details_calls_the_service(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    tacos = house.items["Sam's Tacos"].id
    values = state(
        guests={"type": "number_input", "value": "2"},
        eat_from={"type": "timepicker", "selected_time": "18:30"},
        eat_until={"type": "timepicker", "selected_time": None},
        suggest_item={"type": "static_select", "selected_option": {"value": str(tacos), "text": {}}},
        suggest_text={"type": "plain_text_input", "value": "Pho"},
        note={"type": "plain_text_input", "value": "Back at 6"},
    )
    response = await slack.submit("Sam", ids.DETAILS_VIEW, values, event_id)
    assert response.status == 200 and body(response) == {}
    ctx = await house.context(event_id)
    mine = ctx.response_of(house["Sam"].id)
    assert mine is not None and (mine.status, mine.guests, mine.eat_from, mine.note) == (
        Status.IN,
        2,
        time(18, 30),
        "Back at 6",
    )
    assert {(s.item_id, s.free_text) for s in ctx.suggestions} == {(tacos, None), (None, "Pho")}

    # Prefilled next time.
    slack.fake.clear()
    await slack.click("Sam", "details", str(event_id))
    blocks = {b.get("block_id"): b for b in slack.fake.of("views.open")[0].args["view"]["blocks"]}
    assert blocks["guests"]["element"]["initial_value"] == "2"
    assert blocks["eat_from"]["element"]["initial_time"] == "18:30"
    assert blocks["suggest_item"]["element"]["initial_option"]["value"] == str(tacos)
    assert blocks["suggest_text"]["element"]["initial_value"] == "Pho"
    assert blocks["note"]["element"]["initial_value"] == "Back at 6"


@pytest.mark.parametrize(
    ("values", "errors"),
    [
        (state(guests={"value": "-1"}), {"guests": "Enter a whole number from 0 to 20."}),
        (state(guests={"value": "21"}), {"guests": "Enter a whole number from 0 to 20."}),
        (
            state(eat_from={"selected_time": "19:00"}, eat_until={"selected_time": "18:00"}),
            {"eat_until": "The latest time can't be before the earliest."},
        ),
    ],
)
async def test_details_form_errors_show_on_the_field(
    slack: Slack, house: House, values: dict[str, object], errors: dict[str, str]
) -> None:
    event_id = await open_call(house)
    response = await slack.submit("Sam", ids.DETAILS_VIEW, values, event_id)
    assert body(response) == {"response_action": "errors", "errors": errors}
    assert await status_of(house, event_id, "Sam") is None


async def test_details_rule_errors_after_closing_come_as_a_message(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    values = state(note={"value": "bad\u0007bell"})
    response = await slack.submit("Sam", ids.DETAILS_VIEW, values, event_id)
    assert body(response) == {}
    assert slack.ephemerals("Sam") == ["A note can't contain control characters."]


async def test_details_closed_window_is_explained_instead_of_a_modal(slack: Slack, house: House) -> None:
    event_id = await to_voting(slack)
    slack.fake.clear()
    await slack.click("Sam", "details", str(event_id))
    assert slack.fake.of("views.open") == []
    assert slack.ephemerals("Sam") == ["Responses are closed for this call."]


# Proposals, votes, lock --------------------------------------------------------------------------


async def test_vote_toggles_and_lock_needs_the_coordinator(slack: Slack, house: House) -> None:
    event_id = await to_voting(slack)
    ctx = await house.context(event_id)
    first = ctx.proposals[0].id
    value = ids.proposal_value(event_id, first)
    await slack.click("Sam", "vote", value)
    assert (await house.context(event_id)).votes[first] == [house["Sam"].id]
    await slack.click("Sam", "vote", value)
    assert (await house.context(event_id)).votes.get(first, []) == []

    await slack.click("Sam", "lock", value)
    assert slack.ephemerals("Sam")[-3:] == [VOTED, UNVOTED, "Only the coordinator or an admin can lock a proposal."]
    await slack.click("Alex", "lock", value)  # Alex is the admin
    event = await house.event(event_id)
    assert event.state is EventState.DECIDED and event.decided_item_id == ctx.proposals[0].item_id


async def test_add_proposal_modal_and_submission(slack: Slack, house: House) -> None:
    event_id = await to_voting(slack)
    await slack.click("Sam", "add_proposal", str(event_id))
    (opened,) = slack.fake.of("views.open")
    assert opened.args["view"]["callback_id"] == ids.PROPOSAL_VIEW

    mexican = house.tags["Mexican"].id
    missing_cuisine = state(mode={"selected_option": {"value": "cook"}}, free_text={"value": "Enchiladas"})
    response = await slack.submit("Sam", ids.PROPOSAL_VIEW, missing_cuisine, event_id)
    assert body(response)["errors"] == {"cuisine": "Pick a cuisine for it, so variety and vetoes still work."}

    both = state(
        mode={"selected_option": {"value": "cook"}},
        item={"selected_option": {"value": str(house.items["Sam's Tacos"].id)}},
        free_text={"value": "Enchiladas"},
    )
    assert "free_text" in body(await slack.submit("Sam", ids.PROPOSAL_VIEW, both, event_id))["errors"]
    no_mode = state(free_text={"value": "Enchiladas"}, cuisine={"selected_option": {"value": str(mexican)}})
    assert "mode" in body(await slack.submit("Sam", ids.PROPOSAL_VIEW, no_mode, event_id))["errors"]

    good = state(
        mode={"selected_option": {"value": "pickup"}},
        free_text={"value": "Enchiladas"},
        cuisine={"selected_option": {"value": str(mexican)}},
    )
    assert body(await slack.submit("Sam", ids.PROPOSAL_VIEW, good, event_id)) == {}
    ctx = await house.context(event_id)
    added = [p for p in ctx.proposals if p.free_text == "Enchiladas"]
    assert len(added) == 1 and added[0].mode is Mode.PICKUP and added[0].proposed_by == house["Sam"].id

    # The same again is refused by the service after the modal closed.
    await slack.submit("Sam", ids.PROPOSAL_VIEW, good, event_id)
    assert slack.ephemerals("Sam")[-1] == "Enchiladas is already a pick (pickup). Vote for it instead."


async def test_add_proposal_outside_voting_is_explained(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Sam", "add_proposal", str(event_id))
    assert slack.fake.of("views.open") == []
    assert slack.ephemerals("Sam") == ["You can add proposals only while voting is open."]


async def test_mark_done(slack: Slack, house: House) -> None:
    event_id = await to_voting(slack)
    first = (await house.context(event_id)).proposals[0].id
    await slack.click("Alex", "lock", ids.proposal_value(event_id, first))
    await slack.click("Sam", "mark_done", str(event_id))  # members wait for meal time
    assert (await house.event(event_id)).state is EventState.DECIDED
    assert slack.ephemerals("Sam")[-1] != "Marked done. Thanks!"
    house.clock.now = (await house.event(event_id)).meal_time  # type: ignore[assignment]
    await slack.click("Sam", "mark_done", str(event_id))
    assert (await house.event(event_id)).state is EventState.DONE
    assert slack.ephemerals("Sam")[-1] == "Marked done. Thanks!"


async def test_link_buttons_are_just_acknowledged(slack: Slack) -> None:
    response = await slack.click("Sam", "link_event", None)
    assert response.status == 200
    assert slack.fake.calls == []


# Errors ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "shown"), [(EventState.DONE, "This call already finished."), (EventState.VOTING, STALE)]
)
async def test_a_stale_transition_says_so(
    slack: Slack, house: House, monkeypatch: pytest.MonkeyPatch, current: EventState, shown: str
) -> None:
    async def stale(*args: object, **kwargs: object) -> None:
        raise IllegalTransition(current, EventState.DONE)

    monkeypatch.setattr(calls, "mark_done", stale)
    await slack.click("Sam", "mark_done", "7")
    assert slack.ephemerals("Sam") == [shown]


async def test_a_bug_is_logged_and_the_member_told_to_retry(
    slack: Slack, house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise ZeroDivisionError

    monkeypatch.setattr(calls, "respond", broken)
    await slack.click("Sam", "respond_in", "7")
    assert slack.ephemerals("Sam") == [BROKEN]
    assert any(r.message == "slack action failed" and r.exc_info for r in caplog.records)


async def test_an_ephemeral_that_fails_falls_back_to_a_dm(slack: Slack, house: House) -> None:
    event_id = await to_voting(slack)
    slack.fake.clear()
    slack.fake.fail("chat.postEphemeral", Failure("user_not_in_channel"))
    await slack.click("Kim", "respond_in", str(event_id))
    (dm,) = slack.fake.of("chat.postMessage")
    assert dm.args == {"channel": "U03KIM", "text": "Responses are closed for this call.", "unfurl_links": False}


async def test_a_reply_that_fails_everywhere_is_only_logged(slack: Slack, house: House) -> None:
    event_id = await to_voting(slack)
    slack.fake.fail("chat.postEphemeral", Failure("user_not_in_channel"))
    slack.fake.fail("chat.postMessage", Failure(raises=TimeoutError()))
    await slack.click("Kim", "respond_in", str(event_id))  # no exception escapes


# App Home ----------------------------------------------------------------------------------------


async def test_home_shows_todays_calls_and_quick_buttons(slack: Slack, house: House) -> None:
    event_id = await open_call(house)
    await slack.click("Sam", "respond_in", str(event_id))
    await slack.open_home("Sam")
    home = slack.last_home("Sam")
    text = json.dumps(home)
    assert "You're in." in text
    actions = [e["action_id"] for b in home["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert actions == [
        "respond_in",
        "respond_maybe",
        "respond_out",
        "details",
        "link_event",
        "make_offer",
        "start_call_lunch",
    ]


async def test_home_buttons_act_and_republish_with_a_notice(slack: Slack, house: House) -> None:
    await slack.click("Kim", ids.ACTION_FOR_START[Slot.DINNER], "dinner", home=True)
    event_id = await dinner_id(house)
    assert (await house.event(event_id)).state is EventState.COLLECTING
    assert "Started the dinner call." in json.dumps(slack.last_home("Kim"))
    await slack.click("Kim", "respond_maybe", str(event_id), home=True)
    assert await status_of(house, event_id, "Kim") is Status.MAYBE
    assert "You said maybe." in json.dumps(slack.last_home("Kim"))
    await slack.click("Kim", ids.ACTION_FOR_START[Slot.DINNER], "dinner", home=True)
    assert "already" in json.dumps(slack.last_home("Kim")).lower()
    assert slack.ephemerals("Kim") == []


async def test_home_for_strangers_and_the_messages_tab(slack: Slack) -> None:
    await slack.open_home("U9STRANGER")
    assert JOIN_OFFER in json.dumps(slack.last_home("U9STRANGER"))
    slack.fake.clear()
    await slack.open_home("Sam", tab="messages")
    assert slack.fake.calls == []


async def test_home_publish_failure_is_logged(slack: Slack, caplog: pytest.LogCaptureFixture) -> None:
    slack.fake.fail("views.publish", Failure("internal_error", status=500))
    await slack.open_home("Sam")
    assert any(r.message == "slack home publish failed" for r in caplog.records)


async def test_home_republish_failure_after_an_action_is_logged(
    slack: Slack, house: House, caplog: pytest.LogCaptureFixture
) -> None:
    slack.fake.fail("views.publish", Failure("internal_error", status=500))
    await slack.click("Kim", ids.ACTION_FOR_START[Slot.DINNER], "dinner", home=True)
    assert any(r.message == "slack home publish failed" for r in caplog.records)


# /dinnerbell -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "`/dinnerbell call` starts a dinner call"),
        ("help", "`/dinnerbell call` starts a dinner call"),
        ("prefs", "https://dinnerbell.test/preferences"),
        ("catalog", "https://dinnerbell.test/catalog"),
        ("history", "https://dinnerbell.test/history"),
        ("what", "`/dinnerbell call` starts a dinner call"),
    ],
)
async def test_slash_command_replies(slack: Slack, text: str, expected: str) -> None:
    response = await slack.command("Kim", text)
    reply = body(response)
    assert reply["response_type"] == "ephemeral"
    assert expected in str(reply["text"])
    assert slack.fake.calls == []


async def test_slash_call_starts_a_call(slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    response = await slack.command("Kim", "call")
    assert response.status == 200 and body(response) == {}
    event_id = await dinner_id(house)
    assert (await house.event(event_id)).state is EventState.COLLECTING
    assert slack.ephemerals("Kim") == ["Started the dinner call today. It's posted in <#C0DINNER>."]
    await slack.command("Kim", "call")
    assert "already" in slack.ephemerals("Kim")[-1].lower()


async def test_slash_call_with_a_slot_and_day(slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await slack.command("Kim", "call lunch 2026-10-24")
    assert slack.ephemerals("Kim") == [
        "Scheduled the lunch call for Saturday, Oct 24. It posts in <#C0DINNER> at 10:30 AM that day."
    ]
    await slack.command("Kim", "call dinner soonish")
    assert slack.ephemerals("Kim")[-1].startswith("Try `/dinnerbell call`")


async def test_slack_requests_leave_dinnerbell_log_levels_alone(slack: Slack) -> None:
    """Regression: Bolt copied the root level onto a logger named after the app, "dinnerbell",
    on every request, which hid INFO records from every Dinnerbell module."""
    import logging

    ours = logging.getLogger("dinnerbell")
    saved = ours.level
    ours.setLevel(logging.INFO)
    try:
        await slack.command("Kim", "help")
        assert ours.level == logging.INFO
    finally:
        ours.setLevel(saved)


def test_parse_call_args() -> None:
    from datetime import date

    today = date(2026, 10, 22)
    assert parse_call_args([], today) == (Slot.DINNER, today, False)
    assert parse_call_args(["tomorrow", "lunch"], today) == (Slot.LUNCH, date(2026, 10, 23), False)
    assert parse_call_args(["today"], today) == (Slot.DINNER, today, False)
    assert parse_call_args(["lunch", "now", "tomorrow"], today) == (Slot.LUNCH, date(2026, 10, 23), True)
    assert parse_call_args(["early", "2026-10-24"], today) == (Slot.DINNER, date(2026, 10, 24), True)
    with pytest.raises(EventRuleViolation):
        parse_call_args(["2026-13-01"], today)
