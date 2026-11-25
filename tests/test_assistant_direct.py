# SPDX-License-Identifier: AGPL-3.0-or-later
"""Acting at once, with **Undo** (#374, spec 6.14): each kind in the direct tier acts through
the real tools and services, its record says what changed, and **Undo** puts it back, only for
its member, once, within 30 minutes, and only while what it changed is as it left it. The kinds
that stay drafts stay drafts. The model is ``FakeModel`` where a conversation runs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.models import AssistantDraftRow
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.assistant import AssistantSettings, web_key
from dinnerbell.domain.assistant_drafts import DIRECT_KINDS, DraftKind, acts_directly
from dinnerbell.domain.catalog import TagKind
from dinnerbell.domain.events import Details, EventRuleViolation, EventState, RoleOffer, Status
from dinnerbell.domain.feedback import FeedbackInput, Rating
from dinnerbell.domain.guests import GuestInput
from dinnerbell.domain.meals import MealInput, Slot
from dinnerbell.domain.members import Capabilities
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.domain.schedule import FEEDBACK_KEY
from dinnerbell.services import away, calls, feedback, guests, meals, preferences
from dinnerbell.services.assistant import admin, call_drafts, direct, drafts, own_drafts, tools
from dinnerbell.services.assistant.conversation import AT_CAP, Assistant, converse
from dinnerbell.services.assistant.drafts import TURNED_OFF, DraftUnavailable
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.assistant.tools import ToolResult, ToolSession
from dinnerbell.services.call_context import load
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from tests.support.calls import OPENS, RESPOND_BY, House, dinner_id, furnish, make_house, nobody_coordinates
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, tool_call
from tests.support.feedback import done_dinner

pytestmark = pytest.mark.anyio

CONFIGURED = AssistantConfig(key_set=True)
DINNER = {"slot": "dinner", "day": "today"}
NO_FEEDBACK = {"rating": "", "went_wrong": [], "what_went_wrong": "", "thank_cook": False, "cook_note": ""}


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
    await house.tag("Gluten free", TagKind.DIETARY)
    async with house.sessionmaker() as db:
        await admin.update_assistant(db, CONFIGURED, house["Alex"], AssistantSettings(enabled=True), now=house.clock())
    yield house
    await house.close()


async def act(house: House, use_case: Any, *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


async def tool(house: House, name: str, args: dict[str, Any], *, who: str = "Sam") -> ToolResult:
    member = house[who]
    session = ToolSession(key=web_key(member.id))
    async with house.sessionmaker() as db:
        return await tools.run_tool(name, args, db=db, member=member, private=True, now=house.clock(), session=session)


async def rows(house: House) -> list[AssistantDraftRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AssistantDraftRow).order_by(AssistantDraftRow.id)))


async def undo(house: House, who: str, record_id: int, config: AssistantConfig = CONFIGURED) -> direct.Undone:
    async with house.sessionmaker() as db:
        return await direct.undo(db, house.clock, config, house[who], record_id)


async def collecting(house: House, *eaters: str) -> int:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in eaters:
        await act(house, calls.respond, house[name], event_id, Status.IN)
    return event_id


async def voting(house: House) -> int:
    """Sam and Kim are in, and Sam offers to pick up, so voting opens with picks."""
    event_id = await collecting(house, "Sam", "Kim")
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    await nobody_coordinates(house, event_id)
    return event_id


async def answer_of(house: House, event_id: int, member_id: int) -> Any:
    async with house.sessionmaker() as db:
        found = (await load(db, await house.event(event_id))).response_of(member_id)
    return None if found is None else (found.status, found.offers)


async def bea(house: House, event_id: int) -> int:
    """Sam invites Bea, who has no Slack, so she's in and he answers for her."""
    await act(house, guests.invite, house["Sam"], event_id, new_guest=GuestInput("Bea"))
    async with house.sessionmaker() as db:
        return next(g.id for g in await guests.saved_guests(db, house["Sam"]) if g.display_name == "Bea")


async def level(house: House, who: str, item: str) -> Stance:
    async with house.sessionmaker() as db:
        found = await preferences.my_stance(db, house[who], TargetType.ITEM, house.items[item].id)
    assert found is not None
    return found[1]


