# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a conversation records when its member confirms or cancels a draft (#363, spec 6.14,
**Drafts**): the entry, in labels; the next request, which carries it at the start of the
member's message; Conversation hours, which keep nothing at 0; a channel thread, where only the
member's own entries go with their requests; and conversations stored before entries existed,
which still load. Confirm and Cancel run the real services; the model is ``FakeModel``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from dinnerbell.config import Settings
from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.models import AssistantConversationRow
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.assistant import (
    DRAFT,
    HOURS_KEY,
    MAX_HISTORY_BYTES,
    AssistantSettings,
    Pseudonyms,
    Turn,
    slack_key,
    web_key,
)
from dinnerbell.domain.assistant_catalog import NOT_SHOWN
from dinnerbell.domain.assistant_coordinating import held, person
from dinnerbell.domain.assistant_drafts import Draft, DraftKind
from dinnerbell.domain.events import EventRuleViolation
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.services.assistant import admin, conversation, coordinating, direct, draft_log, drafts, history, tools
from dinnerbell.services.assistant.conversation import Assistant, Reply, converse
from dinnerbell.services.assistant.history import Shown
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.preferences import set_my_preference
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, tool_call

pytestmark = pytest.mark.anyio

OFFER = {"name": "Siam Garden", "mode": "delivery", "slot": "dinner", "day": "tomorrow", "time": "12:15"}
OFFER_TITLE = "Offer to order Siam Garden, dinner tomorrow at 12:15 PM"
DAY = "Thursday, October 22"
"""The household's day at ``FakeClock``'s start, in Chicago."""
OFFERED = f"Confirmed on {DAY}: {OFFER_TITLE}. Your dinner offer for tomorrow is posted."
LEVEL = {"what": "", "name": "Siam Garden", "level": "2"}
LEVEL_CANCELLED = f"Cancelled on {DAY}: Set Siam Garden to +2"
THREAD = slack_key("C0HOUSE", "1790000000.000200")
RECORDED = (
    "Drafts Member 1 confirmed or cancelled, and what was done at once or undone, before this message "
    "(Dinnerbell's record, not a request):"
)


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"anthropic_api_key": "sk-ant-test-not-a-real-key"}


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> AsyncIterator[House]:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    yield house
    await house.close()


@pytest.fixture(autouse=True)
def drafts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every tool drafts here, as past a message's limit for acting at once (#374): these tests
    are about drafts' entries. ``test_direct_actions_record_done_and_undone`` turns it back."""
    monkeypatch.setattr(tools, "MAX_DIRECT_ACTIONS", 0)


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
async def helper(settings: Settings, model: FakeModel, house: House) -> Assistant:
    helper = Assistant(AssistantConfig.from_settings(settings), model, house.clock)
    await switch(house, helper)
    return helper


async def switch(house: House, helper: Assistant, **values: Any) -> None:
    async with house.sessionmaker() as db:
        settings = AssistantSettings(**({"enabled": True} | values))
        await admin.update_assistant(db, helper.config, house["Alex"], settings, now=house.clock())


async def ask(house: House, helper: Assistant, who: str, text: str, *, key: str | None = None) -> Reply:
    member = house[who]
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, member, key or web_key(member.id), private=key is None, text=text)
    assert reply is not None
    return reply


async def confirm(house: House, helper: Assistant, who: str, draft: int) -> Any:
    async with house.sessionmaker() as db:
        return await drafts.confirm_draft(db, house.clock, helper.config, house[who], draft)


async def cancel(house: House, who: str, draft: int) -> None:
    async with house.sessionmaker() as db:
        await drafts.cancel_draft(db, house.clock, house[who], draft)


async def transcript(house: House, key: str) -> list[dict[str, Any]]:
    async with house.sessionmaker() as db:
        row = await db.scalar(select(AssistantConversationRow).where(AssistantConversationRow.key == key))
    assert row is not None
    return list(row.transcript)


