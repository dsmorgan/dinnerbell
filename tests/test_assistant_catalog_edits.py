# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catalog edits and the meal log through the assistant (#254, spec 6.14, **Tools** and
**Drafts**), in any conversation: edit a catalog item or take it off the catalog (FR-23), and log,
edit, or delete a meal the member logged (spec 5.6). Each is a draft, and each **Confirm** runs the
use case the Catalog item form or History's forms call, as the member with a member's role.

The household is the usual one (``tests.support.calls.furnish``): Alex (the admin), Sam, Kim, and
Lee; Siam Garden (pickup and delivery, with a link), Taqueria, and Sam's Tacos, which only Sam
cooks. The assistant is on. In Sam's conversation, Sam is Member 1; a test that names someone else
labels them as a Slack mention would.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import date, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.channels.slack.assistant import draft_card, safe_reply
from dinnerbell.config import Settings
from dinnerbell.db.catalog import CatalogRepository, ItemValues
from dinnerbell.db.models import AssistantDraftRow
from dinnerbell.domain.assistant import AssistantSettings, Pseudonyms, slack_key, web_key
from dinnerbell.domain.assistant_catalog import EDIT_FIELDS, NOT_SHOWN, UNCHANGED, item_snapshot
from dinnerbell.domain.catalog import CatalogItem, CatalogRuleViolation, InvalidCatalogInput, ItemInput, Mode, TagKind
from dinnerbell.domain.events import EventRuleViolation
from dinnerbell.domain.feedback import FeedbackInput, Rating
from dinnerbell.domain.guests import GuestInput
from dinnerbell.domain.meals import CALL_PLACE_FIXED, MealInput, MealLog, Slot
from dinnerbell.domain.members import Member
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services import feedback, guests, meals, members, offers
from dinnerbell.services.assistant import admin, catalog_edits, drafts, tools
from dinnerbell.services.assistant.call_drafts import NOTHING_TO_DO, ActionConfirmed
from dinnerbell.services.assistant.conversation import Assistant, converse
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.assistant.tools import ToolResult, ToolSession
from dinnerbell.services.database import Database, open_database
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, tool_call

pytestmark = pytest.mark.anyio

CONFIGURED = AssistantConfig(key_set=True, flag_on=True)
NAMES = ("Alex", "Sam", "Kim", "Lee", "Robin")
DAY = date(2026, 10, 22)
"""The clock's day (``FakeClock``)."""
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
NO_MEAL_CHANGE = {"delete": False, "day": "", "slot": "", "name": "", "kind": "", "add_people": [], "remove_people": []}
SECRET_NOTES = "Gate code 4321"


def edit(name: str, **changes: Any) -> dict[str, Any]:
    return NO_EDIT | {"name": name} | changes


def meal(number: int, **changes: Any) -> dict[str, Any]:
    return NO_MEAL_CHANGE | {"meal": number} | changes


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
    await house.tag("Vegetarian", TagKind.DIETARY)
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


def sams(house: House, *also: str) -> Pseudonyms:
    """Sam's conversation: Sam, then whoever ``also`` names, as Slack mentions label them."""
    labels = Pseudonyms().with_member(house["Sam"].id)
    for name in also:
        labels = labels.with_member(house[name].id)
    return labels


async def tool(
    house: House,
    name: str,
    args: dict[str, Any],
    *,
    who: str = "Sam",
    labels: Pseudonyms | None = None,
    private: bool = True,
    key: str | None = None,
) -> ToolResult:
    member = house[who]
    pseudonyms = labels if labels is not None else Pseudonyms().with_member(member.id)
    session = ToolSession(key=key or web_key(member.id), pseudonyms=pseudonyms)
    async with house.sessionmaker() as db:
        return await tools.run_tool(
            name, args, db=db, member=member, private=private, now=house.clock(), session=session
        )


def draft_id(result: ToolResult) -> int:
    assert not result.is_error, result.content
    return int(result.content.split()[1])


async def rows(house: House) -> list[AssistantDraftRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AssistantDraftRow).order_by(AssistantDraftRow.id)))


async def row(house: House, draft: int) -> AssistantDraftRow:
    async with house.sessionmaker() as db:
        found = await db.get(AssistantDraftRow, draft)
    assert found is not None
    return found


async def card(house: House, draft: int, who: str = "Sam") -> drafts.DraftView:
    async with house.sessionmaker() as db:
        waiting = await drafts.waiting(db, house[who], web_key(house[who].id), house.clock())
    [found] = [v for v in waiting if v.id == draft]
    return found


async def confirm(house: House, who: str | Member, draft: int) -> Any:
    member = house[who] if isinstance(who, str) else who
    async with house.sessionmaker() as db:
        return await drafts.confirm_draft(db, house.clock, CONFIGURED, member, draft)


async def item(house: House, name: str) -> CatalogItem:
    async with house.sessionmaker() as db:
        found = await CatalogRepository(db).item(house.household.id, house.items[name].id)
    assert found is not None
    return found


