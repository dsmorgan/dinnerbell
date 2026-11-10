# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's candidates, drafts, and Confirm (ADR 0009, spec 6.14, **Tools** and
**Drafts**), against a migrated database. Every model call goes to ``FakeModel``.

The household is the usual one (``tests.support.calls.furnish``) with more places, ranking set
to propose one pick at temperature 0 so the draw is predictable, and these stances: Sam and Kim
love Curry Leaf (so ranking proposes it, and it's taken), Kim vetoes Banned Burger, Kim dislikes
Pho Corner, and Sam likes Veg Garden. Old Favorite was eaten two days ago, inside the variety
window. Sam suggests Pho Corner.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import date, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.meals import household_settings
from dinnerbell.db.models import AssistantDraftRow, AuditRow, ProposalRow
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.assistant import AssistantSettings, Pseudonyms, web_key
from dinnerbell.domain.assistant_drafts import (
    DRAFT_MINUTES,
    OfferOutcome,
    clean_catalog,
    day_words,
    offer_outcome,
)
from dinnerbell.domain.catalog import Meal, Mode, TagKind
from dinnerbell.domain.catalog_file import parse_catalog
from dinnerbell.domain.events import ActingAs, Details, EventRuleViolation, EventState, RoleOffer, Status
from dinnerbell.domain.meals import MealInput, Slot
from dinnerbell.domain.members import Member, Role
from dinnerbell.domain.offers import OfferPath
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.domain.schedule import PROPOSAL_COUNT_KEY, TEMPERATURE_KEY
from dinnerbell.services import calls, lifecycle, meals, offers, scrub
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.assistant import admin, candidates, drafts, retention, tools
from dinnerbell.services.assistant.conversation import Assistant, converse
from dinnerbell.services.assistant.drafts import (
    EXPIRED,
    NOT_A_CANDIDATE,
    TURNED_OFF,
    USED,
    CatalogReady,
    DraftUnavailable,
    OfferConfirmed,
    ProposalConfirmed,
)
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.assistant.tools import ToolResult, ToolSession
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from dinnerbell.services.preferences import set_my_preference
from tests.support.calls import OPENS, RESPOND_BY, House, dinner_id, furnish, make_house, nobody_coordinates
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, tool_call

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 22)
TOMORROW = DAY + timedelta(days=1)
NO_FILTERS = {"slot": "", "day": "", "not_eaten_in_days": 0, "cuisine": "", "mode": ""}
HINT = "Mixed reviews · recently late"


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"anthropic_api_key": "sk-ant-test-not-a-real-key"}


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    await house.add_item("Pho Corner", Mode.PICKUP, cuisine="Vietnamese")
    await house.add_item("Curry Leaf", Mode.PICKUP, Mode.DELIVERY, cuisine="Indian")
    await house.add_item("Veg Garden", Mode.PICKUP, cuisine="Thai", tags=("Vegetarian",))
    await house.add_item("Brunch Barn", Mode.PICKUP, cuisine="American", meals=frozenset({Meal.BREAKFAST}))
    await house.add_item("Pasta Night", Mode.COOK, cuisine="Italian")
    await house.add_item("Banned Burger", Mode.PICKUP, cuisine="American")
    await house.add_item("Old Favorite", Mode.PICKUP, cuisine="Greek")
    sam, kim = house["Sam"], house["Kim"]
    async with house.sessionmaker() as db:
        stored = await household_settings(db, house.household.id)
        await HouseholdRepository(db).update_settings(
            house.household.id, stored | {PROPOSAL_COUNT_KEY: 1, TEMPERATURE_KEY: 0.0}
        )
        await db.commit()
        for who, item, stance in (
            (sam, "Curry Leaf", Stance(score=2)),
            (kim, "Curry Leaf", Stance(score=2)),
            (kim, "Banned Burger", Stance(veto=True)),
            (kim, "Pho Corner", Stance(score=-1)),
            (sam, "Veg Garden", Stance(score=1)),
        ):
            await set_my_preference(db, who, TargetType.ITEM, house.items[item].id, stance, now=house.clock())
        await meals.log_meal(
            db,
            sam,
            MealInput(
                DAY - timedelta(days=2), Slot.DINNER, house.items["Old Favorite"].id, frozenset({sam.id, kim.id})
            ),
            today=DAY,
            now=house.clock(),
        )
    return house


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
async def helper(settings: Settings, model: FakeModel, house: House, hints: None) -> Assistant:
    helper = Assistant(AssistantConfig.from_settings(settings), model, house.clock)
    async with house.sessionmaker() as db:
        await admin.update_assistant(
            db, helper.config, house["Alex"], AssistantSettings(enabled=True), now=house.clock()
        )
    return helper


@pytest.fixture
def hints(monkeypatch: pytest.MonkeyPatch, house: House) -> None:
    """Every item has a hint, so a test can check none reaches the model."""

    async def every_item(*args: Any) -> dict[int, str]:
        return {item.id: HINT for item in house.items.values()}

    monkeypatch.setattr(lifecycle.feedback, "hints_on", every_item)


