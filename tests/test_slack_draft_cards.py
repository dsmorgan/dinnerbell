# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's draft cards in Slack (spec 6.14, **Drafts**): a card per draft with
**Confirm** and **Cancel**, what each does, where the result goes, and who may tap. The drafts
come from the real tools, through a conversation the ``FakeModel`` scripts, and Confirm runs
the real services. Slack is ``FakeSlack``; nothing reaches Slack or Anthropic."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from dinnerbell.channels.slack import assistant as slack_assistant
from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.assistant import (
    CANCELLED,
    IF_CONFIRMED,
    IMPORT_READY,
    PROPOSED,
    REVIEW_IMPORT,
    SIGN_IN_FIRST,
    WORD_JOINER,
    draft_card,
    mention_only_line,
)
from dinnerbell.channels.slack.interactions import ASSISTANT_OFF, JOIN_OFFER, OUT_OF_DATE
from dinnerbell.config import Settings
from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.models import AssistantDraftRow, ProposalRow
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.assistant import AssistantSettings, slack_key
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import EventRuleViolation, EventState, RoleOffer, Status
from dinnerbell.domain.meals import MealInput, Slot
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.domain.schedule import PROPOSAL_COUNT_KEY, TEMPERATURE_KEY
from dinnerbell.services import calls, meals
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.assistant import admin, catalog_edits, drafts
from dinnerbell.services.assistant.conversation import Assistant, Reply
from dinnerbell.services.assistant.drafts import EXPIRED, NO_SUCH_DRAFT, USED, DraftView
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.preferences import set_my_preference
from dinnerbell.web.routes.catalog import draft_import_url
from tests.support.calls import OPENS, RESPOND_BY, House, dinner_id, furnish, make_house, nobody_coordinates
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, tool_call
from tests.support.fake_slack import BOT_USER, CHANNEL, RESPONSE_URL, Failure, block_action, next_ts, send
from tests.support.slack import SLACK_IDS, Slack, wire

pytestmark = pytest.mark.anyio

KEY = "sk-ant-test-not-a-real-key"
SAM_DM, LEE_DM = "D02SAM", "D04LEE"
CARD_TS = "1790000999.000100"
DAY = date(2026, 10, 22)
CATALOG = """version: 1
restaurants:
  - name: Noodle Bar
    cuisine: [Chinese]
    notes: From the draft
  - name: Dumpling House
    cuisine: [Chinese]
"""


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"anthropic_api_key": KEY, "base_url": "https://meals.example.org"}


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> AsyncIterator[House]:
    """The usual household, plus Pho Corner. Ranking proposes one pick at temperature 0, and Sam
    and Kim love Taqueria, so that's the pick and Pho Corner is a candidate."""
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    await house.add_item("Pho Corner", Mode.PICKUP, cuisine="Vietnamese")
    async with house.sessionmaker() as db:
        stored = await household_settings(db, house.household.id)
        await HouseholdRepository(db).update_settings(
            house.household.id, stored | {PROPOSAL_COUNT_KEY: 1, TEMPERATURE_KEY: 0.0}
        )
        await db.commit()
        for who in ("Sam", "Kim"):
            love = Stance(score=2)
            await set_my_preference(db, house[who], TargetType.ITEM, house.items["Taqueria"].id, love, now=clock())
    yield house
    await house.close()


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
async def helper(settings: Settings, model: FakeModel, house: House) -> Assistant:
    helper = Assistant(AssistantConfig.from_settings(settings), model, house.clock)
    await switch(house, helper, on=True)
    return helper


@pytest.fixture
async def slack(house: House, helper: Assistant, settings: Settings) -> Slack:
    def import_link(draft_id: int, member_id: int) -> str:
        return draft_import_url(settings, house.clock, draft_id, member_id)

    return await wire(house, assistant=helper, import_link=import_link)


async def switch(house: House, helper: Assistant, *, on: bool) -> None:
    async with house.sessionmaker() as db:
        values = AssistantSettings(enabled=on)
        await admin.update_assistant(db, helper.config, house["Alex"], values, now=house.clock())


