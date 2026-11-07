# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ⋯ beside a pick on the proposals message, and the bracketed none choices (#214): what the
menu lists, the modals that confirm **Lock this** and **Break the tie**, who gets refused before
any modal opens, and the buttons on messages already posted."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest

from dinnerbell.channels.slack import blocks as bk
from dinnerbell.channels.slack import ids, views
from dinnerbell.channels.slack import preview as samples
from dinnerbell.channels.slack.interactions import OUT_OF_DATE, WAY_SET
from dinnerbell.channels.slack.messages import NEVER_MIND, RenderContext, pick_menu, render
from dinnerbell.config import Settings
from dinnerbell.domain import labels
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import (
    GUESTS_DONT,
    STALE_CALL,
    TIE_BREAKERS,
    EventRuleViolation,
    EventState,
    RoleOffer,
    Status,
    TieHow,
)
from dinnerbell.services import calls, guests
from dinnerbell.services.calls import NO_SUCH_PROPOSAL
from dinnerbell.services.coordinator import NOT_TIED
from dinnerbell.services.database import Database, open_database
from tests.support.blockkit import check_message, check_view
from tests.support.calls import RESPOND_BY, House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import CHANNEL, block_action
from tests.support.slack import SLACK_IDS, Slack, link, wire
from tests.test_coordinator import act, add_guest, open_dinner, pick_ids, to_voting, vote

pytestmark = pytest.mark.anyio

CTX = RenderContext(samples.BASE_URL, samples.DIRECTORY)
PICK = samples.PROPOSALS[0]
EVENT, RUN = samples.VOTING.id, samples.VOTING.run
PAT = "U07PAT"
ONLY_LOCK = "Only the coordinator or an admin can lock a proposal."


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


def options(proposal: note.ProposalInfo, ctx: RenderContext = CTX) -> list[dict[str, Any]]:
    menu = pick_menu(samples.VOTING, proposal, ctx)
    assert menu["action_id"] == ids.PICK_MENU
    found: list[dict[str, Any]] = menu["options"]
    return found


def texts(found: list[dict[str, Any]]) -> list[str]:
    return [o["text"]["text"] for o in found]


LOCK_VALUE = ids.pick_value(ids.PickChoice.LOCK, EVENT, RUN, PICK.id)
TIE_VALUE = ids.pick_value(ids.PickChoice.BREAK_TIE, EVENT, RUN, PICK.id)
PICKUP, DELIVERY = labels.WAY_LABELS[Mode.PICKUP], labels.WAY_LABELS[Mode.DELIVERY]
BOTH_WAYS = (Mode.PICKUP, Mode.DELIVERY)


# What the menu lists -------------------------------------------------------------------------------


def test_a_plain_pick_gets_lock_this_and_a_link_to_the_web() -> None:
    lock, web = options(PICK)
    assert (lock["text"]["text"], lock["value"]) == (labels.LOCK, LOCK_VALUE)
    assert "url" not in lock
    assert web["text"]["text"] == labels.VIEW_ON_WEB
    assert web["value"] == ids.PICK_NOTHING
    assert web["url"] == CTX.url(samples.VOTING.path)


def test_a_tied_pick_gets_break_the_tie_and_needs_no_link() -> None:
    found = options(replace(PICK, tied=True))
    assert [o["value"] for o in found] == [LOCK_VALUE, TIE_VALUE]
    assert texts(found) == [labels.LOCK, labels.BREAK_TIE]
    assert all("url" not in o for o in found)


def test_a_pick_with_ways_lists_them_after_lock_this() -> None:
    found = options(replace(PICK, ways=BOTH_WAYS))
    assert texts(found) == [labels.LOCK, PICKUP, DELIVERY]
    assert found[1]["value"] == ids.way_value(Mode.PICKUP, EVENT, RUN, PICK.id)


def test_the_worst_case_fits_slacks_limit_with_nothing_clipped() -> None:
    worst = replace(PICK, tied=True, ways=BOTH_WAYS, mode=Mode.DELIVERY, mode_chosen=True)
    found = options(worst)
    assert len(found) == bk.MAX_OVERFLOW_OPTIONS
    assert texts(found) == [labels.LOCK, labels.BREAK_TIE, PICKUP, DELIVERY, labels.EITHER_WAY]
    assert found[-1]["value"] == ids.way_value(None, EVENT, RUN, PICK.id, clears=Mode.DELIVERY)