async def act(house: House, use_case: Any, *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


async def open_dinner(house: House, *eaters: str) -> int:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in eaters:
        await act(house, calls.respond, house[name], event_id, Status.IN)
    return event_id


async def to_voting(house: House, *, suggest: bool = True) -> int:
    """Sam and Kim are in, Sam suggests Pho Corner, and Alex offers to pick up. Lee never
    answers. Nobody coordinates."""
    event_id = await open_dinner(house, "Sam", "Kim")
    if suggest:
        details = Details(suggestion_item_ids=(house.items["Pho Corner"].id,))
        await act(house, calls.save_details, house["Sam"], event_id, details)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    await nobody_coordinates(house, event_id)
    return event_id


async def tool(
    house: House, name: str, args: dict[str, Any], *, who: str = "Sam", session: ToolSession | None = None
) -> ToolResult:
    member = house[who]
    async with house.sessionmaker() as db:
        return await tools.run_tool(
            name,
            args,
            db=db,
            member=member,
            private=True,
            now=house.clock(),
            session=session or fresh(member),
        )


async def found(house: House, who: str = "Sam", **narrowing: Any) -> dict[str, Any]:
    result = await tool(house, "find_candidates", NO_FILTERS | narrowing, who=who)
    assert not result.is_error, result.content
    data: dict[str, Any] = json.loads(result.content)
    return data


def names(data: dict[str, Any]) -> list[str]:
    return [c["name"] for c in data["candidates"]]


def fresh(member: Member) -> ToolSession:
    """A message's session, as ``converse`` starts it: the member asking is labeled first."""
    return ToolSession(key=web_key(member.id), pseudonyms=Pseudonyms().with_member(member.id))


def tool_results(model: FakeModel) -> str:
    """Everything Dinnerbell's tools sent back, as one string."""
    return json.dumps(
        [
            block["content"]
            for request in model.requests
            for message in request["messages"]
            if message["role"] == "user" and isinstance(message["content"], list)
            for block in message["content"]
        ],
        ensure_ascii=False,
    )


def draft_id(result: ToolResult) -> int:
    assert not result.is_error, result.content
    return int(result.content.split()[1])


CONFIGURED = AssistantConfig(key_set=True, flag_on=True)


async def confirm(house: House, who: str, draft: int, config: AssistantConfig = CONFIGURED) -> Any:
    async with house.sessionmaker() as db:
        return await drafts.confirm_draft(db, house.clock, config, house[who], draft)


async def cancel(house: House, who: str, draft: int) -> None:
    async with house.sessionmaker() as db:
        await drafts.cancel_draft(db, house.clock, house[who], draft)


async def draft_rows(house: House) -> list[AssistantDraftRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AssistantDraftRow).order_by(AssistantDraftRow.id)))


async def proposals(house: House, event_id: int) -> dict[str, ProposalRow]:
    ctx = await house.context(event_id)
    by_id = {i.id: i.name for i in ctx.items.values()}
    async with house.sessionmaker() as db:
        rows = await db.scalars(select(ProposalRow).where(ProposalRow.event_id == event_id))
        return {by_id.get(r.item_id or 0, r.free_text or ""): r for r in rows}


def propose(name: str, mode: str = "pickup") -> dict[str, Any]:
    return {"slot": "", "day": "", "name": name, "mode": mode}


def offer(name: str, mode: str, *, slot: str = "dinner", day: str = "today", at: str = "") -> dict[str, Any]:
    return {"name": name, "mode": mode, "slot": slot, "day": day, "time": at, "spare_servings": 0}


# The domain ------------------------------------------------------------------------------------


def test_day_words() -> None:
    assert day_words(DAY, DAY) == "today"
    assert day_words(TOMORROW, DAY) == "tomorrow"
    assert day_words(DAY + timedelta(days=3), DAY) == "Sun, Oct 25"


def test_a_catalog_draft_drops_matches_and_strips_cooks_and_active() -> None:
    from dinnerbell.domain.catalog import CatalogItem, ItemKind

    existing = CatalogItem(
        id=1,
        household_id=1,
        kind=ItemKind.RESTAURANT,
        name="Siam Garden",
        modes=frozenset({Mode.PICKUP}),
        tag_ids=frozenset(),
        cook_ids=frozenset(),
        url=None,
        order_url=None,
        notes=None,
        default_servings=None,
        active=False,
    )
    text = """
version: 1
tags:
  dietary: [Vegan]
restaurants:
  - name: siam garden
    cuisine: [Thai]
  - name: Noodle Bar
    cuisine: [Chinese]
    modes: [pickup, delivery]
    active: false
home_meals:
  - name: Lentil Soup
    cuisine: [Indian]
    tags: [Vegan]
    cooks: [kim@example.com]
"""
    clean = clean_catalog(parse_catalog(text), [existing])
    assert clean.added == ("Noodle Bar", "Lentil Soup") and clean.dropped == ("siam garden",)
    again = parse_catalog(clean.text)
    assert again.errors == ()
    assert [e.entry.name for e in again.entries] == ["Noodle Bar", "Lentil Soup"]
    assert all(e.entry.active for e in again.entries)
    assert "cooks" not in clean.text and "active" not in clean.text and "kim@" not in clean.text
    assert "modes: [pickup, delivery]" in clean.text and "Vegan" in clean.text


# Candidates ------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_candidates_pass_every_filter_in_name_order(house: House, helper: Assistant) -> None:
    await to_voting(house)
    data = await found(house)
    listed = names(data)
    meal = [c["name"] for c in data["candidates"] if c["for_this_meal"]]
    others = [c["name"] for c in data["candidates"] if not c["for_this_meal"]]
    assert listed == meal + others  # the call's meal first, then other meals, each in name order
    assert meal == sorted(meal, key=str.casefold) and others == ["Brunch Barn"]
    for never in ("Banned Burger", "Old Favorite", "Curry Leaf"):  # a veto, the repeat window, and taken
        assert never not in listed, never
    assert {"Pho Corner", "Veg Garden", "Brunch Barn", "Pasta Night", "Sam's Tacos"} <= set(listed)
    brunch = next(c for c in data["candidates"] if c["name"] == "Brunch Barn")
    assert brunch["reasons"][0] == "Usually for breakfast."  # meal fit
    assert data["call"] == {
        "meal": "dinner",
        "day": "2026-10-22",
        "when": "today",
        "state": "voting",
        "meal_time": "18:30",
        "roles_offered": ["pick up"],
    }
    # Sam's Tacos has a cook list. Sam is on it, so adding it volunteers him; Kim isn't, and
    # nobody who's in offered to cook, so for her it's uncovered.
    assert "Sam's Tacos" not in names(await found(house, "Kim"))
    narrowed = await found(house, cuisine="viet", mode="pickup")
    assert names(narrowed) == ["Pho Corner"]
    assert names(await found(house, mode="cook")) == ["Pasta Night", "Sam's Tacos"]