async def conversation_row(house: House) -> AssistantConversationRow:
    async with house.sessionmaker() as db:
        row = await db.scalar(select(AssistantConversationRow))
    assert row is not None
    return row


async def store(house: House, member_id: int, turns: list[dict[str, Any]]) -> None:
    """A member's web conversation, stored as ``turns`` as written, as an earlier build left it."""
    async with house.sessionmaker() as db:
        row = AssistantConversationRow(
            household_id=house.household.id,
            key=web_key(member_id),
            private=True,
            owner_member_id=member_id,
            pseudonyms={str(member_id): 1},
            transcript=turns,
            created_at=house.clock(),
            last_message_at=house.clock(),
        )
        db.add(row)
        await db.commit()


def last_message(model: FakeModel) -> str:
    content = model.requests[-1]["messages"][-1]["content"]
    assert isinstance(content, str)
    return content


async def test_confirm_and_cancel_are_recorded_and_the_next_request_says_so(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """Kim confirms an offer and cancels a level: the conversation records both, in order, after
    the exchange that drafted them, and her next message tells the model before her words."""
    kim = house["Kim"]
    model.script = [tool_call("draft_offer", OFFER | {"spare_servings": 0}), tool_call("draft_preference", LEVEL)]
    model.script += [answer("Two drafts.")]
    offer, level = (await ask(house, helper, "Kim", "Offer Siam Garden, and I love it")).drafts
    await confirm(house, helper, "Kim", offer.id)
    await cancel(house, "Kim", level.id)
    key = web_key(kim.id)
    assert (await transcript(house, key))[-2:] == [
        {"role": "draft", "text": OFFERED, "author": "Member 1"},
        {"role": "draft", "text": LEVEL_CANCELLED, "author": "Member 1"},
    ]
    model.script = [answer("Glad it's posted.")]
    await ask(house, helper, "Kim", "Did my offer go out?")
    messages = model.requests[-1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert last_message(model) == (
        f"{RECORDED}\n- {OFFERED}\n- {LEVEL_CANCELLED}\n\n"
        "From Member 1, the member you're answering. This is the request:\n\nDid my offer go out?"
    )
    # Kept after the reply too, where it happened, and shown on the page in that order.
    async with house.sessionmaker() as db:
        shown = await history.web_conversation(db, helper.config, kim, house.clock())
    assert shown.turns == (
        Shown(True, "Offer Siam Garden, and I love it"),
        Shown(False, "Two drafts."),
        Shown(False, OFFERED, draft=True),
        Shown(False, LEVEL_CANCELLED, draft=True),
        Shown(True, "Did my offer go out?"),
        Shown(False, "Glad it's posted."),
    )
    model.script = [answer("Yes.")]
    await ask(house, helper, "Kim", "Sure?")
    earlier = model.requests[-1]["messages"][2]["content"]
    assert earlier.startswith(RECORDED) and "Earlier, from Member 1" in earlier
    assert RECORDED not in last_message(model)  # each entry goes once, before the message after it


async def test_a_refused_confirm_records_nothing(house: House, helper: Assistant, model: FakeModel) -> None:
    """Kim changes her level on Preferences after the assistant drafted one, so Confirm refuses
    the draft and leaves it waiting: the conversation records nothing."""
    model.script = [tool_call("draft_preference", LEVEL), answer("Drafted.")]
    [level] = (await ask(house, helper, "Kim", "I love Siam Garden")).drafts
    key = web_key(house["Kim"].id)
    before = await transcript(house, key)
    async with house.sessionmaker() as db:
        item = house.items["Siam Garden"].id
        await set_my_preference(db, house["Kim"], TargetType.ITEM, item, Stance(score=-1), now=house.clock())
    with pytest.raises(EventRuleViolation, match="changed since the draft"):
        await confirm(house, helper, "Kim", level.id)
    assert await transcript(house, key) == before
    async with house.sessionmaker() as db:
        assert [d.id for d in await drafts.waiting(db, house["Kim"], key, house.clock())] == [level.id]


async def test_at_conversation_hours_zero_nothing_is_recorded(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """At 0, a conversation still stored, as one could be for a moment before the purge, gets no
    entry, and no new conversation is made for one."""
    model.script = [tool_call("draft_offer", OFFER | {"spare_servings": 0}), answer("Drafted.")]
    [offer] = (await ask(house, helper, "Kim", "Offer Siam Garden")).drafts
    key = web_key(house["Kim"].id)
    before = await transcript(house, key)
    async with house.sessionmaker() as db:  # the setting alone, without Admin's purge of what it no longer keeps
        stored = await household_settings(db, house.household.id)
        await HouseholdRepository(db).update_settings(house.household.id, stored | {HOURS_KEY: 0})
        await db.commit()
    await confirm(house, helper, "Kim", offer.id)
    assert await transcript(house, key) == before
    await switch(house, helper, conversation_hours=0)
    model.script = [tool_call("draft_preference", LEVEL), answer("Drafted.")]
    [level] = (await ask(house, helper, "Kim", "I love Siam Garden")).drafts
    await cancel(house, "Kim", level.id)
    async with house.sessionmaker() as db:
        assert list(await db.scalars(select(AssistantConversationRow))) == []


async def test_in_a_channel_thread_only_the_members_own_entries_go_with_their_requests(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """Kim confirms an offer she drafted in the channel thread. Sam's next message there doesn't
    carry it, since what Kim did with her card is hers; Kim's does."""
    model.script = [tool_call("draft_offer", OFFER | {"spare_servings": 0}), answer("Drafted.")]
    [offer] = (await ask(house, helper, "Kim", "Offer Siam Garden", key=THREAD)).drafts
    await confirm(house, helper, "Kim", offer.id)
    assert (await transcript(house, THREAD))[-1] == {"role": "draft", "text": OFFERED, "author": "Member 1"}
    model.script = [answer("Hi Sam.")]
    await ask(house, helper, "Sam", "Hello", key=THREAD)
    assert RECORDED not in str(model.requests[-1]["messages"])
    model.script = [answer("It's posted.")]
    await ask(house, helper, "Kim", "Is it posted?", key=THREAD)
    assert f"- {OFFERED}" in str(model.requests[-1]["messages"])


async def test_a_conversation_stored_before_draft_entries_still_loads(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """A transcript from before #363, with only messages and replies, reads, shows, and goes out
    as it did, and a confirm adds an entry after it."""
    kim = house["Kim"]
    key = web_key(kim.id)
    old = [
        {"role": "user", "text": "Thai tonight?", "author": "Member 1"},
        {"role": "assistant", "text": "Siam Garden, Member 1."},
    ]
    await store(house, kim.id, old)
    async with house.sessionmaker() as db:
        shown = await history.web_conversation(db, helper.config, kim, house.clock())
    assert shown.turns == (Shown(True, "Thai tonight?"), Shown(False, "Siam Garden, Kim."))
    model.script = [tool_call("draft_offer", OFFER | {"spare_servings": 0}), answer("Drafted.")]
    [offer] = (await ask(house, helper, "Kim", "Offer it")).drafts
    assert "Thai tonight?" in str(model.requests[0]["messages"])
    await confirm(house, helper, "Kim", offer.id)
    assert await transcript(house, key) == [
        *old,
        {"role": "user", "text": "Offer it", "author": "Member 1"},
        {"role": "assistant", "text": "Drafted."},
        {"role": "draft", "text": OFFERED, "author": "Member 1"},
    ]


async def test_with_the_switch_off_a_cancel_records_nothing(house: House, helper: Assistant, model: FakeModel) -> None:
    """Cancel works with the assistant off, but the conversation keeps nothing new then, as
    ``_keep`` doesn't."""
    model.script = [tool_call("draft_preference", LEVEL), answer("Drafted.")]
    [level] = (await ask(house, helper, "Kim", "I love Siam Garden")).drafts
    key = web_key(house["Kim"].id)
    before = await transcript(house, key)
    await switch(house, helper, enabled=False)
    await cancel(house, "Kim", level.id)
    assert await transcript(house, key) == before


async def test_an_entry_never_moves_the_last_messages_time(house: House, helper: Assistant, model: FakeModel) -> None:
    model.script = [tool_call("draft_preference", LEVEL), answer("Drafted.")]
    [level] = (await ask(house, helper, "Kim", "I love Siam Garden")).drafts
    asked_at = house.clock()
    house.clock.advance(timedelta(minutes=20))
    await cancel(house, "Kim", level.id)
    async with house.sessionmaker() as db:
        row = await db.scalar(select(AssistantConversationRow))
    assert row is not None and row.last_message_at == asked_at and row.transcript[-1]["role"] == "draft"


async def test_a_conversation_past_its_hours_records_nothing(house: House, helper: Assistant, model: FakeModel) -> None:
    """Conversation hours passed since the last message, before the purge ran: no entry."""
    model.script = [tool_call("draft_preference", LEVEL), answer("Drafted.")]
    [level] = (await ask(house, helper, "Kim", "I love Siam Garden")).drafts
    key = web_key(house["Kim"].id)
    before = await transcript(house, key)
    async with house.sessionmaker() as db:
        await db.execute(
            update(AssistantConversationRow).values(last_message_at=house.clock() - timedelta(hours=24, minutes=1))
        )
        await db.commit()
    await cancel(house, "Kim", level.id)
    assert await transcript(house, key) == before


async def test_a_person_and_a_held_value_are_recorded_by_label_and_as_not_shown(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """What the conversation keeps never holds a name or a value the model never reads, such as
    an item's notes as they were: a person goes in as the conversation's label, a held value as
    not shown."""
    model.script = [tool_call("draft_preference", LEVEL), answer("Drafted.")]
    [level] = (await ask(house, helper, "Kim", "I love Siam Garden")).drafts
    async with house.sessionmaker() as db:
        draft = await AssistantRepository(db).draft(house.household.id, level.id)
    assert draft is not None
    sam = house["Sam"].id
    named = replace(draft, title=f"Hand off to {person(sam)}, notes {held('notes')}")
    outcome = f"Confirmed. {person(sam)} coordinates now."
    async with house.sessionmaker() as db:
        kept = await draft_log.record_use(db, house["Kim"], named, confirmed=True, outcome=outcome, now=house.clock())
    assert kept
    [*_, entry] = await transcript(house, web_key(house["Kim"].id))
    label = Pseudonyms.from_json((await conversation_row(house)).pseudonyms).label(sam)
    assert entry["text"] == f"Confirmed on {DAY}: Hand off to {label}, notes {NOT_SHOWN}. {label} coordinates now."


async def test_an_entry_saved_while_a_message_runs_is_kept_before_it(house: House, helper: Assistant) -> None:
    """Kim cancels a draft while her next message waits on the model: ``_keep`` saves that
    exchange after the entry, keeping both."""
    first = FakeModel(script=[tool_call("draft_preference", LEVEL), answer("Drafted.")])
    [level] = (await ask(house, replace(helper, client=first), "Kim", "I love Siam Garden")).drafts

    class Midway(FakeModel):
        async def create(self, request: Any, *, time_left: float, events: Any = None) -> Any:
            await cancel(house, "Kim", level.id)
            return await super().create(request, time_left=time_left, events=events)

    await ask(house, replace(helper, client=Midway(script=[answer("Sure.")])), "Kim", "Never mind")
    roles = [(t["role"], t["text"]) for t in await transcript(house, web_key(house["Kim"].id))]
    assert roles == [
        ("user", "I love Siam Garden"),
        ("assistant", "Drafted."),
        ("draft", LEVEL_CANCELLED),
        ("user", "Never mind"),
        ("assistant", "Sure."),
    ]


async def test_history_trimmed_to_fit_can_start_with_an_entry(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """At ``MAX_HISTORY_BYTES``, the oldest exchange drops, and the reply it leaves at the front
    drops too, so the history starts with the entry, at the start of the message after it."""
    kim = house["Kim"]
    big = "x" * (MAX_HISTORY_BYTES - 4_000)  # with the rest, just over the limit
    old = [
        {"role": "user", "text": big, "author": "Member 1"},
        {"role": "assistant", "text": "b" * 3_000},
        {"role": "draft", "text": LEVEL_CANCELLED, "author": "Member 1"},
        {"role": "user", "text": "c" * 2_000, "author": "Member 1"},
        {"role": "assistant", "text": "Sure."},
    ]
    await store(house, kim.id, old)
    model.script = [answer("OK.")]
    await ask(house, helper, "Kim", "And now?")
    messages = model.requests[-1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"].startswith(f"{RECORDED}\n- {LEVEL_CANCELLED}\n\nEarlier, from Member 1")
    assert big not in str(messages) and "b" * 3_000 not in str(messages)


def test_a_request_can_start_with_an_entry_folded_into_the_message_after_it() -> None:
    entry = Turn(DRAFT, LEVEL_CANCELLED, "Member 1")
    history = [entry, Turn("user", "Hi", "Member 1"), Turn("assistant", "Hello.")]
    messages = conversation._messages(history, Turn("user", "Thanks", "Member 1"))
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == (
        f"{RECORDED}\n- {LEVEL_CANCELLED}\n\nEarlier, from Member 1, the member you're answering:\n\nHi"
    )
    assert messages[2]["content"].startswith("From Member 1")


def test_a_decided_result_keeps_a_marked_place_name_as_text() -> None:
    """The drafting tool stores the pick's name as plain text already; the result plains it again,
    so a marker in a stored payload can never become a person in what the card or the
    conversation shows (#363)."""
    at = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
    payload = {"name": f"Pho {person(7)} House", "mode": "pickup", "lead": None, "pickup": 3}
    draft = Draft(1, 1, 1, web_key(1), DraftKind.DECIDE, payload, "Lock it", (), at, at)
    told = coordinating._decided_text(draft, "today", Slot.DINNER)
    assert told == f"Confirmed. Today's dinner call is decided on Pho 7 House. {person(3)} picks it up."


async def test_direct_actions_record_done_and_undone(
    house: House, helper: Assistant, model: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#374: a level set at once records "Done", with its record's ID for the page, after the
    exchange; Undo records "Undone"; and the next request carries both before Kim's words."""
    monkeypatch.setattr(tools, "MAX_DIRECT_ACTIONS", 5)
    kim = house["Kim"]
    model.script = [tool_call("draft_preference", LEVEL), answer("Done, it's +2.")]
    [record] = (await ask(house, helper, "Kim", "I love Siam Garden")).drafts
    assert record.record and record.undo
    done = f"Done on {DAY}: Set Siam Garden to +2. Your level for Siam Garden is +2 now."
    key = web_key(kim.id)
    assert (await transcript(house, key))[-1] == {
        "role": "draft",
        "text": done,
        "author": "Member 1",
        "record": record.id,
    }
    async with house.sessionmaker() as db:
        undone = await direct.undo(db, house.clock, helper.config, kim, record.id)
    assert undone.text == "Undone. Your level for Siam Garden is 0 now."
    undone_entry = f"Undone on {DAY}: Set Siam Garden to +2. Your level for Siam Garden is 0 now."
    assert (await transcript(house, key))[-1] == {"role": "draft", "text": undone_entry, "author": "Member 1"}
    async with house.sessionmaker() as db:
        shown = await history.web_conversation(db, helper.config, kim, house.clock())
    assert shown.turns[-2:] == (
        Shown(False, done, draft=True, record=record.id),
        Shown(False, undone_entry, draft=True),
    )
    model.script = [answer("It's back to 0.")]
    await ask(house, helper, "Kim", "What's my level now?")
    assert last_message(model).startswith(f"{RECORDED}\n- {done}\n- {undone_entry}\n\nFrom Member 1")