def test_a_pick_with_no_usable_web_link_still_gets_two_options() -> None:
    found = options(PICK, RenderContext("", samples.DIRECTORY))
    assert texts(found) == [labels.LOCK, NEVER_MIND]
    assert found[1]["value"] == ids.PICK_NOTHING and "url" not in found[1]


@pytest.mark.parametrize(
    "proposal",
    [
        PICK,
        replace(PICK, tied=True),
        replace(PICK, ways=BOTH_WAYS),
        replace(PICK, ways=BOTH_WAYS, tied=True),
        replace(PICK, ways=BOTH_WAYS, mode=Mode.PICKUP, mode_chosen=True),
        replace(PICK, ways=BOTH_WAYS, mode=Mode.PICKUP, mode_chosen=True, tied=True),
    ],
)
@pytest.mark.parametrize("base_url", [samples.BASE_URL, ""])
def test_every_menu_has_two_to_five_options_and_passes_the_block_kit_checks(
    proposal: note.ProposalInfo, base_url: str
) -> None:
    ctx = RenderContext(base_url, samples.DIRECTORY)
    assert 2 <= len(options(proposal, ctx)) <= bk.MAX_OVERFLOW_OPTIONS
    # The footer's own web button needs a real URL, so the whole message only renders with one.
    if base_url:
        check_message(render(note.ProposalsUpdated(samples.VOTING, (proposal,), 3), ctx).payload())


def test_vote_stays_a_primary_button_and_lock_this_is_no_longer_one() -> None:
    message = render(note.ProposalsUpdated(samples.VOTING, (replace(PICK, tied=True),), 3), CTX)
    (row,) = [b for b in message.blocks if b.get("block_id") == f"proposal-actions-{PICK.id}"]
    vote_button, menu = row["elements"]
    assert (vote_button["action_id"], vote_button["style"]) == (ids.VOTE, "primary")
    assert (menu["type"], menu["action_id"]) == ("overflow", ids.PICK_MENU)
    assert not {ids.LOCK, ids.BREAK_TIE, ids.SET_WAY} & {e["action_id"] for b in message.blocks for e in _elements(b)}


def _elements(block: dict[str, Any]) -> list[dict[str, Any]]:
    return block["elements"] if block["type"] == "actions" else []


def test_the_hints_point_to_the_menu() -> None:
    def shown(*proposals: note.ProposalInfo) -> str:
        message = render(note.ProposalsUpdated(samples.VOTING, proposals, 3), CTX)
        return json.dumps(message.blocks, ensure_ascii=False)

    way_hint = "can choose pickup or delivery with ⋯ beside a proposal that allows both."
    tie_hint = "Tied for the most votes. The coordinator or an admin can break the tie with ⋯ beside a tied proposal."
    assert way_hint not in shown(PICK) and tie_hint not in shown(PICK)
    assert tie_hint in shown(replace(PICK, tied=True))
    assert way_hint in shown(replace(PICK, ways=BOTH_WAYS))
    assert way_hint not in shown(replace(PICK, ways=BOTH_WAYS, mode_chosen=True))  # only for an open way


# Values and metadata -------------------------------------------------------------------------------


def test_a_pick_value_reads_back_and_isnt_a_way_value() -> None:
    assert ids.parse_pick(TIE_VALUE) == (ids.PickChoice.BREAK_TIE, EVENT, RUN, PICK.id)
    assert ids.parse_pick(LOCK_VALUE) == (ids.PickChoice.LOCK, EVENT, RUN, PICK.id)
    assert not ids.is_way_value(LOCK_VALUE)
    assert ids.is_way_value(ids.way_value(Mode.PICKUP, EVENT, RUN, PICK.id))


@pytest.mark.parametrize("value", [None, 7, "lock:1:2", "lock:0:1:1", "shuffle:1:1:1", "lock:1:1:1 ", ids.PICK_NOTHING])
def test_a_pick_value_this_app_didnt_write_is_refused(value: object) -> None:
    with pytest.raises(ids.BadValue):
        ids.parse_pick(value)


def test_a_modals_metadata_keeps_the_pick_it_confirms() -> None:
    origin = ids.ViewOrigin(EVENT, CHANNEL, run=RUN, proposal=PICK.id)
    assert ids.ViewOrigin.decode(origin.encode()) == origin
    assert "proposal" not in json.loads(ids.ViewOrigin(EVENT, CHANNEL).encode())
    for bad in (0, True, "7", 2**63):
        text = json.dumps({"event": EVENT, "channel": CHANNEL, "home": False, "proposal": bad})
        with pytest.raises(ids.BadValue):
            ids.ViewOrigin.decode(text)