@pytest.mark.flag("assistant", on=True)
async def test_the_cap_keeps_the_calls_meal_ahead_of_other_meals(house: House, helper: Assistant) -> None:
    for n in range(30):
        await house.add_item(f"Aaa Brunch {n:02}", Mode.PICKUP, cuisine="Brunch", meals=frozenset({Meal.BREAKFAST}))
    await to_voting(house)
    data = await found(house)
    assert data["more"] is True and len(data["candidates"]) == 25
    assert "Pho Corner" in names(data)  # a dinner place, though 30 breakfast places sort before it


@pytest.mark.flag("assistant", on=True)
async def test_a_call_past_voting_says_so_rather_than_to_wait(house: House, helper: Assistant) -> None:
    event_id = await to_voting(house)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    result = await tool(house, "find_candidates", NO_FILTERS | {"slot": "dinner"})
    assert result == ToolResult(candidates.CALL_ENDED, is_error=True)


@pytest.mark.flag("assistant", on=True)
async def test_a_guests_need_rules_out_everything_that_misses_it(house: House, helper: Assistant) -> None:
    await house.add_item("Tofu Hut", Mode.PICKUP, cuisine="Japanese", tags=("Vegetarian",))
    event_id = await open_dinner(house, "Sam", "Kim")
    await house.legacy_guests(event_id, "Kim", 1, frozenset({house.tags["Vegetarian"].id}))
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    assert "Veg Garden" in await proposals(house, event_id)  # Sam likes it, so ranking drew it
    assert names(await found(house)) == ["Tofu Hut"]


@pytest.mark.flag("assistant", on=True)
async def test_the_repeat_window_yields_only_when_nothing_fresh_fits(house: House, helper: Assistant) -> None:
    await to_voting(house)
    assert "Old Favorite" not in names(await found(house))
    assert names(await found(house, cuisine="greek")) == []  # filtered, not merely narrowed away


@pytest.mark.flag("assistant", on=True)
async def test_candidates_before_voting_are_refused_the_same_before_and_after_a_housemate_answers(
    house: House, helper: Assistant
) -> None:
    """FR-29: during answers, the people whose vetoes apply can still change, so asking before
    and after Kim answers In must look the same: refused, with nothing about the catalog."""
    event_id = await open_dinner(house, "Sam")
    before = await tool(house, "find_candidates", NO_FILTERS)
    await act(house, calls.respond, house["Kim"], event_id, Status.IN)
    after = await tool(house, "find_candidates", NO_FILTERS)
    assert before == after == ToolResult(candidates.NOT_VOTING, is_error=True)
    proposal = await tool(house, "draft_proposal", propose("Pho Corner"))
    assert proposal == ToolResult(candidates.NOT_VOTING, is_error=True)


@pytest.mark.flag("assistant", on=True)
async def test_reasons_carry_no_hint_favorite_or_name(house: House, helper: Assistant, model: FakeModel) -> None:
    await to_voting(house)
    model.script = [tool_call("find_candidates", NO_FILTERS), answer("Member 2 suggested Pho Corner.")]
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, house["Kim"], web_key(house["Kim"].id), private=True, text="What fits?")
    assert reply is not None and reply.text == "Sam suggested Pho Corner."
    result = json.loads(model.requests[-1]["messages"][-1]["content"][0]["content"])
    reasons = [r for c in result["candidates"] for r in c["reasons"]]
    pho = next(c for c in result["candidates"] if c["name"] == "Pho Corner")
    assert "Suggested by Member 2." in pho["reasons"]  # Kim asks, so she's Member 1
    sent = model.sent
    for secret in ("Mixed reviews", "recently late", "favorite", "Sam", "Kim", "Alex", "Lee", "@example.com"):
        assert secret not in json.dumps(reasons), secret
    for secret in ("Mixed reviews", "favorite", '"Sam"', "Kim", "Alex", "Lee", "@example.com", '"veto"'):
        assert secret not in sent, secret
    assert sent.count("Sam") == sent.count("Sam's Tacos")


@pytest.mark.flag("assistant", on=True)
async def test_which_call_the_member_named_the_conversations_or_the_only_open_one(
    house: House, helper: Assistant
) -> None:
    event_id = await to_voting(house)
    lunch = await act(house, calls.start_call, house["Kim"], Slot.LUNCH, DAY)
    assert lunch.state is EventState.COLLECTING
    several = await tool(house, "find_candidates", NO_FILTERS)
    assert several.is_error and "Several calls are open" in several.content
    assert "dinner on 2026-10-22 (today, voting)" in several.content
    assert "lunch on 2026-10-22 (today, collecting answers)" in several.content
    assert names(await found(house, slot="dinner"))  # named
    named_lunch = await tool(house, "find_candidates", NO_FILTERS | {"slot": "lunch"})
    assert named_lunch == ToolResult(candidates.NOT_VOTING, is_error=True)
    session = ToolSession(key=web_key(house["Sam"].id), call_id=event_id)  # the thread's call
    threaded = await tool(house, "find_candidates", NO_FILTERS, session=session)
    assert not threaded.is_error
    none = await tool(house, "find_candidates", NO_FILTERS | {"slot": "breakfast"})
    assert none.is_error and "There's no breakfast call on 2026-10-22 (today)" in none.content
    bad = await tool(house, "find_candidates", NO_FILTERS | {"day": "someday"})
    assert bad.is_error and "today, tomorrow, or a date" in bad.content


@pytest.mark.flag("assistant", on=True)
async def test_with_no_call_open_the_tool_says_so(house: House, helper: Assistant) -> None:
    result = await tool(house, "find_candidates", NO_FILTERS)
    assert result == ToolResult(candidates.NO_OPEN_CALL, is_error=True)