# Each kind in the direct tier ---------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """A direct kind: ``setup`` returns what the others need; ``state`` reads what it changes;
    ``move`` changes that another way, as the member would on the web."""

    kind: DraftKind
    tool: str
    args: Callable[[dict[str, Any]], dict[str, Any]]
    setup: Callable[[House], Awaitable[dict[str, Any]]]
    state: Callable[[House, dict[str, Any]], Awaitable[Any]]
    move: Callable[[House, dict[str, Any]], Awaitable[None]]


async def _in(house: House) -> dict[str, Any]:
    return {"event": await collecting(house, "Sam")}


async def _guest(house: House) -> dict[str, Any]:
    found = await _in(house)
    return found | {"guest": await bea(house, found["event"])}


async def _voting(house: House) -> dict[str, Any]:
    event_id = await voting(house)
    async with house.sessionmaker() as db:
        [pick, *_] = (await load(db, await house.event(event_id))).picks
    return {"event": event_id, "pick": pick.id}


async def _nothing(house: House) -> dict[str, Any]:
    return {}


async def _rated(house: House) -> dict[str, Any]:
    event_id = await done_dinner(house)
    async with house.sessionmaker() as db:
        meal = (await load(db, await house.event(event_id))).meal
    assert meal is not None
    await act(house, feedback.give, house["Sam"], meal.id, FeedbackInput(rating=Rating.LIKED))
    return {"meal": meal.id}


async def _votes(house: House, ctx: dict[str, Any]) -> bool:
    async with house.sessionmaker() as db:
        votes = (await load(db, await house.event(ctx["event"]))).votes
    return house["Sam"].id in votes.get(ctx["pick"], [])


async def _suggested(house: House, ctx: dict[str, Any]) -> list[int | None]:
    async with house.sessionmaker() as db:
        found = (await load(db, await house.event(ctx["event"]))).suggestions
    return sorted(s.item_id or 0 for s in found if s.member_id == house["Sam"].id)


async def _feedback(house: House, ctx: dict[str, Any]) -> Any:
    async with house.sessionmaker() as db:
        saved = await FeedbackRepository(db).get(house.household.id, ctx["meal"], house["Sam"].id)
    return None if saved is None else saved.answers.rating


async def _me(house: House) -> Any:
    return await house.refresh("Sam")


async def _details(house: House, ctx: dict[str, Any]) -> None:
    taqueria = house.items["Taqueria"].id
    await act(house, calls.save_details, house["Sam"], ctx["event"], Details(suggestion_item_ids=(taqueria,)))


CASES = [
    Case(
        DraftKind.ANSWER,
        "draft_answer",
        lambda c: DINNER | {"answer": "maybe"},
        _in,
        lambda h, c: answer_of(h, c["event"], h["Sam"].id),
        lambda h, c: act(h, calls.respond, h["Sam"], c["event"], Status.OUT),
    ),
    Case(
        DraftKind.ROLE,
        "draft_role",
        lambda c: DINNER | {"role": "pickup", "offer": True},
        _in,
        lambda h, c: answer_of(h, c["event"], h["Sam"].id),
        lambda h, c: act(h, calls.offer_role, h["Sam"], c["event"], RoleOffer.ORDER, True),
    ),
    Case(
        DraftKind.SUGGESTION,
        "draft_suggestion",
        lambda c: DINNER | {"name": "Siam Garden"},
        _in,
        _suggested,
        _details,
    ),
    Case(
        DraftKind.GUEST_ANSWER,
        "draft_guest_answer",
        lambda c: DINNER | {"name": "Bea", "answer": "out", "role": "", "offer": False},
        _guest,
        lambda h, c: answer_of(h, c["event"], c["guest"]),
        lambda h, c: act(h, guests.answer_for, h["Sam"], c["event"], c["guest"], role=RoleOffer.COOK),
    ),
    Case(
        DraftKind.VOTE,
        "draft_vote",
        lambda c: DINNER | {"pick": c["pick"], "name": "", "mode": "", "vote": True},
        _voting,
        _votes,
        lambda h, c: act(h, calls.unvote, h["Sam"], c["event"], c["pick"]),
    ),
    Case(
        DraftKind.PREFERENCE,
        "draft_preference",
        lambda c: {"what": "", "name": "Siam Garden", "level": "veto"},
        _nothing,
        lambda h, c: level(h, "Sam", "Siam Garden"),
        lambda h, c: act(
            h, preferences.set_my_preference, h["Sam"], TargetType.ITEM, h.items["Siam Garden"].id, Stance(score=-1)
        ),
    ),
    Case(
        DraftKind.AWAY,
        "draft_away",
        lambda c: {"first": "tomorrow", "last": "2026-10-26", "clear": False},
        _nothing,
        lambda h, c: _me(h),
        lambda h, c: act(h, away.set_my_away, h["Sam"], *_days(h, 3, 4)),
    ),
    Case(
        DraftKind.NEEDS,
        "draft_dietary_needs",
        lambda c: {"add": ["vegetarian"], "remove": []},
        _nothing,
        lambda h, c: _me(h),
        lambda h, c: act(h, guests.set_my_needs, h["Sam"], frozenset({h.tags["Gluten free"].id})),
    ),
    Case(
        DraftKind.CAN_USUALLY,
        "draft_can_usually",
        lambda c: {"cook": "on", "pick_up": "", "order": ""},
        _nothing,
        lambda h, c: _me(h),
        lambda h, c: act(
            h,
            preferences.update_my_settings,
            h["Sam"],
            preferences.MySettings(Capabilities(can_order=True), mute_dm_reminders=False, feedback_reminder=False),
        ),
    ),
    Case(
        DraftKind.FEEDBACK,
        "draft_feedback",
        lambda c: NO_FEEDBACK | {"meal": c["meal"], "rating": "loved"},
        _rated,
        _feedback,
        lambda h, c: act(h, feedback.give, h["Sam"], c["meal"], FeedbackInput(rating=Rating.HATED)),
    ),
]


def _days(house: House, first: int, last: int) -> tuple[Any, Any]:
    today = house.clock().date()
    return today + timedelta(days=first), today + timedelta(days=last)


def test_the_cases_cover_the_direct_tier() -> None:
    assert {c.kind for c in CASES} == DIRECT_KINDS


IDS = [c.kind.value for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_each_kind_acts_at_once_and_undo_puts_it_back_once(house: House, case: Case) -> None:
    ctx = await case.setup(house)
    before = await case.state(house, ctx)
    result = await tool(house, case.tool, case.args(ctx))
    assert result.content.startswith("Done at once, not a draft:"), result.content
    assert f"Undo for {direct.UNDO_MINUTES} minutes" in result.content
    [record] = [r for r in await rows(house) if r.kind == case.kind.value]
    assert record.direct and record.confirmed_at is not None and record.payload["undo"] is not None
    assert record.lines and record.lines[0] and direct.NO_UNDO not in record.lines
    done = await case.state(house, ctx)
    assert done != before
    undone = await undo(house, "Sam", record.id)
    assert undone.text.startswith("Undone.") and undone.record.undone and not undone.record.undo
    assert await case.state(house, ctx) == before
    with pytest.raises(DraftUnavailable, match=direct.UNDONE):  # a double tap does nothing twice
        await undo(house, "Sam", record.id)
    assert await case.state(house, ctx) == before


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_undo_is_refused_once_what_it_changed_has_moved(house: House, case: Case) -> None:
    ctx = await case.setup(house)
    await tool(house, case.tool, case.args(ctx))
    [record] = [r for r in await rows(house) if r.kind == case.kind.value]
    await case.move(house, ctx)
    moved = await case.state(house, ctx)
    with pytest.raises(EventRuleViolation, match="changed since the assistant did it"):
        await undo(house, "Sam", record.id)
    assert await case.state(house, ctx) == moved
    [still] = [r for r in await rows(house) if r.id == record.id]
    assert still.undone_at is None  # the refusal leaves the record as it was


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_only_its_member_undoes_it_and_only_for_30_minutes(house: House, case: Case) -> None:
    ctx = await case.setup(house)
    await tool(house, case.tool, case.args(ctx))
    [record] = [r for r in await rows(house) if r.kind == case.kind.value]
    done = await case.state(house, ctx)
    for who in ("Kim", "Alex"):  # an admin included
        with pytest.raises(NotFound, match=direct.NO_SUCH_RECORD):
            await undo(house, who, record.id)
    house.clock.advance(timedelta(minutes=direct.UNDO_MINUTES))
    with pytest.raises(DraftUnavailable, match=direct.UNDO_CLOSED):
        await undo(house, "Sam", record.id)
    assert await case.state(house, ctx) == done


# What has no Undo, and what stays a draft --------------------------------------------------------


async def test_a_first_answer_acts_but_has_no_undo(house: House) -> None:
    """Dinnerbell can't un-answer anyone, so a first answer's record says to answer again."""
    event_id = await collecting(house)
    result = await tool(house, "draft_answer", DINNER | {"answer": "in"})
    assert result.content.startswith("Done at once") and "no Undo" in result.content
    [record] = await rows(house)
    assert record.payload["undo"] is None
    assert list(record.lines) == ["You're in for today's dinner call.", direct.NO_UNDO]
    async with house.sessionmaker() as db:
        [view] = await direct.records(db, house["Sam"], web_key(house["Sam"].id), house.clock())
    assert not view.undo
    with pytest.raises(DraftUnavailable, match=direct.NO_UNDO):
        await undo(house, "Sam", record.id)
    assert await answer_of(house, event_id, house["Sam"].id) == (Status.IN, frozenset())
    again = await tool(house, "draft_answer", DINNER | {"answer": "out"})
    assert again.content.startswith("Done at once") and "Undo for" in again.content  # answering again


async def test_a_refused_action_leaves_no_record(house: House) -> None:
    await collecting(house, "Sam")
    result = await tool(house, "draft_answer", DINNER | {"answer": "in"})
    assert result.is_error  # in already: nothing to do
    house.clock.now = RESPOND_BY
    await house.tick()
    late = await tool(house, "draft_role", DINNER | {"role": "pickup", "offer": True}, who="Lee")
    assert not late.content.startswith("Done")  # joining late first is a draft
    assert all(not r.direct for r in await rows(house))


async def test_what_loosens_joins_late_or_tells_someone_stays_a_draft(house: House) -> None:
    """Joining late, an out that ends coordinating, lifting a veto, taking off a need, a level
    the member's own ratings move, a first feedback, and a thank-you to the cook all draft."""
    sam = house["Sam"]
    event_id = await done_dinner(house)  # Sam cooked, and Kim ate
    await act(house, preferences.set_my_preference, sam, TargetType.ITEM, house.items["Taqueria"].id, Stance(veto=True))
    await act(house, guests.set_my_needs, sam, frozenset({house.tags["Vegetarian"].id}))
    lift = await tool(house, "draft_preference", {"what": "", "name": "Taqueria", "level": "1"})
    off = await tool(house, "draft_dietary_needs", {"add": [], "remove": ["Vegetarian"]})
    async with house.sessionmaker() as db:
        meal = (await load(db, await house.event(event_id))).meal
    assert meal is not None
    first = await tool(house, "draft_feedback", NO_FEEDBACK | {"meal": meal.id, "rating": "liked"}, who="Kim")
    await act(house, feedback.give, house["Kim"], meal.id, FeedbackInput(rating=Rating.LIKED))
    thanks = await tool(house, "draft_feedback", NO_FEEDBACK | {"meal": meal.id, "thank_cook": True}, who="Kim")
    rated = await tool(house, "draft_preference", {"what": "", "name": "Sam's Tacos", "level": "2"}, who="Kim")
    for result in (lift, off, first, thanks, rated):
        assert result.content.startswith("Draft "), result.content
    assert not any(r.direct for r in await rows(house))


