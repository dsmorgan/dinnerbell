# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coordinating through the assistant (#252, spec 6.14, **Tools** and **Drafts**), in private
conversations only: look up the call, lock a pick or break a tie, shuffle, change the plan as
**Adjust** does, hand off, mark done, and call off. Each is a draft, and each **Confirm** runs the
use case the call page or Slack's button runs, as the member with a member's role, so the
services' own checks decide who may: an admin who isn't the coordinator is refused.

The household is the usual one (``tests.support.calls.furnish``): Alex (the admin), Sam, Kim, and
Lee (whose usual settings offer to order). Alex, Sam, Kim, and Lee answer In for dinner, Sam
offers to pick up, and Kim coordinates. Ranking proposes Siam Garden and Taqueria. In Kim's
conversation, Kim is Member 1, and the lookup labels Alex, Sam, and Lee, in that order, Member 2
to Member 4. The assistant is on.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Iterator
from datetime import date, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.catalog import CatalogRepository, ItemValues
from dinnerbell.db.events import ProposalRepository
from dinnerbell.db.models import AssistantDraftRow
from dinnerbell.domain import events as rules
from dinnerbell.domain.assistant import AssistantSettings, Pseudonyms, slack_key
from dinnerbell.domain.assistant_coordinating import FIRST_VOLUNTEER, ROLES_TOLD
from dinnerbell.domain.assistant_drafts import COORDINATING
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import STALE_CALL, AssignmentRole, EventState, ProposalSource, RoleOffer, Status
from dinnerbell.domain.guests import GuestInput
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.logs import JsonFormatter
from dinnerbell.services import activity, call_admin, calls, coordinator, guests, members
from dinnerbell.services.assistant import admin, coordinating, drafts, tools
from dinnerbell.services.assistant.call_drafts import ActionConfirmed
from dinnerbell.services.assistant.conversation import Assistant, converse
from dinnerbell.services.assistant.prompt import SYSTEM_PROMPT
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.assistant.tools import PRIVATE_ONLY, ToolContext, ToolResult, ToolSession
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from tests.support.calls import MEAL, OPENS, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, tool_call

pytestmark = pytest.mark.anyio

CONFIGURED = AssistantConfig(key_set=True, flag_on=True)
DINNER = {"slot": "dinner", "day": "today"}
NAMES = ("Alex", "Sam", "Kim", "Lee", "Gwendolyn")
DM = slack_key("D0KIM", "1790000000.000100")
THREAD = slack_key("C0HOUSE", "1790000000.000200")
NO_ROLES = {"way": "", "lead": "", "pickup": ""}
COORDINATING_TOOLS = {
    "look_up_coordinating",
    "draft_decide",
    "draft_shuffle",
    "draft_plan",
    "draft_hand_off",
    "draft_mark_done",
    "draft_call_off",
}


def lock(name: str = "Taqueria", mode: str = "pickup", *, tie: bool = False, **roles: str) -> dict[str, Any]:
    return DINNER | {"pick": 0, "name": name, "mode": mode, "break_tie": tie} | NO_ROLES | roles


def change(**parts: str) -> dict[str, Any]:
    return DINNER | {"meal_time": "", "place": "", "way": "", "lead": "", "pickup": ""} | parts


SAMPLES: dict[str, dict[str, Any]] = {
    "look_up_coordinating": DINNER,
    "draft_decide": lock(),
    "draft_shuffle": DINNER,
    "draft_plan": change(meal_time="19:00"),
    "draft_hand_off": DINNER | {"to": "Member 2"},
    "draft_mark_done": DINNER,
    "draft_call_off": DINNER | {"reason": ""},
}


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
async def switched_on(house: House) -> None:
    async with house.sessionmaker() as db:
        await admin.update_assistant(db, CONFIGURED, house["Alex"], AssistantSettings(enabled=True), now=house.clock())


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
async def helper(settings: Settings, model: FakeModel, house: House) -> Assistant:
    return Assistant(AssistantConfig.from_settings(settings), model, house.clock)


async def act(house: House, use_case: Any, *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


async def voting(house: House, *, guest: bool = False) -> int:
    """Alex, Sam, Kim, and Lee are in; Sam offers to pick up; it's voting, and Kim coordinates.
    With ``guest``, Kim brings Gwendolyn, a new guest without Slack, who's in."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in ("Alex", "Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    if guest:
        await act(house, guests.invite, house["Kim"], event_id, new_guest=GuestInput("Gwendolyn"))
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Kim"].id)
    return event_id


async def decided(house: House) -> int:
    """As ``voting``, then Kim locks Taqueria (pickup): Sam picks up, and Lee orders."""
    event_id = await voting(house)
    taqueria = next(p for p in (await house.context(event_id)).picks if p.item_id == house.items["Taqueria"].id)
    await act(house, calls.lock, house["Kim"], event_id, taqueria.id)
    assert (await house.event(event_id)).state is EventState.DECIDED
    return event_id


def kims(house: House) -> Pseudonyms:
    """Kim's conversation as the lookup leaves it: Kim, then Alex, Sam, and Lee."""
    return Pseudonyms({house[n].id: number for number, n in enumerate(("Kim", "Alex", "Sam", "Lee"), start=1)})


async def tool(
    house: House,
    name: str,
    args: dict[str, Any],
    *,
    who: str = "Kim",
    private: bool = True,
    labels: Pseudonyms | None = None,
) -> ToolResult:
    member = house[who]
    pseudonyms = (
        labels if labels is not None else (kims(house) if who == "Kim" else Pseudonyms().with_member(member.id))
    )
    session = ToolSession(key=DM, pseudonyms=pseudonyms)
    async with house.sessionmaker() as db:
        return await tools.run_tool(
            name, args, db=db, member=member, private=private, now=house.clock(), session=session
        )


def draft_id(result: ToolResult) -> int:
    assert not result.is_error, result.content
    return int(result.content.split()[1])


async def row(house: House, draft: int) -> AssistantDraftRow:
    async with house.sessionmaker() as db:
        found = await db.get(AssistantDraftRow, draft)
    assert found is not None
    return found


async def rows(house: House) -> list[AssistantDraftRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AssistantDraftRow).order_by(AssistantDraftRow.id)))


