# SPDX-License-Identifier: AGPL-3.0-or-later
# ruff: noqa: F811
"""The assistant's memory (#367, ADR 0012, spec 6.14 **Memory**): the checks on what a rollup
writes, when it runs, what each request carries, housemates' Preferences, and the controls on
the Assistant page and Admin > Assistant. ``test_assistant_security`` has the attacks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dinnerbell.config import Settings
from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.memory import MemoryRepository
from dinnerbell.db.models import AssistantMemoryFactRow, AuditRow, HouseholdRow, MemberRow
from dinnerbell.domain import assistant_memory as am
from dinnerbell.domain.assistant import (
    DEFAULT_ROLLUP_MODEL,
    MODELS,
    AssistantSettings,
    Conversation,
    InvalidAssistantSettings,
    Pseudonyms,
    Turn,
    assistant_settings,
    check_assistant_settings,
    settings_to_json,
    web_key,
)
from dinnerbell.domain.assistant_memory import Fact
from dinnerbell.domain.preferences import Stance
from dinnerbell.services import members as member_service
from dinnerbell.services import reset
from dinnerbell.services.assistant import conversation, memory, prompt, retention, tools
from dinnerbell.services.assistant.conversation import Assistant, least_private_call_micros
from dinnerbell.services.assistant.tools import ToolResult, ToolSession
from tests.conftest import csrf_from, page
from tests.support.calls import House, household_members
from tests.support.fake_model import answer
from tests.support.fake_oidc import FakeProvider
from tests.test_admin import sign_in_as
from tests.test_assistant_security import (  # noqa: F401 - fixtures
    CONFIGURED,
    ROLLUP_MODEL,
    Attacker,
    database,
    facts_of,
    fetch,
    helper,
    hints,
    house,
    model,
    plant,
    remember,
    roll,
    rollup_answer,
    say,
    settings_overrides,
    stance,
    switch,
    web,
)
from tests.test_auth_flow import rows

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)


def fact(n: int, kind: str = "like", text: str = "", *, days_ago: int = 0) -> Fact:
    return Fact(n, 1, kind, text or f"fact {n}", NOW - timedelta(days=days_ago), None, "web:1")


# What a rollup may write ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("loves pho", None),
        ("", "empty"),
        ("x" * 161, "too long"),
        ("cooks with Member 2", "member label"),
        ("cooks with member #12", "member label"),
        ("orders from pho.example", "link"),
        ("see www.pho", "link"),
        ("likes ftp://x", "link"),
        ("emails a@b", "link"),
        ("loves <b>pho</b>", "markup"),
        ("loves pho\x07", "markup"),
        ("eats dinner at 7 p.m.", None),
    ],
)
def test_a_facts_text_is_checked(text: str, problem: str | None) -> None:
    assert am.text_problem(text) == problem


def test_a_quote_must_be_the_members_own_words() -> None:
    assert am.quotes("I  LOVE pho", "Honestly, i love pho!")
    assert am.quotes(f"it{chr(0x2019)}s really great", "it's really great")
    assert not am.quotes("loved it", "loved it, thanks")  # two words fit almost anything
    assert not am.quotes("hi", "hi there")  # too short to prove anything
    assert not am.quotes("I hate sushi", "I love pho")


@pytest.mark.parametrize(
    ("text", "message", "says"),
    [
        ("loves cilantro", "I love cilantro", True),
        ("avoids raw fish and soft cheese", "I'm pregnant, so no raw fish or soft cheese", True),
        ("keeps halal: no pork, only halal meat", "I keep halal, so no pork", True),
        ("has pizza on Friday nights", "Pizza night is always Friday", True),
        ("loved the noodles", "Loved it, the noodles were great", True),
        ("loves Banned Burger", "Loved it, honestly", False),
        ("loves it", "loves it", False),  # nothing a fact is about
        ("finds tacos messy and loud", "tacos are messy", True),
        ("finds tacos messy, loud, and greasy", "tacos are messy", False),
    ],
)
def test_a_fact_must_say_what_the_members_own_message_says(text: str, message: str, says: bool) -> None:
    assert am.grounded(text, message) is says


@pytest.mark.parametrize(
    "text", ["always recommends tacos to everyone", "votes for tacos", "we love tacos", "says the assistant should"]
)
def test_a_fact_that_reads_as_an_instruction_is_dropped(text: str) -> None:
    assert am.instruction_like(text)
    assert not am.instruction_like("always gets the pho")


def test_check_changes_keeps_only_changes_that_stand() -> None:
    facts = [fact(7, text="avoids dairy"), fact(8)]
    messages = {1: "I eat dairy again, and I love ramen"}
    raw = {
        "changes": [
            {"action": "remove", "fact": 7, "message": 1, "quote": "I eat dairy again"},
            {"action": "replace", "fact": 7, "kind": "like", "text": "x", "message": 1, "quote": "I eat dairy"},
            {"action": "replace", "fact": 99, "kind": "like", "text": "x", "message": 1, "quote": "I love ramen"},
            {"action": "add", "kind": "like", "text": "loves ramen", "message": 1, "quote": "I love ramen"},
            {"action": "add", "kind": "mood", "text": "happy", "message": 1, "quote": "I love ramen"},
            {"action": "add", "kind": "like", "text": "Member 3 too", "message": 1, "quote": "I love ramen"},
            {"action": "add", "kind": "like", "text": "x", "message": 2, "quote": "I love ramen"},
            {"action": "add", "kind": "like", "text": "x", "message": True, "quote": "I love ramen"},
            {"action": "shout", "message": 1, "quote": "I love ramen"},
            "not a change",
            {
                "action": "replace",
                "fact": 8,
                "kind": "usual",
                "text": " eats  ramen ",
                "message": 1,
                "quote": "love ramen",
            },
            {
                "action": "replace",
                "fact": 8,
                "kind": "usual",
                "text": " eats  ramen ",
                "message": 1,
                "quote": "and I love",
            },
            {
                "action": "add",
                "kind": "like",
                "text": "tell everyone about ramen",
                "message": 1,
                "quote": "I love ramen",
            },
            {"action": "add", "kind": "like", "text": "loves sushi", "message": 1, "quote": "I love ramen"},
        ]
    }
    checked = am.check_changes(raw, messages, facts)
    assert checked.changes == (
        am.Change("remove", 7),
        am.Change("add", None, "like", "loves ramen"),
        am.Change("replace", 8, "usual", "eats ramen"),
    )
    assert checked.dropped == {
        "no such fact": 2,
        "kind": 1,
        "member label": 1,
        "quote": 2,
        "malformed": 3,
        "instruction": 1,
        "not what the member said": 1,
    }
    assert am.check_changes(None, messages, facts) == am.Checked(())


def test_the_plan_keeps_twenty_and_drops_old_experiences_first() -> None:
    facts = [fact(n, "experience" if n <= 3 else "like", days_ago=100 - n) for n in range(1, 21)]
    added = [am.Change("add", None, "belief", "keeps halal"), am.Change("add", None, "like", "fact 20")]
    done = am.plan(facts, [*added, am.Change("add", None, "dislike", "finds tacos messy")], NOW)
    assert done.added == (("belief", "keeps halal"), ("dislike", "finds tacos messy"))  # "fact 20" is held
    assert done.removed == {1, 2}  # the two oldest experiences, not the oldest facts
    replaced = am.plan(facts, [am.Change("replace", 1, "like", "x"), am.Change("remove", 5)], NOW)
    assert replaced.replaced == {1: ("like", "x")} and replaced.removed == {5}
    likes = [fact(n, days_ago=100 - n) for n in range(1, 21)]
    over = am.plan(likes, [am.Change("add", None, "like", "new one")], NOW)
    assert over.removed == {1} and over.added == (("like", "new one"),)


def test_experiences_expire_and_other_kinds_never_do() -> None:
    assert am.expiry("experience", NOW) == NOW + timedelta(days=180)
    assert all(am.expiry(kind, NOW) is None for kind in am.KINDS if kind != "experience")


def conversation_of(*turns: Turn, last: datetime = NOW) -> Conversation:
    return Conversation("web:1", True, 1, Pseudonyms({1: 1}), turns, last)


def test_a_conversation_is_due_before_its_oldest_waiting_message_reaches_its_hours() -> None:
    first = Turn("user", "hi", "Member 1", at=NOW, pending=True)
    waiting = conversation_of(first, Turn("assistant", "hello"))
    assert not am.due(waiting, NOW + timedelta(hours=23, minutes=44), 24)
    assert am.due(waiting, NOW + timedelta(hours=23, minutes=45), 24)
    assert not am.due(conversation_of(Turn("user", "hi", "Member 1", at=NOW)), NOW + timedelta(days=9), 24)
    assert not am.due(waiting, NOW + timedelta(days=9), 0)
    old = conversation_of(Turn("user", "hi", "Member 1", pending=True), last=NOW - timedelta(hours=30))
    assert am.due(old, NOW, 24)  # past its hours: the last try
    big = [Turn("user", "x" * 9_000, "Member 1", at=NOW, pending=True) for _ in range(3)]
    assert am.due(conversation_of(*big), NOW, 24)
    assert len(am.taken(conversation_of(*big))) == 2
    assert len(am.taken(conversation_of(Turn("user", "x" * 30_000, "Member 1", pending=True)))) == 1


def test_rolled_marks_only_the_messages_taken() -> None:
    a, b = Turn("user", "a", "Member 1", at=NOW, pending=True), Turn("user", "b", "Member 1", at=NOW, pending=True)
    after = am.rolled((a, Turn("assistant", "r"), b), [a])
    assert [t.pending for t in after] == [False, False, True]
    assert am.cut_waiting((a, b), (b,)) == 1


def test_a_stored_message_keeps_its_time_and_whether_it_waits() -> None:
    turn = Turn("user", "hi", "Member 1", at=NOW, pending=True)
    assert Turn.from_json(turn.to_json()) == turn
    assert Turn.from_json({"role": "user", "text": "hi", "at": "nonsense", "pending": "yes"}) == Turn("user", "hi")
    assert Turn.from_json({"role": "user", "text": "hi", "at": "2026-11-27T12:00:00"}) == Turn("user", "hi")
    assert Turn.from_json({"role": "assistant", "text": "hi", "at": NOW.isoformat(), "pending": True}) == Turn(
        "assistant", "hi"
    )


def test_the_block_puts_the_member_answered_first_and_stops_at_its_budget() -> None:
    block = am.memory_block([("Member 1", True, [fact(1), fact(2, days_ago=1)]), ("Member 2", False, [fact(3)])])
    lines = block.split("\n")
    assert lines[:3] == [am.MEMORY_HEADING, "<memory>", "Member 1 (the member you are answering):"]
    assert lines[3].endswith('"fact 1"') and lines[4].endswith('"fact 2"') and lines[-1] == "</memory>"
    assert am.memory_block([("Member 1", True, [])]) == ""
    cut = am.memory_block([("Member 1", True, [fact(1)]), ("Member 2", False, [fact(2)])], max_bytes=40)
    assert "Member 2" not in cut


def test_pass_input_sends_the_members_own_messages_and_the_replies_around_them() -> None:
    turns = (
        Turn("assistant", "earlier reply"),
        Turn("user", "Thai?", "Member 2"),
        Turn("assistant", "How was Siam Garden?"),
        Turn("user", "Loved it", "Member 1", pending=True),
        Turn("assistant", "Great.", shown_to="Member 2"),
        Turn("assistant", "Noted."),
    )
    shared = am.pass_input(turns, "Member 1", [turns[3]], [fact(4)], "{}", replies=False)
    assert "Reply" not in shared.text and shared.messages == {1: "Loved it"}
    part = am.pass_input(turns, "Member 1", [turns[3]], [fact(4)], "{}")
    assert part.messages == {1: "Loved it"}
    assert 'Reply, context only: "How was Siam Garden?"' in part.text
    assert "Thai?" not in part.text and "Great." not in part.text and "earlier reply" not in part.text
    assert 'Reply, context only: "Noted."' in part.text  # the next reply this member saw
    assert 'Note 4 (like): "fact 4"' in part.text


# Settings ---------------------------------------------------------------------------------------


def test_memory_settings_read_forgivingly_and_check() -> None:
    assert assistant_settings({}) == AssistantSettings()
    assert assistant_settings({"assistant_memory": "yes", "assistant_rollup_model": "gpt"}).rollup_model == (
        DEFAULT_ROLLUP_MODEL
    )
    on = AssistantSettings(enabled=True, memory=True, rollup_model="claude-sonnet-5-5")
    assert assistant_settings(settings_to_json(on)) == on and on.remembers
    assert not AssistantSettings(memory=True).remembers
    with pytest.raises(InvalidAssistantSettings, match="rollup model"):
        check_assistant_settings(AssistantSettings(rollup_model="gpt"))
    assert DEFAULT_ROLLUP_MODEL in MODELS


def test_the_memory_prompt_rewords_the_lines_about_anyone_elses_preferences() -> None:
    plain, remembering = prompt.system_prompt(keeps_history=True), prompt.system_prompt(keeps_history=True, memory=True)
    assert prompt.OWN_ONLY in plain and prompt.DONT_SPECULATE in plain
    assert prompt.OWN_ONLY not in remembering and prompt.DONT_SPECULATE not in remembering
    assert prompt.OWN_WITH_MEMORY in remembering and remembering.endswith(prompt.MEMORY_NOTE)
    assert prompt.system_prompt(keeps_history=False, memory=True).endswith(prompt.NO_HISTORY_NOTE)


async def test_memory_raises_the_smallest_messages_floor(house: House) -> None:
    members = await household_members(house)
    assert least_private_call_micros("claude-sonnet-5-5", members, memory=True) > least_private_call_micros(
        "claude-sonnet-5-5", members
    )


# Requests and rollups -------------------------------------------------------------------------------


async def test_each_request_carries_every_active_members_facts_and_a_deactivated_members_go(
    house: House, helper: Assistant, model: Attacker
) -> None:
    """Sam's request carries Sam's facts first, then Kim's, in private and in a channel thread.
    Deactivating Kim deletes hers."""
    await remember(house, helper)
    await plant(house, "Kim", "avoids dairy")
    await plant(house, "Sam", "loves pho")
    await say(house, helper, "Sam", "What should we get?")
    await say(house, helper, "Sam", "And here?", key="slack:C0HOUSE:1790000000.000300", private=False)
    for request in model.requests:
        asked = request["messages"][-1]["content"]
        assert asked.index('"loves pho"') < asked.index('"avoids dairy"') < asked.index("From Member 1")
    async with house.sessionmaker() as db:
        await member_service.set_member_active(db, house["Alex"], house["Kim"].id, False, now=house.clock())
    assert await facts_of(house, "Kim") == []


async def test_a_rollup_writes_replaces_and_removes_only_its_authors_facts(
    house: House, helper: Assistant, model: Attacker
) -> None:
    await remember(house, helper)
    await plant(house, "Sam", "avoids dairy")
    await plant(house, "Kim", "avoids dairy")
    await stance(house, "Sam", "Curry Leaf", Stance(score=2))
    await say(house, helper, "Sam", "I eat dairy again. I always get the pho")
    async with house.sessionmaker() as db:
        [sams] = await MemoryRepository(db).facts(house.household.id, house.clock(), house["Sam"].id)
        [kims] = await MemoryRepository(db).facts(house.household.id, house.clock(), house["Kim"].id)
    model.rollups = [
        rollup_answer(
            {"action": "remove", "fact": sams.id, "quote": "I eat dairy again"},
            {"action": "remove", "fact": kims.id, "quote": "I eat dairy again"},
            {"kind": "usual", "text": "always gets the pho", "quote": "I always get the pho"},
        )
    ]
    result = await roll(house, helper, web_key(house["Sam"].id))
    assert result == memory.Pass(changed=1)
    assert await facts_of(house, "Sam") == ["always gets the pho"]
    assert await facts_of(house, "Kim") == ["avoids dairy"]
    [request] = model.rolling
    assert request["model"] == ROLLUP_MODEL and "tools" not in request
    assert request["output_config"]["format"]["schema"] == am.ROLLUP_SCHEMA
    assert '"name": "Curry Leaf", "level": "2"' in request["messages"][0]["content"]


async def test_a_rollup_that_fails_or_races_leaves_the_messages_waiting(
    house: House, helper: Assistant, model: Attacker, monkeypatch: pytest.MonkeyPatch
) -> None:
    await remember(house, helper)
    await say(house, helper, "Sam", "I love pho")
    key = web_key(house["Sam"].id)
    model.rollups = [answer("not json", model=ROLLUP_MODEL), answer("{}", model=ROLLUP_MODEL)]
    assert (await roll(house, helper, key)).waiting == 0  # unreadable: no changes, but rolled up
    await say(house, helper, "Sam", "I love ramen")
    model.rollups = [conversation.ModelCallFailed(maybe_billed=False)]
    assert (await roll(house, helper, key)).waiting == 1
    original = MemoryRepository.facts
    calls = 0

    async def racing(self: MemoryRepository, *args: Any, **kwargs: Any) -> list[Fact]:
        nonlocal calls
        calls += 1
        found = await original(self, *args, **kwargs)
        return [*found, fact(999)] if calls == 2 else found  # a Remove or another pass meanwhile

    monkeypatch.setattr(MemoryRepository, "facts", racing)
    model.rollups = [rollup_answer({"text": "loves ramen", "quote": "I love ramen"})]
    assert (await roll(house, helper, key)).waiting == 1
    monkeypatch.setattr(MemoryRepository, "facts", original)
    model.rollups = [rollup_answer({"text": "loves ramen", "quote": "I love ramen"})]
    assert (await roll(house, helper, key)).changed == 1
    assert await facts_of(house, "Sam") == ["loves ramen"]


async def test_the_purge_job_rolls_up_before_it_deletes(house: House, helper: Assistant, model: Attacker) -> None:
    """15 minutes before the oldest waiting message is Conversation hours old, the purge job rolls
    it up; one still waiting at expiry gets a last try, then the conversation goes."""
    await remember(house, helper, conversation_hours=2)
    await say(house, helper, "Sam", "I love pho")
    house.clock.now += timedelta(hours=1, minutes=44)
    assert await memory.roll_up_due(house.sessionmaker, helper) == 0 and model.rolling == []
    house.clock.now += timedelta(minutes=1)
    model.rollups = [rollup_answer({"text": "loves pho", "quote": "I love pho"})]
    assert await memory.roll_up_due(house.sessionmaker, helper) == 1
    await say(house, helper, "Kim", "I love ramen")
    house.clock.now += timedelta(hours=3)
    model.rollups = [conversation.ModelCallFailed(maybe_billed=False)]
    await memory.roll_up_due(house.sessionmaker, helper)
    assert await retention.purge_idle_conversations(house.sessionmaker, house.clock()) == 2
    assert await facts_of(house, "Kim") == [] and await facts_of(house, "Sam") == ["loves pho"]
    assert await memory.roll_up_due(house.sessionmaker, None) == 0


async def test_a_former_members_messages_are_rolled_up_into_nothing(
    house: House, helper: Assistant, model: Attacker
) -> None:
    await remember(house, helper)
    thread = "slack:C0HOUSE:1790000000.000400"
    await say(house, helper, "Kim", "I love ramen", key=thread, private=False)
    async with house.sessionmaker() as db:
        await member_service.set_member_active(db, house["Alex"], house["Kim"].id, False, now=house.clock())
    await roll(house, helper, thread)
    assert model.rolling == []
    async with house.sessionmaker() as db:
        stored = await AssistantRepository(db).conversation(house.household.id, thread)
    assert stored is not None and am.waiting(stored) == ()


async def test_expired_facts_are_purged(house: House, helper: Assistant) -> None:
    await plant(house, "Sam", "loved the noodles", kind="experience")
    async with house.sessionmaker() as db:
        await db.execute(AssistantMemoryFactRow.__table__.update().values(expires_at=house.clock()))
        await db.commit()
    assert await memory.purge_expired_facts(house.sessionmaker, house.clock()) == 1
    assert await memory.purge_expired_facts(house.sessionmaker, house.clock()) == 0


async def test_scrub_test_and_reset_history_forget_memory(house: House, helper: Assistant) -> None:
    await remember(house, helper)
    await plant(house, "Sam", "loves pho")
    async with house.sessionmaker() as db:
        forgotten = await retention.forget_for_test(db, house.household.id)
        await db.commit()
    assert forgotten.facts == 1 and await facts_of(house, "Sam") == []
    async with house.sessionmaker() as db:
        assert not assistant_settings(await conversation.household_settings(db, house.household.id)).memory
    assert "assistant_memory_fact" in reset.CLEAR


# Housemates' Preferences -------------------------------------------------------------------------


async def housemates(house: House, args: dict[str, str], *, who: str = "Sam", on: bool = True) -> ToolResult:
    member = house[who]
    session = ToolSession(
        key=web_key(member.id),
        pseudonyms=conversation.with_roster(Pseudonyms().with_member(member.id), await household_members(house)),
    )
    async with house.sessionmaker() as db:
        return await tools.run_tool(
            "look_up_housemates_preferences",
            args,
            db=db,
            member=member,
            private=True,
            now=house.clock(),
            session=session,
            memory=on,
        )


async def test_housemates_preferences_send_levels_needs_and_can_usually_only(house: House) -> None:
    """Kim vetoes Banned Burger, loves Curry Leaf, and hated Old Favorite with a note: her levels
    go, never her rating or its note, and never Sam's own."""
    found = json.loads((await housemates(house, {"member": "", "name": ""})).content)
    by_label = {h["member"]: h for h in found["housemates"]}
    assert set(by_label) == {"Member 2", "Member 3", "Member 4"}  # Alex, Kim, Lee; never Sam
    kim = next(h for h in found["housemates"] if {"name": "Banned Burger", "level": "veto"} in h["items"])
    assert {"name": "Curry Leaf", "level": "2"} in kim["items"]
    assert set(kim) == {"member", "tags", "items", "more", "dietary_needs", "can_usually"}
    assert "RatingNoteSecret" not in json.dumps(found) and "hated" not in json.dumps(found)
    one = json.loads((await housemates(house, {"member": kim["member"], "name": "curry"})).content)
    assert [h["items"] for h in one["housemates"]] == [[{"name": "Curry Leaf", "level": "2"}]]


