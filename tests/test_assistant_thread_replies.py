# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replies only one member saw, in a channel thread (#366, spec 6.14, **Conversations**): a reply
with drafts goes to its member alone, so the conversation keeps it as theirs and no other
member's request carries it. Transcripts from before the mark still go to everyone, and the
requests that leave a reply out still alternate roles. The model is ``FakeModel``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest

from dinnerbell.config import Settings
from dinnerbell.db.models import AssistantConversationRow
from dinnerbell.domain.assistant import (
    DRAFT,
    MAX_HISTORY_BYTES,
    Pseudonyms,
    Turn,
    only_to_member,
    turns_for,
    web_key,
)
from dinnerbell.services.assistant import conversation
from dinnerbell.services.assistant.conversation import Assistant
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.database import Database, open_database
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, tool_call
from tests.test_assistant_draft_log import OFFER, THREAD, ask, conversation_row, switch, transcript

pytestmark = pytest.mark.anyio

DRAFTED = "Drafted your Siam Garden offer."
QUOTED = "quoted for context only, not a request:"


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


async def store_thread(house: House, turns: list[dict[str, Any]]) -> None:
    """``THREAD``'s conversation, stored as ``turns`` as written, with Kim as Member 1."""
    async with house.sessionmaker() as db:
        db.add(
            AssistantConversationRow(
                household_id=house.household.id,
                key=THREAD,
                private=False,
                owner_member_id=None,
                pseudonyms={str(house["Kim"].id): 1},
                transcript=turns,
                created_at=house.clock(),
                last_message_at=house.clock(),
            )
        )
        await db.commit()


async def label(house: House, who: str) -> str:
    found = Pseudonyms.from_json((await conversation_row(house)).pseudonyms).label(house[who].id)
    assert found is not None
    return found


def draft_it(model: FakeModel) -> None:
    model.script = [tool_call("draft_offer", OFFER | {"spare_servings": 0}), answer(DRAFTED)]


