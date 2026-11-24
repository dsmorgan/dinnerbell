# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a conversation records when its member confirms or cancels a draft (#363, spec 6.14,
**Drafts**): the entry, in labels; the next request, which carries it at the start of the
member's message; Conversation hours, which keep nothing at 0; a channel thread, where only the
member's own entries go with their requests; and conversations stored before entries existed,
which still load. Confirm and Cancel run the real services; the model is ``FakeModel``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.meals import household_settings
from dinnerbell.db.models import AssistantConversationRow
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.assistant import HOURS_KEY, AssistantSettings, slack_key, web_key
from dinnerbell.domain.events import EventRuleViolation
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.services.assistant import admin, drafts, history
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
OFFERED = f"Confirmed: {OFFER_TITLE}. Your dinner offer for tomorrow is posted."
LEVEL = {"what": "", "name": "Siam Garden", "level": "2"}
LEVEL_CANCELLED = "Cancelled: Set Siam Garden to +2"
THREAD = slack_key("C0HOUSE", "1790000000.000200")
RECORDED = "used these drafts. Dinnerbell recorded what happened"


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
        f"Before this message, Member 1 {RECORDED}; not a request:\n- {OFFERED}\n- {LEVEL_CANCELLED}\n\n"
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
    assert earlier.startswith(f"Before this message, Member 1 {RECORDED}") and "Earlier, from Member 1" in earlier
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
    async with house.sessionmaker() as db:
        row = AssistantConversationRow(
            household_id=house.household.id,
            key=key,
            private=True,
            owner_member_id=kim.id,
            pseudonyms={str(kim.id): 1},
            transcript=old,
            created_at=house.clock(),
            last_message_at=house.clock(),
        )
        db.add(row)
        await db.commit()
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