async def card(house: House, draft: int, who: str = "Kim") -> drafts.DraftView:
    """The draft's card, as its member's conversation draws it."""
    async with house.sessionmaker() as db:
        waiting = await drafts.waiting(db, house[who], DM, house.clock())
    [found] = [v for v in waiting if v.id == draft]
    return found


async def confirm(house: House, who: str | Member, draft: int) -> Any:
    member = house[who] if isinstance(who, str) else who
    async with house.sessionmaker() as db:
        return await drafts.confirm_draft(db, house.clock, CONFIGURED, member, draft)


async def holders(house: House, event_id: int) -> dict[AssignmentRole, str]:
    ctx = await house.context(event_id)
    return {a.role: ctx.name(a.member_id) for a in ctx.assignments if a.role is not AssignmentRole.COORDINATOR}


def strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def assert_no_names(*texts: str) -> None:
    for text in texts:
        for name in NAMES:
            assert not re.search(rf"\b{name}\b", text), (name, text)


# Done when ---------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_lock_the_taco_place_member_2_picks_it_up_in_a_dm_drafts_then_confirm_decides(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """In her DM, Kim says "lock the taco place, @Sam picks it up", so the model reads Member 2.
    It looks the call up and drafts a lock with Member 2 picking up, which changes nothing. Kim's
    card names Sam; the model reads Member 2. Confirm locks Taqueria through ``calls.lock`` with
    the roles in the same transaction, so Sam hears once that he picks it up. Nobody's name goes
    to the model, then or in the next message, after the card and Confirm named Sam."""
    event_id = await voting(house)
    kim, sam = house["Kim"], house["Sam"]
    model.script = [
        tool_call("look_up_coordinating", {"slot": "", "day": ""}, call_id="toolu_look"),
        tool_call("draft_decide", lock(pickup="Member 2") | {"slot": "", "day": ""}, call_id="toolu_lock"),
        answer("Here's the lock: Member 2 picks it up."),
    ]

    def mentions(label: Any) -> str:
        return f"lock the taco place, {label(sam.id)} picks it up"

    async with house.sessionmaker() as db:
        reply = await converse(db, helper, kim, DM, private=True, text=mentions)
    assert reply is not None and reply.text == "Here's the lock: Sam picks it up."
    looked = json.loads(model.requests[1]["messages"][-1]["content"][0]["content"])
    assert looked["in"] == [
        {"who": "Member 1", "offers": []},
        {"who": "Member 2", "offers": ["pickup"]},
        {"who": "Member 3", "offers": []},
        {"who": "Member 4", "offers": ["order"]},
    ]
    assert {p["name"] for p in looked["picks"]} == {"Siam Garden", "Taqueria"}
    assert all(p["tied"] is False for p in looked["picks"]) and looked["guests_in"] == 0
    summary = model.requests[2]["messages"][-1]["content"][0]["content"]
    assert "Member 2 picks it up." in summary
    [card] = reply.drafts
    assert (card.kind, card.title) == ("decide", "Lock Taqueria (pickup)")
    assert card.confirm_lines == (
        "It locks Taqueria (pickup): today's dinner call is decided on it now, and voting ends.",
        "It assigns the roles with it, as Assign roles does, so nobody is asked to choose:",
        "Sam picks it up.",
        FIRST_VOLUNTEER,
        "If the roles can't be assigned, nothing is locked.",
    )
    stored = await row(house, card.id)
    assert_no_names(stored.title, *stored.lines)  # stored by member ID, never a name or a label
    assert not any("Member" in line for line in stored.lines)
    assert (await house.event(event_id)).state is EventState.VOTING  # nothing until Confirm
    assert_no_names(json.dumps([r["messages"] for r in model.requests]))

    before = len(await house.outbox())
    done = await confirm(house, "Kim", card.id)
    assert done == ActionConfirmed(event_id, "Confirmed. Today's dinner call is decided on Taqueria. Sam picks it up.")
    event = await house.event(event_id)
    assert event.state is EventState.DECIDED and event.decided_item_id == house.items["Taqueria"].id
    assert event.roles_due_at is None
    assert (await holders(house, event_id))[AssignmentRole.PICKUP] == "Sam"
    decided = [a for a in await house.audits() if a.action == "event.decided"]
    assert decided[-1].payload["roles_named"] is True
    assert told((await house.outbox())[before:], sam) == ["assignment_made"]  # once, never made then ended
    with pytest.raises(drafts.DraftUnavailable):
        await confirm(house, "Kim", card.id)

    model.script = [answer("You're welcome.")]
    model.requests.clear()
    async with house.sessionmaker() as db:
        await converse(db, helper, kim, DM, private=True, text="thanks")
    assert_no_names(json.dumps(model.requests))


@pytest.mark.flag("assistant", on=True)
async def test_the_same_request_in_a_channel_thread_is_refused_with_a_pointer_to_the_dm(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """In a channel thread the coordinating tools aren't offered: the model's calls are refused,
    nothing is drafted, and the prompt says to ask in a direct message."""
    event_id = await voting(house)
    kim = house["Kim"]
    model.script = [
        tool_call(
            "draft_decide",
            lock(pickup="Member 2"),
            call_id="toolu_0",
            **{f"toolu_{name}": (name, SAMPLES[name]) for name in sorted(COORDINATING_TOOLS)},
        ),
        answer("Ask me in a DM."),
    ]
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, kim, THREAD, private=False, text="lock the taco place, Member 2 picks it up")
    assert reply is not None and reply.drafts == ()
    assert {t["name"] for t in model.requests[0]["tools"]}.isdisjoint(COORDINATING_TOOLS)
    for block in model.requests[1]["messages"][-1]["content"]:
        assert block["is_error"] and "There's no tool named" in block["content"]
    assert await rows(house) == []
    assert (await house.event(event_id)).state is EventState.VOTING
    assert "In a shared thread, tell \nthe member" not in SYSTEM_PROMPT
    assert "In a shared thread, tell the member to ask in a direct message with Dinnerbell instead." in SYSTEM_PROMPT


async def test_exactly_these_tools_are_coordinating_and_private_only() -> None:
    by_name = {t.name: t for t in tools.TOOLS}
    assert set(by_name) >= COORDINATING_TOOLS and set(SAMPLES) == COORDINATING_TOOLS
    assert all(by_name[n].private_only for n in COORDINATING_TOOLS)
    assert {t.name for t in tools.tools_for(False)}.isdisjoint(COORDINATING_TOOLS)
    assert {t.name for t in tools.tools_for(True)} >= COORDINATING_TOOLS
    assert {k.value for k in COORDINATING} == {"decide", "shuffle", "plan", "hand_off", "mark_done", "call_off"}


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("name", sorted(COORDINATING_TOOLS))
async def test_each_coordinating_tool_refuses_in_a_channel_thread(house: House, name: str) -> None:
    """In a channel thread the tool isn't there at all; called some other way, it refuses by
    itself, pointing to a direct message. In private it runs."""
    await voting(house)
    result = await tool(house, name, SAMPLES[name], private=False)
    assert result == ToolResult(f"There's no tool named {name!r} here.", is_error=True)
    run = next(t for t in tools.TOOLS if t.name == name).run
    async with house.sessionmaker() as db:
        ctx = ToolContext(db, tools.tool_actor(house["Kim"]), False, house.clock(), ToolSession(key=THREAD))
        with pytest.raises(tools.ToolRefusal) as refused:
            await run(ctx, SAMPLES[name])
    assert str(refused.value) == PRIVATE_ONLY and "direct message" in PRIVATE_ONLY
    assert await tool(house, name, SAMPLES[name]) != result  # in private, it runs
    assert all(r.kind != "catalog" for r in await rows(house))


# Who may ----------------------------------------------------------------------------------------

VOTING_TRIES = {
    "look_up_coordinating": DINNER,
    "draft_decide": lock(),
    "draft_shuffle": DINNER,
    "draft_hand_off": DINNER | {"to": "Member 2"},
    "draft_call_off": DINNER | {"reason": "Rain"},
}
DECIDED_TRIES = {
    "look_up_coordinating": DINNER,
    "draft_plan": change(meal_time="19:00"),
    "draft_hand_off": DINNER | {"to": "Member 2"},
    "draft_mark_done": DINNER,
    "draft_call_off": DINNER | {"reason": ""},
}
"""Before the meal time: only an admin may mark it done that early, an admin's override."""


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("stage", ["voting", "decided"])
async def test_an_admin_who_isnt_the_coordinator_is_refused_as_a_member_is(house: House, stage: str) -> None:
    """Alex, an admin who isn't the coordinator, gets exactly the refusal Sam, a member, gets, from
    every coordinating tool, though Alex's own role would pass each check; Kim, the coordinator,
    gets a draft or a lookup. Nothing is drafted for Alex or Sam."""
    event_id = await (voting(house) if stage == "voting" else decided(house))
    tries = VOTING_TRIES if stage == "voting" else DECIDED_TRIES
    event, alex = await house.event(event_id), house["Alex"]
    if stage == "voting":  # Alex's own admin role would pass each check the tools refuse him by
        rules.check_can_lock(event, alex)
        rules.check_can_shuffle(event, alex)
        assert rules.check_can_cancel(event, alex) is rules.ActingAs.ADMIN
    else:
        rules.check_can_manage_decision(event, alex)
        rules.check_can_mark_done(event, alex, None, now=house.clock())
    for name, args in tries.items():
        as_admin = await tool(house, name, args, who="Alex")
        as_member = await tool(house, name, args, who="Sam")
        assert as_admin.is_error and as_admin == as_member, (name, as_admin, as_member)
        assert "/admin" not in as_admin.content
        as_coordinator = await tool(house, name, args)
        if name == "draft_mark_done":  # the coordinator waits for the meal time, as any member does
            assert as_coordinator == ToolResult("You can mark the meal done once it's meal time.", is_error=True)
        else:
            assert not as_coordinator.is_error, (name, as_coordinator)
    assert {r.member_id for r in await rows(house)} == {house["Kim"].id}