async def as_web(house: House, who: str, item_name: str, **changes: Any) -> CatalogItem:
    """``who`` saves the Catalog item form for ``item_name`` with ``changes``, as the web does."""
    now = await item(house, item_name)
    data = ItemInput(
        kind=now.kind,
        name=now.name,
        modes=now.modes,
        tag_ids=now.tag_ids,
        cook_ids=now.cook_ids,
        url=now.url,
        order_url=now.order_url,
        notes=now.notes,
        default_servings=now.default_servings,
        meals=now.meals,
    )
    data = replace(data, **changes)
    async with house.sessionmaker() as db:
        return await catalog_service.update_item(db, house[who], now.id, data, now=house.clock())


async def log(house: House, who: str, item_name: str, *people: str, day: date = DAY) -> MealLog:
    eaten = MealInput(day, Slot.LUNCH, house.items[item_name].id, frozenset(house[p].id for p in people or (who,)))
    async with house.sessionmaker() as db:
        return await meals.log_meal(db, house[who], eaten, today=DAY, now=house.clock())


async def meal_now(house: House, meal_id: int) -> MealLog | None:
    async with house.sessionmaker() as db:
        from dinnerbell.db.meals import MealLogRepository

        return await MealLogRepository(db).get(house.household.id, meal_id)


def strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from strings(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from strings(inner)


def assert_no_names(*texts: str) -> None:
    for text in texts:
        text = text.replace("Sam's Tacos", "")  # an item's name: catalog text, not a person
        for name in NAMES:
            assert not re.search(rf"\b{name}\b", text), (name, text)


# Done when ---------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_mark_siam_garden_as_delivery_only_drafts_a_diff_and_confirm_saves_it(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """On the web, Sam says "Mark Siam Garden as delivery only". The model drafts an edit, which
    changes nothing: the card shows the ways from what they are to what they become, and nothing
    else. Confirm saves it through ``catalog.update_item``, the Catalog form's use case, which
    audits it as Sam's edit."""
    sam = house["Sam"]
    before = await item(house, "Siam Garden")
    model.script = [
        tool_call("draft_catalog_edit", edit("Siam Garden", modes=["delivery"])),
        answer("Here's the change: Siam Garden becomes delivery only once you confirm."),
    ]
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, sam, web_key(sam.id), private=True, text="Mark Siam Garden as delivery only")
    assert reply is not None
    [view] = reply.drafts
    assert (view.kind, view.title) == ("item_edit", "Edit Siam Garden")
    assert view.confirm_lines == ("Ways: delivery (was pickup and delivery).", UNCHANGED)
    summary = model.requests[1]["messages"][-1]["content"][0]["content"]
    assert "Ways: delivery (was pickup and delivery)." in summary and "Nothing has changed yet" in summary
    assert await item(house, "Siam Garden") == before  # nothing until Confirm

    done = await confirm(house, "Sam", view.id)
    assert done == ActionConfirmed(
        None, "Confirmed. Siam Garden is updated.", link=(f"/catalog/items/{before.id}", "Open it in the catalog")
    )
    after = await item(house, "Siam Garden")
    assert after.modes == frozenset({Mode.DELIVERY})
    assert item_snapshot(after) | {"modes": ["pickup", "delivery"]} == item_snapshot(before)
    [audit] = await house.audits("catalog.item_updated")
    assert audit.actor_member_id == sam.id and audit.payload["modes"] == ["delivery"]
    with pytest.raises(drafts.DraftUnavailable):
        await confirm(house, "Sam", view.id)


@pytest.mark.flag("assistant", on=True)
async def test_cooks_come_only_from_the_households_active_members_by_label(house: House) -> None:
    """A cook is named by a label the conversation gives, resolved at draft time to a member of
    the household: never someone the model invented, a guest, someone deactivated, a name, or an
    email. Each of those gets the same refusal, which names nobody, and drafts nothing. A real
    member's label drafts a cook by member ID: the card names them, the model reads their label."""
    async with house.sessionmaker() as db:
        robin = await guests.add_guest(db, house["Alex"], GuestInput("Robin"), now=house.clock())
        await members.set_member_active(db, house["Alex"], house["Lee"].id, False, now=house.clock())
    labels = sams(house, "Kim", "Lee").with_member(robin.id)  # Kim 2, Lee 3, Robin 4
    refusal = catalog_edits.NOT_SOMEONE
    for label in ("Member 9", "Member 3", "Member 4"):
        result = await tool(house, "draft_catalog_edit", edit("Sam's Tacos", add_cooks=[label]), labels=labels)
        assert result == ToolResult(refusal.format(label=label), is_error=True), label
    for written in ("Kim", "kim@example.com", f"{house['Kim'].id}"):
        result = await tool(house, "draft_catalog_edit", edit("Sam's Tacos", add_cooks=[written]), labels=labels)
        assert result == ToolResult(catalog_edits.LABEL_ONLY, is_error=True), written
    assert await rows(house) == []

    result = await tool(house, "draft_catalog_edit", edit("Sam's Tacos", add_cooks=["Member 2"]), labels=labels)
    assert "Cooks: adds Member 2." in result.content
    assert_no_names(result.content)
    stored = await row(house, draft_id(result))
    assert stored.payload["after"]["cook_ids"] == sorted({house["Sam"].id, house["Kim"].id})
    assert_no_names(stored.title, *stored.lines)
    assert (await card(house, stored.id)).confirm_lines[0] == "Cooks: adds Kim."
    await confirm(house, "Sam", stored.id)
    assert (await item(house, "Sam's Tacos")).cook_ids == {house["Sam"].id, house["Kim"].id}