async def test_housemates_preferences_refuse_without_memory_or_a_housemate(house: House) -> None:
    assert (await housemates(house, {"member": "", "name": ""}, on=False)).is_error
    assert (await housemates(house, {"member": "Member 1", "name": ""})).content == tools.NO_SUCH_HOUSEMATE
    assert (await housemates(house, {"member": "Member 9", "name": ""})).content == tools.NO_SUCH_HOUSEMATE
    assert (await housemates(house, {"member": "Kim", "name": ""})).is_error
    names = {t.name for t in tools.tools_for(False, memory=True)}
    assert "look_up_housemates_preferences" not in names


# The pages ---------------------------------------------------------------------------------------


def _add_fact(settings: Settings, member_email: str, text: str) -> int:
    engine = create_engine(settings.sync_database_url)
    try:
        with Session(engine) as db:
            [household] = list(db.scalars(select(HouseholdRow)))
            member = next(m for m in db.scalars(select(MemberRow)) if m.email == member_email)
            row = AssistantMemoryFactRow(
                household_id=household.id,
                member_id=member.id,
                kind="like",
                text=text,
                source_key="web:1",
                written_at=datetime.now(UTC),
                expires_at=None,
            )
            db.add(row)
            db.commit()
            return row.id
    finally:
        engine.dispose()