# Proposal drafts and Confirm -------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_a_proposal_draft_says_what_confirm_does_and_confirm_does_it(house: House, helper: Assistant) -> None:
    event_id = await to_voting(house)
    session = fresh(house["Kim"])
    result = await tool(house, "draft_proposal", propose("Pho Corner"), who="Kim", session=session)
    draft = draft_id(result)
    assert "Nothing has changed yet" in result.content and "Member 1" in result.content
    [view] = session.drafts
    assert view.title == "Propose Pho Corner (pickup)" and view.kind == "proposal"
    assert view.confirm_lines == (
        "It's added to today's dinner vote as a proposal.",
        "You'll be down to pick it up.",
    )
    assert "Pho Corner" not in await proposals(house, event_id)  # nothing yet
    confirmed = await confirm(house, "Kim", draft)
    assert isinstance(confirmed, ProposalConfirmed) and confirmed.event_id == event_id
    assert (await proposals(house, event_id))["Pho Corner"].proposed_by == house["Kim"].id
    kims = (await house.context(event_id)).response_of(house["Kim"].id)
    assert kims is not None and RoleOffer.PICKUP in kims.offers
    with pytest.raises(DraftUnavailable, match=USED):
        await confirm(house, "Kim", draft)


@pytest.mark.flag("assistant", on=True)
async def test_someone_who_isnt_in_joins_late_on_confirm(house: House, helper: Assistant) -> None:
    event_id = await to_voting(house)
    session = fresh(house["Lee"])
    draft_id(await tool(house, "draft_proposal", propose("Veg Garden"), who="Lee", session=session))
    [view] = session.drafts
    assert "You'll join late." in view.confirm_lines
    await confirm(house, "Lee", view.id)
    lees = (await house.context(event_id)).response_of(house["Lee"].id)
    assert lees is not None and lees.is_in and lees.late
    assert "Veg Garden" in await proposals(house, event_id)


@pytest.mark.flag("assistant", on=True)
async def test_only_a_candidate_can_be_drafted_and_the_refusal_says_nothing_about_why(
    house: House, helper: Assistant
) -> None:
    await to_voting(house)
    for name, mode in (("Banned Burger", "pickup"), ("Old Favorite", "pickup"), ("Pho Corner", "delivery")):
        result = await tool(house, "draft_proposal", propose(name, mode))
        assert result == ToolResult(NOT_A_CANDIDATE, is_error=True), name
    assert await draft_rows(house) == []


@pytest.mark.flag("assistant", on=True)
async def test_confirm_is_single_use_even_when_two_arrive_at_once(house: House, helper: Assistant) -> None:
    event_id = await to_voting(house)
    draft = draft_id(await tool(house, "draft_proposal", propose("Pho Corner")))
    outcomes = await asyncio.gather(confirm(house, "Sam", draft), confirm(house, "Sam", draft), return_exceptions=True)
    assert sum(isinstance(o, ProposalConfirmed) for o in outcomes) == 1
    assert sum(isinstance(o, DraftUnavailable) for o in outcomes) == 1
    picks = [r for r in (await proposals(house, event_id)).values() if r.proposed_by == house["Sam"].id]
    assert len(picks) == 1


@pytest.mark.flag("assistant", on=True)
async def test_an_expired_draft_another_members_draft_and_a_cancelled_one_are_refused(
    house: House, helper: Assistant
) -> None:
    await to_voting(house)
    first = draft_id(await tool(house, "draft_proposal", propose("Pho Corner")))
    second = draft_id(await tool(house, "draft_proposal", propose("Veg Garden")))
    with pytest.raises(NotFound):
        await confirm(house, "Kim", first)  # looks the same as no draft at all
    with pytest.raises(NotFound):
        await cancel(house, "Kim", first)
    with pytest.raises(NotFound):
        await confirm(house, "Sam", 999_999)
    await cancel(house, "Sam", second)
    with pytest.raises(DraftUnavailable, match=USED):
        await confirm(house, "Sam", second)
    with pytest.raises(DraftUnavailable, match=USED):
        await cancel(house, "Sam", second)
    house.clock.advance(timedelta(minutes=DRAFT_MINUTES))
    with pytest.raises(DraftUnavailable, match=EXPIRED):
        await confirm(house, "Sam", first)


@pytest.mark.flag("assistant", on=True)
async def test_confirm_reapplies_every_filter_and_a_refusal_leaves_the_draft_unused(
    house: House, helper: Assistant
) -> None:
    event_id = await to_voting(house)
    draft = draft_id(await tool(house, "draft_proposal", propose("Pho Corner")))
    async with house.sessionmaker() as db:
        await set_my_preference(
            db, house["Kim"], TargetType.ITEM, house.items["Pho Corner"].id, Stance(veto=True), now=house.clock()
        )
    with pytest.raises(EventRuleViolation, match=lifecycle.DOESNT_FIT):  # the service's own refusal
        await confirm(house, "Sam", draft)
    [row] = await draft_rows(house)
    assert row.used_at is None and "Pho Corner" not in await proposals(house, event_id)
    async with house.sessionmaker() as db:  # the veto lifted, the same draft goes through
        await set_my_preference(
            db, house["Kim"], TargetType.ITEM, house.items["Pho Corner"].id, Stance(), now=house.clock()
        )
    assert isinstance(await confirm(house, "Sam", draft), ProposalConfirmed)


@pytest.mark.flag("assistant", on=True)
async def test_a_draft_for_an_earlier_run_gets_the_services_stale_error(house: House, helper: Assistant) -> None:
    from dinnerbell.domain.events import STALE_CALL
    from dinnerbell.services import call_admin

    event_id = await to_voting(house)
    draft = draft_id(await tool(house, "draft_proposal", propose("Pho Corner")))
    await act(house, call_admin.rerun, house["Alex"], event_id)
    with pytest.raises(EventRuleViolation, match=STALE_CALL):
        await confirm(house, "Sam", draft)