@pytest.mark.flag("assistant", on=True)
async def test_a_cook_who_stops_being_a_member_before_confirm_is_refused_by_the_service(house: House) -> None:
    labels = sams(house, "Kim")
    result = await tool(house, "draft_catalog_edit", edit("Sam's Tacos", add_cooks=["Member 2"]), labels=labels)
    async with house.sessionmaker() as db:
        await members.set_member_active(db, house["Alex"], house["Kim"].id, False, now=house.clock())
    with pytest.raises(EventRuleViolation, match="isn't an active member"):
        await confirm(house, "Sam", draft_id(result))
    assert (await item(house, "Sam's Tacos")).cook_ids == {house["Sam"].id}
    assert (await row(house, draft_id(result))).used_at is None


@pytest.mark.flag("assistant", on=True)
async def test_a_deactivated_cook_the_item_has_stays_and_can_be_taken_off(house: House) -> None:
    """As the form allows, an edit keeps a cook who has since been deactivated; taking them off
    works by their label."""
    async with house.sessionmaker() as db:
        await members.set_member_active(db, house["Alex"], house["Sam"].id, False, now=house.clock())
    labels = Pseudonyms().with_member(house["Kim"].id).with_member(house["Sam"].id)
    kept = await tool(house, "draft_catalog_edit", edit("Sam's Tacos", meals=["dinner"]), who="Kim", labels=labels)
    assert (await row(house, draft_id(kept))).payload["after"]["cook_ids"] == [house["Sam"].id]
    off = await tool(
        house, "draft_catalog_edit", edit("Sam's Tacos", remove_cooks=["Member 2"]), who="Kim", labels=labels
    )
    assert "Cooks: takes off Member 2." in off.content
    await confirm(house, "Kim", draft_id(off))
    assert (await item(house, "Sam's Tacos")).cook_ids == frozenset()


# Refusals mirror the service ---------------------------------------------------------------------


REFUSED_EDITS = [
    ("rename onto another", edit("Siam Garden", new_name="taqueria"), {"name": "taqueria"}),
    ("no cuisine left", edit("Siam Garden", remove_tags=["Thai"]), {"tag_ids": frozenset()}),
    ("a link that isn't one", edit("Siam Garden", url="ftp://siam.example"), {"url": "ftp://siam.example"}),
    ("no meals", None, {"meals": frozenset()}),
]


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize(("why", "args", "web"), REFUSED_EDITS, ids=[r[0] for r in REFUSED_EDITS])
async def test_an_edit_the_form_refuses_is_refused_in_the_forms_words(
    house: House, why: str, args: dict[str, Any] | None, web: dict[str, Any]
) -> None:
    """Each refusal is the one ``catalog.update_item`` gives the form for the same change."""
    with pytest.raises((InvalidCatalogInput, CatalogRuleViolation)) as refused:
        await as_web(house, "Sam", "Siam Garden", **web)
    if args is not None:
        result = await tool(house, "draft_catalog_edit", args)
        assert result == ToolResult(str(refused.value), is_error=True)
    assert await rows(house) == []


@pytest.mark.flag("assistant", on=True)
async def test_an_edit_only_the_admin_could_make_has_no_way_in(house: House) -> None:
    """Renaming and merging tags are the admin's (FR-25), and an item's kind never changes: the
    tool takes none of them, so an extra field is refused before anything runs. New tags are left
    to the Catalog page, and fields a kind doesn't have are refused rather than dropped."""
    for extra in ({"rename_tag": "Thai"}, {"merge_tag": "Thai"}, {"new_kind": "home"}, {"member": "Member 2"}):
        result = await tool(house, "draft_catalog_edit", edit("Siam Garden", **extra), who="Alex")
        assert result.is_error and result.content.startswith("Send exactly these fields"), extra
    refusals = {
        "a new tag": (edit("Siam Garden", add_tags=["Brand New"]), catalog_edits.NO_SUCH_TAG.format(name="Brand New")),
        "cooks on a restaurant": (edit("Siam Garden", add_cooks=["Member 1"]), catalog_edits.NO_COOKS),
        "ways on a home meal": (edit("Sam's Tacos", modes=["pickup"]), catalog_edits.HOME_MODES),
        "servings on a restaurant": (edit("Siam Garden", servings=4), catalog_edits.NO_SERVINGS),
        "an order link on a home meal": (
            edit("Sam's Tacos", order_url="https://x.example"),
            catalog_edits.NO_ORDER_LINK,
        ),
        "a tag it hasn't": (edit("Siam Garden", remove_tags=["Vegetarian"]), "It has no tag named Vegetarian."),
        "nothing": (edit("Siam Garden", modes=["pickup", "delivery"]), NOTHING_TO_DO),
        "off with an edit": (edit("Siam Garden", catalog="off", notes="x"), catalog_edits.ACTIVE_ALONE),
        "back while on": (edit("Siam Garden", catalog="back"), catalog_edits.ALREADY_ON),
    }
    for why, (args, words) in refusals.items():
        assert await tool(house, "draft_catalog_edit", args, who="Alex") == ToolResult(words, is_error=True), why
    assert await rows(house) == []


