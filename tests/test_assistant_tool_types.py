# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every assistant tool checks the type of every field it takes (#283). No tool is strict, so the
API doesn't hold the model to the schema: each tool's own checks are all that stand between the
model's input and the services.

Each tool has a baseline: input it accepts, against a call in the state the tool needs, in the
usual household (``tests.support.calls.furnish``). The test breaks one field at a time, and one
element of each list, and the refusal must name that field. A refusal on a later meaning check
would name something else, so a field the baseline can't reach before a meaning check fails the
test instead of passing it.

The refusal's advice must match the field, too: for a text field the baseline accepts empty, it
says an empty string works, and for one the baseline refuses empty, it says nothing about one.
Whether a field is required can depend on another, such as a meal's name, which a new meal needs
and an edit keeps when empty; the baseline decides.

Sam asks, in a private conversation where Sam is Member 1, Alex Member 2, Kim Member 3, and Lee
Member 4.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import time, timedelta
from typing import Any

import pytest

from dinnerbell.config import Settings
from dinnerbell.domain.assistant import AssistantSettings, Pseudonyms, web_key
from dinnerbell.domain.catalog import Mode, TagKind
from dinnerbell.domain.events import EventState, RoleOffer, Status
from dinnerbell.domain.guests import GuestInput
from dinnerbell.domain.meals import MealInput, Slot
from dinnerbell.services import call_admin, calls, guests, meals, offers
from dinnerbell.services.assistant import admin, tools
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.assistant.tools import ToolResult, ToolSession
from dinnerbell.services.away import set_my_away
from dinnerbell.services.database import Database, open_database
from tests.support.assistant import drafting
from tests.support.calls import MEAL, OPENS, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.feedback import done_dinner
from tests.support.links import MENU_LINK, StubLinks

pytestmark = pytest.mark.anyio

WRONG: tuple[object, ...] = (7, None, [], {}, True, "x")
"""One value of each JSON type, and a bare string. A field breaks with each one its type doesn't
allow; a list breaks with each one its elements' type doesn't allow."""
ASKER = "Sam"
LABELS = ("Sam", "Alex", "Kim", "Lee")
DINNER = {"slot": "dinner", "day": "today"}
CATALOG_FILE = """version: 1
restaurants:
  - name: Noodle Stop
    cuisine: [Noodles]
"""


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
    async with house.sessionmaker() as db:
        await admin.update_assistant(
            db,
            AssistantConfig(key_set=True),
            house["Alex"],
            AssistantSettings(enabled=True),
            now=house.clock(),
        )
    yield house
    await house.close()