@pytest.mark.flag("assistant", on=True)
async def test_confirm_needs_the_key_the_flag_and_the_switch(house: House, helper: Assistant) -> None:
    await to_voting(house)
    draft = draft_id(await tool(house, "draft_proposal", propose("Pho Corner")))
    for config in (AssistantConfig(key_set=False, flag_on=True), AssistantConfig(key_set=True, flag_on=False)):
        with pytest.raises(DraftUnavailable, match=TURNED_OFF):
            await confirm(house, "Sam", draft, config)
    async with house.sessionmaker() as db:
        await admin.update_assistant(db, helper.config, house["Alex"], AssistantSettings(), now=house.clock())
    with pytest.raises(DraftUnavailable, match=TURNED_OFF):
        await confirm(house, "Sam", draft)
    [row] = await draft_rows(house)
    assert row.used_at is None
    await cancel(house, "Sam", draft)  # Cancel still works


# Offers ----------------------------------------------------------------------------------------


async def offer_draft(house: House, who: str, args: dict[str, Any]) -> tuple[int, tuple[str, ...]]:
    session = fresh(house[who])
    result = await tool(house, "draft_offer", args, who=who, session=session)
    draft = draft_id(result)
    [view] = session.drafts
    return draft, view.confirm_lines


@pytest.mark.flag("assistant", on=True)
async def test_a_cooking_offer_with_no_call_yet_keeps_the_days_call_from_opening(
    house: House, helper: Assistant
) -> None:
    draft, lines = await offer_draft(house, "Sam", offer("Sam's Tacos", "cook"))
    assert lines == ("It keeps today's dinner call from opening while your offer is on.", "You'll cook it.")
    [row] = await draft_rows(house)
    assert row.title == "Offer to cook Sam's Tacos, dinner today at 6:30 PM"
    confirmed = await confirm(house, "Sam", draft)
    assert isinstance(confirmed, OfferConfirmed) and confirmed.posted.path is OfferPath.NEW
    house.clock.now = OPENS
    await house.tick()
    from dinnerbell.db.events import EventRepository

    async with house.sessionmaker() as db:
        assert await EventRepository(db).call_for(house.household.id, Slot.DINNER, DAY) is None


@pytest.mark.flag("assistant", on=True)
async def test_a_cooking_offer_for_a_slot_with_no_schedule_says_no_call_can_start(
    house: House, helper: Assistant
) -> None:
    _, lines = await offer_draft(house, "Sam", offer("Sam's Tacos", "cook", slot="lunch", day="tomorrow"))
    assert lines[0] == "No one can start a lunch call for tomorrow while your offer is on."
    _, later = await offer_draft(house, "Sam", offer("Sam's Tacos", "cook", at="23:00"))
    assert later[0] == "It keeps today's dinner call from opening while your offer is on."
    house.clock.now = RESPOND_BY  # today's dinner call can't open any more
    _, past = await offer_draft(house, "Sam", offer("Sam's Tacos", "cook", at="23:30"))
    assert past[0] == "No one can start a dinner call for today while your offer is on."


@pytest.mark.flag("assistant", on=True)
async def test_an_offer_draft_made_before_midnight_confirms_after_it(house: House, helper: Assistant) -> None:
    from datetime import UTC, datetime

    house.clock.now = datetime(2026, 10, 23, 4, 50, tzinfo=UTC)  # 11:50 PM in Chicago
    draft, lines = await offer_draft(house, "Lee", offer("Siam Garden", "pickup", slot="lunch", day="tomorrow"))
    assert lines[0] == "It's posted as an offer of its own."
    house.clock.advance(timedelta(minutes=15))  # "tomorrow" is today now
    assert isinstance(await confirm(house, "Lee", draft), OfferConfirmed)


@pytest.mark.flag("assistant", on=True)
async def test_an_offer_takes_over_the_places_pick_in_another_mode_and_says_so(house: House, helper: Assistant) -> None:
    event_id = await to_voting(house)
    curry = (await proposals(house, event_id))["Curry Leaf"]
    assert (curry.mode, curry.proposed_by) == ("pickup", None)  # ranking's pick
    draft, lines = await offer_draft(house, "Sam", offer("Curry Leaf", "delivery", at="19:00"))
    assert lines[0] == "It changes today's dinner pick Curry Leaf from pickup to delivery, and its votes stay."
    [row] = await draft_rows(house)
    assert row.title == "Offer to order Curry Leaf for today's dinner call"
    confirmed = await confirm(house, "Sam", draft)
    assert isinstance(confirmed, OfferConfirmed) and confirmed.posted.path is OfferPath.ATTACH
    after = (await proposals(house, event_id))["Curry Leaf"]
    assert (after.id, after.mode, after.proposed_by) == (curry.id, "delivery", house["Sam"].id)
    # The same place in the same mode, or someone else's pick of it, is refused as the service does.
    same = await tool(house, "draft_offer", offer("Curry Leaf", "delivery"))
    assert same.is_error and "Curry Leaf is already a proposal (delivery)" in same.content
    others = await tool(house, "draft_offer", offer("Curry Leaf", "pickup"), who="Kim")
    assert others.is_error and "already a proposal" in others.content


@pytest.mark.flag("assistant", on=True)
async def test_an_offer_whose_pick_was_taken_meanwhile_is_stale(house: House, helper: Assistant) -> None:
    event_id = await to_voting(house)
    draft, _ = await offer_draft(house, "Sam", offer("Pho Corner", "pickup"))
    pho = draft_id(await tool(house, "draft_proposal", propose("Pho Corner"), who="Kim"))
    await confirm(house, "Kim", pho)
    with pytest.raises(EventRuleViolation, match="already a proposal"):
        await confirm(house, "Sam", draft)
    assert (await proposals(house, event_id))["Pho Corner"].proposed_by == house["Kim"].id