# Each draft, and what Confirm runs ---------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_breaking_a_tie_chooses_a_tied_pick_and_refuses_one_that_isnt(house: House) -> None:
    event_id = await voting(house)
    ctx = await house.context(event_id)
    siam, taqueria = (next(p for p in ctx.picks if p.item_id == house.items[n].id) for n in ("Siam Garden", "Taqueria"))
    await act(house, calls.vote, house["Sam"], event_id, siam.id)
    await act(house, calls.vote, house["Lee"], event_id, taqueria.id)
    looked = json.loads((await tool(house, "look_up_coordinating", DINNER)).content)
    assert {p["name"]: p["tied"] for p in looked["picks"]} == {"Siam Garden": True, "Taqueria": True}
    assert "votes" not in json.dumps(looked) and "Member" not in json.dumps(looked["picks"])
    await act(house, calls.unvote, house["Lee"], event_id, taqueria.id)
    refused = await tool(house, "draft_decide", lock("Taqueria", tie=True))
    assert refused == ToolResult(coordinator.NOT_TIED, is_error=True)
    await act(house, calls.vote, house["Lee"], event_id, taqueria.id)
    draft = draft_id(await tool(house, "draft_decide", lock("Siam Garden", tie=True)))
    assert (await row(house, draft)).title == "Break the tie with Siam Garden (pickup)"
    done = await confirm(house, "Kim", draft)
    assert done == ActionConfirmed(event_id, "Confirmed. Today's dinner call is decided on Siam Garden.")
    event = await house.event(event_id)
    assert event.decided_item_id == house.items["Siam Garden"].id and event.tie_by == house["Kim"].id