async def to_voting(house: House) -> int:
    """Sam and Kim are in and Alex offers to pick up; voting opens with Taqueria, and nobody
    coordinates."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    async with house.sessionmaker() as db:
        for name in ("Sam", "Kim"):
            await calls.respond(db, house[name], event_id, Status.IN, now=house.clock())
        await calls.offer_role(db, house["Alex"], event_id, RoleOffer.PICKUP, True, now=house.clock())
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    await nobody_coordinates(house, event_id)
    return event_id


PROPOSE = {"slot": "", "day": "", "name": "Pho Corner", "mode": "pickup"}
OFFER = {"name": "Siam Garden", "mode": "delivery", "slot": "dinner", "day": "tomorrow", "time": "12:15"}


def drafting(model: FakeModel, tool: str, args: dict[str, Any], text: str = "Here's a draft.") -> None:
    model.script = [tool_call(tool, args | ({"spare_servings": 0} if tool == "draft_offer" else {})), answer(text)]


def posts(slack: Slack) -> list[dict[str, Any]]:
    return [c.args for c in slack.fake.of("chat.postMessage")]


def ephemerals(slack: Slack) -> list[dict[str, Any]]:
    return [c.args for c in slack.fake.of("chat.postEphemeral")]


def card_values(card: dict[str, Any]) -> dict[str, str]:
    return {
        e["action_id"]: e.get("value") or e.get("url")
        for b in card["blocks"]
        if b["type"] == "actions"
        for e in b["elements"]
    }


def card_text(card: dict[str, Any]) -> str:
    return "\n".join(b["text"]["text"] for b in card["blocks"] if b["type"] == "section")


async def dm_card(slack: Slack, model: FakeModel, who: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """``who`` asks in their DM, and the model drafts with ``tool``: the card Dinnerbell posts."""
    drafting(model, tool, args)
    await slack.dm(who, "Draft it, please")
    reply, card = posts(slack)
    assert reply["text"] == "Here's a draft." and card["thread_ts"] == reply["thread_ts"]
    slack.fake.clear()
    return card


async def thread_card(slack: Slack, model: FakeModel, who: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """``who`` mentions Dinnerbell in the channel, and the model drafts: the ephemeral card."""
    drafting(model, tool, args)
    root = next_ts()
    await slack.mention(who, f"<@{BOT_USER}> draft it, please", ts=root)
    assert posts(slack) == []
    reply, card = ephemerals(slack)
    for sent in (reply, card):
        assert (sent["channel"], sent["user"], sent["thread_ts"]) == (CHANNEL, SLACK_IDS[who], root)
    slack.fake.clear()
    return card


async def tap_dm(slack: Slack, who: str, action_id: str, card: dict[str, Any], **kwargs: Any) -> None:
    channel = "D" + SLACK_IDS.get(who, who)[1:]
    await slack.click(who, action_id, card_values(card)[action_id], channel=channel, message_ts=CARD_TS, **kwargs)


async def tap_thread(slack: Slack, who: str, action_id: str, card: dict[str, Any], **kwargs: Any) -> None:
    await slack.click(who, action_id, card_values(card)[action_id], **{"ephemeral": True, **kwargs})


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(1000):  # up to 10 seconds on a slow runner
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def updated(slack: Slack) -> list[dict[str, Any]]:
    """The DM cards replaced with ``chat.update``."""
    return [c.args for c in slack.fake.of("chat.update")]


def replaced(slack: Slack) -> list[dict[str, Any]]:
    """The ephemeral cards replaced through a ``response_url``."""
    return [c.args for c in slack.fake.of("response_url")]


async def draft_rows(house: House) -> list[AssistantDraftRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AssistantDraftRow).order_by(AssistantDraftRow.id)))


async def pho_corner(house: House, event_id: int) -> ProposalRow | None:
    async with house.sessionmaker() as db:
        return await db.scalar(
            select(ProposalRow).where(
                ProposalRow.event_id == event_id, ProposalRow.item_id == house.items["Pho Corner"].id
            )
        )


# The card ----------------------------------------------------------------------------------------


def test_a_card_has_the_title_what_confirm_does_and_buttons_whose_value_is_the_draft_id() -> None:
    view = DraftView(42, "proposal", "Propose <!here> evil.example", ("It pings @channel.", "Then www.x.example"))
    card = draft_card(view)
    section, buttons = card["blocks"]
    assert card["text"] == "Propose &lt;!here&gt; evil[.]example"
    assert section["text"]["text"] == (
        f"*Propose &lt;!here&gt; evil[.]example*\n{IF_CONFIRMED}\n"
        f"• It pings @{WORD_JOINER}channel.\n• Then www[.]x[.]example"
    )
    confirm, cancel = buttons["elements"]
    assert (confirm["action_id"], confirm["value"], confirm["style"]) == (ids.DRAFT_CONFIRM, "42", "primary")
    assert (cancel["action_id"], cancel["value"]) == (ids.DRAFT_CANCEL, "42") and "style" not in cancel
    assert "url" not in confirm and "url" not in cancel


def test_a_card_without_lines_has_only_its_title() -> None:
    card = draft_card(DraftView(1, "offer", "", ()))
    assert card_text(card) == "*…*"


@pytest.mark.parametrize("value", ["42", "1", str(2**63 - 1)])
def test_draft_values_round_trip(value: str) -> None:
    assert ids.draft_value(ids.parse_draft(value)) == value


BAD_VALUES = [None, 42, "", "0", "042", "4 2", "42 ", "+42", "42:1", "\uff14\uff12", "9" * 20, str(2**63)]


@pytest.mark.parametrize("value", BAD_VALUES)
def test_draft_values_must_be_exactly_what_dinnerbell_wrote(value: object) -> None:
    with pytest.raises(ids.BadValue):
        ids.parse_draft(value)


@pytest.mark.flag("assistant", on=True)
async def test_a_tampered_value_is_refused_and_changes_nothing(house: House, model: FakeModel, slack: Slack) -> None:
    await to_voting(house)
    await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    await slack.click("Kim", ids.DRAFT_CONFIRM, "01", ephemeral=True)
    assert slack.ephemerals("Kim") == [OUT_OF_DATE] and replaced(slack) == []
    [row] = await draft_rows(house)
    assert row.used_at is None


# Confirm and Cancel -------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_confirm_on_a_dm_card_proposes_and_the_card_says_so(house: House, model: FakeModel, slack: Slack) -> None:
    event_id = await to_voting(house)
    card = await dm_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    assert card_text(card).startswith(f"*Propose Pho Corner (pickup)*\n{IF_CONFIRMED}\n• ")
    assert set(card_values(card).values()) == {str((await draft_rows(house))[0].id)}
    await tap_dm(slack, "Kim", ids.DRAFT_CONFIRM, card)
    [update] = updated(slack)
    assert (update["channel"], update["ts"]) == ("D03KIM", CARD_TS)
    assert update["text"] == PROPOSED and card_text(update) == PROPOSED
    assert [b["type"] for b in update["blocks"]] == ["section"]  # no buttons left
    found = await pho_corner(house, event_id)
    assert found is not None and found.proposed_by == house["Kim"].id
    assert posts(slack) == [] and replaced(slack) == []


@pytest.mark.flag("assistant", on=True)
async def test_confirm_on_a_dm_card_posts_the_offer(house: House, model: FakeModel, slack: Slack) -> None:
    card = await dm_card(slack, model, "Lee", "draft_offer", OFFER)
    assert card_text(card).startswith("*Offer to order Siam Garden, dinner tomorrow at 12:15 PM*")
    await tap_dm(slack, "Lee", ids.DRAFT_CONFIRM, card)
    [update] = updated(slack)
    posted = "Posted your dinner offer for Friday, Oct 23 in the Dinnerbell channel. Thanks for sharing!"
    assert update["text"] == posted
    [row] = await draft_rows(house)
    assert row.used_at is not None


@pytest.mark.flag("assistant", on=True)
async def test_confirm_in_a_channel_thread_stays_ephemeral(house: House, model: FakeModel, slack: Slack) -> None:
    event_id = await to_voting(house)
    card = await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    await tap_thread(slack, "Kim", ids.DRAFT_CONFIRM, card)
    [result] = replaced(slack)
    assert result["url"] == RESPONSE_URL and result["replace_original"] is True
    assert result["response_type"] == "ephemeral" and result["text"] == PROPOSED
    assert posts(slack) == [] and updated(slack) == [] and ephemerals(slack) == []
    assert await pho_corner(house, event_id) is not None


@pytest.mark.flag("assistant", on=True)
async def test_a_call_action_in_a_channel_thread_confirms_only_for_its_member(
    house: House, model: FakeModel, slack: Slack
) -> None:
    """#251: "put me down for pickup" from Lee, who isn't in, while voting. The card and its
    result go to Lee alone, and Confirm joins Lee late and offers the role."""
    event_id = await to_voting(house)
    role = {"slot": "", "day": "", "role": "pickup", "offer": True}
    card = await thread_card(slack, model, "Lee", "draft_role", role)
    assert card_text(card).startswith("*Offer to pick up for today's dinner call*")
    await tap_thread(slack, "Lee", ids.DRAFT_CONFIRM, card)
    [result] = replaced(slack)
    assert result["response_type"] == "ephemeral"
    assert result["text"] == "Confirmed. You're down to pick up for today's dinner call."
    assert posts(slack) == [] and ephemerals(slack) == []
    mine = (await house.context(event_id)).response_of(house["Lee"].id)
    assert mine is not None and mine.late and RoleOffer.PICKUP in mine.offers


DELIVERY_ONLY = {
    "name": "Siam Garden",
    "kind": "",
    "catalog": "",
    "new_name": "",
    "modes": ["delivery"],
    "meals": [],
    "add_tags": [],
    "remove_tags": [],
    "add_cooks": [],
    "remove_cooks": [],
    "url": "",
    "order_url": "",
    "notes": "",
    "servings": 0,
    "clear": [],
}
LUNCH = {
    "meal": 0,
    "delete": False,
    "day": "today",
    "slot": "lunch",
    "name": "Taqueria",
    "kind": "",
    "add_people": [],
    "remove_people": [],
}


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize(
    ("tool", "args", "title", "confirmed"),
    [
        ("draft_catalog_edit", DELIVERY_ONLY, "*Edit Siam Garden*", "Confirmed. Siam Garden is updated."),
        ("draft_meal", LUNCH, "*Log Taqueria for lunch, today*", "Confirmed. Taqueria is logged for lunch today."),
    ],
)
async def test_a_catalog_edit_or_a_meal_in_a_channel_thread_is_the_askers_alone(
    house: House, model: FakeModel, slack: Slack, tool: str, args: dict[str, Any], title: str, confirmed: str
) -> None:
    """#254: "mark Siam Garden as delivery only" or "log lunch" in the channel. The card and its
    result go to Kim alone, as ephemerals in the thread, and nothing is posted for everyone."""
    card = await thread_card(slack, model, "Kim", tool, args)
    assert card_text(card).startswith(f"{title}\n{IF_CONFIRMED}\n• ")
    await tap_thread(slack, "Kim", ids.DRAFT_CONFIRM, card)
    [result] = replaced(slack)
    assert result["response_type"] == "ephemeral" and result["text"] == confirmed
    assert posts(slack) == [] and updated(slack) == [] and ephemerals(slack) == []
    [row] = await draft_rows(house)
    assert row.member_id == house["Kim"].id and row.used_at is not None


@pytest.mark.flag("assistant", on=True)
async def test_a_meal_lookup_in_a_channel_thread_names_no_eater_and_labels_nobody(
    house: House, model: FakeModel, slack: Slack
) -> None:
    """#254: "what meals did I log?" in the channel posts its reply for everyone, since it has no
    card. So the lookup leaves out who ate: the model can't name them, even by label, and the
    thread's shared pseudonyms gain nobody. In a DM, the same lookup gives who ate, by label."""
    eaten = MealInput(
        DAY, Slot.LUNCH, house.items["Taqueria"].id, frozenset({house[n].id for n in ("Kim", "Sam", "Lee")})
    )
    async with house.sessionmaker() as db:
        await meals.log_meal(db, house["Kim"], eaten, today=DAY, now=house.clock())
    model.script = [
        tool_call("look_up_my_meals", {"name": ""}),
        answer("You logged Taqueria for lunch with Member 2 and Member 3."),
    ]
    root = next_ts()
    await slack.mention("Kim", f"<@{BOT_USER}> what meals did I log?", ts=root)
    [reply] = posts(slack)
    assert ephemerals(slack) == []
    for name in ("Sam", "Lee"):
        assert name not in reply["text"]
    [result] = model.requests[-1]["messages"][-1]["content"]
    looked = json.loads(result["content"])
    assert "people" not in looked["meals"][0] and looked["people"] == catalog_edits.PEOPLE_IN_PRIVATE
    async with house.sessionmaker() as db:
        thread = await AssistantRepository(db).conversation(house.household.id, slack_key(CHANNEL, root))
    assert thread is not None and set(thread.pseudonyms.numbers) == {house["Kim"].id}

    slack.fake.clear()
    model.script = [tool_call("look_up_my_meals", {"name": ""}), answer("With Member 2 and Member 3.")]
    model.requests.clear()
    await slack.dm("Kim", "what meals did I log?")
    [result] = model.requests[-1]["messages"][-1]["content"]
    assert json.loads(result["content"])["meals"][0]["people"] == ["Member 1", "Member 2", "Member 3"]