@pytest.mark.flag("assistant", on=True)
async def test_draft_ids_are_never_reused_after_the_purge(house: House, helper: Assistant) -> None:
    old = draft_id(await tool(house, "draft_offer", offer("Siam Garden", "pickup", day="tomorrow")))
    house.clock.advance(timedelta(minutes=DRAFT_MINUTES))
    assert await retention.purge_expired_drafts(house.sessionmaker, house.clock()) == 1
    assert await draft_rows(house) == []
    new = draft_id(await tool(house, "draft_offer", offer("Siam Garden", "pickup", day="tomorrow")))
    assert new > old
    with pytest.raises(NotFound):
        await confirm(house, "Sam", old)


@pytest.mark.flag("assistant", on=True)
async def test_a_cooking_offer_replaces_a_scheduled_call(house: House, helper: Assistant) -> None:
    call = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TOMORROW)
    assert call.state is EventState.SCHEDULED
    draft, lines = await offer_draft(house, "Sam", offer("Sam's Tacos", "cook", day="tomorrow", at="19:00"))
    assert lines[0] == "It replaces tomorrow's scheduled dinner call, which won't open."
    confirmed = await confirm(house, "Sam", draft)
    assert isinstance(confirmed, OfferConfirmed) and confirmed.posted.path is OfferPath.REPLACE
    assert (await house.event(call.id)).state is EventState.CANCELLED


@pytest.mark.flag("assistant", on=True)
async def test_an_offer_joins_a_collecting_call_as_a_pick(house: House, helper: Assistant) -> None:
    event_id = await open_dinner(house, "Kim")
    draft, lines = await offer_draft(house, "Sam", offer("Sam's Tacos", "cook"))
    assert lines[0] == "It joins today's dinner call, which is collecting answers, as a pick, and you're in."
    confirmed = await confirm(house, "Sam", draft)
    assert isinstance(confirmed, OfferConfirmed) and confirmed.posted.path is OfferPath.ATTACH
    assert confirmed.posted.event_id == event_id
    assert "Sam's Tacos" in await proposals(house, event_id)
    sams = (await house.context(event_id)).response_of(house["Sam"].id)
    assert sams is not None and sams.is_in


@pytest.mark.flag("assistant", on=True)
async def test_an_offer_with_nothing_to_join_stands_alone(house: House, helper: Assistant) -> None:
    draft, lines = await offer_draft(house, "Lee", offer("Siam Garden", "delivery", day="tomorrow", at="12:15"))
    assert lines == ("It's posted as an offer of its own.", "You'll order it.")
    confirmed = await confirm(house, "Lee", draft)
    assert isinstance(confirmed, OfferConfirmed) and confirmed.posted.path is OfferPath.NEW
    offer_event = await house.event(confirmed.posted.event_id)
    assert offer_event.is_offer and offer_event.capacity == 3


@pytest.mark.flag("assistant", on=True)
async def test_an_offer_whose_outcome_changed_is_refused_and_nothing_changes(house: House, helper: Assistant) -> None:
    call = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TOMORROW)
    draft, _ = await offer_draft(house, "Sam", offer("Sam's Tacos", "cook", day="tomorrow"))
    await act(house, calls.cancel_call, house["Kim"], call.id)
    with pytest.raises(EventRuleViolation, match=r"That changed since the draft\. Now: It's posted as an offer"):
        await confirm(house, "Sam", draft)
    [row] = await draft_rows(house)
    assert row.used_at is None


@pytest.mark.flag("assistant", on=True)
async def test_offer_drafts_check_what_post_offer_checks(house: House, helper: Assistant) -> None:
    refused = {
        "no such item": offer("Noodle Bar", "pickup"),
        "not that way": offer("Taqueria", "delivery"),
        "cook list": offer("Sam's Tacos", "cook") | {"day": "today"},
        "past": offer("Siam Garden", "pickup", at="08:00"),
        "too far": offer("Siam Garden", "pickup", day="2027-01-01"),
        "bad time": offer("Siam Garden", "pickup", at="7pm"),
    }
    for case, args in refused.items():
        result = await tool(house, "draft_offer", args, who="Kim")
        assert result.is_error, case
    assert await draft_rows(house) == []
    many = await tool(house, "draft_offer", offer("Siam Garden", "pickup") | {"spare_servings": 21})
    assert many.is_error and "spare_servings" in many.content


@pytest.mark.flag("assistant", on=True)
async def test_an_offer_cant_join_a_vote_its_offerer_isnt_in(house: House, helper: Assistant) -> None:
    await to_voting(house)
    result = await tool(house, "draft_offer", offer("Siam Garden", "pickup", at="19:30"), who="Lee")
    assert result.is_error and "isn't in it" in result.content


# Catalog drafts --------------------------------------------------------------------------------

CATALOG = """version: 1
restaurants:
  - name: Noodle Bar
    cuisine: [Chinese]
    active: false
  - name: SIAM GARDEN
    cuisine: [Thai]
    notes: Changed by the draft
home_meals:
  - name: Lentil Soup
    cuisine: [Indian]
    cooks: [sam@example.com]
"""


@pytest.mark.flag("assistant", on=True)
async def test_a_catalog_draft_only_adds_at_draft_time_and_at_import(house: House, helper: Assistant) -> None:
    session = fresh(house["Kim"])
    result = await tool(house, "draft_catalog_items", {"catalog_file": CATALOG}, who="Kim", session=session)
    draft = draft_id(result)
    [view] = session.drafts
    assert view.title == "Add 2 items to the catalog"
    assert view.confirm_lines == (
        "Opens the import preview to add Noodle Bar and Lentil Soup; it changes no item the catalog has.",
        "Left out, already in the catalog: SIAM GARDEN.",
    )
    [row] = await draft_rows(house)
    assert "cooks" not in row.payload["content"] and "active" not in row.payload["content"]
    # A matching item appears before the import: it's left alone.
    await house.add_item("Noodle Bar", Mode.PICKUP, cuisine="Chinese")
    ready = await confirm(house, "Kim", draft)
    assert isinstance(ready, CatalogReady) and ready.add_only and ready.member_id == house["Kim"].id
    assert ready.sha256 == catalog_service.content_digest(ready.content)
    assert [c.state.name for c in ready.plan.items_to_create] == ["Lentil Soup"]
    assert ready.plan.items_to_update == () and {u.name for u in ready.plan.unchanged} == {"Noodle Bar"}
    async with house.sessionmaker() as db:
        plan = await catalog_service.apply_import(
            db,
            house.household.id,
            house["Kim"],
            ready.content,
            now=house.clock(),
            expected_fingerprint=ready.fingerprint,
            add_only=True,
        )
    assert [c.state.name for c in plan.items_to_create] == ["Lentil Soup"]
    async with house.sessionmaker() as db:
        siam = await catalog_service.get_item(db, house["Kim"], house.items["Siam Garden"].id)
        soup = next(i for i in (await catalog_service.catalog(db, house["Kim"])).items if i.name == "Lentil Soup")
    assert siam.notes is None and soup.cook_ids == frozenset() and soup.active