async def test_joining_late_and_an_out_that_ends_coordinating_draft(house: House) -> None:
    event_id = await collecting(house, "Sam")
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    out = await tool(house, "draft_answer", DINNER | {"answer": "out"})
    assert out.content.startswith("Draft ")
    await act(house, calls.respond, house["Kim"], event_id, Status.IN)
    await act(house, calls.offer_role, house["Kim"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    late = await tool(house, "draft_answer", DINNER | {"answer": "in"}, who="Lee")
    assert late.content.startswith("Draft ")
    assert acts_directly(DraftKind.ANSWER, {"join_late": False, "coordinating": False})


async def test_undo_never_answers_a_member_out_of_coordinating(house: House) -> None:
    """Sam answered In at once, then claimed coordinating; Undo back to Out would end it and tell
    the household, so it's refused and he's still the coordinator."""
    event_id = await collecting(house)
    await act(house, calls.respond, house["Sam"], event_id, Status.OUT)
    await tool(house, "draft_answer", DINNER | {"answer": "in"})
    [record] = await rows(house)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    with pytest.raises(EventRuleViolation, match="You coordinate this call now"):
        await undo(house, "Sam", record.id)
    assert (await house.event(event_id)).coordinator_id == house["Sam"].id


# Undo needs no model ------------------------------------------------------------------------------


async def test_undo_works_at_the_cap_and_at_conversation_hours_zero_and_not_while_off(house: House) -> None:
    await tool(house, "draft_preference", {"what": "", "name": "Siam Garden", "level": "veto"})
    [record] = await rows(house)
    async with house.sessionmaker() as db:
        off = AssistantSettings(enabled=False)
        await admin.update_assistant(db, CONFIGURED, house["Alex"], off, now=house.clock())
    with pytest.raises(DraftUnavailable, match=TURNED_OFF):
        await undo(house, "Sam", record.id)
    async with house.sessionmaker() as db:
        tight = AssistantSettings(enabled=True, cap_dollars=1, conversation_hours=0)
        await admin.update_assistant(db, CONFIGURED, house["Alex"], tight, now=house.clock())
    from tests.test_assistant_security import spent  # the month's cap, spent

    async with house.sessionmaker() as db:
        db.add(spent(house, 1_000_000, "2026-10"))
        await db.commit()
    model = FakeModel(script=[answer("never")])
    helper = Assistant(CONFIGURED, model, house.clock)
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, house["Sam"], web_key(house["Sam"].id), private=True, text="Hi")
    assert reply is not None and reply.text == AT_CAP
    done = await undo(house, "Sam", record.id)
    assert done.text == "Undone. Your level for Siam Garden is 0 now." and model.requests == []
    assert await level(house, "Sam", "Siam Garden") == Stance()


async def test_a_record_is_neither_confirmed_nor_cancelled(house: House) -> None:
    await tool(house, "draft_can_usually", {"cook": "on", "pick_up": "", "order": ""})
    [record] = await rows(house)
    async with house.sessionmaker() as db:
        with pytest.raises(NotFound):
            await drafts.confirm_draft(db, house.clock, CONFIGURED, house["Sam"], record.id)
        with pytest.raises(NotFound):
            await drafts.cancel_draft(db, house.clock, house["Sam"], record.id)
        assert await drafts.waiting(db, house["Sam"], web_key(house["Sam"].id), house.clock()) == []
    async with house.sessionmaker() as db:
        draft = await tool(house, "draft_call", {"slot": "lunch", "day": "tomorrow"})
        assert draft.content.startswith("Draft ")
        with pytest.raises(NotFound):
            await direct.undo(db, house.clock, CONFIGURED, house["Sam"], int(draft.content.split()[1]))


async def test_a_conversation_returns_its_records_even_when_the_model_fails(house: House) -> None:
    """The model sets a level, then its next call fails: the reply says so and still carries the
    record, since the level is set."""
    from tests.support.fake_model import failure

    model = FakeModel(script=[tool_call("draft_preference", {"what": "", "name": "Siam Garden", "level": "1"})])
    model.script.append(failure(maybe_billed=False))
    helper = Assistant(CONFIGURED, model, house.clock)
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, house["Sam"], web_key(house["Sam"].id), private=True, text="I like Siam")
    assert reply is not None
    [card] = reply.drafts
    assert card.record and card.undo
    assert await level(house, "Sam", "Siam Garden") == Stance(score=1)