@pytest.fixture
def remembering(web: TestClient, model: Attacker) -> TestClient:
    model.active = ("admin",)
    data = {"enabled": "on", "memory": "on", "cap_dollars": "5", "conversation_hours": "24"}
    response = web.post("/admin/assistant", data=data | {"rollup_model": ROLLUP_MODEL, "csrf_token": csrf_from(web)})
    assert response.status_code == 303
    return web


def test_the_admin_turns_memory_on_and_it_is_audited(remembering: TestClient, migrated: Settings) -> None:
    [household] = rows(migrated, HouseholdRow)
    assert household.settings["assistant_memory"] is True
    [audit] = [a for a in rows(migrated, AuditRow) if a.action == "household.assistant_updated"]
    assert audit.payload["assistant_memory"] == {"from": False, "to": True}
    shown = page(remembering.get("/admin/assistant"))
    assert "Turn on Memory" in shown and "Memory keeps no notes." in shown
    refused = remembering.post(
        "/admin/assistant",
        data={"enabled": "on", "memory": "on", "cap_dollars": "5", "conversation_hours": "0"}
        | {"csrf_token": csrf_from(remembering)},
    )
    assert refused.status_code == 400 and "Memory needs Conversation hours above 0" in page(refused)


def test_the_admin_clears_memory_from_the_page(remembering: TestClient, migrated: Settings) -> None:
    _add_fact(migrated, migrated.admin_email, "loves pho")
    shown = page(remembering.get("/admin/assistant"))
    assert "1 note kept." in shown and "Clear memory" in shown and "Delete all memory" not in shown
    bad = remembering.post("/admin/assistant/memory/clear", data={"member": "x", "csrf_token": csrf_from(remembering)})
    assert bad.status_code == 400 and "Choose whose memory to clear." in page(bad)
    done = remembering.post(
        "/admin/assistant/memory/clear", data={"member": "all", "csrf_token": csrf_from(remembering)}
    )
    assert done.status_code == 303 and rows(migrated, AssistantMemoryFactRow) == []
    assert "Memory cleared." in page(remembering.get("/admin/assistant?notice=cleared"))