@pytest.mark.flag("assistant", on=True)
async def test_taking_an_item_off_and_back_runs_the_forms_use_case(house: House) -> None:
    off = await tool(house, "draft_catalog_edit", edit("Taqueria", catalog="off"))
    view = await card(house, draft_id(off))
    assert view.title == "Take Taqueria off the catalog"
    assert view.confirm_lines[0] == "It takes Taqueria off the catalog: it's hidden, and calls stop proposing it."
    assert (await item(house, "Taqueria")).active
    done = await confirm(house, "Sam", view.id)
    assert done.text == "Confirmed. Taqueria is off the catalog."
    assert not (await item(house, "Taqueria")).active
    assert [a.action for a in await house.audits("catalog.")] == ["catalog.item_deactivated"]
    back = await tool(house, "draft_catalog_edit", edit("Taqueria", catalog="back"))
    assert (await confirm(house, "Sam", draft_id(back))).text == "Confirmed. Taqueria is back on the catalog."
    assert (await item(house, "Taqueria")).active


# Stale drafts ------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("change", ["notes", "name", "deactivated", "tag merged away"])
async def test_a_diff_is_refused_once_the_item_changed_since_the_draft(house: House, change: str) -> None:
    """Sam drafts delivery only; meanwhile Kim changes Siam Garden on the web. Confirm refuses
    under the write lock, before ``update_item``, so Kim's change stays, and the draft stays
    unused. The same goes for a draft to take it off."""
    if change == "tag merged away":
        thai = await house.tag("Thai food")
        await as_web(house, "Kim", "Siam Garden", tag_ids=frozenset({house.tags["Thai"].id, thai.id}))
    edit_draft = draft_id(await tool(house, "draft_catalog_edit", edit("Siam Garden", modes=["delivery"])))
    off_draft = draft_id(await tool(house, "draft_catalog_edit", edit("Siam Garden", catalog="off")))
    if change == "notes":
        await as_web(house, "Kim", "Siam Garden", notes="Call ahead")
    elif change == "name":
        await as_web(house, "Kim", "Siam Garden", name="Siam Garden Express")
    elif change == "deactivated":
        async with house.sessionmaker() as db:
            await catalog_service.set_item_active(
                db, house["Kim"], house.items["Siam Garden"].id, False, now=house.clock()
            )
    else:
        async with house.sessionmaker() as db:
            await catalog_service.merge_tags(
                db, house["Alex"], house.tags["Thai food"].id, house.tags["Thai"].id, now=house.clock()
            )
    kims = item_snapshot(await item(house, "Siam Garden"))
    for draft in (edit_draft, off_draft):
        with pytest.raises(EventRuleViolation) as refused:
            await confirm(house, "Sam", draft)
        assert str(refused.value) == catalog_edits.ITEM_CHANGED
        assert (await row(house, draft)).used_at is None
    assert item_snapshot(await item(house, "Siam Garden")) == kims


# The diff card -------------------------------------------------------------------------------------

BROAD = {
    "Siam Garden": edit(
        "Siam Garden",
        new_name="Siam Garden Two",
        modes=["delivery"],
        meals=["dinner"],
        add_tags=["Vegetarian"],
        url="https://siam2.example/menu",
        order_url="https://order.siam2.example",
        notes="Ask for mild",
    ),
    "Sam's Tacos": edit(
        "Sam's Tacos", meals=["breakfast", "dinner"], add_cooks=["Member 2"], servings=6, notes="Corn tortillas"
    ),
    "Taqueria": edit("Taqueria", clear=["url", "notes"], add_tags=["Vegetarian"]),
}


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("name", list(BROAD))
async def test_the_diff_card_shows_exactly_what_confirm_changes(house: House, name: str) -> None:
    """The card has one line for each field Confirm changes and none for any it keeps, from the
    value it has to the one it gets; the model reads the same lines, with people as labels and
    the old link and notes not shown."""
    await as_web(house, "Sam", "Taqueria", notes=SECRET_NOTES, url="https://taq.example")
    before = item_snapshot(await item(house, name))
    result = await tool(house, "draft_catalog_edit", BROAD[name], labels=sams(house, "Kim"))
    view = await card(house, draft_id(result))
    await confirm(house, "Sam", view.id)
    after = item_snapshot(await item(house, name))
    changed = {EDIT_FIELDS[k] for k in EDIT_FIELDS if before[k] != after[k]}
    shown = {line.split(":", 1)[0] for line in view.confirm_lines if line != UNCHANGED}
    assert shown == changed and view.confirm_lines[-1] == UNCHANGED
    assert SECRET_NOTES not in result.content and "taq.example" not in result.content
    assert_no_names(result.content)
    if name == "Taqueria":
        assert f"Notes: none (was “{SECRET_NOTES}”)." in view.confirm_lines  # Sam sees what he removes
        assert f"Notes: none (was {NOT_SHOWN})." in result.content  # the model doesn't
    if name == "Sam's Tacos":
        assert "Cooks: adds Kim." in view.confirm_lines and "Cooks: adds Member 2." in result.content
        assert "Servings: 6 (was none)." in view.confirm_lines
    if name == "Siam Garden":
        assert "Link: “https://siam2.example/menu” (was “https://siam.example”)." in view.confirm_lines
        assert after["name"] == "Siam Garden Two"