@pytest.mark.flag("assistant", on=True)
async def test_a_shuffle_redraws_through_the_service_and_one_made_meanwhile_is_refused(house: House) -> None:
    for name in ("Noodle Bar", "Curry House", "Falafel Stop"):
        await house.add_item(name, Mode.PICKUP, cuisine="Thai")
    event_id = await voting(house)
    draft = draft_id(await tool(house, "draft_shuffle", DINNER))
    assert list((await row(house, draft)).lines)[-1] == "You'll have 2 shuffles left."
    done = await confirm(house, "Kim", draft)
    assert isinstance(done, ActionConfirmed) and done.text.endswith("2 shuffles left.")
    assert (await house.event(event_id)).shuffles == 1
    stale = draft_id(await tool(house, "draft_shuffle", DINNER))
    await act(house, coordinator.shuffle, house["Kim"], event_id)
    with pytest.raises(rules.EventRuleViolation, match="shuffled since the draft"):
        await confirm(house, "Kim", stale)
    assert (await row(house, stale)).used_at is None and (await house.event(event_id)).shuffles == 2


@pytest.mark.flag("assistant", on=True)
async def test_the_plan_changes_as_adjust_changes_it(house: House) -> None:
    """The meal time, then the place and way with who orders, then who picks up: each a draft
    whose Confirm runs ``coordinator.adjust``. The same meal time again is nothing to draft."""
    event_id = await decided(house)
    assert await tool(house, "draft_plan", change(meal_time="18:30")) == ToolResult(
        coordinating.NOTHING_TO_DO, is_error=True
    )
    draft = draft_id(await tool(house, "draft_plan", change(meal_time="19:00")))
    found = await row(house, draft)
    assert found.title == "Change the plan for today's dinner"
    assert list(found.lines) == ["The meal time moves from 6:30 PM to 7:00 PM.", ROLES_TOLD]
    assert await confirm(house, "Kim", draft) == ActionConfirmed(
        event_id, "Confirmed. The plan for today's dinner is changed."
    )
    meal = (await house.event(event_id)).meal_time
    assert meal is not None and meal == MEAL + timedelta(minutes=30)

    already = await tool(house, "draft_plan", change(place="Siam Garden", way="delivery", lead="Member 4"))
    assert already == ToolResult(coordinating.ALREADY.format(label="Member 4", does="orders it"), is_error=True)
    drafted = await tool(house, "draft_plan", change(place="Siam Garden", way="delivery", lead="Member 3"))
    draft = draft_id(drafted)
    assert "Member 3 orders it." in drafted.content  # the model reads the label
    assert list((await card(house, draft)).confirm_lines)[:3] == [
        "The place changes from Taqueria to Siam Garden. People keep their roles if the new way has them.",
        "It switches from pickup to delivery.",
        "Sam orders it.",  # the card names him to Kim
    ]
    await confirm(house, "Kim", draft)
    event = await house.event(event_id)
    assert (event.decided_item_id, event.decided_mode) == (house.items["Siam Garden"].id, Mode.DELIVERY)
    assert await holders(house, event_id) == {AssignmentRole.ORDER: "Sam"}

    draft = draft_id(await tool(house, "draft_plan", change(way="pickup", pickup="Member 2")))
    assert "Alex picks it up." in (await card(house, draft)).confirm_lines
    await confirm(house, "Kim", draft)
    assert (await holders(house, event_id))[AssignmentRole.PICKUP] == "Alex"
    bad = await tool(house, "draft_plan", change(lead="Lee"))
    assert bad == ToolResult(coordinating.LABEL_ONLY, is_error=True)


@pytest.mark.flag("assistant", on=True)
async def test_a_plan_changed_elsewhere_since_the_draft_is_never_written_over(house: House) -> None:
    event_id = await decided(house)
    draft = draft_id(await tool(house, "draft_plan", change(meal_time="19:00")))
    await act(house, calls.set_meal_time, house["Kim"], event_id, time(18, 45))
    with pytest.raises(rules.EventRuleViolation) as refused:
        await confirm(house, "Kim", draft)
    assert str(refused.value) == coordinating.CHANGED
    assert (await row(house, draft)).used_at is None
    assert (await house.event(event_id)).meal_time == MEAL + timedelta(minutes=15)


@pytest.mark.flag("assistant", on=True)
async def test_hand_off_mark_done_and_call_off_run_their_use_cases(house: House) -> None:
    event_id = await decided(house)
    house.clock.now = MEAL
    done = draft_id(await tool(house, "draft_mark_done", DINNER))
    off = draft_id(await tool(house, "draft_call_off", DINNER | {"reason": "  Rain,\n sorry "}))
    assert '"Rain, sorry"' in " ".join((await row(house, off)).lines)
    handing = draft_id(await tool(house, "draft_hand_off", DINNER | {"to": "Member 3"}))
    assert (await card(house, handing)).title == "Hand off coordinating today's dinner to Sam"
    assert await confirm(house, "Kim", handing) == ActionConfirmed(
        event_id, "Confirmed. Sam coordinates today's dinner now."
    )
    assert (await house.event(event_id)).coordinator_id == house["Sam"].id
    assert await confirm(house, "Kim", done) == ActionConfirmed(event_id, "Confirmed. Today's dinner is done.")
    assert (await house.event(event_id)).state is EventState.DONE
    with pytest.raises(rules.EventRuleViolation, match="changed since the draft"):
        await confirm(house, "Kim", off)  # it was decided when drafted


@pytest.mark.flag("assistant", on=True)
async def test_a_call_off_ends_the_call_with_the_members_reason(house: House) -> None:
    event_id = await voting(house)
    off = draft_id(await tool(house, "draft_call_off", DINNER | {"reason": "Rain, sorry"}))
    assert await confirm(house, "Kim", off) == ActionConfirmed(
        event_id, "Confirmed. Today's dinner call is called off."
    )
    event = await house.event(event_id)
    assert event.state is EventState.CANCELLED and event.end_note == "Rain, sorry"