@pytest.mark.flag("assistant", on=True)
async def test_an_add_only_import_is_refused_when_an_item_appeared_after_the_preview(
    house: House, helper: Assistant
) -> None:
    draft = draft_id(await tool(house, "draft_catalog_items", {"catalog_file": CATALOG}))
    ready = await confirm(house, "Sam", draft)
    assert isinstance(ready, CatalogReady)
    await house.add_item("Lentil Soup", Mode.COOK, cuisine="Indian")
    async with house.sessionmaker() as db:
        with pytest.raises(catalog_service.ImportChanged) as changed:
            await catalog_service.apply_import(
                db,
                house.household.id,
                house["Sam"],
                ready.content,
                now=house.clock(),
                expected_fingerprint=ready.fingerprint,
                add_only=True,
            )
    assert changed.value.plan.items_to_update == ()  # the new preview still only adds


@pytest.mark.flag("assistant", on=True)
async def test_a_catalog_confirm_that_cant_run_leaves_the_draft_unused(house: House, helper: Assistant) -> None:
    draft = draft_id(await tool(house, "draft_catalog_items", {"catalog_file": CATALOG}))
    await house.tag("Chinese", TagKind.DIETARY)  # a tag of another kind takes the cuisine's name
    with pytest.raises(EventRuleViolation, match="can't be imported now"):
        await confirm(house, "Sam", draft)
    second = draft_id(await tool(house, "draft_catalog_items", {"catalog_file": CATALOG.replace("Chinese", "Thai")}))
    await house.add_item("Noodle Bar", Mode.PICKUP, cuisine="Thai")
    await house.add_item("Lentil Soup", Mode.COOK, cuisine="Indian")
    with pytest.raises(EventRuleViolation, match="nothing to add"):
        await confirm(house, "Sam", second)
    assert [row.used_at for row in await draft_rows(house)] == [None, None]


@pytest.mark.flag("assistant", on=True)
async def test_an_offer_draft_for_a_call_that_moved_to_voting_is_stale(house: House, helper: Assistant) -> None:
    event_id = await open_dinner(house, "Sam", "Kim")
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    draft, lines = await offer_draft(house, "Sam", offer("Sam's Tacos", "cook", at="19:30"))
    assert "collecting answers" in lines[0]
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.respond, house["Lee"], event_id, Status.OUT)  # everyone answered
    house.clock.advance(timedelta(minutes=11))  # after a quiet spell, voting opens early
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    with pytest.raises(EventRuleViolation, match="which is voting"):
        await confirm(house, "Sam", draft)


@pytest.mark.flag("assistant", on=True)
async def test_catalog_drafts_refuse_bad_files_and_files_with_nothing_new(house: House, helper: Assistant) -> None:
    bad = await tool(house, "draft_catalog_items", {"catalog_file": "version: 2\nrestaurants: nope\n"})
    assert bad.is_error and "isn't a valid catalog file" in bad.content
    nothing = await tool(
        house,
        "draft_catalog_items",
        {"catalog_file": "version: 1\nrestaurants:\n  - {name: Taqueria, cuisine: [Mexican]}\n"},
    )
    assert nothing.is_error and "all of those already" in nothing.content
    clash = await tool(
        house,
        "draft_catalog_items",
        {"catalog_file": "version: 1\nrestaurants:\n  - {name: New Place, cuisine: [Vegetarian]}\n"},
    )
    assert clash.is_error and "can't be imported" in clash.content
    huge = await tool(house, "draft_catalog_items", {"catalog_file": "x" * 40_001})
    assert huge.is_error and "at most" in huge.content
    assert await draft_rows(house) == []


# Admins, limits, and what's sent ---------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_an_admin_who_isnt_the_coordinator_is_refused_wherever_a_member_is(
    house: House, helper: Assistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    alex = house["Alex"]
    assert alex.is_admin
    event_id = await open_dinner(house, "Sam", "Kim", "Alex")
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    for name, args in (
        ("find_candidates", NO_FILTERS),
        ("draft_proposal", propose("Pho Corner")),
        ("draft_offer", offer("Sam's Tacos", "cook")),
        (
            "draft_catalog_items",
            {"catalog_file": "version: 1\nrestaurants:\n  - {name: Taqueria, cuisine: [Mexican]}\n"},
        ),
    ):
        as_admin = await tool(house, name, args, who="Alex")
        as_member = await tool(house, name, args, who="Kim")
        assert as_admin.is_error and as_admin == as_member, name
    house.clock.now = RESPOND_BY
    await house.tick()
    await nobody_coordinates(house, event_id)
    for name, args in (("draft_proposal", propose("Banned Burger")),):
        assert await tool(house, name, args, who="Alex") == await tool(house, name, args, who="Kim")
    kims = draft_id(await tool(house, "draft_proposal", propose("Pho Corner"), who="Kim"))
    with pytest.raises(NotFound):
        await confirm(house, "Alex", kims)  # an admin can't confirm someone else's draft
    seen: list[Member] = []
    real = calls.add_proposal

    async def spy(db: Any, actor: Member, *args: Any, **kwargs: Any) -> Any:
        seen.append(actor)
        return await real(db, actor, *args, **kwargs)

    monkeypatch.setattr(drafts.calls, "add_proposal", spy)
    mine = draft_id(await tool(house, "draft_proposal", propose("Veg Garden"), who="Alex"))
    await confirm(house, "Alex", mine)
    assert [(m.id, m.role, m.is_admin) for m in seen] == [(alex.id, Role.MEMBER, False)]
    async with house.sessionmaker() as db:
        audits = list(await db.scalars(select(AuditRow).where(AuditRow.actor_member_id == alex.id)))
    assert ActingAs.ADMIN.value not in json.dumps([a.payload for a in audits])