# The meal log -------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_logging_a_meal_drafts_then_confirm_logs_it_as_the_member(house: House) -> None:
    labels = sams(house, "Kim")
    args = meal(0, day="yesterday", slot="dinner", name="Taqueria", add_people=["Member 1", "Member 2"])
    result = await tool(house, "draft_meal", args, labels=labels)
    assert_no_names(result.content)
    view = await card(house, draft_id(result))
    assert view.title == "Log Taqueria for dinner, yesterday"
    assert view.confirm_lines[:2] == ("It logs Taqueria for dinner yesterday in History.", "Who ate: Sam and Kim.")
    assert "Who ate: Member 1 and Member 2." in result.content
    async with house.sessionmaker() as db:
        assert (await meals.history(db, house["Sam"])).weeks == []
    done = await confirm(house, "Sam", view.id)
    assert done == ActionConfirmed(
        None, "Confirmed. Taqueria is logged for dinner yesterday.", link=("/history", "Open History")
    )
    async with house.sessionmaker() as db:
        [(_, [logged])] = (await meals.history(db, house["Sam"])).weeks
    assert (logged.day, logged.slot, logged.logged_by) == (DAY - timedelta(days=1), Slot.DINNER, house["Sam"].id)
    assert {p.id for p in logged.participants} == {house["Sam"].id, house["Kim"].id}


@pytest.mark.flag("assistant", on=True)
async def test_a_new_meal_needs_its_day_slot_and_item_and_who_ate_must_be_members(house: House) -> None:
    async with house.sessionmaker() as db:
        robin = await guests.add_guest(db, house["Alex"], GuestInput("Robin"), now=house.clock())
    labels = sams(house).with_member(robin.id)
    for args in (meal(0, slot="lunch", name="Taqueria"), meal(0, day="today", name="Taqueria"), meal(0, delete=True)):
        assert await tool(house, "draft_meal", args) == ToolResult(catalog_edits.NEW_MEAL, is_error=True)
    guest = meal(0, day="today", slot="lunch", name="Taqueria", add_people=["Member 2"])
    assert await tool(house, "draft_meal", guest, labels=labels) == ToolResult(
        catalog_edits.NOT_SOMEONE.format(label="Member 2"), is_error=True
    )
    future = meal(0, day="2026-10-23", slot="lunch", name="Taqueria")
    assert await tool(house, "draft_meal", future) == ToolResult(
        "You can log meals for today or earlier, not the future.", is_error=True
    )
    assert await rows(house) == []


@pytest.mark.flag("assistant", on=True)
async def test_the_member_edits_and_deletes_only_the_meals_history_lets_them(house: House) -> None:
    """Sam's own meal is his to edit; Kim's isn't, and nor is it Alex's, an admin, through the
    assistant, though History lets an admin change any meal. Each gets History's refusal."""
    sams_meal = await log(house, "Sam", "Taqueria", "Sam", "Kim")
    kims_meal = await log(house, "Kim", "Siam Garden", "Kim", "Sam")
    for who, number in (("Sam", kims_meal.id), ("Alex", sams_meal.id), ("Alex", kims_meal.id)):
        for args in (meal(number, slot="dinner"), meal(number, delete=True)):
            assert await tool(house, "draft_meal", args, who=who) == ToolResult(meals.ONLY_YOURS, is_error=True)
    async with house.sessionmaker() as db:  # the web's admin can, so the assistant's refusal is the role's
        await meals.update_meal(
            db, house["Alex"], kims_meal.id, replace_slot(kims_meal, Slot.DINNER), today=DAY, now=house.clock()
        )
    assert await rows(house) == []

    looked = json.loads((await tool(house, "look_up_my_meals", {"name": ""}, labels=sams(house, "Kim"))).content)
    assert looked == {
        "today": DAY.isoformat(),
        "meals": [
            {
                "meal": sams_meal.id,
                "day": DAY.isoformat(),
                "slot": "lunch",
                "name": "Taqueria",
                "from_a_call": False,
                "people": ["Member 2", "Member 1"],
            }
        ],
        "more": False,
    }
    edited = await tool(
        house,
        "draft_meal",
        meal(sams_meal.id, name="Siam Garden", remove_people=["Member 2"]),
        labels=sams(house, "Kim"),
    )
    view = await card(house, draft_id(edited))
    assert view.confirm_lines == (
        "Item: Taqueria → Siam Garden.",
        "Who ate: takes off Kim.",
        "Its feedback is deleted, since it was about another item.",
        "It takes a new snapshot of the item's cuisines and the people who ate, as History's Save does.",
    )
    assert_no_names(edited.content)
    done = await confirm(house, "Sam", view.id)
    assert done.text == "Confirmed. The meal is updated in History."
    now = await meal_now(house, sams_meal.id)
    assert now is not None and now.item_id == house.items["Siam Garden"].id
    assert {p.id for p in now.participants} == {house["Sam"].id}
    deleted = await tool(house, "draft_meal", meal(sams_meal.id, delete=True))
    assert (await confirm(house, "Sam", draft_id(deleted))).text == "Confirmed. The meal is deleted from History."
    assert await meal_now(house, sams_meal.id) is None