@pytest.mark.flag("assistant", on=True)
async def test_the_member_who_started_a_call_can_call_it_off_until_its_decided(house: House) -> None:
    house.clock.now = OPENS
    started = await act(house, calls.start_call, house["Sam"], Slot.LUNCH, date(2026, 10, 23))
    off = draft_id(await tool(house, "draft_call_off", {"slot": "lunch", "day": "tomorrow", "reason": ""}, who="Sam"))
    await confirm(house, "Sam", off)
    assert (await house.event(started.id)).state is EventState.CANCELLED
    refused = await tool(house, "draft_call_off", {"slot": "lunch", "day": "tomorrow", "reason": ""}, who="Lee")
    assert refused.is_error


# Stale runs --------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_a_draft_for_an_earlier_run_of_the_call_is_refused(house: House) -> None:
    """Kim drafts marking the meal done; Alex re-runs the call, and it's decided again on the new
    run before Kim confirms. ``mark_done`` takes no run, so the draft's own run check refuses it,
    and the draft stays unused."""
    event_id = await decided(house)
    house.clock.now = MEAL
    draft = draft_id(await tool(house, "draft_mark_done", DINNER))
    off_draft = draft_id(await tool(house, "draft_call_off", DINNER | {"reason": ""}))
    await act(house, call_admin.rerun, house["Alex"], event_id)
    rerun = await house.event(event_id)
    assert rerun.state is EventState.COLLECTING
    await act(house, calls.close_window, house["Kim"], event_id)
    taqueria = next(p for p in (await house.context(event_id)).picks if p.item_id == house.items["Taqueria"].id)
    await act(house, calls.lock, house["Alex"], event_id, taqueria.id)
    again = await house.event(event_id)
    assert again.state is EventState.DECIDED and again.run == rerun.run
    for stale in (draft, off_draft):
        with pytest.raises(rules.EventRuleViolation) as refused:
            await confirm(house, "Kim", stale)
        assert str(refused.value) == STALE_CALL
        assert (await row(house, stale)).used_at is None
    assert (await house.event(event_id)).state is EventState.DECIDED


# Pseudonyms --------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_a_label_never_targets_a_guest_and_the_service_refuses_the_rest(house: House) -> None:
    """Kim's map holds Gwendolyn, her guest, as Member 5, Mia, who never answered, as Member 6,
    and Lee, deactivated since, as Member 4. A guest is no one a label names, even for a role the
    service would let a guest do; the services refuse someone deactivated or not in, in their
    words, which name nobody."""
    await house.add_member("Mia")
    event_id = await voting(house, guest=True)
    [gwendolyn] = (await house.context(event_id)).guests
    await act(house, members.set_member_active, house["Alex"], house["Lee"].id, False)
    labels = Pseudonyms({**kims(house).numbers, gwendolyn.id: 5, house["Mia"].id: 6})

    async def kim(name: str, args: dict[str, Any]) -> ToolResult:
        return await tool(house, name, args, labels=labels)

    guest = coordinating.NOT_A_LABEL.format(label="Member 5")
    assert await kim("draft_decide", lock(pickup="Member 5")) == ToolResult(guest, is_error=True)
    assert await kim("draft_hand_off", DINNER | {"to": "Member 5"}) == ToolResult(guest, is_error=True)
    nobody = coordinating.NOT_A_LABEL.format(label="Member 9")
    assert await kim("draft_decide", lock(pickup="Member 9")) == ToolResult(nobody, is_error=True)
    assert await kim("draft_decide", lock(pickup="Member 4")) == ToolResult("No such member.", is_error=True)
    assert await kim("draft_hand_off", DINNER | {"to": "Member 4"}) == ToolResult(
        "Choose another active member.", is_error=True
    )
    assert await kim("draft_decide", lock(pickup="Member 6")) == ToolResult("Pick someone who's in.", is_error=True)
    assert await rows(house) == []
    taqueria = next(p for p in (await house.context(event_id)).picks if p.item_id == house.items["Taqueria"].id)
    await act(house, calls.lock, house["Kim"], event_id, taqueria.id)
    assert await kim("draft_hand_off", DINNER | {"to": "Member 6"}) == ToolResult(
        rules.COORDINATOR_NOT_IN, is_error=True
    )
    assert await kim("draft_plan", change(pickup="Member 5")) == ToolResult(guest, is_error=True)
    assert await kim("draft_plan", change(pickup="Member 6")) == ToolResult("Pick someone who's in.", is_error=True)
    assert await rows(house) == []


# No names ----------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_results_and_stored_drafts_carry_no_names_and_cards_show_them(house: House) -> None:
    """Every coordinating tool's result, and each stored title and line, name nobody, the guest
    included: the model reads labels, a guest is "a guest" or a count, and a stored line marks a
    person by member ID. The cards and Confirm's results, which only Kim sees, name people."""
    event_id = await voting(house, guest=True)
    said = [
        await tool(house, "look_up_coordinating", DINNER),
        await tool(house, "draft_decide", lock(pickup="Member 3", lead="Member 4")),
        await tool(house, "draft_shuffle", DINNER),
        await tool(house, "draft_hand_off", DINNER | {"to": "Member 2"}),
        await tool(house, "draft_call_off", DINNER | {"reason": ""}),
    ]
    looked = json.loads(said[0].content)
    assert looked["guests_in"] == 1 and len(looked["in"]) == 4
    done = [await confirm(house, "Kim", draft_id(said[1]))]
    assert (await holders(house, event_id)) == {AssignmentRole.ORDER: "Lee", AssignmentRole.PICKUP: "Sam"}
    said += [
        await tool(house, "look_up_coordinating", DINNER),
        await tool(house, "draft_plan", change(meal_time="19:15", pickup="Member 2")),
    ]
    done.append(await confirm(house, "Kim", draft_id(said[-1])))
    house.clock.now = MEAL + timedelta(minutes=45)
    said.append(await tool(house, "draft_mark_done", DINNER))
    done.append(await confirm(house, "Kim", draft_id(said[-1])))
    decision = json.loads(said[5].content)["decision"]
    assert decision["roles"] == {"order": "Member 4", "pickup": "Member 3"}
    assert not any(r.is_error for r in said), said
    texts = [v for r in said for v in strings(json.loads(r.content) if r.content.startswith("{") else r.content)]
    texts += [t for r in await rows(house) for t in (r.title, *r.lines)]
    assert_no_names(*texts)
    assert done[0].text == "Confirmed. Today's dinner call is decided on Taqueria. Lee orders it. Sam picks it up."


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("way", ["cook", ""])
async def test_a_cook_list_refusal_names_the_label_not_the_person(house: House, way: str) -> None:
    """``check_assignee``'s own refusal names the person; the model gets their label instead, with
    the way given or, for a place with one way, worked out as Adjust works it out."""
    await decided(house)
    refused = await tool(house, "draft_plan", change(place="Sam's Tacos", way=way, lead="Member 2"))
    assert refused == ToolResult("Sam's Tacos has a cook list, and Member 2 isn't on it.", is_error=True)