@pytest.mark.flag("assistant", on=True)
async def test_when_slack_wont_replace_a_thread_card_only_the_tapper_is_told(
    house: House, model: FakeModel, slack: Slack
) -> None:
    await to_voting(house)
    card = await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    await tap_thread(slack, "Kim", ids.DRAFT_CONFIRM, card, response_url=None)
    assert slack.ephemerals("Kim") == [PROPOSED]
    assert posts(slack) == [] and updated(slack) == []


@pytest.mark.flag("assistant", on=True)
async def test_a_thread_card_tapped_as_if_it_were_public_is_refused(
    house: House, model: FakeModel, slack: Slack
) -> None:
    await to_voting(house)
    card = await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    await tap_thread(slack, "Kim", ids.DRAFT_CONFIRM, card, ephemeral=False)
    assert slack.ephemerals("Kim") == [OUT_OF_DATE] and replaced(slack) == [] and updated(slack) == []
    [row] = await draft_rows(house)
    assert row.used_at is None


@pytest.mark.flag("assistant", on=True)
async def test_cancel_replaces_the_card_and_changes_nothing_else(house: House, model: FakeModel, slack: Slack) -> None:
    event_id = await to_voting(house)
    card = await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    await tap_thread(slack, "Kim", ids.DRAFT_CANCEL, card)
    [result] = replaced(slack)
    assert result["text"] == CANCELLED and card_text(result) == CANCELLED
    assert await pho_corner(house, event_id) is None
    [row] = await draft_rows(house)
    assert row.used_at is not None
    await tap_thread(slack, "Kim", ids.DRAFT_CONFIRM, card)  # a second tap, on the card as it was
    assert len(replaced(slack)) == 1 and await pho_corner(house, event_id) is None