async def act(house: House, use_case: Any, *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


# Stages: the household as each tool's baseline needs it --------------------------------------------


async def nothing(house: House) -> None:
    """No call: the tool needs none."""


async def answering(house: House) -> None:
    """Dinner is open for answers. Kim and Lee are in; Sam hasn't answered."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in ("Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)


async def answered(house: House) -> None:
    """As ``answering``, and Sam is in too."""
    await answering(house)
    await act(house, calls.respond, house[ASKER], await dinner_id(house), Status.IN)


async def with_a_guest(house: House) -> None:
    """As ``answered``, and Sam brings Gwendolyn, a new guest without Slack, who's in."""
    await answered(house)
    await act(house, guests.invite, house[ASKER], await dinner_id(house), new_guest=GuestInput("Gwendolyn"))


async def voting(house: House) -> None:
    """Alex, Sam, Kim, and Lee are in; Kim offers to pick up; it's voting, and Sam coordinates."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in LABELS:
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Kim"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house[ASKER].id)


async def decided(house: House) -> None:
    """As ``voting``, then Sam locks Taqueria (pickup), and it's the meal time."""
    await voting(house)
    event_id = await dinner_id(house)
    taqueria = next(p for p in (await house.context(event_id)).picks if p.item_id == house.items["Taqueria"].id)
    await act(house, calls.lock, house[ASKER], event_id, taqueria.id)
    assert (await house.event(event_id)).state is EventState.DECIDED
    house.clock.now = MEAL


async def offered(house: House) -> None:
    """Sam offers to pick up Taqueria for tomorrow's lunch."""
    data = offers.OfferInput(
        slot=Slot.LUNCH,
        day=house.clock().date() + timedelta(days=1),
        mode=Mode.PICKUP,
        at=time(12, 0),
        capacity=3,
        item_id=house.items["Taqueria"].id,
    )
    await act(house, offers.post_offer, house[ASKER], data)


async def eaten(house: House) -> dict[str, Any]:
    """Sam ate last night's dinner, so it takes Sam's feedback; the baseline names its meal."""
    await done_dinner(house, eaters=("Alex", ASKER, "Kim"))
    [meal] = await house.meals()
    return {"meal": meal.id}


async def voting_with_pick(house: House) -> dict[str, Any]:
    """As ``voting``; the baseline names Taqueria's pick by its number."""
    await voting(house)
    ctx = await house.context(await dinner_id(house))
    return {"pick": next(p.id for p in ctx.picks if p.item_id == house.items["Taqueria"].id)}


async def logged(house: House) -> dict[str, Any]:
    """Sam logged Siam Garden for dinner two days ago, with Sam alone; the baseline names it."""
    async with house.sessionmaker() as db:
        meal = await meals.log_meal(
            db,
            house[ASKER],
            MealInput(
                house.clock().date() - timedelta(days=2),
                Slot.DINNER,
                house.items["Siam Garden"].id,
                frozenset({house[ASKER].id}),
            ),
            today=house.clock().date(),
            now=house.clock(),
        )
    return {"meal": meal.id}


async def away(house: House) -> None:
    """Sam is away tomorrow."""
    tomorrow = house.clock().date() + timedelta(days=1)
    await act(house, set_my_away, house[ASKER], tomorrow, tomorrow)


async def with_dietary_tags(house: House) -> None:
    await house.tag("Vegetarian", TagKind.DIETARY)


@dataclass(frozen=True)
class Baseline:
    """Input the tool accepts once ``stage`` has run. A stage may return values for the fields
    that depend on what it made, such as a meal's number; a tool without that field ignores it.
    A tool has more than one baseline when another field decides whether a text field may be
    empty, such as a pick by number, whose name is then empty, or by name with pick 0."""

    stage: Callable[[House], Awaitable[Mapping[str, Any] | None]]
    input: Mapping[str, Any] = field(default_factory=dict)
    label: str = ""
    """Names a tool's second and later baselines in the test IDs."""


CALL = {"slot": "", "day": ""}
NO_ROLES = {"way": "", "lead": "", "pickup": ""}
NO_EDIT = {
    "kind": "",
    "catalog": "",
    "new_name": "",
    "modes": [],
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
BASELINES: dict[str, tuple[Baseline, ...]] = {
    "look_up_catalog_items": (Baseline(nothing, {"name": "", "tag": ""}),),
    "look_up_history": (
        Baseline(nothing, {"name": "", "tag": "", "kind": "", "since": "", "until": "", "order": "most"}),
    ),
    "find_candidates": (Baseline(voting, DINNER | {"not_eaten_in_days": 0, "cuisine": "", "mode": ""}),),
    "draft_proposal": (Baseline(voting, DINNER | {"name": "Sam's Tacos", "mode": "cook"}),),
    "draft_offer": (
        Baseline(
            nothing,
            {"name": "Taqueria", "mode": "pickup", "slot": "lunch", "day": "tomorrow", "time": "", "spare_servings": 0},
        ),
    ),
    "draft_catalog_items": (Baseline(nothing, {"catalog_file": CATALOG_FILE}),),
    "draft_catalog_from_link": (Baseline(nothing, {"url": MENU_LINK, "kind": ""}),),
    "look_up_call": (Baseline(answering, CALL),),
    "draft_answer": (Baseline(answering, CALL | {"answer": "in"}),),
    "draft_vote": (
        Baseline(voting, CALL | {"pick": 0, "name": "Taqueria", "mode": "pickup", "vote": True}),
        Baseline(voting_with_pick, CALL | {"pick": 0, "name": "", "mode": "", "vote": True}, "by_number"),
    ),
    "draft_suggestion": (Baseline(answered, CALL | {"name": "Taqueria"}),),
    "draft_role": (Baseline(answered, CALL | {"role": "pickup", "offer": True}),),
    "draft_take_back_offer": (Baseline(offered, CALL),),
    "draft_invite_guest": (Baseline(answered, CALL | {"name": "Gwendolyn", "needs": []}),),
    "draft_guest_answer": (
        Baseline(with_a_guest, CALL | {"name": "Gwendolyn", "answer": "out", "role": "", "offer": False}),
    ),
    "draft_call": (Baseline(nothing, {"slot": "lunch", "day": "tomorrow"}),),
    "look_up_my_preferences": (Baseline(nothing, {"name": ""}),),
    "look_up_housemates_preferences": (Baseline(nothing, {"member": "", "name": ""}),),
    "draft_preference": (Baseline(nothing, {"what": "", "name": "Taqueria", "level": "veto"}),),
    "look_up_my_feedback": (Baseline(eaten, {"name": ""}),),
    "draft_feedback": (
        Baseline(
            eaten,
            {
                "meal": 0,
                "rating": "liked",
                "went_wrong": [],
                "what_went_wrong": "",
                "thank_cook": False,
                "cook_note": "",
            },
        ),
    ),
    "draft_away": (
        Baseline(nothing, {"first": "tomorrow", "last": "tomorrow", "clear": False}),
        Baseline(away, {"first": "", "last": "", "clear": True}, "clear"),
    ),
    "draft_dietary_needs": (Baseline(with_dietary_tags, {"add": ["Vegetarian"], "remove": []}),),
    "draft_can_usually": (Baseline(nothing, {"cook": "on", "pick_up": "", "order": ""}),),
    "look_up_coordinating": (Baseline(voting, DINNER),),
    "draft_decide": (
        Baseline(voting, DINNER | {"pick": 0, "name": "Taqueria", "mode": "pickup", "break_tie": False} | NO_ROLES),
        Baseline(
            voting_with_pick,
            DINNER | {"pick": 0, "name": "", "mode": "", "break_tie": False} | NO_ROLES,
            "by_number",
        ),
    ),
    "draft_shuffle": (Baseline(voting, DINNER),),
    "draft_plan": (Baseline(decided, DINNER | {"meal_time": "19:00", "place": "Siam Garden"} | NO_ROLES),),
    "draft_hand_off": (Baseline(voting, DINNER | {"to": "Member 3"}),),
    "draft_mark_done": (Baseline(decided, DINNER),),
    "draft_call_off": (Baseline(voting, DINNER | {"reason": ""}),),
    "draft_catalog_edit": (
        Baseline(nothing, NO_EDIT | {"name": "Taqueria", "meals": ["dinner"], "notes": "Cash only."}),
    ),
    "look_up_my_meals": (Baseline(nothing, {"name": ""}),),
    "draft_meal": (
        Baseline(
            nothing,
            {
                "meal": 0,
                "delete": False,
                "day": "yesterday",
                "slot": "dinner",
                "name": "Taqueria",
                "kind": "",
                "add_people": [],
                "remove_people": [],
            },
        ),
        Baseline(
            logged,
            {
                "meal": 0,
                "delete": False,
                "day": "",
                "slot": "",
                "name": "",
                "kind": "",
                "add_people": ["Member 3"],
                "remove_people": [],
            },
            "edit",
        ),
    ),
}


async def run(house: House, name: str, raw: Mapping[str, Any]) -> ToolResult:
    member = house[ASKER]
    session = drafting(  # so a baseline that acts at once (#374) can run again
        ToolSession(
            key=web_key(member.id),
            pseudonyms=Pseudonyms({house[n].id: number for number, n in enumerate(LABELS, start=1)}),
            links=StubLinks(),
        )
    )
    async with house.sessionmaker() as db:
        return await tools.run_tool(
            name, raw, db=db, member=member, private=True, now=house.clock(), session=session, memory=True
        )


def breaks(spec: Mapping[str, Any], baseline: object) -> list[tuple[str, object]]:
    """Each way to give a field of type ``spec`` a value of the wrong type: the value itself, and,
    for a list, the baseline's elements followed by one of the wrong type."""
    found: list[tuple[str, object]] = [(f"{wrong!r}", wrong) for wrong in WRONG if not _fits(spec, wrong)]
    if spec.get("type") == "array":
        assert isinstance(baseline, list)
        items = spec["items"]
        found += [(f"[..., {wrong!r}]", [*baseline, wrong]) for wrong in WRONG if not _fits(items, wrong)]
    return found


def test_every_baseline_is_for_a_tool_and_has_exactly_its_fields() -> None:
    """A tool added without a baseline fails the next test; this one fails on a stale baseline."""
    by_name = {t.name: t for t in tools.TOOLS}
    assert set(BASELINES) == set(by_name)
    for name, baselines in BASELINES.items():
        for baseline in baselines:
            assert set(baseline.input) == set(by_name[name].input_schema["properties"]), name


CASES = [
    pytest.param(tool, n, id=tool.name + (f"-{b.label}" if n else ""))
    for tool in tools.TOOLS
    for n, b in enumerate(BASELINES.get(tool.name, (Baseline(nothing),)))
]
"""Each tool's baselines. A tool without one still gets a case, which fails."""


@pytest.mark.parametrize(("tool", "n"), CASES)
async def test_every_tool_refuses_input_of_the_wrong_type(house: House, tool: tools.Tool, n: int) -> None:
    """The baseline works, and breaking any one field, or one element of a list, is refused in
    words that name that field: an error for the model, never a crash or a refusal about
    something else. For a text field, the words suggest an empty string only when the tool
    takes one there."""
    baseline = BASELINES[tool.name][n]
    made = await baseline.stage(house) or {}
    good = {name: made.get(name, value) for name, value in baseline.input.items()}
    accepted = await run(house, tool.name, good)
    assert not accepted.is_error, accepted.content
    for name, spec in tool.input_schema["properties"].items():
        text = spec.get("type") == "string" and "enum" not in spec
        empty_ok = text and not (await run(house, tool.name, good | {name: ""})).is_error
        for shown, wrong in breaks(spec, good[name]):
            result = await run(house, tool.name, good | {name: wrong})
            assert result.is_error and result.content.startswith(f"{name} "), (name, shown, result.content)
            if text:
                assert ("empty" in result.content) == empty_ok, (name, shown, empty_ok, result.content)


def _fits(spec: Mapping[str, Any], value: object) -> bool:
    """Whether ``value`` already has a type ``spec`` allows, so it isn't a wrong one."""
    kinds = spec.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    python = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
    if value is None:
        return "null" in kinds
    if isinstance(value, bool):
        return "boolean" in kinds
    return any(isinstance(value, python[k]) for k in kinds if k in python)