def replace_slot(logged: MealLog, slot: Slot) -> MealInput:
    return MealInput(logged.day, slot, logged.item_id, frozenset(p.id for p in logged.participants))


@pytest.mark.flag("assistant", on=True)
async def test_a_meal_changed_on_history_since_the_draft_is_refused(house: House) -> None:
    """Sam drafts an edit and a delete; meanwhile he changes the meal on History. Confirm checks
    the meal's ``form_stamp`` under the write lock, as History's own forms do, so neither puts the
    old meal back or deletes the new one, and both drafts stay unused. Feedback on it stays."""
    logged = await log(house, "Sam", "Taqueria", "Sam", "Kim")
    async with house.sessionmaker() as db:
        await feedback.give(db, house["Kim"], logged.id, FeedbackInput(rating=Rating.LIKED), now=house.clock())
    edit_draft = draft_id(await tool(house, "draft_meal", meal(logged.id, slot="dinner")))
    delete_draft = draft_id(await tool(house, "draft_meal", meal(logged.id, delete=True)))
    async with house.sessionmaker() as db:
        await meals.update_meal(
            db, house["Sam"], logged.id, replace_slot(logged, Slot.BREAKFAST), today=DAY, now=house.clock()
        )
    for draft in (edit_draft, delete_draft):
        with pytest.raises(EventRuleViolation) as refused:
            await confirm(house, "Sam", draft)
        assert str(refused.value) == catalog_edits.MEAL_CHANGED
        assert (await row(house, draft)).used_at is None
    now = await meal_now(house, logged.id)
    assert now is not None and now.slot is Slot.BREAKFAST
    async with house.sessionmaker() as db:
        assert (await feedback.form(db, house["Kim"], logged.id, now=house.clock())).mine is not None


@pytest.mark.flag("assistant", on=True)
async def test_a_meal_from_a_call_keeps_its_place(house: House) -> None:
    """A meal only an admin could change (a call's, logged by nobody) is refused like anyone's."""
    logged = await log(house, "Sam", "Taqueria")
    async with house.sessionmaker() as db:
        from dinnerbell.db.models import MealLogRow

        found = await db.get(MealLogRow, logged.id)
        assert found is not None
        found.logged_by = None
        await db.commit()
    result = await tool(house, "draft_meal", meal(logged.id, name="Siam Garden"))
    assert result == ToolResult(meals.ONLY_YOURS, is_error=True)


# Where the drafts go ------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_the_tools_work_in_a_channel_thread_and_their_drafts_are_the_askers(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """Catalog edits and the meal log aren't private-only: a channel thread offers them, their
    drafts are the asker's alone, and nobody else, an admin included, can confirm them."""
    thread = slack_key("C0HOUSE", "1790000000.000200")
    offered = {t.name for t in tools.tools_for(False)}
    assert {"draft_catalog_edit", "look_up_my_meals", "draft_meal"} <= offered
    model.script = [
        tool_call(
            "draft_catalog_edit",
            edit("Siam Garden", modes=["delivery"]),
            toolu_2=("draft_meal", meal(0, day="today", slot="lunch", name="Taqueria")),
        ),
        answer("Two drafts."),
    ]
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, house["Kim"], thread, private=False, text="delivery only, and log lunch")
    assert reply is not None and [d.kind for d in reply.drafts] == ["item_edit", "meal_log"]
    for draft in reply.drafts:
        for other in ("Sam", "Alex"):
            with pytest.raises(Exception, match="That draft is gone"):
                await confirm(house, other, draft.id)
    assert {r.member_id for r in await rows(house)} == {house["Kim"].id}


@pytest.mark.flag("assistant", on=True)
async def test_no_result_or_summary_names_anyone(house: House, helper: Assistant, model: FakeModel) -> None:
    """Every tool here, in a conversation where Sam mentioned Kim: what the model reads names
    people only by label, and never shows an item's notes or link."""
    await as_web(house, "Sam", "Taqueria", notes=SECRET_NOTES)
    logged = await log(house, "Sam", "Taqueria", "Sam", "Kim", "Lee")
    sam = house["Sam"]

    def words(label: Any) -> str:
        return f"{label(house['Kim'].id)} cooks the tacos too; fix my lunch; mild at Taqueria"

    model.script = [
        tool_call(
            "look_up_my_meals",
            {"name": ""},
            toolu_2=("draft_catalog_edit", edit("Sam's Tacos", add_cooks=["Member 2"], remove_cooks=["Member 1"])),
            toolu_3=("draft_meal", meal(logged.id, remove_people=["Member 2"])),
            toolu_4=("draft_catalog_edit", edit("Taqueria", notes="Ask for mild")),
            toolu_5=("draft_meal", meal(0, day="today", slot="dinner", name="Sam's Tacos", add_people=["Member 2"])),
        ),
        answer("Done drafting."),
    ]
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, sam, web_key(sam.id), private=True, text=words)
    assert reply is not None and len(reply.drafts) == 4
    sent = json.dumps([r["messages"] for r in model.requests], ensure_ascii=False)
    assert_no_names(sent)
    assert SECRET_NOTES not in sent and "@example.com" not in sent
    names = {d.title for d in reply.drafts} | {line for d in reply.drafts for line in d.confirm_lines}
    assert any("Kim" in line for line in names)  # only the card names people