@pytest.mark.flag("assistant", on=True)
async def test_the_services_own_cook_list_refusal_is_relabeled_too(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind the assistant's own check, Adjust's ``check_assignee`` names the person. Its refusal
    reaches the model only written with the label."""
    await decided(house)
    monkeypatch.setattr(coordinating, "_can_do", lambda *args: None)
    refused = await tool(house, "draft_plan", change(place="Sam's Tacos", lead="Member 2"))
    assert refused == ToolResult("Sam's Tacos has a cook list, and Member 2 isn't on it.", is_error=True)


async def cooks_only(house: House, item: str, *cooks: str) -> None:
    """Give ``item`` a cook list of ``cooks``, as an admin's edit would."""
    found = house.items[item]
    values = ItemValues(
        kind=found.kind,
        name=found.name,
        modes=found.modes,
        meals=found.meals,
        tag_ids=found.tag_ids,
        cook_ids=frozenset(house[c].id for c in cooks),
        url=found.url,
        order_url=None,
        notes=None,
        default_servings=None,
        active=True,
    )
    async with house.sessionmaker() as db:
        await CatalogRepository(db).update_item(house.household.id, found.id, values, house.clock())
        await db.commit()


@pytest.mark.flag("assistant", on=True)
async def test_a_cook_list_refusal_at_confirm_names_nobody(house: House) -> None:
    """A cook list added between the draft and Confirm: Adjust refuses, naming the person; the
    card says someone named isn't on it, and the draft stays unused."""
    await house.add_item("Pasta Bake", Mode.COOK, cuisine="Italian")
    event_id = await decided(house)
    draft = draft_id(await tool(house, "draft_plan", change(place="Pasta Bake", lead="Member 2")))
    await cooks_only(house, "Pasta Bake", "Sam")
    with pytest.raises(rules.EventRuleViolation) as refused:
        await confirm(house, "Kim", draft)
    assert str(refused.value) == coordinating.COOK_LIST_AT_CONFIRM
    assert (await row(house, draft)).used_at is None
    assert (await house.event(event_id)).decided_item_id == house.items["Taqueria"].id


@pytest.mark.flag("assistant", on=True)
async def test_a_plan_partly_saved_says_why_the_rest_wasnt_naming_nobody(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The meal time saves, then the roles are refused with a refusal that names someone: the
    result says it's partly done, and names nobody."""
    event_id = await decided(house)
    draft = draft_id(await tool(house, "draft_plan", change(meal_time="19:00", pickup="Member 2")))

    async def refuses(*args: Any, **kwargs: Any) -> None:
        raise rules.EventRuleViolation("Taqueria has a cook list, and Lee isn't on it.", code="cook_list")

    monkeypatch.setattr(coordinator, "choose_roles", refuses)
    done = await confirm(house, "Kim", draft)
    assert done == ActionConfirmed(
        event_id,
        f"Partly confirmed: some of the plan changed, but not all. {coordinating.COOK_LIST_AT_CONFIRM}",
    )
    assert (await house.event(event_id)).meal_time == MEAL + timedelta(minutes=30)


@pytest.mark.flag("assistant", on=True)
async def test_naming_who_has_the_role_already_is_refused_not_carded(house: House) -> None:
    """Adjust drops a person named for the role they have, so a card saying it gives it to them
    would say what Confirm doesn't do: the draft is refused instead."""
    await decided(house)
    for args, label, does in (
        (change(pickup="Member 3"), "Member 3", "picks it up"),
        (change(lead="Member 4"), "Member 4", "orders it"),
    ):
        refused = await tool(house, "draft_plan", args)
        assert refused == ToolResult(coordinating.ALREADY.format(label=label, does=does), is_error=True)
    assert await rows(house) == []


@pytest.mark.flag("assistant", on=True)
async def test_refusals_the_model_can_act_on(house: House) -> None:
    """Input the tools can't use says what to send instead, and names nobody."""
    event_id = await voting(house)
    assert await tool(house, "draft_decide", lock("Taqueria", way="", pickup="Member 3") | {"way": "delivery"}) == (
        ToolResult("Taqueria isn't available by delivery.", is_error=True)
    )
    assert await tool(house, "draft_decide", lock("Siam Garden", way="delivery", pickup="Member 3")) == ToolResult(
        coordinating.NO_PICKUP, is_error=True
    )
    assert await tool(house, "draft_hand_off", DINNER | {"to": ""}) == ToolResult(
        coordinating.LABEL_ONLY, is_error=True
    )
    assert await tool(house, "look_up_coordinating", {"slot": "lunch", "day": ""}) == ToolResult(
        "There's no lunch call on 2026-10-22 (today).", is_error=True
    )
    taqueria = next(p for p in (await house.context(event_id)).picks if p.item_id == house.items["Taqueria"].id)
    await act(house, calls.lock, house["Kim"], event_id, taqueria.id)
    assert await tool(house, "draft_plan", change(way="delivery", pickup="Member 3")) == ToolResult(
        coordinating.NO_PICKUP, is_error=True
    )
    assert await tool(house, "draft_plan", change(place="Nowhere")) == ToolResult(
        coordinating.NO_SUCH_PLACE, is_error=True
    )
    assert await tool(house, "draft_plan", change(place="Taqueria", way="delivery")) == ToolResult(
        "Taqueria isn't available by delivery.", is_error=True
    )


@pytest.mark.flag("assistant", on=True)
async def test_with_two_calls_of_hers_open_the_assistant_asks_which(house: House) -> None:
    await voting(house)
    await act(house, calls.start_call, house["Kim"], Slot.LUNCH, date(2026, 10, 23))
    asked = await tool(house, "look_up_coordinating", {"slot": "", "day": ""})
    assert asked.is_error and asked.content.startswith("The member has several open calls: dinner on 2026-10-22")
    assert "lunch on 2026-10-23 (tomorrow, scheduled)" in asked.content
    off = await tool(house, "draft_call_off", {"slot": "", "day": "tomorrow", "reason": ""})
    assert not off.is_error, off.content


@pytest.mark.flag("assistant", on=True)
async def test_a_lock_whose_named_roles_are_refused_locks_nothing(house: House) -> None:
    """The lock and its named roles are one transaction: Lee, named to order, is deactivated
    before Confirm, so the refusal rolls back the lock and the draft's use, and nobody is told
    anything."""
    event_id = await voting(house)
    draft = draft_id(await tool(house, "draft_decide", lock(lead="Member 4")))
    await act(house, members.set_member_active, house["Alex"], house["Lee"].id, False)
    before = await house.kinds()
    with pytest.raises(NotFound, match="No such member"):
        await confirm(house, "Kim", draft)
    assert (await house.event(event_id)).state is EventState.VOTING
    assert (await row(house, draft)).used_at is None
    assert await house.kinds() == before


@pytest.mark.flag("assistant", on=True)
async def test_a_hand_off_by_anyone_but_the_coordinator_says_so_before_reading_a_label(house: House) -> None:
    """Sam isn't the coordinator: a label nobody has and one somebody has get the same refusal,
    the coordinator's, so the refusal never says which labels exist."""
    await voting(house)
    for to in ("Member 9", "Member 1"):
        refused = await tool(house, "draft_hand_off", DINNER | {"to": to}, who="Sam")
        assert refused == ToolResult("Only the coordinator can hand off coordinating.", is_error=True), to


@pytest.mark.flag("assistant", on=True)
async def test_confirm_uses_the_draft_before_it_checks_the_run_or_runs_the_use_case(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Confirm that loses the race for the draft hears it's used, even on a stale run, and the
    use case never runs."""
    event_id = await decided(house)
    house.clock.now = MEAL
    draft = draft_id(await tool(house, "draft_mark_done", DINNER))
    await act(house, call_admin.rerun, house["Alex"], event_id)  # a stale run, too
    ran: list[str] = []

    async def done(*args: Any, **kwargs: Any) -> None:
        ran.append("mark_done")

    monkeypatch.setattr(calls, "mark_done", done)

    async def used() -> None:
        raise drafts.DraftUnavailable(drafts.USED)

    async with house.sessionmaker() as db:
        found = await AssistantRepository(db).draft(house.household.id, draft)
        assert found is not None
        with pytest.raises(drafts.DraftUnavailable, match="already confirmed"):
            await coordinating.confirm(
                db, tools.tool_actor(house["Kim"]), found, today=date(2026, 10, 22), now=house.clock(), use=used
            )
    assert ran == []


@pytest.mark.flag("assistant", on=True)
async def test_someone_to_pick_up_a_one_way_place_that_isnt_pickup_is_refused(house: House) -> None:
    """With no way given, the way is the place's only one, as Adjust takes it: a home meal has
    nobody to pick up, so naming one is refused rather than dropped from the card."""
    await decided(house)
    refused = await tool(house, "draft_plan", change(place="Sam's Tacos", pickup="Member 2"))
    assert refused == ToolResult(coordinating.NO_PICKUP, is_error=True)


@pytest.mark.flag("assistant", on=True)
async def test_a_switch_of_way_names_on_the_card_who_adjust_gives_the_role(house: House) -> None:
    """Siam Garden is decided for pickup; switching to delivery names nobody, and Adjust gives
    ordering to its first volunteer, Lee: the card says so, as the service will do it."""
    event_id = await voting(house)
    siam = next(p for p in (await house.context(event_id)).picks if p.item_id == house.items["Siam Garden"].id)
    await act(house, calls.lock, house["Kim"], event_id, siam.id)
    await act(house, coordinator.choose_roles, house["Kim"], event_id, mode=Mode.PICKUP, member_id=house["Sam"].id)
    drafted = await tool(house, "draft_plan", change(way="delivery"))
    lines = (await card(house, draft_id(drafted))).confirm_lines
    assert "It switches from pickup to delivery." in lines and "Lee orders it." in lines
    await confirm(house, "Kim", draft_id(drafted))
    assert await holders(house, event_id) == {AssignmentRole.ORDER: "Lee"}


def told(entries: list[Any], member: Member) -> list[str]:
    """The kinds of the messages to ``member`` among ``entries``."""
    return [e.kind for e in entries if e.payload.get("member", {}).get("id") == member.id]


@pytest.mark.flag("assistant", on=True)
async def test_a_lock_naming_someone_else_to_pick_up_tells_each_person_once(house: House) -> None:
    """Sam offered to pick up, and Kim's lock names Alex instead. The decision takes Alex at once:
    Sam, never assigned, hears nothing about a role, and Alex hears once that the coordinator gave
    him pickup, not a message to Sam and then another taking it back."""
    event_id = await voting(house)
    draft = draft_id(await tool(house, "draft_decide", lock(pickup="Member 2")))
    before = len(await house.outbox())
    await confirm(house, "Kim", draft)
    assert (await holders(house, event_id))[AssignmentRole.PICKUP] == "Alex"
    entries = (await house.outbox())[before:]
    assert told(entries, house["Sam"]) == []
    assert told(entries, house["Alex"]) == ["assignment_made"]
    [made] = [e for e in entries if e.kind == "assignment_made" and e.payload["member"]["id"] == house["Alex"].id]
    assert made.payload["assigned_by"]["id"] == house["Kim"].id


def lines_logged(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(JsonFormatter().format(r)) for r in caplog.records if r.name == activity.log.name]


@pytest.mark.flag("assistant", on=True)
async def test_a_lock_with_roles_is_audited_and_logged_as_assign_roles_is(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    """Roles named with a lock write what **Assign roles** writes, in the lock's transaction: an
    ``event.roles_assigned`` row, with the way it switched from, and an ``assign_roles`` line."""
    event_id = await voting(house)
    siam = next(p for p in (await house.context(event_id)).picks if p.item_id == house.items["Siam Garden"].id)
    other = Mode.DELIVERY if siam.mode is Mode.PICKUP else Mode.PICKUP
    draft = draft_id(
        await tool(house, "draft_decide", lock("Siam Garden", siam.mode.value, way=other.value, lead="Member 4"))
    )
    caplog.set_level(logging.INFO)
    caplog.clear()
    await confirm(house, "Kim", draft)
    event = await house.event(event_id)
    assert event.decided_mode is other
    [row_] = [a for a in await house.audits() if a.action == "event.roles_assigned"]
    payload = row_.payload if isinstance(row_.payload, dict) else json.loads(row_.payload)
    assert payload["switched_from"] == siam.mode.value and payload["mode"] == other.value
    assert payload["member_id"] == house["Lee"].id and payload["volunteered"] is True
    assert payload["assignments"]["order"] == house["Lee"].id
    [line] = [line for line in lines_logged(caplog) if line["msg"] == "assign_roles"]
    assert (line["switched"], line["volunteered"], line["to_member_id"]) == (True, True, house["Lee"].id)


@pytest.mark.flag("assistant", on=True)
async def test_breaking_a_tie_with_roles_takes_them_in_the_decision(house: House) -> None:
    event_id = await voting(house)
    ctx = await house.context(event_id)
    siam, taqueria = (next(p for p in ctx.picks if p.item_id == house.items[n].id) for n in ("Siam Garden", "Taqueria"))
    await act(house, calls.vote, house["Sam"], event_id, siam.id)
    await act(house, calls.vote, house["Lee"], event_id, taqueria.id)
    draft = draft_id(await tool(house, "draft_decide", lock("Taqueria", tie=True, pickup="Member 2")))
    await confirm(house, "Kim", draft)
    event = await house.event(event_id)
    assert event.tie_by == house["Kim"].id and event.decided_item_id == house.items["Taqueria"].id
    assert (await holders(house, event_id))[AssignmentRole.PICKUP] == "Alex"


@pytest.mark.flag("assistant", on=True)
async def test_a_pick_named_with_the_person_markers_never_poses_as_a_person(house: House) -> None:
    """A typed pick whose text carries the characters that mark a person in a stored line. The
    forms refuse those characters, so the row goes in directly, as one saved before that check,
    or by other means, would be. Drafting a lock of it by that name keeps it text: neither the
    card nor the model's summary turns it into a housemate's name or label."""
    event_id = await voting(house)
    posing = f"Pho {chr(0xE000)}{house['Sam'].id}{chr(0xE001)} House"
    async with house.sessionmaker() as db:
        await ProposalRepository(db).add(
            household_id=house.household.id,
            event_id=event_id,
            mode=Mode.PICKUP,
            rank=9,
            source=ProposalSource.MEMBER,
            reasons=(),
            now=house.clock(),
            free_text=posing,
            free_cuisine_id=house.tags["Thai"].id,
            proposed_by=house["Kim"].id,
        )
        await db.commit()
    result = await tool(house, "draft_decide", lock(posing))
    drafted = await card(house, draft_id(result))
    assert drafted.title == f"Lock Pho {house['Sam'].id} House (pickup)"
    assert_no_names(drafted.title, *drafted.confirm_lines, result.content)
    assert not re.search(r"Member [2-9]", result.content)


@pytest.mark.flag("assistant", on=True)
async def test_a_guest_given_a_role_by_a_switch_of_way_reads_as_a_guest(house: House) -> None:
    """Gwendolyn, Kim's guest, is the only one who offered to pick up. Switching the decided
    delivery to pickup gives her pickup, as Adjust's first volunteer: the card and the model both
    read "A guest", and she gets no label."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in ("Alex", "Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, guests.invite, house["Kim"], event_id, new_guest=GuestInput("Gwendolyn"))
    [gwendolyn] = (await house.context(event_id)).guests
    await act(house, guests.answer_for, house["Kim"], event_id, gwendolyn.id, role=RoleOffer.PICKUP)
    house.clock.now = RESPOND_BY
    await house.tick()
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Kim"].id)
    siam = next(p for p in (await house.context(event_id)).picks if p.item_id == house.items["Siam Garden"].id)
    await act(house, calls.lock, house["Kim"], event_id, siam.id)
    await act(house, coordinator.choose_roles, house["Kim"], event_id, mode=Mode.DELIVERY, member_id=house["Lee"].id)
    result = await tool(house, "draft_plan", change(way="pickup"))
    assert "A guest picks it up." in result.content and "Gwendolyn" not in result.content
    lines = (await card(house, draft_id(result))).confirm_lines
    assert "A guest picks it up." in lines and not any("Gwendolyn" in line for line in lines)
    looked = await tool(house, "look_up_coordinating", DINNER)
    assert json.loads(looked.content)["decision"]["roles"] == {"order": "Member 4"}
    assert not re.search(r"Member [5-9]", result.content + looked.content)