@pytest.mark.flag("assistant", on=True)
async def test_a_member_has_at_most_ten_drafts_waiting(house: House, helper: Assistant) -> None:
    for _ in range(10):
        draft_id(await tool(house, "draft_offer", offer("Siam Garden", "pickup", day="tomorrow")))
    eleventh = await tool(house, "draft_offer", offer("Siam Garden", "pickup", day="tomorrow"))
    assert eleventh == ToolResult(drafts.TOO_MANY, is_error=True)
    assert not (await tool(house, "draft_offer", offer("Siam Garden", "pickup", day="tomorrow"), who="Kim")).is_error


@pytest.mark.flag("assistant", on=True)
async def test_the_reply_carries_the_drafts_and_the_model_gets_only_a_summary(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    await to_voting(house)
    model.script = [
        tool_call("draft_proposal", propose("Pho Corner")),
        answer("Drafted Pho Corner for Member 1. Tap Confirm."),
    ]
    sam = house["Sam"]
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, sam, web_key(sam.id), private=True, text="Propose pho, I'll grab it")
    assert reply is not None and reply.text == "Drafted Pho Corner for Sam. Tap Confirm."
    [view] = reply.drafts
    assert view.title == "Propose Pho Corner (pickup)"
    result = model.requests[-1]["messages"][-1]["content"][0]
    assert result["content"].startswith(f"Draft {view.id} is waiting for Member 1 to confirm")
    assert "item_id" not in result["content"] and "join_late" not in result["content"]
    async with house.sessionmaker() as db:
        assert await drafts.waiting(db, sam, web_key(sam.id), house.clock()) == [view]
        assert await drafts.waiting(db, sam, "web:0", house.clock()) == []


@pytest.mark.flag("assistant", on=True)
async def test_nothing_personal_leaves_the_house_through_the_draft_tools(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    await to_voting(house)
    kim = house["Kim"]
    model.script = [
        tool_call(
            "find_candidates",
            NO_FILTERS,
            toolu_2=("draft_offer", offer("Siam Garden", "pickup", day="tomorrow")),
            toolu_3=("draft_catalog_items", {"catalog_file": CATALOG}),
            toolu_4=("draft_proposal", propose("Veg Garden")),
        ),
        answer("Done."),
    ]
    async with house.sessionmaker() as db:
        await converse(db, helper, kim, web_key(kim.id), private=True, text="Plan dinner")
    sent = tool_results(model)
    assert "Draft" in sent and "Pho Corner" in sent
    for secret in (
        "@example.com",
        "Changed by the draft",
        "veto",
        "favorite",
        "Mixed reviews",
        "Alex",
        "Lee",
        '"Kim"',
        "score",
        "cook_ids",
        "U0",
    ):
        assert secret not in sent, secret
    assert sent.count("Sam") == sent.count("Sam's Tacos")


# Retention -------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_the_purge_deletes_expired_drafts_and_scrub_test_deletes_all(house: House, helper: Assistant) -> None:
    draft_id(await tool(house, "draft_offer", offer("Siam Garden", "pickup", day="tomorrow")))
    assert await retention.purge_expired_drafts(house.sessionmaker, house.clock()) == 0
    house.clock.advance(timedelta(minutes=DRAFT_MINUTES))
    draft_id(await tool(house, "draft_offer", offer("Siam Garden", "pickup", day="tomorrow")))
    assert await retention.purge_expired_drafts(house.sessionmaker, house.clock()) == 1
    assert len(await draft_rows(house)) == 1
    async with house.sessionmaker() as db:
        summary = await scrub.scrub_test(db, instance_label="TEST", keep=[house["Alex"].email or ""], now=house.clock())
    assert summary.drafts_deleted == 1 and "Assistant drafts deleted: 1" in summary.lines()
    assert await draft_rows(house) == []


def test_offer_outcomes_follow_offer_path() -> None:
    from dinnerbell.domain.events import Event, EventKind

    def call(state: EventState) -> Event:
        at = OPENS
        return Event(1, 1, EventKind.CALL, Slot.DINNER, DAY, state, 1, at, at, 0, 0)

    assert offer_outcome(None, Mode.COOK) is OfferOutcome.HOLD
    assert offer_outcome(None, Mode.PICKUP) is OfferOutcome.ALONE
    assert offer_outcome(call(EventState.SCHEDULED), Mode.COOK) is OfferOutcome.REPLACE
    assert offer_outcome(call(EventState.SCHEDULED), Mode.PICKUP) is OfferOutcome.ALONE
    assert offer_outcome(call(EventState.VOTING), Mode.PICKUP) is OfferOutcome.JOIN
    assert offer_outcome(call(EventState.DECIDED), Mode.COOK) is OfferOutcome.ALONE


@pytest.mark.flag("assistant", on=True)
async def test_offers_service_still_posts_without_a_check(house: House) -> None:
    data = offers.OfferInput(Slot.DINNER, TOMORROW, Mode.PICKUP, time(19, 0), 2, item_id=house.items["Taqueria"].id)
    posted = await act(house, offers.post_offer, house["Kim"], data)
    assert posted.path is OfferPath.NEW