# The card tells the truth in Slack too (#254 review) --------------------------------------------

LONG_URL = "https://menu.siam.example/" + "/".join(f"section-{n:03}" for n in range(38))
FAKE_BULLET = "Call ahead\n• Nothing else about it changes."


@pytest.mark.flag("assistant", on=True)
async def test_the_slack_card_shows_each_new_value_whole_and_a_value_cant_fake_a_line(house: House) -> None:
    """Old notes 400 characters long and a new link near the limit: the Slack card, which clips
    other drafts' lines at 300 characters, shows each new value in full, with the old notes cut
    after the new value. A new value's line break can't start a line of its own, on the Slack
    card or the web's."""
    await as_web(house, "Sam", "Siam Garden", notes="Old " * 100)
    assert 400 < len(LONG_URL) <= tools.MAX_EDIT_LINK
    result = await tool(house, "draft_catalog_edit", edit("Siam Garden", url=LONG_URL, notes=FAKE_BULLET))
    view = await card(house, draft_id(result))
    text = "\n".join(b["text"]["text"] for b in draft_card(view)["blocks"] if b["type"] == "section")
    assert safe_reply(LONG_URL) in text
    assert safe_reply("“Call ahead ↵ • Nothing else about it changes.”") in text
    assert [line for line in text.splitlines() if line.startswith("• Nothing else")] == [f"• {UNCHANGED}"]
    assert all("\n" not in line for line in view.confirm_lines)  # the web's card: one item a line
    [notes] = [line for line in view.confirm_lines if line.startswith("Notes:")]
    assert notes.endswith(f"(was “{('Old ' * 100)[:80].rstrip()}…”).")
    too_long = await tool(house, "draft_catalog_edit", edit("Siam Garden", url=LONG_URL + "x" * 500))
    assert too_long.is_error and "at most 500" in too_long.content


@pytest.mark.flag("assistant", on=True)
async def test_a_guess_at_held_notes_drafts_the_same_whether_it_matches_or_not(house: House) -> None:
    """The model never reads an item's notes. Sending notes it guessed drafts either way, with the
    same words, so a refusal can't tell it the guess was right."""
    await as_web(house, "Sam", "Taqueria", notes=SECRET_NOTES)
    right = await tool(house, "draft_catalog_edit", edit("Taqueria", notes=SECRET_NOTES))
    wrong = await tool(house, "draft_catalog_edit", edit("Taqueria", notes="Gate code 1234"))
    assert not right.is_error and not wrong.is_error
    words = [re.sub(r"Draft \d+", "Draft N", r.content).replace("1234", "4321") for r in (right, wrong)]
    assert words[0] == words[1]
    assert f"Notes: “{SECRET_NOTES}” (was {NOT_SHOWN})." in right.content
    cleared = await tool(house, "draft_catalog_edit", edit("Siam Garden", clear=["notes"]))  # it has none
    assert not cleared.is_error


@pytest.mark.flag("assistant", on=True)
async def test_servings_and_clearing_them_at_once_is_refused(house: House) -> None:
    result = await tool(house, "draft_catalog_edit", edit("Sam's Tacos", servings=4, clear=["servings"]))
    assert result == ToolResult(catalog_edits.BOTH.format(field="servings"), is_error=True)


@pytest.mark.flag("assistant", on=True)
async def test_a_name_carrying_marker_characters_stays_text(house: House) -> None:
    """An item named with the held marker's characters, as a planted import could name one, can't
    make the card or the summary write out its notes."""
    planted = await house.add_item("Trojan \ue002notes\ue003 Tacos", Mode.PICKUP, cuisine="Mexican")
    async with house.sessionmaker() as db:
        await CatalogRepository(db).update_item(
            house.household.id,
            planted.id,
            ItemValues(
                kind=planted.kind,
                name=planted.name,
                modes=planted.modes,
                meals=planted.meals,
                tag_ids=planted.tag_ids,
                cook_ids=planted.cook_ids,
                url=None,
                order_url=None,
                notes=SECRET_NOTES,
                default_servings=None,
                active=True,
            ),
            house.clock(),
        )
        await db.commit()
    result = await tool(house, "draft_catalog_edit", edit(planted.name, new_name="Trojan Tacos"))
    view = await card(house, draft_id(result))
    shown = " ".join([view.title, *view.confirm_lines])
    assert view.title == "Edit Trojan notes Tacos" and "(was “Trojan notes Tacos”)" in shown
    assert SECRET_NOTES not in shown and SECRET_NOTES not in result.content