@pytest.mark.flag("assistant", on=True)
async def test_a_double_tap_leaves_the_card_showing_the_first_result(
    house: House, model: FakeModel, slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two taps at once: the first holds Confirm up; the second waits for it, finds the draft
    used, and leaves the card the first replaced."""
    event_id = await to_voting(house)
    card = await dm_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    gate, calls_made = asyncio.Event(), []
    real = slack_assistant.confirm_draft

    async def held(*args: Any) -> Any:
        calls_made.append(args[-1])
        await gate.wait()
        return await real(*args)

    monkeypatch.setattr(slack_assistant, "confirm_draft", held)
    value = card_values(card)[ids.DRAFT_CONFIRM]
    for _ in range(2):
        tap = block_action("U03KIM", ids.DRAFT_CONFIRM, value, channel="D03KIM", message_ts=CARD_TS)
        assert (await send(slack.conn.app, "interactive", tap)).status == 200
    await until(lambda: len(calls_made) == 1)
    assert len(slack.conn.interactions.tasks) == 2  # both taken; the second waits its turn
    gate.set()
    await slack.conn.interactions.settle()
    assert [u["text"] for u in updated(slack)] == [PROPOSED]
    assert slack.ephemerals("Kim") == [] and await pho_corner(house, event_id) is not None


@pytest.mark.flag("assistant", on=True)
async def test_a_card_slack_wouldnt_replace_never_ignores_the_next_tap(
    house: House, model: FakeModel, slack: Slack
) -> None:
    """The first tap's result reached the member another way, so the card still has its
    buttons. A second tap there says the draft was used, rather than nothing at all."""
    await to_voting(house)
    card = await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    slack.fake.fail("response_url", Failure("expired_url", status=404))
    await tap_thread(slack, "Kim", ids.DRAFT_CONFIRM, card)
    assert slack.ephemerals("Kim") == [PROPOSED]
    await tap_thread(slack, "Kim", ids.DRAFT_CONFIRM, card)
    assert replaced(slack)[-1]["text"] == USED


@pytest.mark.flag("assistant", on=True)
async def test_a_refusal_replaces_the_card_with_the_services_words(
    house: House, model: FakeModel, slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    await to_voting(house)
    card = await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)

    async def refusing(*args: Any) -> Any:
        raise EventRuleViolation("Pho Corner & <Friends> at noodles.example isn't open tonight.", code="closed")

    monkeypatch.setattr(slack_assistant, "confirm_draft", refusing)
    await tap_thread(slack, "Kim", ids.DRAFT_CONFIRM, card)
    shown = "Pho Corner &amp; &lt;Friends&gt; at noodles[.]example isn't open tonight."
    assert replaced(slack)[-1]["text"] == shown  # the service's words, made safe as a reply is


@pytest.mark.flag("assistant", on=True)
async def test_a_stale_draft_gets_the_services_own_error_and_stays_waiting(
    house: House, model: FakeModel, slack: Slack
) -> None:
    event_id = await to_voting(house)
    card = await dm_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    async with house.sessionmaker() as db:
        await calls.cancel_call(db, house["Alex"], event_id, now=house.clock())
    [row] = await draft_rows(house)
    async with house.sessionmaker() as db:
        with pytest.raises(EventRuleViolation) as refused:
            await drafts.confirm_draft(db, house.clock, AssistantConfig(True, True), house["Kim"], row.id)
    await tap_dm(slack, "Kim", ids.DRAFT_CONFIRM, card)
    assert updated(slack)[-1]["text"] == str(refused.value)
    [row] = await draft_rows(house)
    assert row.used_at is None and await pho_corner(house, event_id) is None


@pytest.mark.flag("assistant", on=True)
async def test_an_expired_draft_says_so(house: House, model: FakeModel, slack: Slack) -> None:
    await to_voting(house)
    card = await dm_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    house.clock.now += timedelta(minutes=31)
    await tap_dm(slack, "Kim", ids.DRAFT_CONFIRM, card)
    assert updated(slack)[-1]["text"] == EXPIRED


@pytest.mark.flag("assistant", on=True)
async def test_a_draft_used_elsewhere_says_so(house: House, model: FakeModel, slack: Slack) -> None:
    await to_voting(house)
    card = await dm_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    [row] = await draft_rows(house)
    async with house.sessionmaker() as db:
        await drafts.cancel_draft(db, house.clock, house["Kim"], row.id)  # as from the web
    await tap_dm(slack, "Kim", ids.DRAFT_CONFIRM, card)
    assert updated(slack)[-1]["text"] == USED


@pytest.mark.flag("assistant", on=True)
async def test_someone_elses_forwarded_or_replayed_card_does_nothing(
    house: House, model: FakeModel, slack: Slack
) -> None:
    """Lee taps Kim's ephemeral card (forwarded, or the tap replayed as Lee's), and Kim's DM
    card from Lee's own DM: neither is Lee's draft, so nothing changes, and Kim's still works."""
    event_id = await to_voting(house)
    card = await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    for action in (ids.DRAFT_CONFIRM, ids.DRAFT_CANCEL):
        await tap_thread(slack, "Lee", action, card)
        await tap_dm(slack, "Lee", action, card)
    assert {r["text"] for r in replaced(slack)} == {NO_SUCH_DRAFT}
    assert {(u["channel"], u["text"]) for u in updated(slack)} == {(LEE_DM, NO_SUCH_DRAFT)}
    assert posts(slack) == []
    [row] = await draft_rows(house)
    assert row.used_at is None and await pho_corner(house, event_id) is None
    await tap_thread(slack, "Kim", ids.DRAFT_CONFIRM, card)
    assert replaced(slack)[-1]["text"] == PROPOSED


