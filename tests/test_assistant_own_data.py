# SPDX-License-Identifier: AGPL-3.0-or-later
"""The member's own data through the assistant (#253, spec 6.14, **Tools** and **Drafts**): their
levels on Preferences, feedback on meals they ate, and away days, in private conversations only.
Each is a draft, and each **Confirm** runs the use case the Preferences page or the feedback form
runs, as the member, never the admin's path to someone else's (#181).

The household is the usual one (``tests.support.calls.furnish``): Alex (the admin), Sam, Kim,
and Lee. Sam cooks Pasta Bake, a home meal, for a dinner Alex, Sam, and Kim eat; Siam Garden is
a restaurant. The assistant is on.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.models import AssistantDraftRow, AssistantUsageRow
from dinnerbell.domain.assistant import AssistantSettings, Pseudonyms, slack_key, web_key
from dinnerbell.domain.assistant_drafts import OWN_DATA
from dinnerbell.domain.away import check_away
from dinnerbell.domain.catalog import Mode, TagKind
from dinnerbell.domain.events import EventRuleViolation
from dinnerbell.domain.feedback import (
    CLOSED,
    ISSUE_NOTE_TOO_LONG,
    NO_PLACE,
    NOT_A_PARTICIPANT,
    OWN_COOKING,
    FeedbackInput,
    InvalidFeedback,
    PlaceIssue,
    Rating,
)
from dinnerbell.domain.meals import MealInput, Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.preferences import MINUS, Stance, TargetType
from dinnerbell.services import away, coordinator, feedback, meals, preferences
from dinnerbell.services.assistant import admin, call_drafts, drafts, own_drafts, tools
from dinnerbell.services.assistant.call_drafts import ActionConfirmed
from dinnerbell.services.assistant.conversation import (
    AT_CAP,
    CAP_BELOW_ONE_CALL,
    Assistant,
    converse,
    least_private_call_micros,
)
from dinnerbell.services.assistant.drafts import USED, DraftUnavailable
from dinnerbell.services.assistant.prompt import SYSTEM_PROMPT, system_prompt
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.assistant.tools import ToolContext, ToolResult, ToolSession
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from tests.support.calls import DONE_AT, House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, tool_call
from tests.support.feedback import done_dinner

pytestmark = pytest.mark.anyio

CONFIGURED = AssistantConfig(key_set=True, flag_on=True)
NEXT_DAY = DONE_AT + timedelta(hours=16)
"""Noon the day after the dinner, in the household's time zone: the dinner was last night."""
TODAY = date(2026, 10, 23)
OWN_TOOLS = {"look_up_my_preferences", "draft_preference", "look_up_my_feedback", "draft_feedback", "draft_away"}
PRIVACY = (
    "Only you see your rating and what went wrong. The household sees a summary of everyone's, never who said what."
)
"""The feedback form's own privacy line, on every feedback card."""
NO_FEEDBACK = {"rating": "", "went_wrong": [], "what_went_wrong": "", "thank_cook": False, "cook_note": ""}
SAMPLES: dict[str, dict[str, Any]] = {
    "look_up_my_preferences": {"name": ""},
    "draft_preference": {"what": "", "name": "Siam Garden", "level": "veto"},
    "look_up_my_feedback": {"name": ""},
    "draft_feedback": NO_FEEDBACK | {"meal": 1, "rating": "liked"},
    "draft_away": {"first": "tomorrow", "last": "2026-10-26", "clear": False},
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
    await house.add_item("Pasta Bake", Mode.COOK, cuisine="Italian", cooks=("Sam",))
    await house.tag("Mushrooms", TagKind.OTHER)
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


async def tool(house: House, name: str, args: dict[str, Any], *, who: str = "Kim", private: bool = True) -> ToolResult:
    member = house[who]
    session = ToolSession(key=web_key(member.id), pseudonyms=Pseudonyms().with_member(member.id))
    async with house.sessionmaker() as db:
        return await tools.run_tool(
            name, args, db=db, member=member, private=private, now=house.clock(), session=session
        )


def draft_id(result: ToolResult) -> int:
    assert not result.is_error, result.content
    return int(result.content.split()[1])


async def draft(house: House, draft_id: int) -> AssistantDraftRow:
    async with house.sessionmaker() as db:
        row = await db.get(AssistantDraftRow, draft_id)
    assert row is not None
    return row


async def confirm(house: House, who: str | Member, draft_id: int) -> Any:
    member = house[who] if isinstance(who, str) else who
    async with house.sessionmaker() as db:
        return await drafts.confirm_draft(db, house.clock, CONFIGURED, member, draft_id)


async def level(house: House, who: str, target_type: TargetType, name: str) -> Stance:
    target = house.tags[name].id if target_type is TargetType.TAG else house.items[name].id
    async with house.sessionmaker() as db:
        found = await preferences.my_stance(db, house[who], target_type, target)
    assert found is not None
    return found[1]


async def mine(house: House, who: str, meal_id: int) -> FeedbackInput | None:
    async with house.sessionmaker() as db:
        return (await feedback.form(db, house[who], meal_id, now=house.clock())).mine


async def dinner(house: House, item: str = "Siam Garden", mode: Mode = Mode.PICKUP) -> int:
    """Last night's dinner of ``item``, which Alex, Sam, and Kim ate; it's noon the next day."""
    event_id = await done_dinner(house, item, mode)
    house.clock.now = NEXT_DAY
    [meal] = [m for m in await house.meals() if m.event_id == event_id]
    return meal.id


def strings(value: Any) -> Iterator[str]:
    """Every string in ``value``, dict keys included, as written, not escaped into JSON."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


# Done when ---------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_rate_last_nights_dinner_drafts_feedback_and_confirm_saves_it_through_the_service(
    house: House, helper: Assistant, model: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Rate last night's dinner: hated it, it was cold" looks up Kim's meals, drafts the feedback,
    changes nothing, and Confirm saves it through ``feedback.give``, the feedback form's use case."""
    meal_id = await dinner(house)
    kim = house["Kim"]
    hated = NO_FEEDBACK | {"meal": meal_id, "rating": "hated", "went_wrong": ["cold"]}
    model.script = [
        tool_call("look_up_my_feedback", {"name": ""}, call_id="toolu_look"),
        tool_call("draft_feedback", hated, call_id="toolu_draft"),
        answer("Here's your feedback to confirm."),
    ]
    async with house.sessionmaker() as db:
        reply = await converse(
            db, helper, kim, web_key(kim.id), private=True, text="Rate last night's dinner: hated it, it was cold"
        )
    assert reply is not None
    looked = json.loads(model.requests[1]["messages"][-1]["content"][0]["content"])
    assert looked == {
        "today": "2026-10-23",
        "meals": [
            {
                "meal": meal_id,
                "day": "2026-10-22",
                "slot": "dinner",
                "name": "Siam Garden",
                "kind": "restaurant",
                "open_until": "2026-10-29",
                "cook_to_thank": False,
                "yours": None,
            }
        ],
        "more": False,
    }
    [card] = reply.drafts
    assert card.kind == "feedback" and card.title == "Feedback on Siam Garden, dinner yesterday"
    assert card.confirm_lines == (
        "It saves your feedback on Siam Garden (dinner, yesterday): Hated it; went wrong: cold.",
        PRIVACY,
        "Your rating can also move your own score for it.",
    )
    assert await mine(house, "Kim", meal_id) is None  # nothing until Confirm

    gave: list[int] = []
    real_give = feedback.give

    async def spy(db: Any, actor: Member, meal: int, data: FeedbackInput, **kwargs: Any) -> Any:
        gave.append(actor.id)
        return await real_give(db, actor, meal, data, **kwargs)

    monkeypatch.setattr(own_drafts.feedback_service, "give", spy)
    done = await confirm(house, "Kim", card.id)
    assert done == ActionConfirmed(
        None,
        "Confirmed. Thanks for the feedback on Siam Garden! Only you see it.",
        link=(f"/history/meals/{meal_id}/feedback", "Open your feedback"),
    )
    assert gave == [kim.id]
    assert await mine(house, "Kim", meal_id) == FeedbackInput(
        rating=Rating.HATED, place_issues=frozenset({PlaceIssue.COLD})
    )
    with pytest.raises(DraftUnavailable):
        await confirm(house, "Kim", card.id)  # once only


@pytest.mark.flag("assistant", on=True)
async def test_veto_mushrooms_drafts_a_preference_change_and_confirm_saves_it_on_preferences(
    house: House, helper: Assistant, model: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Veto mushrooms" drafts a level for the tag Mushrooms; Confirm runs ``set_my_preference``,
    the Preferences page's use case, never the admin's ``set_member_preference``."""
    kim = house["Kim"]
    model.script = [
        tool_call("draft_preference", {"what": "", "name": "mushrooms", "level": "veto"}),
        answer("Drafted."),
    ]
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, kim, web_key(kim.id), private=True, text="Veto mushrooms")
    assert reply is not None
    [card] = reply.drafts
    assert (card.kind, card.title) == ("preference", "Veto Mushrooms")
    assert card.confirm_lines == (
        "Your level for the tag Mushrooms on Preferences goes from 0 to Veto.",
        "Ranking leaves out everything with that tag on every call you're in.",
    )
    assert await level(house, "Kim", TargetType.TAG, "Mushrooms") == Stance()

    async def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the admin's path to someone else's preferences")

    monkeypatch.setattr(preferences, "set_member_preference", never)
    monkeypatch.setattr(preferences, "member_for_admin", never)
    done = await confirm(house, "Kim", card.id)
    assert done == ActionConfirmed(
        None, "Confirmed. Your level for Mushrooms is Veto now.", link=own_drafts.PREFERENCES
    )
    assert await level(house, "Kim", TargetType.TAG, "Mushrooms") == Stance(veto=True)
    assert await level(house, "Sam", TargetType.TAG, "Mushrooms") == Stance()
    with pytest.raises(DraftUnavailable):
        await confirm(house, "Kim", card.id)  # once only


# Private only ------------------------------------------------------------------------------------


async def test_exactly_the_own_data_tools_are_private_only() -> None:
    """A new tool that reads or changes the member's own data must be private-only, and the
    tests below cover each one, from the registry."""
    assert {t.name for t in tools.TOOLS if t.private_only} == OWN_TOOLS == set(SAMPLES)
    assert {t.name for t in tools.tools_for(False)}.isdisjoint(OWN_TOOLS)
    assert {t.name for t in tools.tools_for(True)} >= OWN_TOOLS


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("name", sorted(OWN_TOOLS))
async def test_each_own_data_tool_refuses_in_a_channel_thread(house: House, name: str) -> None:
    """In a channel thread the tool isn't there at all, so its own check never runs; called some
    other way, the tool refuses by itself too."""
    await dinner(house)
    result = await tool(house, name, SAMPLES[name], private=False)
    assert result == ToolResult(f"There's no tool named {name!r} here.", is_error=True)
    run = next(t for t in tools.TOOLS if t.name == name).run
    kim = house["Kim"]
    async with house.sessionmaker() as db:
        ctx = ToolContext(db, tools.tool_actor(kim), False, house.clock(), ToolSession(key="slack:C0:1.2"))
        with pytest.raises(tools.ToolRefusal, match="only in a private conversation"):
            await run(ctx, SAMPLES[name])
    assert await tool(house, name, SAMPLES[name]) != result  # in private, it runs


@pytest.mark.flag("assistant", on=True)
async def test_a_channel_thread_never_offers_them_and_drafts_nothing(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """The model asks for every own-data tool in a channel thread anyway: each is refused, and
    nothing is drafted or changed."""
    await dinner(house)
    kim = house["Kim"]
    model.script = [
        tool_call(
            "draft_preference",
            SAMPLES["draft_preference"],
            call_id="toolu_0",
            **{f"toolu_{n}": (name, SAMPLES[name]) for n, name in enumerate(sorted(OWN_TOOLS), start=1)},
        ),
        answer("Ask me in a DM."),
    ]
    thread = slack_key("C0HOUSE", "1790000000.000100")
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, kim, thread, private=False, text="Veto Siam Garden")
    assert reply is not None and reply.drafts == ()
    offered = {t["name"] for t in model.requests[0]["tools"]}
    assert offered.isdisjoint(OWN_TOOLS)
    for block in model.requests[1]["messages"][-1]["content"]:
        assert block["is_error"] and "There's no tool named" in block["content"]
    async with house.sessionmaker() as db:
        assert await db.get(AssistantDraftRow, 1) is None
    assert await level(house, "Kim", TargetType.ITEM, "Siam Garden") == Stance()


async def test_the_prompt_says_own_data_is_private_and_stays_the_same_for_everyone() -> None:
    """The prompt names the own-data tools' rules, carries no household data, and is the same
    text for every request, so the prompt cache holds."""
    assert "only when they ask about them, and only theirs" in SYSTEM_PROMPT
    assert "In a shared thread, tell the member to ask there instead." in SYSTEM_PROMPT
    assert (
        "even \\\nwhen you know" not in SYSTEM_PROMPT and "even when you know the member's own vetoes" in SYSTEM_PROMPT
    )
    assert system_prompt(keeps_history=True) == SYSTEM_PROMPT
    assert system_prompt(keeps_history=False).startswith(SYSTEM_PROMPT)


# Nobody else's data ------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_results_never_carry_another_members_data(house: House) -> None:
    """Sam vetoes Taqueria, loves Siam Garden, and rates both of last night's meals with notes;
    Lee sets away days. Kim's own lookups and drafts say nothing of any of it: no name but the
    items', no other level, rating, note, or hint, checked on each string as the model reads it."""
    await act(
        house,
        preferences.set_my_preference,
        house["Sam"],
        TargetType.ITEM,
        house.items["Taqueria"].id,
        Stance(veto=True),
    )
    await act(
        house,
        preferences.set_my_preference,
        house["Sam"],
        TargetType.ITEM,
        house.items["Siam Garden"].id,
        Stance(score=2),
    )
    await act(
        house, preferences.set_my_preference, house["Sam"], TargetType.TAG, house.tags["Mushrooms"].id, Stance(score=-2)
    )
    home = await dinner(house, "Pasta Bake", Mode.COOK)
    sams = FeedbackInput(rating=Rating.LOVED)
    await act(house, feedback.give, house["Sam"], home, sams)
    alexs = FeedbackInput(rating=Rating.DISLIKED, cook_thanks=True, cook_note="AlexNoteSecret")
    await act(house, feedback.give, house["Alex"], home, alexs)
    await act(house, away.set_my_away, house["Lee"], date(2026, 10, 25), date(2026, 10, 27))
    async with house.sessionmaker() as db:
        hints = await feedback.item_hints(db, house["Kim"], today=TODAY)
    said = [
        await tool(house, "look_up_my_preferences", {"name": ""}),
        await tool(house, "look_up_my_preferences", {"name": "e"}),
        await tool(house, "look_up_my_feedback", {"name": ""}),
        await tool(house, "draft_feedback", NO_FEEDBACK | {"meal": home, "rating": "fine", "thank_cook": True}),
        await tool(house, "draft_preference", {"what": "", "name": "Taqueria", "level": "1"}),
        await tool(house, "draft_away", {"first": "2026-10-24", "last": "2026-10-24", "clear": False}),
    ]
    assert not any(r.is_error for r in said), said
    listed = json.loads(said[1].content)
    assert {i["name"]: i["level"] for i in listed["items"]} == {"Pasta Bake": "0", "Siam Garden": "0", "Taqueria": "0"}
    assert listed["away"] is None and json.loads(said[0].content)["items"] == []
    [meal] = json.loads(said[2].content)["meals"]
    assert meal["cook_to_thank"] is True and meal["yours"] is None
    values = [
        v
        for r in said
        for v in (*strings(json.loads(r.content) if r.content.startswith("{") else r.content), r.content)
    ]
    for value in values:
        for name in ("Alex", "Sam", "Lee", "Kim"):
            assert not re.search(rf"\b{name}\b", value), (name, value)
        for leak in (
            "veto",
            "loved",
            "Loved",
            "disliked",
            "AlexNoteSecret",
            "2026-10-25",
            "2026-10-27",
            "Member 2",
            *hints.values(),
        ):
            assert leak not in value, (leak, value)
    async with house.sessionmaker() as db:
        rows = [await db.get(AssistantDraftRow, n) for n in (1, 2, 3)]
    for row in rows:
        assert row is not None and row.member_id == house["Kim"].id
        card = " ".join([row.title, *row.lines])
        assert "Sam" not in card and "Alex" not in card and "AlexNoteSecret" not in card


@pytest.mark.flag("assistant", on=True)
async def test_a_level_read_back_is_the_members_own_and_says_what_their_ratings_did(house: House) -> None:
    """Kim's lookup gives her own level and what her own ratings did, from the code Preferences
    uses, never the household's hint."""
    meal_id = await dinner(house)
    await act(house, feedback.give, house["Kim"], meal_id, FeedbackInput(rating=Rating.LOVED))
    async with house.sessionmaker() as db:
        own = await preferences.my_preferences(db, house["Kim"], today=TODAY)
    [item] = json.loads((await tool(house, "look_up_my_preferences", {"name": ""})).content)["items"]
    assert item == {
        "name": "Siam Garden",
        "kind": "restaurant",
        "in_catalog": True,
        "level": "0",
        "from_your_ratings": own.rating_notes[house.items["Siam Garden"].id],
    }


# Refusals, in the services' words ----------------------------------------------------------------


async def service_refusal(use_case: Any, *args: Any, **kwargs: Any) -> str:
    """What a use case refuses with, in its own words."""
    try:
        await use_case(*args, **kwargs)
    except Exception as exc:
        return str(exc)
    raise AssertionError("the service didn't refuse")


@pytest.mark.flag("assistant", on=True)
async def test_feedback_refusals_are_the_feedback_forms(house: House) -> None:
    """Each refusal is what ``feedback.give`` says for the same answers on the form."""
    home = await dinner(house, "Pasta Bake", Mode.COOK)
    cases = [
        ("Lee", NO_FEEDBACK | {"meal": home, "rating": "fine"}, NOT_A_PARTICIPANT),
        ("Kim", NO_FEEDBACK | {"meal": home, "went_wrong": ["late"]}, NO_PLACE),
        ("Sam", NO_FEEDBACK | {"meal": home, "thank_cook": True}, OWN_COOKING),
        ("Kim", NO_FEEDBACK | {"meal": home, "thank_cook": True, "cook_note": "x" * 201}, "Keep the note to 200"),
        ("Kim", NO_FEEDBACK | {"meal": 999, "rating": "fine"}, "No such meal."),
    ]
    for who, args, words in cases:
        result = await tool(house, "draft_feedback", args, who=who)
        assert result.is_error and words in result.content, (who, args, result)
    answers = FeedbackInput(place_issues=frozenset({PlaceIssue.LATE}))
    async with house.sessionmaker() as db:
        assert await service_refusal(feedback.give, db, house["Kim"], home, answers, now=house.clock()) == NO_PLACE
    restaurant = await tool(house, "draft_feedback", NO_FEEDBACK | {"meal": home, "what_went_wrong": "y" * 201})
    assert restaurant.is_error  # a home meal takes no issue note
    house.clock.advance(timedelta(days=7))
    closed = await tool(house, "draft_feedback", NO_FEEDBACK | {"meal": home, "rating": "fine"})
    assert closed == ToolResult(CLOSED, is_error=True)
    assert await tool(house, "look_up_my_feedback", {"name": ""}) == ToolResult(
        json.dumps({"today": "2026-10-30", "meals": [], "more": False}), False
    )


@pytest.mark.flag("assistant", on=True)
async def test_an_issue_note_too_long_once_trimmed_gets_the_services_words(house: House) -> None:
    meal_id = await dinner(house)
    long = NO_FEEDBACK | {"meal": meal_id, "what_went_wrong": "z" * 201}
    assert await tool(house, "draft_feedback", long) == ToolResult(ISSUE_NOTE_TOO_LONG, is_error=True)
    padded = NO_FEEDBACK | {"meal": meal_id, "what_went_wrong": "  " + "z" * 200 + "  "}
    assert not (await tool(house, "draft_feedback", padded)).is_error  # trimmed, as the form does


@pytest.mark.flag("assistant", on=True)
async def test_away_refusals_are_the_preferences_pages(house: House) -> None:
    """Each refusal is what ``away.set_my_away`` says for the same days on Preferences."""
    for first, last in (("2026-10-25", "2026-10-24"), ("2026-10-01", "2026-10-02"), ("2026-10-22", "2027-10-22")):
        result = await tool(house, "draft_away", {"first": first, "last": last, "clear": False})
        async with house.sessionmaker() as db:
            words = await service_refusal(
                away.set_my_away,
                db,
                house["Kim"],
                date.fromisoformat(first),
                date.fromisoformat(last),
                now=house.clock(),
            )
        assert result == ToolResult(words, is_error=True)
    assert await tool(house, "draft_away", {"first": "", "last": "", "clear": True}) == ToolResult(
        own_drafts.NO_AWAY, is_error=True
    )
    both = await tool(house, "draft_away", {"first": "today", "last": "", "clear": True})
    assert both.is_error and "empty strings" in both.content


@pytest.mark.flag("assistant", on=True)
async def test_a_level_nothing_has_or_two_things_have_is_refused(house: House) -> None:
    await house.tag("Siam Garden", TagKind.OTHER)  # a tag named as the restaurant is
    assert await tool(house, "draft_preference", {"what": "", "name": "Nowhere", "level": "1"}) == ToolResult(
        own_drafts.NO_SUCH_TARGET, is_error=True
    )
    assert await tool(house, "draft_preference", {"what": "", "name": "Siam Garden", "level": "1"}) == ToolResult(
        own_drafts.TWO_TARGETS, is_error=True
    )
    assert not (
        await tool(house, "draft_preference", {"what": "restaurant", "name": "Siam Garden", "level": "1"})
    ).is_error
    assert await tool(house, "draft_preference", {"what": "home", "name": "Siam Garden", "level": "1"}) == ToolResult(
        own_drafts.NO_SUCH_TARGET, is_error=True
    )
    assert await tool(house, "draft_preference", {"what": "tag", "name": "Mushrooms", "level": "0"}) == ToolResult(
        call_drafts.NOTHING_TO_DO, is_error=True
    )


# What Confirm does -------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_feedback_that_replaces_or_takes_back_says_so(house: House) -> None:
    home = await dinner(house, "Pasta Bake", Mode.COOK)
    await act(house, feedback.give, house["Kim"], home, FeedbackInput(rating=Rating.LIKED))
    keep = NO_FEEDBACK | {"meal": home, "rating": "liked"}
    assert await tool(house, "draft_feedback", keep) == ToolResult(call_drafts.NOTHING_TO_DO, is_error=True)
    thanks = await tool(house, "draft_feedback", keep | {"thank_cook": True, "cook_note": "So good"})
    row = await draft(house, draft_id(thanks))
    assert row.lines == [
        'It saves your feedback on Pasta Bake (dinner, yesterday): Liked it; thanks to the cook ("So good").',
        "It replaces what you said before: Liked it.",
        "The cook sees your thank-you and your note.",
        PRIVACY,
        "Your rating can also move your own score for it.",
    ]
    back = await tool(house, "draft_feedback", NO_FEEDBACK | {"meal": home})
    row = await draft(house, draft_id(back))
    assert row.title == "Take back your feedback on Pasta Bake"
    assert row.lines == ["It takes back your feedback on Pasta Bake (dinner, yesterday): Liked it."]
    done = await confirm(house, "Kim", row.id)
    assert isinstance(done, ActionConfirmed) and done.text == "Confirmed. Your feedback on Pasta Bake is taken back."
    assert await mine(house, "Kim", home) is None


@pytest.mark.flag("assistant", on=True)
async def test_away_days_draft_set_replace_and_clear(house: House) -> None:
    house.clock.now = NEXT_DAY
    set_days = await tool(house, "draft_away", {"first": "tomorrow", "last": "2026-10-26", "clear": False})
    row = await draft(house, draft_id(set_days))
    assert (row.title, row.lines) == (
        "Away from tomorrow through Mon, Oct 26",
        [
            "You're away from tomorrow through Mon, Oct 26, and back Tue, Oct 27.",
            "Calls on those days answer you Out, marked as away, unless you've answered them. You can still answer In.",
        ],
    )
    done = await confirm(house, "Kim", row.id)
    assert done == ActionConfirmed(
        None,
        "Confirmed. You're away from tomorrow through Mon, Oct 26, and back Tue, Oct 27.",
        link=own_drafts.AWAY_PAGE,
    )
    assert (await house.refresh("Kim")).away == check_away(date(2026, 10, 24), date(2026, 10, 26), TODAY)
    assert [a.action for a in await house.audits("member.")] == ["member.away_set"]  # as Preferences audits it
    with pytest.raises(DraftUnavailable):
        await confirm(house, "Kim", row.id)  # once only
    same = await tool(house, "draft_away", {"first": "2026-10-24", "last": "2026-10-26", "clear": False})
    assert same == ToolResult(call_drafts.NOTHING_TO_DO, is_error=True)
    moved = await draft(
        house, draft_id(await tool(house, "draft_away", {"first": "today", "last": "today", "clear": False}))
    )
    assert moved.lines[-1] == "It replaces your away days from tomorrow through Mon, Oct 26."
    clear = await draft(house, draft_id(await tool(house, "draft_away", {"first": "", "last": "", "clear": True})))
    assert (clear.title, clear.lines) == (
        "Clear your away days",
        ["Your away days, from tomorrow through Mon, Oct 26, are cleared."],
    )
    done = await confirm(house, "Kim", clear.id)
    assert isinstance(done, ActionConfirmed) and done.text == "Confirmed. Your away days are cleared."
    assert (await house.refresh("Kim")).away_now(TODAY) is None
    with pytest.raises(EventRuleViolation, match="Your away days changed since the draft"):
        await confirm(house, "Kim", moved.id)  # drafted to replace the window that's gone now


@pytest.mark.flag("assistant", on=True)
async def test_a_level_on_an_item_her_ratings_move_says_the_level_wins(house: House) -> None:
    meal_id = await dinner(house)
    await act(house, feedback.give, house["Kim"], meal_id, FeedbackInput(rating=Rating.LOVED))
    row = await draft(
        house, draft_id(await tool(house, "draft_preference", {"what": "", "name": "Siam Garden", "level": "0"}))
    )
    assert row.title == "Set Siam Garden back to 0"
    assert row.lines == [
        "Your level for the restaurant Siam Garden on Preferences goes from 0 to 0.",
        "Your earlier ratings of it stop moving your score: the level you set wins.",
    ]
    vetoed = await draft(
        house, draft_id(await tool(house, "draft_preference", {"what": "", "name": "Siam Garden", "level": "veto"}))
    )
    assert vetoed.lines == [
        "Your level for the restaurant Siam Garden on Preferences goes from 0 to Veto.",
        "Ranking leaves it out of every call you're in.",
    ]
    await confirm(house, "Kim", vetoed.id)
    lifted = await draft(
        house, draft_id(await tool(house, "draft_preference", {"what": "", "name": "Siam Garden", "level": "-1"}))
    )
    assert lifted.lines == [
        "Your level for the restaurant Siam Garden on Preferences goes from Veto to " + MINUS + "1.",
        "It's no longer vetoed, so calls you're in can propose it again.",
    ]


# Stale drafts ------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_feedback_given_since_the_draft_is_never_overwritten(house: House) -> None:
    """Kim drafts feedback, then gives some on the web; Confirm refuses, keeps the web's, and
    leaves the draft unused. The same once she's changed feedback she'd given before."""
    meal_id = await dinner(house)
    first = draft_id(await tool(house, "draft_feedback", NO_FEEDBACK | {"meal": meal_id, "rating": "hated"}))
    web = FeedbackInput(rating=Rating.LOVED)
    await act(house, feedback.give, house["Kim"], meal_id, web)
    with pytest.raises(EventRuleViolation) as refused:
        await confirm(house, "Kim", first)
    assert str(refused.value) == own_drafts.FEEDBACK_CHANGED and refused.value.code == "stale_draft"
    assert await mine(house, "Kim", meal_id) == web and (await draft(house, first)).used_at is None

    second = draft_id(await tool(house, "draft_feedback", NO_FEEDBACK | {"meal": meal_id, "rating": "fine"}))
    house.clock.advance(timedelta(minutes=1))
    await act(house, feedback.give, house["Kim"], meal_id, FeedbackInput(rating=Rating.LIKED))
    with pytest.raises(EventRuleViolation, match="changed since the draft"):
        await confirm(house, "Kim", second)
    assert await mine(house, "Kim", meal_id) == FeedbackInput(rating=Rating.LIKED)


@pytest.mark.flag("assistant", on=True)
async def test_feedback_that_closed_since_the_draft_gets_the_forms_words(house: House) -> None:
    """Drafted ten minutes before midnight on its last day (in the household's time zone), and
    confirmed five minutes after: the form's refusal, and nothing saved."""
    meal_id = await dinner(house)
    house.clock.now = datetime(2026, 10, 30, 4, 50, tzinfo=UTC)  # 11:50 PM on Oct 29, in Chicago
    row = draft_id(await tool(house, "draft_feedback", NO_FEEDBACK | {"meal": meal_id, "rating": "hated"}))
    house.clock.advance(timedelta(minutes=15))
    with pytest.raises(EventRuleViolation, match=re.escape(CLOSED)):
        await confirm(house, "Kim", row)
    assert (await draft(house, row)).used_at is None
    async with house.sessionmaker() as db:
        assert await FeedbackRepository(db).get(house.household.id, meal_id, house["Kim"].id) is None


@pytest.mark.flag("assistant", on=True)
async def test_a_level_set_on_preferences_since_the_draft_is_kept(house: House) -> None:
    row = draft_id(await tool(house, "draft_preference", {"what": "", "name": "Siam Garden", "level": "veto"}))
    item = house.items["Siam Garden"].id
    await act(house, preferences.set_my_preference, house["Kim"], TargetType.ITEM, item, Stance(score=1))
    with pytest.raises(EventRuleViolation, match="Your level for Siam Garden changed since the draft: it's \\+1 now"):
        await confirm(house, "Kim", row)
    assert await level(house, "Kim", TargetType.ITEM, "Siam Garden") == Stance(score=1)
    assert (await draft(house, row)).used_at is None


@pytest.mark.flag("assistant", on=True)
async def test_away_days_set_on_preferences_since_the_draft_are_kept(house: House) -> None:
    row = draft_id(await tool(house, "draft_away", {"first": "2026-10-24", "last": "2026-10-25", "clear": False}))
    await act(house, away.set_my_away, house["Kim"], date(2026, 10, 28), date(2026, 10, 29))
    with pytest.raises(EventRuleViolation, match="Your away days changed since the draft"):
        await confirm(house, "Kim", row)
    assert (await house.refresh("Kim")).away == check_away(date(2026, 10, 28), date(2026, 10, 29), TODAY)


@pytest.mark.flag("assistant", on=True)
async def test_only_the_drafts_member_confirms_and_only_while_the_assistant_is_on(house: House) -> None:
    row = draft_id(await tool(house, "draft_preference", {"what": "", "name": "Mushrooms", "level": "veto"}))
    for who in ("Sam", "Alex"):
        with pytest.raises(NotFound):
            await confirm(house, who, row)
    async with house.sessionmaker() as db:
        await admin.update_assistant(db, CONFIGURED, house["Alex"], AssistantSettings(enabled=False), now=house.clock())
    with pytest.raises(DraftUnavailable):
        await confirm(house, "Kim", row)
    assert await level(house, "Kim", TargetType.TAG, "Mushrooms") == Stance()


# Admin stays admin's -----------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_an_admin_gets_a_members_path_and_never_the_admin_edits_member_one(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alex, an admin, runs every own-data tool and confirms each draft. None of it reaches
    ``require_admin``, the ``member_`` use cases Admin > Members calls, or an /admin link."""
    meal_id = await dinner(house)
    reached: list[str] = []
    for module, names in (
        (preferences, ("member_for_admin", "member_preferences", "member_stance", "set_member_preference")),
        (away, ("set_member_away", "clear_member_away", "member_for_admin")),
        (preferences, ("require_admin",)),
    ):
        for name in names:
            real = getattr(module, name)

            def spy(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
                reached.append(_name)
                return _real(*args, **kwargs)

            monkeypatch.setattr(module, name, spy)
    samples = SAMPLES | {"draft_feedback": NO_FEEDBACK | {"meal": meal_id, "rating": "liked"}}
    said = [await tool(house, name, args, who="Alex") for name, args in samples.items()]
    assert not any(r.is_error for r in said), said
    confirmed = []
    async with house.sessionmaker() as db:
        rows = [await db.get(AssistantDraftRow, n) for n in (1, 2, 3)]
    for row in rows:
        assert row is not None and row.kind in OWN_DATA
        confirmed.append(await confirm(house, "Alex", row.id))
    assert reached == []
    shown = " ".join([*(r.content for r in said), *(str(c) for c in confirmed)])
    assert "/admin" not in shown
    assert await level(house, "Alex", TargetType.ITEM, "Siam Garden") == Stance(veto=True)


# The cap -----------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_at_the_cap_an_own_data_message_drafts_and_changes_nothing(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """With the month's cap spent, "Veto Siam Garden and I'm away tomorrow" makes no model call,
    so no tool runs: no draft, no change."""
    async with house.sessionmaker() as db:
        await admin.update_assistant(
            db, CONFIGURED, house["Alex"], AssistantSettings(enabled=True, cap_dollars=1), now=house.clock()
        )
        now = house.clock()
        db.add(
            AssistantUsageRow(
                household_id=house.household.id,
                member_id=None,
                model="earlier",
                month="2026-10",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_micros=1_000_000,
                state="settled",
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()
    model.script = [
        tool_call("draft_preference", SAMPLES["draft_preference"], toolu_2=("draft_away", SAMPLES["draft_away"])),
        answer("Drafted."),
    ]
    kim = house["Kim"]
    async with house.sessionmaker() as db:
        reply = await converse(
            db, helper, kim, web_key(kim.id), private=True, text="Veto Siam Garden, I'm away tomorrow"
        )
    assert reply is not None and reply.text == AT_CAP and reply.drafts == ()
    assert model.requests == []
    async with house.sessionmaker() as db:
        assert await db.get(AssistantDraftRow, 1) is None
    assert await level(house, "Kim", TargetType.ITEM, "Siam Garden") == Stance()
    assert (await house.refresh("Kim")).away is None


@pytest.mark.flag("assistant", on=True)
async def test_lookups_read_back_her_own_feedback_and_away_days_and_clearing_under_way_ends_them(
    house: House,
) -> None:
    meal_id = await dinner(house)
    answers = FeedbackInput(rating=Rating.FINE, place_issues=frozenset({PlaceIssue.OTHER}), issue_note="Soggy")
    await act(house, feedback.give, house["Kim"], meal_id, answers)
    await act(house, away.set_my_away, house["Kim"], TODAY, date(2026, 10, 24))
    [meal] = json.loads((await tool(house, "look_up_my_feedback", {"name": "siam"})).content)["meals"]
    assert meal["yours"] == {
        "rating": "fine",
        "went_wrong": ["other"],
        "what_went_wrong": "Soggy",
        "thank_cook": False,
        "cook_note": "",
    }
    found = json.loads((await tool(house, "look_up_my_preferences", {"name": ""})).content)
    assert found["away"] == {"first": "2026-10-23", "last": "2026-10-24", "back": "2026-10-25"}
    clear = await draft(house, draft_id(await tool(house, "draft_away", {"first": "", "last": "", "clear": True})))
    assert clear.lines == [
        "Your away days, from today through tomorrow, end now.",
        "Calls still taking answers ask you again.",
    ]


@pytest.mark.flag("assistant", on=True)
async def test_a_cap_below_one_private_call_says_so_truthfully_and_spends_nothing(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """With Opus, one private message's worst case is more than a $1 cap. That isn't the month's
    cap reached: nothing was spent, and next month won't help, so the member hears to ask an
    admin to raise it, and nothing is reserved or sent."""
    opus = replace(helper, config=replace(helper.config, model="claude-opus-5-5"))
    assert least_private_call_micros("claude-opus-5-5") > 1_000_000
    async with house.sessionmaker() as db:
        await admin.update_assistant(
            db, CONFIGURED, house["Alex"], AssistantSettings(enabled=True, cap_dollars=1), now=house.clock()
        )
    kim = house["Kim"]
    async with house.sessionmaker() as db:
        reply = await converse(db, opus, kim, web_key(kim.id), private=True, text="Veto mushrooms")
        usage = list(await db.scalars(select(AssistantUsageRow)))
    assert reply is not None and reply.text == CAP_BELOW_ONE_CALL and reply.text != AT_CAP
    assert model.requests == [] and usage == []
    async with house.sessionmaker() as db:
        view = await admin.assistant_admin(db, CONFIGURED, house["Alex"], now=house.clock())
    assert view.least_call_micros == least_private_call_micros("claude-sonnet-5-5")  # the configured model's
    assert not view.cap_below_one_call


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("kind", ["preference", "feedback", "away"])
async def test_a_double_tapped_confirm_hears_the_draft_is_used(
    house: House, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Two Confirms at once both pass the early check; the second, under the lock after the
    first commits, must say the draft is used, not that what it changes changed since."""
    meal_id = await dinner(house)
    args = {
        "preference": ("draft_preference", {"what": "", "name": "Mushrooms", "level": "veto"}),
        "feedback": ("draft_feedback", NO_FEEDBACK | {"meal": meal_id, "rating": "hated"}),
        "away": ("draft_away", {"first": "tomorrow", "last": "2026-10-26", "clear": False}),
    }[kind]
    row = draft_id(await tool(house, *args))
    await confirm(house, "Kim", row)
    monkeypatch.setattr(drafts, "_check_usable", lambda draft, now: None)  # as if it raced the first
    with pytest.raises(DraftUnavailable, match=re.escape(USED)):
        await confirm(house, "Kim", row)


@pytest.mark.flag("assistant", on=True)
async def test_feedback_drafted_before_the_place_was_corrected_is_refused(house: House) -> None:
    """#220: the admin corrects last night's place after Kim drafts feedback on it. Her draft was
    about the old place, so Confirm refuses, and nothing is saved on the new one."""
    meal_id = await dinner(house)
    row = draft_id(await tool(house, "draft_feedback", NO_FEEDBACK | {"meal": meal_id, "rating": "hated"}))
    event_id = next(m.event_id for m in await house.meals() if m.id == meal_id)
    siam, taqueria = house.items["Siam Garden"].id, house.items["Taqueria"].id
    await act(
        house,
        coordinator.correct_place,
        house["Alex"],
        event_id,
        item_id=taqueria,
        mode=Mode.PICKUP,
        opened_item_id=siam,
    )
    with pytest.raises(EventRuleViolation, match=re.escape(own_drafts.FEEDBACK_CHANGED)):
        await confirm(house, "Kim", row)
    assert await mine(house, "Kim", meal_id) is None


@pytest.mark.flag("assistant", on=True)
async def test_the_meals_listed_are_only_those_the_member_ate(house: House) -> None:
    """Lee didn't eat last night's dinner, and only Lee ate today's lunch: each sees only theirs."""
    dinner_id = await dinner(house)
    lunch = MealInput(TODAY, Slot.LUNCH, house.items["Taqueria"].id, frozenset({house["Lee"].id}))
    logged = await act(house, meals.log_meal, house["Lee"], lunch, today=TODAY)
    for who, expected in (("Kim", [dinner_id]), ("Lee", [logged.id])):
        result = await tool(house, "look_up_my_feedback", {"name": ""}, who=who)
        assert not result.is_error, result.content
        assert [m["meal"] for m in json.loads(result.content)["meals"]] == expected, who


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("raced", [NotFound("No such meal."), InvalidFeedback(NOT_A_PARTICIPANT)])
async def test_a_meal_changed_during_the_lookup_drops_out_and_the_filter_still_guards(
    house: House, monkeypatch: pytest.MonkeyPatch, raced: Exception
) -> None:
    """A meal deleted, or edited to leave Kim out, between the lookup's read and its form drops
    out of the list rather than failing the whole lookup. The participants filter is still the
    guard: the form is asked only about meals Kim ate, never Lee's."""
    dinner_id = await dinner(house)
    lunch = MealInput(TODAY, Slot.LUNCH, house.items["Taqueria"].id, frozenset({house["Kim"].id}))
    lunch_id = (await act(house, meals.log_meal, house["Kim"], lunch, today=TODAY)).id
    lees = MealInput(TODAY - timedelta(days=2), Slot.DINNER, house.items["Taqueria"].id, frozenset({house["Lee"].id}))
    lees_id = (await act(house, meals.log_meal, house["Lee"], lees, today=TODAY)).id
    asked: list[int] = []
    real_form = own_drafts.feedback_service.form

    async def racing(db: Any, actor: Member, meal_id: int, **kwargs: Any) -> Any:
        asked.append(meal_id)
        if meal_id == lunch_id:
            raise raced
        return await real_form(db, actor, meal_id, **kwargs)

    monkeypatch.setattr(own_drafts.feedback_service, "form", racing)
    result = await tool(house, "look_up_my_feedback", {"name": ""})
    assert not result.is_error, result.content
    assert [m["meal"] for m in json.loads(result.content)["meals"]] == [dinner_id]
    assert sorted(asked) == sorted([dinner_id, lunch_id]) and lees_id not in asked