# Meals: single use, guests, calls, and the draft-time guards (#254 review) -----------------------


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("delete", [False, True])
async def test_a_meal_draft_confirms_once_even_when_the_meal_is_as_drafted_again(house: House, delete: bool) -> None:
    """Confirm uses the draft under History's own lock, so a second Confirm hears it's used, even
    after the meal is put back as it was drafted, and a Cancel after it does too."""
    logged = await log(house, "Sam", "Taqueria")
    args = meal(logged.id, delete=True) if delete else meal(logged.id, slot="dinner")
    draft = draft_id(await tool(house, "draft_meal", args))
    await confirm(house, "Sam", draft)
    assert (await row(house, draft)).used_at is not None
    if not delete:
        async with house.sessionmaker() as db:
            await meals.update_meal(
                db, house["Sam"], logged.id, replace_slot(logged, Slot.LUNCH), today=DAY, now=house.clock()
            )
    with pytest.raises(drafts.DraftUnavailable):
        await confirm(house, "Sam", draft)
    with pytest.raises(drafts.DraftUnavailable):
        async with house.sessionmaker() as db:
            await drafts.cancel_draft(db, house.clock, house["Sam"], draft)
    now = await meal_now(house, logged.id)
    assert now is None if delete else (now is not None and now.slot is Slot.LUNCH)


async def set_meal(house: House, meal_id: int, **values: Any) -> None:
    from dinnerbell.db.models import MealLogRow

    async with house.sessionmaker() as db:
        found = await db.get(MealLogRow, meal_id)
        assert found is not None
        for key, value in values.items():
            setattr(found, key, value)
        await db.commit()


@pytest.mark.flag("assistant", on=True)
async def test_a_guest_who_ate_reads_as_a_guest_and_gets_no_label(house: House) -> None:
    async with house.sessionmaker() as db:
        robin = await guests.add_guest(db, house["Alex"], GuestInput("Robin"), now=house.clock())
    logged = await log(house, "Sam", "Taqueria")
    await set_meal(
        house, logged.id, participants=[{"id": robin.id, "name": "Robin"}, {"id": house["Sam"].id, "name": "Sam"}]
    )
    session = ToolSession(key=web_key(house["Sam"].id), pseudonyms=sams(house))
    async with house.sessionmaker() as db:
        result = await tools.run_tool(
            "look_up_my_meals",
            {"name": ""},
            db=db,
            member=house["Sam"],
            private=True,
            now=house.clock(),
            session=session,
        )
    [found] = json.loads(result.content)["meals"]
    assert found["people"] == ["a guest", "Member 1"]
    assert robin.id not in session.pseudonyms.numbers
    assert_no_names(result.content)


async def post_call_and_offer(house: House) -> tuple[int, int]:
    """Today's dinner call, and Kim's offer of Siam Garden for tomorrow's lunch."""
    from tests.support.calls import OPENS, dinner_id

    house.clock.now = OPENS
    await house.tick()
    call_id = await dinner_id(house)
    data = offers.OfferInput(
        slot=Slot.LUNCH,
        day=DAY + timedelta(days=1),
        mode=Mode.PICKUP,
        at=time(12, 0),
        capacity=3,
        item_id=house.items["Siam Garden"].id,
    )
    async with house.sessionmaker() as db:
        posted = await offers.post_offer(db, house["Kim"], data, now=house.clock())
    return call_id, int(posted.event_id)


@pytest.mark.flag("assistant", on=True)
async def test_a_calls_meal_keeps_its_place_and_an_offers_doesnt(house: House) -> None:
    """A meal whose place is its call's (``meals.call_place``) can't change item here, and the
    lookup says so; an offer's meal can, as on History."""
    call_id, offer_id = await post_call_and_offer(house)
    call_meal = await log(house, "Sam", "Taqueria")
    offer_meal = await log(house, "Sam", "Siam Garden", day=DAY - timedelta(days=1))
    await set_meal(house, call_meal.id, event_id=call_id)
    await set_meal(house, offer_meal.id, event_id=offer_id)
    looked = json.loads((await tool(house, "look_up_my_meals", {"name": ""})).content)
    assert {m["meal"]: m["from_a_call"] for m in looked["meals"]} == {call_meal.id: True, offer_meal.id: False}
    fixed = await tool(house, "draft_meal", meal(call_meal.id, name="Siam Garden"))
    assert fixed == ToolResult(CALL_PLACE_FIXED, is_error=True)
    assert not (await tool(house, "draft_meal", meal(call_meal.id, slot="dinner"))).is_error  # its day and meal can
    assert not (await tool(house, "draft_meal", meal(offer_meal.id, name="Taqueria"))).is_error


@pytest.mark.flag("assistant", on=True)
async def test_a_meal_is_logged_only_with_an_active_item_and_a_delete_takes_no_changes(house: House) -> None:
    async with house.sessionmaker() as db:
        await catalog_service.set_item_active(db, house["Kim"], house.items["Taqueria"].id, False, now=house.clock())
    new = meal(0, day="today", slot="lunch", name="Taqueria")
    assert await tool(house, "draft_meal", new) == ToolResult(catalog_edits.NO_ACTIVE_ITEM, is_error=True)
    logged = await log(house, "Sam", "Siam Garden")
    assert await tool(house, "draft_meal", meal(logged.id, name="Taqueria")) == ToolResult(
        catalog_edits.NO_ACTIVE_ITEM, is_error=True
    )
    both = meal(logged.id, delete=True, slot="dinner")
    assert await tool(house, "draft_meal", both) == ToolResult(catalog_edits.DELETE_ALONE, is_error=True)
    assert await rows(house) == []