def test_a_member_sees_and_removes_their_own_notes(
    remembering: TestClient, migrated: Settings, model: Attacker
) -> None:
    fact_id = _add_fact(migrated, migrated.admin_email, "loves pho")
    shown = page(remembering.get("/assistant"))
    assert "What the assistant remembers about you" in shown and "loves pho" in shown
    removed = remembering.post(f"/assistant/memory/{fact_id}/remove", data={"csrf_token": csrf_from(remembering)})
    assert removed.status_code == 303 and rows(migrated, AssistantMemoryFactRow) == []
    assert "Removed." in page(remembering.get("/assistant?notice=forgot"))


def test_start_over_rolls_up_first(remembering: TestClient, migrated: Settings, model: Attacker) -> None:
    sent = remembering.post("/assistant", data={"message": "I love pho", "csrf_token": csrf_from(remembering)})
    assert sent.status_code == 303
    model.rollups = [rollup_answer({"text": "loves pho", "quote": "I love pho"})]
    gone = remembering.post("/assistant/start-over", data={"csrf_token": csrf_from(remembering)})
    assert gone.status_code == 303 and len(model.rolling) == 1
    assert [r.text for r in rows(migrated, AssistantMemoryFactRow)] == ["loves pho"]


def test_a_member_who_isnt_an_admin_cant_clear_memory(
    remembering: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    remembering.post(
        "/admin/members",
        data={
            "display_name": "Sam",
            "email": "sam@example.com",
            "role": "member",
            "csrf_token": csrf_from(remembering),
        },
    )
    _add_fact(migrated, migrated.admin_email, "loves pho")
    sign_in_as(remembering, provider, "sam-sub", "sam@example.com")
    refused = remembering.post(
        "/admin/assistant/memory/clear", data={"member": "all", "csrf_token": csrf_from(remembering)}
    )
    assert refused.status_code == 403 and len(rows(migrated, AssistantMemoryFactRow)) == 1