@pytest.mark.flag("assistant", on=True)
async def test_a_dm_card_tapped_in_someone_elses_dm_is_refused(house: House, model: FakeModel, slack: Slack) -> None:
    await to_voting(house)
    card = await dm_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    await slack.click("Lee", ids.DRAFT_CONFIRM, card_values(card)[ids.DRAFT_CONFIRM], channel="D03KIM")
    assert updated(slack) == [] and replaced(slack) == []
    [row] = await draft_rows(house)
    assert row.used_at is None


# Who may tap, and when ----------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_with_the_switch_off_at_tap_time_nothing_changes(
    house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    event_id = await to_voting(house)
    card = await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    await switch(house, helper, on=False)
    for action in (ids.DRAFT_CONFIRM, ids.DRAFT_CANCEL):
        await tap_thread(slack, "Kim", action, card)
    assert slack.ephemerals("Kim") == [ASSISTANT_OFF, ASSISTANT_OFF] and replaced(slack) == []
    [row] = await draft_rows(house)
    assert row.used_at is None and await pho_corner(house, event_id) is None


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("settings_overrides", [{}])
async def test_without_the_assistant_a_draft_button_says_its_off(settings: Settings, house: House) -> None:
    assert settings.anthropic_api_key is None
    slack = await wire(house)
    await slack.click("Kim", ids.DRAFT_CONFIRM, "1", ephemeral=True)
    assert slack.ephemerals("Kim") == [ASSISTANT_OFF] and replaced(slack) == []