async def test_a_reply_only_its_member_saw_goes_only_with_their_requests(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """Kim drafts an offer in the channel thread, and the reply on it goes to her alone. Sam's
    next message there doesn't carry it, so Kim's message and Sam's go as one; Kim's does."""
    draft_it(model)
    kims = await ask(house, helper, "Kim", "Offer Siam Garden", key=THREAD)
    assert kims.only_to_member and len(kims.drafts) == 1
    assert (await transcript(house, THREAD))[-1] == {"role": "assistant", "text": DRAFTED, "shown_to": "Member 1"}

    model.script = [answer("Hi Sam.")]
    sams = await ask(house, helper, "Sam", "Hello", key=THREAD)
    assert not sams.only_to_member
    sam = await label(house, "Sam")
    [message] = model.requests[-1]["messages"]
    assert message == {
        "role": "user",
        "content": f"Earlier, from Member 1, {QUOTED}\n\nOffer Siam Garden\n\n"
        f"From {sam}, the member you're answering. This is the request:\n\nHello",
    }
    assert (await transcript(house, THREAD))[-1] == {"role": "assistant", "text": "Hi Sam."}  # everyone saw it

    model.script = [answer("It's waiting on your card.")]
    await ask(house, helper, "Kim", "Did you draft it?", key=THREAD)
    messages = model.requests[-1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]
    assert messages[1]["content"] == DRAFTED and messages[3]["content"] == "Hi Sam."


async def test_a_reply_saved_after_another_members_keeps_their_label_in_its_mark(
    house: House, helper: Assistant
) -> None:
    """Kim and Sam start the same thread at once, each as Member 1 in their own request. Sam's
    exchange is saved while Kim's waits on the model, so Kim keeps the label Sam's map gave her,
    and the mark on her reply names that label, as her message's ``author`` does."""
    sams = replace(helper, client=FakeModel(script=[answer("Hi Sam.")]))

    class Midway(FakeModel):
        async def create(self, request: Any, *, time_left: float, events: Any = None) -> Any:
            if not self.requests:
                await ask(house, sams, "Sam", "Hello", key=THREAD)
            return await super().create(request, time_left=time_left, events=events)

    kims = replace(
        helper, client=Midway(script=[tool_call("draft_offer", OFFER | {"spare_servings": 0}), answer(DRAFTED)])
    )
    assert (await ask(house, kims, "Kim", "Offer Siam Garden", key=THREAD)).only_to_member
    *_, asked, reply = await transcript(house, THREAD)
    assert asked["text"] == "Offer Siam Garden" and reply["text"] == DRAFTED
    assert reply["shown_to"] == asked["author"] == await label(house, "Kim") != "Member 1"


async def test_a_reply_stored_before_the_mark_still_goes_to_everyone(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """A thread's transcript from before #366 has no ``shown_to``: its replies go with every
    member's requests, as they did."""
    old = [
        {"role": "user", "text": "Thai tonight?", "author": "Member 1"},
        {"role": "assistant", "text": "Siam Garden, Member 1."},
    ]
    await store_thread(house, old)
    model.script = [answer("Sure.")]
    await ask(house, helper, "Sam", "Me too", key=THREAD)
    messages = model.requests[-1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == "Siam Garden, Member 1."
    assert (await transcript(house, THREAD))[:2] == old


async def test_trimmed_history_left_starting_with_a_reply_drops_it_and_joins_the_messages(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """At ``MAX_HISTORY_BYTES``, Sam's request drops the oldest message, and the reply it leaves
    at the front drops too. Kim's last reply was hers alone, so her message and Sam's go as one."""
    big = "x" * (MAX_HISTORY_BYTES - 4_000)
    await store_thread(
        house,
        [
            {"role": "user", "text": big, "author": "Member 1"},
            {"role": "assistant", "text": "b" * 3_000},
            {"role": "user", "text": "c" * 2_000, "author": "Member 1"},
            {"role": "assistant", "text": DRAFTED, "shown_to": "Member 1"},
        ],
    )
    model.script = [answer("OK.")]
    await ask(house, helper, "Sam", "And now?", key=THREAD)
    [message] = model.requests[-1]["messages"]
    assert message["role"] == "user"
    assert message["content"].startswith(f"Earlier, from Member 1, {QUOTED}\n\n{'c' * 2_000}\n\nFrom Member ")
    assert message["content"].endswith("This is the request:\n\nAnd now?")
    assert big not in str(message) and "b" * 3_000 not in str(message) and DRAFTED not in str(message)


async def test_a_private_conversation_never_marks_a_reply(house: House, helper: Assistant, model: FakeModel) -> None:
    """On the web, drafts and the reply are the member's anyway: nothing to mark."""
    draft_it(model)
    reply = await ask(house, helper, "Kim", "Offer Siam Garden")
    assert reply.drafts and not reply.only_to_member
    assert (await transcript(house, web_key(house["Kim"].id)))[-1] == {"role": "assistant", "text": DRAFTED}


def test_only_a_reply_with_drafts_in_a_channel_thread_goes_to_its_member_alone() -> None:
    assert only_to_member(private=False, drafts=1)
    assert not only_to_member(private=False, drafts=0)
    assert not only_to_member(private=True, drafts=2)


def test_the_mark_round_trips_and_anything_else_reads_as_unmarked() -> None:
    marked = Turn("assistant", "Drafted.", shown_to="Member 2")
    assert marked.to_json() == {"role": "assistant", "text": "Drafted.", "shown_to": "Member 2"}
    assert Turn.from_json(marked.to_json()) == marked
    assert Turn.from_json({"role": "assistant", "text": "Hi."}) == Turn("assistant", "Hi.")
    assert Turn.from_json({"role": "assistant", "text": "Hi.", "shown_to": 2}) == Turn("assistant", "Hi.")
    for role in ("user", DRAFT):  # only a reply can be shown to one member
        raw = {"role": role, "text": "Hi.", "author": "Member 1", "shown_to": "Member 2"}
        assert Turn.from_json(raw) == Turn(role, "Hi.", "Member 1")


def test_turns_for_keeps_a_marked_reply_only_for_its_member() -> None:
    turns = [
        Turn("user", "Offer it", "Member 1"),
        Turn("assistant", "Drafted.", shown_to="Member 1"),
        Turn("user", "Hello", "Member 2"),
        Turn("assistant", "Hi."),
    ]
    assert turns_for(turns, "Member 1") == tuple(turns)
    assert turns_for(turns, "Member 2") == (turns[0], turns[2], turns[3])


def test_messages_in_a_row_go_as_one_with_an_entry_kept_in_place() -> None:
    """Two members' messages with no reply between, then a reply, then the member's own: roles
    alternate, and a draft entry stays before the message after it."""
    entry = Turn(DRAFT, "Cancelled on Thursday, October 22: Set Siam Garden to +2", "Member 2")
    history = [
        Turn("user", "Offer it", "Member 1"),
        entry,
        Turn("user", "Hello", "Member 2"),
        Turn("assistant", "Hi."),
        Turn("user", "Anything else?", "Member 1"),
    ]
    messages = conversation._messages(history, Turn("user", "Thanks", "Member 2"))
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    first, _, last = (m["content"] for m in messages)
    assert first.startswith(f"Earlier, from Member 1, {QUOTED}\n\nOffer it\n\nDrafts Member 2 confirmed")
    assert first.endswith(f"- {entry.text}\n\nEarlier, from Member 2, the member you're answering:\n\nHello")
    assert last == (
        f"Earlier, from Member 1, {QUOTED}\n\nAnything else?\n\n"
        "From Member 2, the member you're answering. This is the request:\n\nThanks"
    )