def test_a_confirmation_needs_the_pick_and_escapes_its_name() -> None:
    with pytest.raises(ValueError, match="needs the pick"):
        views.pick_modal(samples.VOTING_VIEW, ids.PickChoice.LOCK, "x", samples.CALL_OFF_ORIGIN)
    modal = samples.preview("break_tie_modal")
    check_view(modal)
    assert modal["title"]["text"] == labels.BREAK_TIE_TITLE and len(modal["title"]["text"]) <= bk.MAX_MODAL_TITLE
    assert labels.decides_now("Siam &lt;Garden&gt;") in json.dumps(modal, ensure_ascii=False)


# Opening and confirming ----------------------------------------------------------------------------


async def pick_opens(slack: Slack, who: str, choice: ids.PickChoice, event_id: int, pick: int, run: int = 1) -> None:
    await slack.click(who, ids.PICK_MENU, None, selected=ids.pick_value(choice, event_id, run, pick))


async def test_lock_this_opens_a_modal_and_decides_on_submit(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await pick_opens(slack, "Sam", ids.PickChoice.LOCK, event_id, siam)
    (opened,) = slack.fake.of("views.open")
    view = opened.args["view"]
    assert (view["callback_id"], view["title"]["text"]) == (ids.LOCK_VIEW, labels.LOCK_TITLE)
    assert (view["submit"]["text"], view["close"]["text"]) == (labels.LOCK_SUBMIT, "Not yet")
    assert labels.decides_now("Siam Garden") in json.dumps(view)
    check_view(view)
    assert (await house.event(event_id)).state is EventState.VOTING  # nothing happens until they confirm
    origin = ids.ViewOrigin.decode(view["private_metadata"])
    assert (origin.event_id, origin.channel_id, origin.run, origin.proposal) == (event_id, CHANNEL, 1, siam)

    await slack.submit("Sam", ids.LOCK_VIEW, {}, event_id, origin=origin)
    event = await house.event(event_id)
    assert (event.state, event.locked_by) == (EventState.DECIDED, house["Sam"].id)
    assert slack.ephemerals("Sam") == []
    await slack.submit("Sam", ids.LOCK_VIEW, {}, event_id, origin=origin)  # a second tab's modal
    assert slack.ephemerals("Sam")[-1] == "Voting is closed."


async def test_break_the_tie_opens_a_modal_and_decides_on_submit(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    picks = await pick_ids(house, event_id)
    await vote(house, "Alex", event_id, picks["Siam Garden"])
    await vote(house, "Lee", event_id, picks["Taqueria"])
    await pick_opens(slack, "Sam", ids.PickChoice.BREAK_TIE, event_id, picks["Taqueria"])
    (opened,) = slack.fake.of("views.open")
    view = opened.args["view"]
    assert (view["callback_id"], view["title"]["text"]) == (ids.BREAK_TIE_VIEW, labels.BREAK_TIE_TITLE)
    assert view["submit"]["text"] == labels.BREAK_TIE_SUBMIT
    assert labels.decides_now("Taqueria") in json.dumps(view)
    assert (await house.event(event_id)).state is EventState.VOTING

    origin = ids.ViewOrigin.decode(view["private_metadata"])
    await slack.submit("Sam", ids.BREAK_TIE_VIEW, {}, event_id, origin=origin)
    event = await house.event(event_id)
    assert (event.state, event.tie_how, event.tie_by) == (EventState.DECIDED, TieHow.CHOSE, house["Sam"].id)


async def test_an_admin_who_isnt_coordinating_gets_the_modal(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await pick_opens(slack, "Alex", ids.PickChoice.LOCK, event_id, siam)
    assert slack.fake.of("views.open") and slack.ephemerals("Alex") == []


async def test_a_pick_no_longer_tied_by_submit_is_refused(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    picks = await pick_ids(house, event_id)
    await vote(house, "Alex", event_id, picks["Siam Garden"])
    await vote(house, "Lee", event_id, picks["Taqueria"])
    await pick_opens(slack, "Sam", ids.PickChoice.BREAK_TIE, event_id, picks["Taqueria"])
    origin = ids.ViewOrigin.decode(slack.fake.of("views.open")[0].args["view"]["private_metadata"])
    await vote(house, "Sam", event_id, picks["Siam Garden"])
    await slack.submit("Sam", ids.BREAK_TIE_VIEW, {}, event_id, origin=origin)
    assert slack.ephemerals("Sam")[-1] == NOT_TIED
    assert (await house.event(event_id)).state is EventState.VOTING


async def test_a_modal_left_open_across_a_new_run_decides_nothing(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    picks = await pick_ids(house, event_id)
    stale = ids.ViewOrigin(event_id, CHANNEL, run=2, proposal=picks["Siam Garden"])
    for view_id in (ids.LOCK_VIEW, ids.BREAK_TIE_VIEW):
        await slack.submit("Sam", view_id, {}, event_id, origin=stale)
        assert slack.ephemerals("Sam")[-1] == STALE_CALL
    assert (await house.event(event_id)).state is EventState.VOTING


@pytest.mark.parametrize("view_id", [ids.LOCK_VIEW, ids.BREAK_TIE_VIEW])
async def test_a_confirmation_without_a_pick_or_run_is_ignored(slack: Slack, house: House, view_id: str) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    pick = (await pick_ids(house, event_id))["Siam Garden"]
    for origin in (ids.ViewOrigin(event_id, CHANNEL, run=1), ids.ViewOrigin(event_id, CHANNEL, proposal=pick)):
        await slack.submit("Sam", view_id, {}, event_id, origin=origin)
    assert (await house.event(event_id)).state is EventState.VOTING
    assert slack.ephemerals("Sam") == []


async def test_only_a_pick_confirmation_may_name_a_pick(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    origin = ids.ViewOrigin(event_id, CHANNEL, run=1, proposal=(await pick_ids(house, event_id))["Siam Garden"])
    await slack.submit("Sam", ids.CANCEL_VIEW, {}, event_id, origin=origin)
    assert (await house.event(event_id)).state is EventState.VOTING


async def test_lock_takes_the_run_it_saw(house: House) -> None:
    """The service behind the modal refuses a run that isn't the call's (``check_run``)."""
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    with pytest.raises(EventRuleViolation, match=STALE_CALL):
        await act(house, calls.lock, house["Sam"], event_id, siam, run=2)
    assert (await house.event(event_id)).state is EventState.VOTING
    await act(house, calls.lock, house["Sam"], event_id, siam, run=1)
    assert (await house.event(event_id)).state is EventState.DECIDED


# Refused before any modal --------------------------------------------------------------------------


async def test_a_member_who_cant_is_refused_with_no_modal(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    picks = await pick_ids(house, event_id)
    await vote(house, "Alex", event_id, picks["Siam Garden"])
    await vote(house, "Sam", event_id, picks["Taqueria"])
    await pick_opens(slack, "Lee", ids.PickChoice.LOCK, event_id, picks["Siam Garden"])
    assert slack.ephemerals("Lee")[-1] == ONLY_LOCK
    await pick_opens(slack, "Lee", ids.PickChoice.BREAK_TIE, event_id, picks["Siam Garden"])
    assert slack.ephemerals("Lee")[-1] == TIE_BREAKERS
    assert slack.fake.of("views.open") == []
    assert (await house.event(event_id)).state is EventState.VOTING


async def test_a_stale_or_missing_or_untied_pick_is_refused_with_no_modal(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await pick_opens(slack, "Sam", ids.PickChoice.LOCK, event_id, siam, run=2)
    assert slack.ephemerals("Sam")[-1] == STALE_CALL
    await pick_opens(slack, "Sam", ids.PickChoice.LOCK, event_id, 999_999)
    assert slack.ephemerals("Sam")[-1] == NO_SUCH_PROPOSAL
    await pick_opens(slack, "Sam", ids.PickChoice.BREAK_TIE, event_id, siam)  # nobody has voted
    assert slack.ephemerals("Sam")[-1] == NOT_TIED
    await slack.click("Sam", ids.PICK_MENU, None, selected="something:else")
    assert slack.ephemerals("Sam")[-1] == OUT_OF_DATE
    assert slack.fake.of("views.open") == []


async def test_a_pick_without_a_trigger_is_refused(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    value = ids.pick_value(ids.PickChoice.LOCK, event_id, 1, siam)
    payload = block_action(SLACK_IDS["Sam"], ids.PICK_MENU, None, selected=value)
    del payload["trigger_id"]
    await slack._send("interactive", payload)
    assert slack.ephemerals("Sam") == [OUT_OF_DATE] and slack.fake.of("views.open") == []


async def test_a_pick_after_the_vote_is_refused_with_no_modal(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await act(house, calls.lock, house["Sam"], event_id, siam, run=1)
    await pick_opens(slack, "Sam", ids.PickChoice.LOCK, event_id, siam)
    assert slack.ephemerals("Sam")[-1] == "Voting is closed."
    assert slack.fake.of("views.open") == []


async def guest_coordinating(house: House) -> int:
    """Pat, a guest with Slack, coordinates a vote on Siam Garden, whose place allows both ways."""
    pat = await add_guest(house, "Pat")
    await link(house, "Pat", PAT)
    event_id = await open_dinner(house)
    for name in ("Alex", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, guests.invite, house["Lee"], event_id, guest_id=pat.id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.claim_coordinator, pat, event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    return event_id


async def test_a_guest_coordinator_sets_the_way_from_the_menu_but_cant_lock(slack: Slack, house: House) -> None:
    event_id = await guest_coordinating(house)
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await pick_opens(slack, PAT, ids.PickChoice.LOCK, event_id, siam)
    assert slack.ephemerals(PAT)[-1] == GUESTS_DONT
    await pick_opens(slack, PAT, ids.PickChoice.BREAK_TIE, event_id, siam)
    assert slack.ephemerals(PAT)[-1] == GUESTS_DONT
    assert slack.fake.of("views.open") == []

    way = ids.way_value(Mode.DELIVERY, event_id, 1, siam)
    await slack.click(PAT, ids.PICK_MENU, None, selected=way)
    assert slack.ephemerals(PAT)[-1] == WAY_SET[Mode.DELIVERY]
    pick = (await house.context(event_id)).proposal(siam)
    assert pick is not None and (pick.mode, pick.mode_chosen) == (Mode.DELIVERY, True)


async def test_a_member_setting_a_way_from_the_menu_gets_the_way_setters_refusal(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await slack.click("Lee", ids.PICK_MENU, None, selected=ids.way_value(Mode.DELIVERY, event_id, 1, siam))
    assert "Only the coordinator or an admin" in slack.ephemerals("Lee")[-1]
    pick = (await house.context(event_id)).proposal(siam)
    assert pick is not None and not pick.mode_chosen


# The link to the web -------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["Lee", "U0STRANGER"])
async def test_the_link_option_is_acknowledged_and_does_nothing(slack: Slack, house: House, who: str) -> None:
    """Slack sends a link's click too. Nobody is told anything, even someone Dinnerbell doesn't
    know, who'd otherwise be asked to join because they opened a link."""
    response = await slack.click(who, ids.PICK_MENU, None, selected=ids.PICK_NOTHING)
    assert response.status == 200
    assert slack.fake.of("chat.postEphemeral") == [] and slack.fake.of("chat.postMessage") == []
    assert slack.fake.of("views.open") == []


# Messages already posted ---------------------------------------------------------------------------


async def test_old_buttons_on_posted_messages_keep_working(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    picks = await pick_ids(house, event_id)
    siam, taqueria = picks["Siam Garden"], picks["Taqueria"]

    await slack.click("Sam", ids.SET_WAY, None, selected=ids.way_value(Mode.DELIVERY, event_id, 1, siam))
    assert slack.ephemerals("Sam")[-1] == WAY_SET[Mode.DELIVERY]
    await slack.click("Lee", ids.LOCK, ids.proposal_value(event_id, taqueria))
    assert slack.ephemerals("Lee")[-1] == ONLY_LOCK
    await slack.click("Sam", ids.LOCK, ids.proposal_value(event_id, taqueria))  # Slack confirmed it already
    assert slack.fake.of("views.open") == []
    event = await house.event(event_id)
    assert (event.state, event.locked_by) == (EventState.DECIDED, house["Sam"].id)


async def test_a_guest_still_cant_use_an_old_lock_or_tie_button(slack: Slack, house: House) -> None:
    event_id = await guest_coordinating(house)
    value = ids.proposal_value(event_id, (await pick_ids(house, event_id))["Siam Garden"])
    for action_id in (ids.LOCK, ids.BREAK_TIE):
        await slack.click(PAT, action_id, value)
        assert slack.ephemerals(PAT)[-1] == GUESTS_DONT
    assert (await house.event(event_id)).state is EventState.VOTING


# The none choices ----------------------------------------------------------------------------------


def test_none_choices_are_bracketed_and_the_errors_quote_them_plainly() -> None:
    assert views.NOTHING_FROM_CATALOG == "[Nothing from the catalog]"
    assert views.NO_SAVED_GUEST == "[No saved guest]"
    assert views.NO_ANSWER["text"]["text"] == "[No answer]"
    assert views.BOTH_ITEMS.endswith("To keep what you typed, pick [Nothing from the catalog].")
    assert views.BOTH_GUESTS.endswith("To invite someone new, pick [No saved guest].")
    assert views.PICK_A_RATING == "Pick a rating, or [No answer]."