def test_feedback_counted_and_without_the_cook_acts() -> None:
    plain = {"counted": True, "cook_thanks": False, "cook_note": None, "replaced": {"cook_thanks": False}}
    assert acts_directly(DraftKind.FEEDBACK, plain)
    assert not acts_directly(DraftKind.FEEDBACK, plain | {"counted": False})
    assert not acts_directly(DraftKind.FEEDBACK, plain | {"replaced": {"cook_note": "Thanks"}})
    assert not acts_directly(DraftKind.PROPOSAL, {})
    assert acts_directly(DraftKind.PREFERENCE, {"before": "veto", "level": "veto"})
    assert not acts_directly(DraftKind.PREFERENCE, {"before": "veto", "level": "2"})


async def test_an_action_its_use_case_refuses_leaves_no_record(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    """Something changed between the tool's checks and the use case: the refusal goes back to
    the model as it is, and the record goes too, so nothing waits or shows."""

    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise EventRuleViolation("Responses are closed for this call.", code="responses_closed")

    await collecting(house, "Sam")
    monkeypatch.setattr(drafts, "confirm_direct", refused)
    result = await tool(house, "draft_answer", DINNER | {"answer": "maybe"})
    assert result == ToolResult("Responses are closed for this call.", is_error=True)
    assert await rows(house) == []


async def test_a_record_whose_words_cant_be_kept_still_counts_as_done(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The level and its Undo commit together; a failure saving the record's words afterward is
    logged, and the model still hears it's done, with an Undo that works."""
    real = direct.AssistantRepository.keep_record
    calls_made: list[int] = []

    async def second_fails(self: Any, *args: Any, **kwargs: Any) -> None:
        calls_made.append(1)
        if len(calls_made) == 2:
            raise RuntimeError("the database went away")
        await real(self, *args, **kwargs)

    monkeypatch.setattr(direct.AssistantRepository, "keep_record", second_fails)
    result = await tool(house, "draft_preference", {"what": "", "name": "Siam Garden", "level": "1"})
    assert result.content.startswith("Done at once") and "Undo for" in result.content
    [record] = await rows(house)
    assert record.payload["undo"]["level"] == "0" and await level(house, "Sam", "Siam Garden") == Stance(score=1)
    monkeypatch.undo()
    assert (await undo(house, "Sam", record.id)).text == "Undone. Your level for Siam Garden is 0 now."


# Telling nobody new: answers that would close early (#374 review) ---------------------------------


@pytest.mark.parametrize(
    ("others", "first", "word"),
    [
        # Everyone else is out and Sam a maybe: his out leaves nobody in, so the next tick
        # abandons the call and says so.
        ({"Alex": Status.OUT, "Kim": Status.OUT, "Lee": Status.OUT}, Status.MAYBE, "out"),
        # Kim is in to pick up, everyone else out, and Sam hasn't answered: his in completes the
        # answers, so ten quiet minutes later the scheduler closes them and posts.
        ({"Alex": Status.OUT, "Kim": Status.IN, "Lee": Status.OUT}, None, "in"),
    ],
)
async def test_an_answer_that_completes_the_calls_answers_drafts_and_says_why(
    house: House, others: dict[str, Status], first: Status | None, word: str
) -> None:
    event_id = await collecting(house)
    for name, status in others.items():
        await act(house, calls.respond, house[name], event_id, status)
    await act(house, calls.offer_role, house["Kim"], event_id, RoleOffer.PICKUP, True)
    if first is not None:
        await act(house, calls.respond, house["Sam"], event_id, first)
    kinds = await house.kinds()
    result = await tool(house, "draft_answer", DINNER | {"answer": word})
    assert result.content.startswith("Draft ") and result.content.endswith(direct.CLOSES_EARLY)
    assert not any(r.direct for r in await rows(house))
    for minutes in (0, 11):
        house.clock.advance(timedelta(minutes=minutes))
        await house.tick()
    assert (await house.event(event_id)).state is EventState.COLLECTING
    assert await house.kinds() == kinds


async def test_undo_never_completes_the_calls_answers_either(house: House) -> None:
    """Sam answered maybe at once; Kim, Alex, and Lee answer out meanwhile. Putting his out back
    would leave nobody in, so Undo is refused and the call keeps waiting."""
    event_id = await collecting(house)
    await act(house, calls.respond, house["Sam"], event_id, Status.OUT)
    await tool(house, "draft_answer", DINNER | {"answer": "maybe"})
    [record] = await rows(house)
    for name in ("Alex", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    with pytest.raises(EventRuleViolation, match="close the call's answers early"):
        await undo(house, "Sam", record.id)
    assert await answer_of(house, event_id, house["Sam"].id) == (Status.MAYBE, frozenset())


async def test_an_out_that_would_end_coordinating_is_caught_under_the_lock(house: House) -> None:
    """The draft was worked out before Sam claimed coordinating; the check under the call's lock
    finds he coordinates now, so it drafts, and no record is left."""
    event_id = await collecting(house, "Sam")
    async with house.sessionmaker() as db:
        spec = await call_drafts.answer(
            db, house["Sam"], await house.event(event_id), Status.OUT, today=house.clock().date(), now=house.clock()
        )
    assert spec.payload["coordinating"] is False
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    async with house.sessionmaker() as db:
        with pytest.raises(direct.DraftInstead, match="coordinates that call now"):
            await direct.act(db, house["Sam"], web_key(house["Sam"].id), spec, now=house.clock())
    assert await rows(house) == []
    assert await answer_of(house, event_id, house["Sam"].id) == (Status.IN, frozenset())


# Undo puts back the whole answer (#374 review) -----------------------------------------------------


async def whole_answer(house: House, event_id: int, member_id: int) -> Any:
    async with house.sessionmaker() as db:
        found = (await load(db, await house.event(event_id))).response_of(member_id)
    return None if found is None else replace(found, updated_at=None)


async def test_undo_puts_an_away_out_back_as_the_windows(house: House) -> None:
    """Sam's away window answered him out; he says he's in after all, then undoes it. His out is
    the window's again, so "I'm back" takes it away as it would have."""
    event_id = await collecting(house)
    day = (await house.event(event_id)).day
    await act(house, away.set_my_away, house["Sam"], day, day + timedelta(days=2))
    sam = house["Sam"].id
    before = await whole_answer(house, event_id, sam)
    assert before is not None and before.away_until is not None
    await tool(house, "draft_answer", DINNER | {"answer": "in"})
    [record] = await rows(house)
    await undo(house, "Sam", record.id)
    assert await whole_answer(house, event_id, sam) == before
    await act(house, away.clear_my_away, await house.refresh("Sam"))
    assert await answer_of(house, event_id, sam) is None  # the window's out goes with the window


async def test_undo_puts_back_a_maybe_whose_usual_offers_havent_had_their_turn(house: House) -> None:
    """Sam usually picks up. He's a maybe; answering in at once turns that offer on, its one
    turn. Undo makes him the maybe he was, offers and turn and when he answered included, so
    answering in again later turns it on again."""
    settings = preferences.MySettings(Capabilities(can_pickup=True), mute_dm_reminders=False, feedback_reminder=False)
    await act(house, preferences.update_my_settings, house["Sam"], settings)
    sam = await house.refresh("Sam")
    event_id = await collecting(house)
    await act(house, calls.respond, sam, event_id, Status.MAYBE)
    before = await whole_answer(house, event_id, sam.id)
    house.clock.advance(timedelta(minutes=1))
    await tool(house, "draft_answer", DINNER | {"answer": "in"})
    done = await whole_answer(house, event_id, sam.id)
    assert done.offers == {RoleOffer.PICKUP} and done.usual_offers_done
    [record] = await rows(house)
    await undo(house, "Sam", record.id)
    assert await whole_answer(house, event_id, sam.id) == before
    await act(house, calls.respond, sam, event_id, Status.IN)
    assert await answer_of(house, event_id, sam.id) == (Status.IN, frozenset({RoleOffer.PICKUP}))


# A level's ratings (#374 review) ------------------------------------------------------------------


async def rate_siam(house: House, rating: Rating = Rating.LOVED) -> None:
    """Sam ate Siam Garden for lunch today and rates it."""
    lunch = MealInput(house.clock().date(), Slot.LUNCH, house.items["Siam Garden"].id, frozenset({house["Sam"].id}))
    meal = await act(house, meals.log_meal, house["Sam"], lunch, today=house.clock().date())
    await act(house, feedback.give, house["Sam"], meal.id, FeedbackInput(rating=rating))


async def test_a_level_on_an_item_sams_ratings_move_drafts_even_with_feedback_off(house: House) -> None:
    """With Feedback at 0, ratings move no score and Preferences shows no note, but a level still
    stops them for good once Feedback is on, so it's a draft."""
    async with house.sessionmaker() as db:
        stored = await household_settings(db, house.household.id)
        await HouseholdRepository(db).update_settings(house.household.id, stored | {FEEDBACK_KEY: 0})
        await db.commit()
    await rate_siam(house)
    result = await tool(house, "draft_preference", {"what": "", "name": "Siam Garden", "level": "1"})
    assert result.content.startswith("Draft ")
    [row] = await rows(house)
    assert not row.direct and row.payload["ratings_move"] is True


async def test_a_rating_saved_meanwhile_is_caught_under_the_lock(house: House) -> None:
    async with house.sessionmaker() as db:
        spec = await own_drafts.stance_draft(
            db, house["Sam"], "", "Siam Garden", Stance(score=1), today=house.clock().date()
        )
    await rate_siam(house)
    async with house.sessionmaker() as db:
        with pytest.raises(direct.DraftInstead, match="ratings of it count now"):
            await direct.act(db, house["Sam"], web_key(house["Sam"].id), spec, now=house.clock())
    assert await rows(house) == []


async def test_undo_of_a_level_lets_a_rating_saved_since_count(house: House) -> None:
    """Sam sets Siam Garden to +1 at once, then rates it, which counts, as a rating after a level
    does. Undo puts back that he never set a level on it, rather than setting one now, which
    would stop that rating for good (FR-55)."""
    await tool(house, "draft_preference", {"what": "", "name": "Siam Garden", "level": "1"})
    [record] = await rows(house)
    house.clock.advance(timedelta(minutes=1))
    await rate_siam(house)
    today = house.clock().date()
    async with house.sessionmaker() as db:
        assert await preferences.my_ratings_move(db, house["Sam"], house.items["Siam Garden"].id, today=today)
    house.clock.advance(timedelta(minutes=1))
    await undo(house, "Sam", record.id)
    async with house.sessionmaker() as db:
        assert await preferences.my_ratings_move(db, house["Sam"], house.items["Siam Garden"].id, today=today)
        assert await preferences.my_item_edit(db, house["Sam"], house.items["Siam Garden"].id) is None


def test_a_record_with_no_undo_says_why_for_whom() -> None:
    assert direct._no_undo_line(DraftKind.ANSWER, {}) == direct.NO_UNDO
    assert direct._no_undo_line(DraftKind.GUEST_ANSWER, {"name": "Bea"}).startswith("It was Bea's first answer")
    assert direct._no_undo_line(DraftKind.AWAY, {}) == direct.NO_UNDO_SAVED


async def test_a_coordinators_maybe_says_it_can_still_end_coordinating(house: House) -> None:
    event_id = await collecting(house, "Sam")
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    await tool(house, "draft_answer", DINNER | {"answer": "maybe"})
    [record] = await rows(house)
    assert record.direct and direct.MAYBE_COORDINATING in record.lines


async def test_undo_never_puts_back_an_away_out_that_no_window_owns(house: House) -> None:
    """Sam's window answered him Out; he answers in at once, then clears his away days on
    Preferences. Putting the window's out back now would leave an out no window owns, which
    "I'm back" could never clear, so Undo is refused as changed since, and he stays in."""
    event_id = await collecting(house)
    day = (await house.event(event_id)).day
    await act(house, away.set_my_away, house["Sam"], day, day + timedelta(days=2))
    await tool(house, "draft_answer", DINNER | {"answer": "in"})
    [record] = await rows(house)
    await act(house, away.clear_my_away, await house.refresh("Sam"))
    with pytest.raises(EventRuleViolation, match="changed since the assistant did it"):
        await undo(house, "Sam", record.id)
    assert await answer_of(house, event_id, house["Sam"].id) == (Status.IN, frozenset())


async def test_undo_never_erases_a_level_sam_set_again_himself(house: House) -> None:
    """Sam sets Siam Garden to +1 at once, then sets +1 again on Preferences: that level is his
    own now, edit time and all, so Undo is refused as changed since."""
    await tool(house, "draft_preference", {"what": "", "name": "Siam Garden", "level": "1"})
    [record] = await rows(house)
    house.clock.advance(timedelta(minutes=1))
    siam = house.items["Siam Garden"].id
    await act(house, preferences.set_my_preference, house["Sam"], TargetType.ITEM, siam, Stance(score=1))
    with pytest.raises(EventRuleViolation, match="changed since the assistant did it"):
        await undo(house, "Sam", record.id)
    assert await level(house, "Sam", "Siam Garden") == Stance(score=1)
    async with house.sessionmaker() as db:
        assert await preferences.my_item_edit(db, house["Sam"], siam) == house.clock()


async def test_away_days_that_would_close_a_call_draft(house: House) -> None:
    """Everyone else is out of tonight's call and Sam hasn't answered: away days covering today
    would answer him out and leave nobody in, so the call would be abandoned and posted."""
    event_id = await collecting(house)
    for name in ("Alex", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    kinds = await house.kinds()
    day = (await house.event(event_id)).day
    window = {"first": day.isoformat(), "last": (day + timedelta(days=1)).isoformat(), "clear": False}
    result = await tool(house, "draft_away", window)
    assert result.content.startswith("Draft ") and result.content.endswith(direct.CLOSES_A_CALL)
    await house.tick()
    assert (await house.event(event_id)).state is EventState.COLLECTING and await house.kinds() == kinds
    later = {"first": (day + timedelta(days=3)).isoformat(), "last": (day + timedelta(days=4)).isoformat()}
    assert (await tool(house, "draft_away", later | {"clear": False})).content.startswith("Done at once")


async def test_ordering_that_would_cover_a_call_drafts(house: House) -> None:
    """Sam and Kim are in with nobody to order, and the quiet spell has passed: turning on Order
    would cover the call, so voting would open and be posted. Cooking counts nowhere, so it acts."""
    event_id = await collecting(house, "Sam", "Kim")
    for name in ("Alex", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    house.clock.advance(timedelta(minutes=15))
    await house.tick()
    kinds = await house.kinds()
    result = await tool(house, "draft_can_usually", {"cook": "", "pick_up": "", "order": "on"})
    assert result.content.startswith("Draft ") and result.content.endswith(direct.CLOSES_A_CALL)
    await house.tick()
    assert (await house.event(event_id)).state is EventState.COLLECTING and await house.kinds() == kinds
    assert (await tool(house, "draft_can_usually", {"cook": "on", "pick_up": "", "order": ""})).content.startswith(
        "Done at once"
    )