@pytest.mark.flag("assistant", on=True)
async def test_someone_dinnerbell_doesnt_know_gets_the_usual_refusal(
    house: House, model: FakeModel, slack: Slack
) -> None:
    await to_voting(house)
    card = await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    await tap_thread(slack, "U09NOBODY", ids.DRAFT_CONFIRM, card)
    [offer] = [e for e in ephemerals(slack) if e["user"] == "U09NOBODY"]
    assert offer["text"] == JOIN_OFFER and ids.JOIN_DINNERBELL in str(offer["blocks"]) and replaced(slack) == []
    [row] = await draft_rows(house)
    assert row.used_at is None


@pytest.mark.flag("assistant", on=True)
async def test_the_tap_is_acknowledged_before_any_work(
    house: House, model: FakeModel, slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slack gets its acknowledgement while Confirm is still waiting, so a slow service never
    passes Slack's 3 seconds; the work runs in a tracked task that shutdown settles."""
    await to_voting(house)
    card = await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    gate, started = asyncio.Event(), asyncio.Event()
    real = slack_assistant.confirm_draft

    async def slow(*args: Any) -> Any:
        started.set()
        await gate.wait()
        return await real(*args)

    monkeypatch.setattr(slack_assistant, "confirm_draft", slow)
    value = card_values(card)[ids.DRAFT_CONFIRM]
    response = await send(
        slack.conn.app, "interactive", block_action("U03KIM", ids.DRAFT_CONFIRM, value, ephemeral=True)
    )
    assert response.status == 200
    await asyncio.wait_for(started.wait(), timeout=10)
    assert replaced(slack) == [] and slack.conn.interactions.tasks
    gate.set()
    await slack.conn.interactions.settle()
    assert replaced(slack)[-1]["text"] == PROPOSED


# Catalog drafts -----------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_a_catalog_confirm_links_the_add_only_import_from_the_base_url(
    settings: Settings, house: House, model: FakeModel, slack: Slack
) -> None:
    model.script = [
        tool_call("draft_catalog_items", {"catalog_file": CATALOG}),
        answer("Drafted. Or import it yourself at https://evil.example/catalog/import/draft?t=forged"),
    ]
    await slack.dm("Sam", "Add Noodle Bar and Dumpling House")
    reply, card = posts(slack)
    assert "evil[.]example" in reply["text"] and "https://" not in reply["text"]
    slack.fake.clear()
    await tap_dm(slack, "Sam", ids.DRAFT_CONFIRM, card)
    [update] = updated(slack)
    url = card_values(update)[ids.DRAFT_IMPORT_LINK]
    assert url.startswith(f"{settings.base_url}/catalog/import/draft?t=") and "evil" not in url
    assert update["blocks"][-1] == {"type": "context", "elements": [{"type": "mrkdwn", "text": SIGN_IN_FIRST}]}
    [button] = [e for b in update["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert (button["text"]["text"], button["style"], "value" in button) == (REVIEW_IMPORT, "primary", False)
    assert update["text"] == f"{IMPORT_READY} <{url}|{REVIEW_IMPORT}>"
    [row] = await draft_rows(house)
    (token,) = parse_qs(urlsplit(url).query)["t"]
    assert token and row.used_at is not None
    async with house.sessionmaker() as db:
        names = {item.name for item in (await catalog_service.catalog(db, house["Sam"])).items}
    assert "Siam Garden" in names and "Noodle Bar" not in names  # nothing is saved until the import


@pytest.mark.flag("assistant", on=True)
async def test_the_import_links_click_is_acknowledged_and_ignored(house: House, slack: Slack) -> None:
    await slack.click("Sam", ids.DRAFT_IMPORT_LINK, None, channel=SAM_DM)
    assert slack.fake.calls == []


# The reply that carries the cards -----------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_the_mention_only_line_reaches_each_member_once_and_then_the_thread(
    house: House, slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def drafting(*args: Any, **kwargs: Any) -> Reply:
        return Reply("Here's a draft.", (DraftView(1, "proposal", "Propose Pho Corner", ()),))

    monkeypatch.setattr(slack_assistant, "converse", drafting)
    root = next_ts()
    note = mention_only_line(BOT_USER)
    await slack.mention("Kim", f"<@{BOT_USER}> propose", ts=root)
    await slack.mention("Kim", f"<@{BOT_USER}> and again", thread_ts=root)
    await slack.mention("Lee", f"<@{BOT_USER}> me too", thread_ts=root)
    bodies = [(e["user"], note in e["text"]) for e in ephemerals(slack) if e["blocks"][0]["type"] == "markdown"]
    assert bodies == [("U03KIM", True), ("U03KIM", False), ("U04LEE", True)]  # once to each
    assert posts(slack) == []
    plain = Reply("Taqueria, then.")
    monkeypatch.setattr(slack_assistant, "converse", lambda *args, **kwargs: _returning(plain))
    await slack.mention("Kim", f"<@{BOT_USER}> what's the pick?", thread_ts=root)
    await slack.mention("Kim", f"<@{BOT_USER}> thanks", thread_ts=root)
    assert [note in p["text"] for p in posts(slack)] == [True, False]  # then once to the thread


async def _returning(reply: Reply) -> Reply:
    return reply


@pytest.mark.flag("assistant", on=True)
async def test_a_reply_of_only_drafts_posts_only_the_cards(
    house: House, slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def drafting(*args: Any, **kwargs: Any) -> Reply:
        return Reply("", (DraftView(1, "proposal", "Propose Pho Corner", ()), DraftView(2, "offer", "Offer", ())))

    monkeypatch.setattr(slack_assistant, "converse", drafting)
    await slack.dm("Kim", "Draft two")
    assert [p["text"] for p in posts(slack)] == ["Propose Pho Corner", "Offer"]


@pytest.mark.flag("assistant", on=True)
async def test_a_card_slack_refuses_is_logged_and_the_rest_still_go(
    house: House, slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def drafting(*args: Any, **kwargs: Any) -> Reply:
        return Reply("Two drafts.", (DraftView(1, "proposal", "One", ()), DraftView(2, "offer", "Two", ())))

    monkeypatch.setattr(slack_assistant, "converse", drafting)
    refused = (Failure("msg_too_long"), Failure("msg_too_long"), Failure("invalid_blocks"))
    slack.fake.fail("chat.postEphemeral", *refused)
    await slack.mention("Kim", f"<@{BOT_USER}> draft two")
    body, again, one, two = ephemerals(slack)  # each tried, though the body (twice) and One were refused
    assert body["text"].startswith("Two drafts.") and body["blocks"][0]["type"] == "markdown"
    assert again["text"] == body["text"] and "blocks" not in again  # then as text alone (#284)
    assert (one["text"], two["text"]) == ("One", "Two")
    assert slack.conn.assistant is not None and len(slack.conn.assistant.replied) == 0


@pytest.mark.flag("assistant", on=True)
async def test_a_result_slack_wont_put_on_the_card_goes_in_its_thread(
    house: House, model: FakeModel, slack: Slack
) -> None:
    await to_voting(house)
    card = await thread_card(slack, model, "Kim", "draft_proposal", PROPOSE)
    root = "1790000555.000100"
    payload = block_action("U03KIM", ids.DRAFT_CONFIRM, card_values(card)[ids.DRAFT_CONFIRM], ephemeral=True)
    payload["container"]["thread_ts"] = root
    slack.fake.fail("response_url", Failure("expired_url", status=404))
    await send(slack.conn.app, "interactive", payload)
    await slack.conn.interactions.settle()
    [told] = ephemerals(slack)
    assert (told["user"], told["thread_ts"], told["text"]) == ("U03KIM", root, PROPOSED)
    assert posts(slack) == [] and updated(slack) == []


@pytest.mark.flag("assistant", on=True)
async def test_a_link_that_cant_be_built_leaves_the_draft_waiting(house: House, model: FakeModel, slack: Slack) -> None:
    """The link comes first, so a draft is never used without one."""
    from dinnerbell.channels.slack.interactions import BROKEN

    card = await dm_card(slack, model, "Sam", "draft_catalog_items", {"catalog_file": CATALOG})
    assert slack.conn.assistant is not None

    def broken(draft_id: int, member_id: int) -> str:
        raise RuntimeError("no link")

    slack.conn.assistant.import_link = broken
    await tap_dm(slack, "Sam", ids.DRAFT_CONFIRM, card)
    assert slack.ephemerals("Sam") == [BROKEN] and updated(slack) == []
    [row] = await draft_rows(house)
    assert row.used_at is None


def test_an_offer_result_naming_a_domain_isnt_a_link() -> None:
    from datetime import date

    from dinnerbell.domain.meals import Slot
    from dinnerbell.domain.offers import OfferPath
    from dinnerbell.services.assistant.drafts import OfferConfirmed
    from dinnerbell.services.offers import Posted

    done = OfferConfirmed(Posted(OfferPath.ATTACH, 1, Slot.DINNER, date(2026, 10, 22), "noodles.example"))
    text = slack_assistant.confirmed_text(done)
    assert "noodles[.]example is a proposal on it now" in text and "noodles.example" not in text
