# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's call actions (#251, spec 6.14, **Tools** and **Drafts**): answering, joining
late, voting and taking a vote back, suggesting, taking or giving up a role, and taking back an
offer. Each is a draft, and each **Confirm** runs the use case the web form or Slack button runs.

The household is the usual one (``tests.support.calls.furnish``): Alex (the admin), Sam (on the
tacos' cook list), Kim, and Lee (whose usual settings offer to order). Ranking proposes every
place it can, so a voting call has Siam Garden and Taqueria as picks. The assistant is on.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import date, time, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select

from dinnerbell.config import Settings
from dinnerbell.db.events import EventRepository
from dinnerbell.db.models import AssistantDraftRow, TagRow
from dinnerbell.domain.assistant import AssistantSettings, Pseudonyms, slack_key, web_key
from dinnerbell.domain.catalog import Mode, TagKind
from dinnerbell.domain.events import (
    STALE_CALL,
    Details,
    EventRuleViolation,
    EventState,
    RoleOffer,
    Status,
)
from dinnerbell.domain.guests import GuestInput
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.services import calls, guests, offers
from dinnerbell.services.assistant import admin, call_drafts, drafts, tools
from dinnerbell.services.assistant.call_drafts import ActionConfirmed
from dinnerbell.services.assistant.conversation import Assistant, converse
from dinnerbell.services.assistant.drafts import USED, DraftUnavailable
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.assistant.tools import ToolResult, ToolSession
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.offers import OfferInput
from tests.support.calls import OPENS, RESPOND_BY, House, dinner_id, furnish, make_house, nobody_coordinates
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, tool_call

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 22)
CONFIGURED = AssistantConfig(key_set=True, flag_on=True)
CALL = {"slot": "", "day": ""}


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


async def open_dinner(house: House, *eaters: str) -> int:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in eaters:
        await act(house, calls.respond, house[name], event_id, Status.IN)
    return event_id


async def to_voting(house: House) -> int:
    """Sam and Kim are in; Lee never answers; nobody coordinates."""
    event_id = await open_dinner(house, "Sam", "Kim")
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    await nobody_coordinates(house, event_id)
    return event_id


def fresh(member: Member) -> ToolSession:
    return ToolSession(key=web_key(member.id), pseudonyms=Pseudonyms().with_member(member.id))


async def tool(house: House, name: str, args: dict[str, Any], *, who: str = "Lee", private: bool = True) -> ToolResult:
    member = house[who]
    async with house.sessionmaker() as db:
        return await tools.run_tool(
            name, args, db=db, member=member, private=private, now=house.clock(), session=fresh(member)
        )


def draft_id(result: ToolResult) -> int:
    assert not result.is_error, result.content
    return int(result.content.split()[1])


async def confirm(house: House, who: str, draft: int) -> Any:
    async with house.sessionmaker() as db:
        return await drafts.confirm_draft(db, house.clock, CONFIGURED, house[who], draft)


async def rows(house: House) -> list[AssistantDraftRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AssistantDraftRow).order_by(AssistantDraftRow.id)))


async def lines(house: House, draft: int) -> list[str]:
    [row] = [r for r in await rows(house) if r.id == draft]
    return list(row.lines)


def answer_args(word: str) -> dict[str, Any]:
    return CALL | {"answer": word}


def vote_args(name: str = "", *, vote: bool = True, mode: str = "", pick: int = 0) -> dict[str, Any]:
    return CALL | {"pick": pick, "name": name, "mode": mode, "vote": vote}


def role_args(role: str, *, offer: bool = True) -> dict[str, Any]:
    return CALL | {"role": role, "offer": offer}


def spy(monkeypatch: pytest.MonkeyPatch, *names: str) -> list[str]:
    """Record each named ``calls`` use case as Confirm calls it, then run the real one."""
    called: list[str] = []
    for name in names:
        real = getattr(calls, name)

        def wrap(real: Callable[..., Any], name: str) -> Callable[..., Any]:
            async def spied(*args: Any, **kwargs: Any) -> Any:
                called.append(name)
                return await real(*args, **kwargs)

            return spied

        monkeypatch.setattr(calls, name, wrap(real, name))
    return called


# The issue's example ----------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_im_in_pickup_and_a_vote_make_one_draft_each_and_each_confirm_runs_its_use_case(
    house: House, helper: Assistant, model: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#251's "Done when": "I'm in, put me down for pickup, and vote for the Thai place" while
    voting, from someone who never answered, makes three drafts and changes nothing; each
    Confirm, in any order, goes through the use case the buttons call."""
    event_id = await to_voting(house)
    lee = house["Lee"]
    model.script = [
        tool_call(
            "draft_answer",
            answer_args("in"),
            call_id="toolu_in",
            toolu_role=("draft_role", role_args("pickup")),
            toolu_vote=("draft_vote", vote_args("Siam Garden")),
        ),
        answer("Three drafts are waiting for you."),
    ]
    before = await house.context(event_id)
    async with house.sessionmaker() as db:
        reply = await converse(
            db, helper, lee, web_key(lee.id), private=True, text="I'm in, put me down for pickup, and vote for Siam"
        )
    assert reply is not None
    assert [d.title for d in reply.drafts] == [
        "Join today's dinner call late",
        "Offer to pick up for today's dinner call",
        "Vote for Siam Garden (pickup)",
    ]
    assert reply.drafts[1].confirm_lines == (
        "You'll join today's dinner call late.",
        "Your votes show, and count once the coordinator says so.",
        "You'll be down to pick up for today's dinner call.",
    )
    assert (await house.context(event_id)).response_of(lee.id) == before.response_of(lee.id) is None
    called = spy(monkeypatch, "join_late", "offer_role", "vote")
    answer_draft, role_draft, vote_draft = (d.id for d in reply.drafts)
    voted = await confirm(house, "Lee", vote_draft)
    assert voted == ActionConfirmed(event_id, "Confirmed. Your vote for Siam Garden is in.")
    assert called == ["vote"]
    await confirm(house, "Lee", role_draft)
    await confirm(house, "Lee", answer_draft)
    assert called == ["vote", "offer_role", "join_late"]
    ctx = await house.context(event_id)
    mine = ctx.response_of(lee.id)
    assert mine is not None and mine.is_in and mine.late and RoleOffer.PICKUP in mine.offers
    siam = next(p for p in ctx.picks if p.item_id == house.items["Siam Garden"].id)
    assert lee.id in ctx.votes[siam.id]
    with pytest.raises(DraftUnavailable, match=USED):
        await confirm(house, "Lee", vote_draft)


@pytest.mark.flag("assistant", on=True)
async def test_in_a_channel_thread_each_tool_works_and_the_drafts_are_the_askers(
    house: House, helper: Assistant, model: FakeModel
) -> None:
    """Call actions work in any conversation, including a channel thread, and the drafts are
    only the asker's: nobody else can confirm them."""
    event_id = await open_dinner(house)
    kim = house["Kim"]
    model.script = [tool_call("draft_answer", answer_args("maybe")), answer("Drafted.")]
    async with house.sessionmaker() as db:
        reply = await converse(
            db, helper, kim, slack_key("C1", "1790000000.000100"), private=False, text="@Dinnerbell maybe for me"
        )
    assert reply is not None and [d.title for d in reply.drafts] == ["Answer Maybe for today's dinner call"]
    for other in ("Sam", "Alex"):
        with pytest.raises(NotFound):
            await confirm(house, other, reply.drafts[0].id)
    await confirm(house, "Kim", reply.drafts[0].id)
    mine = (await house.context(event_id)).response_of(kim.id)
    assert mine is not None and mine.status is Status.MAYBE


# Answers ----------------------------------------------------------------------------------------


async def test_answers_while_collecting_say_what_confirm_does(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    event_id = await open_dinner(house, "Sam")
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    out = draft_id(await tool(house, "draft_answer", answer_args("out"), who="Sam"))
    assert await lines(house, out) == [
        "You'll be out of today's dinner call.",
        "Your role offers are withdrawn.",
        "You'll stop coordinating it.",
    ]
    lee_in = draft_id(await tool(house, "draft_answer", answer_args("in")))
    assert await lines(house, lee_in) == [
        "You'll be in for today's dinner call.",
        "Your usual offers from Preferences turn on.",
    ]
    kim_in = draft_id(await tool(house, "draft_answer", answer_args("in"), who="Kim"))
    assert await lines(house, kim_in) == ["You'll be in for today's dinner call."]
    called = spy(monkeypatch, "respond")
    assert await confirm(house, "Sam", out) == ActionConfirmed(
        event_id, "Confirmed. You're out of today's dinner call."
    )
    await confirm(house, "Lee", lee_in)
    assert called == ["respond", "respond"]
    ctx = await house.context(event_id)
    sam, lee = ctx.response_of(house["Sam"].id), ctx.response_of(house["Lee"].id)
    assert sam is not None and sam.status is Status.OUT and not sam.offers
    assert ctx.event.coordinator_id is None
    assert lee is not None and lee.is_in and RoleOffer.ORDER in lee.offers


async def test_an_answer_that_changes_nothing_or_isnt_allowed_is_refused(house: House) -> None:
    event_id = await open_dinner(house, "Sam", "Kim")
    await act(house, calls.offer_role, house["Kim"], event_id, RoleOffer.PICKUP, True)
    same = await tool(house, "draft_answer", answer_args("in"), who="Sam")
    assert same == ToolResult(call_drafts.NOTHING_TO_DO, is_error=True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    for word in ("out", "maybe"):
        closed = await tool(house, "draft_answer", answer_args(word), who="Sam")
        assert closed == ToolResult("Responses are closed for this call.", is_error=True)
    in_already = await tool(house, "draft_answer", answer_args("in"), who="Sam")
    assert in_already == ToolResult(call_drafts.NOTHING_TO_DO, is_error=True)
    assert await rows(house) == []


async def test_a_stale_answer_gets_the_services_refusal_and_stays_waiting(house: House) -> None:
    """Drafted while answers are open, confirmed once voting started: ``respond`` refuses, and
    the draft is left as it was."""
    await open_dinner(house, "Sam", "Kim")
    house.clock.now = RESPOND_BY - timedelta(minutes=5)
    draft = draft_id(await tool(house, "draft_answer", answer_args("maybe")))
    house.clock.now = RESPOND_BY
    await house.tick()
    with pytest.raises(EventRuleViolation, match="Responses are closed"):
        await confirm(house, "Lee", draft)
    [row] = await rows(house)
    assert row.used_at is None


async def test_a_draft_for_an_earlier_run_of_the_call_is_refused(house: House) -> None:
    """A vote has no run of its own to check, so Confirm checks the draft's run first: after a
    call-off and restart, the old card gets ``check_run``'s refusal, as a stale Slack card does."""
    event_id = await to_voting(house)
    draft = draft_id(await tool(house, "draft_vote", vote_args("Siam Garden"), who="Kim"))
    await act(house, calls.cancel_call, house["Alex"], event_id)
    await act(house, calls.restart_call, house["Alex"], event_id)
    with pytest.raises(EventRuleViolation, match=STALE_CALL):
        await confirm(house, "Kim", draft)
    [row] = await rows(house)
    assert row.used_at is None


# Votes ------------------------------------------------------------------------------------------


async def test_votes_and_taking_them_back(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    event_id = await to_voting(house)
    first = draft_id(await tool(house, "draft_vote", vote_args("siam garden", mode="pickup"), who="Kim"))
    assert await lines(house, first) == ["It adds your vote for Siam Garden (pickup)."]
    called = spy(monkeypatch, "vote", "unvote")
    await confirm(house, "Kim", first)
    again = await tool(house, "draft_vote", vote_args("Siam Garden"), who="Kim")
    assert again == ToolResult(call_drafts.NOTHING_TO_DO, is_error=True)
    back = draft_id(await tool(house, "draft_vote", vote_args("Siam Garden", vote=False), who="Kim"))
    assert await lines(house, back) == ["It takes back your vote for Siam Garden (pickup)."]
    assert await confirm(house, "Kim", back) == ActionConfirmed(
        event_id, "Confirmed. You took back your vote for Siam Garden."
    )
    assert called == ["vote", "unvote"]
    assert not any(house["Kim"].id in voters for voters in (await house.context(event_id)).votes.values())


async def test_a_vote_is_refused_when_the_service_would_refuse_it(house: House) -> None:
    event_id = await open_dinner(house, "Sam", "Lee")
    for name in ("Siam Garden", "Nowhere"):  # the same either way: nothing says which picks exist
        early = await tool(house, "draft_vote", vote_args(name), who="Sam")
        assert early == ToolResult("Voting is closed.", is_error=True)
    await act(house, calls.respond, house["Kim"], event_id, Status.OUT)
    house.clock.now = RESPOND_BY
    await house.tick()
    await nobody_coordinates(house, event_id)
    missing = await tool(house, "draft_vote", vote_args("Nowhere"), who="Sam")
    assert missing == ToolResult(call_drafts.NO_SUCH_PROPOSAL, is_error=True)
    not_in = await tool(house, "draft_vote", vote_args("Siam Garden", vote=False), who="Kim")
    assert not_in == ToolResult("Only people who are in can vote.", is_error=True)
    assert await rows(house) == []


# Suggestions ------------------------------------------------------------------------------------


async def test_a_suggestion_keeps_the_rest_of_the_members_details(house: House) -> None:
    event_id = await open_dinner(house)
    details = Details(
        eat_from=time(18, 0),
        suggestion_item_ids=(house.items["Taqueria"].id,),
        suggestion_texts=("Noodle Bar",),
        note="Running late",
    )
    await act(house, calls.save_details, house["Kim"], event_id, details)
    sams = Details(eat_until=time(20, 0), suggestion_item_ids=(house.items["Sam's Tacos"].id,), note="Sam's note")
    await act(house, calls.save_details, house["Sam"], event_id, sams)
    sam_before = (await house.context(event_id)).response_of(house["Sam"].id)
    draft = draft_id(await tool(house, "draft_suggestion", CALL | {"name": "siam garden"}, who="Kim"))
    assert await lines(house, draft) == [
        "It adds Siam Garden to your suggestions for today's dinner call.",
        "Your eating times, note, and other suggestions stay as they are.",
    ]
    assert await confirm(house, "Kim", draft) == ActionConfirmed(
        event_id, "Confirmed. Siam Garden is one of your suggestions."
    )
    ctx = await house.context(event_id)
    mine = ctx.response_of(house["Kim"].id)
    assert mine is not None and mine.eat_from == time(18, 0) and mine.note == "Running late"
    theirs = {(s.item_id, s.free_text) for s in ctx.suggestions if s.member_id == house["Kim"].id}
    assert theirs == {(house.items["Taqueria"].id, None), (None, "Noodle Bar"), (house.items["Siam Garden"].id, None)}
    sam_rows = {(s.item_id, s.free_text) for s in ctx.suggestions if s.member_id == house["Sam"].id}
    assert sam_rows == {(house.items["Sam's Tacos"].id, None)}
    assert ctx.response_of(house["Sam"].id) == sam_before
    lee = draft_id(await tool(house, "draft_suggestion", CALL | {"name": "Taqueria"}))
    assert (await lines(house, lee))[-1] == "You'll be in, as saving Details does."


async def test_a_suggestion_is_refused_as_the_service_would_refuse_it(house: House) -> None:
    event_id = await open_dinner(house, "Sam")
    ids = tuple(house.items[n].id for n in ("Taqueria", "Siam Garden", "Sam's Tacos"))
    await act(house, calls.save_details, house["Kim"], event_id, Details(suggestion_item_ids=ids[:2]))
    repeat = await tool(house, "draft_suggestion", CALL | {"name": "Taqueria"}, who="Kim")
    assert repeat == ToolResult(call_drafts.NOTHING_TO_DO, is_error=True)
    unknown = await tool(house, "draft_suggestion", CALL | {"name": "Nowhere"}, who="Kim")
    assert unknown == ToolResult(call_drafts.NO_SUCH_ITEM, is_error=True)
    await act(
        house,
        calls.save_details,
        house["Kim"],
        event_id,
        Details(suggestion_item_ids=ids[:2], suggestion_texts=("Pho",)),
    )
    full = await tool(house, "draft_suggestion", CALL | {"name": "Sam's Tacos"}, who="Kim")
    assert full == ToolResult("Suggest up to 3 things.", is_error=True)
    assert await rows(house) == []


# Roles ------------------------------------------------------------------------------------------


async def test_roles_while_collecting(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    event_id = await open_dinner(house)
    take = draft_id(await tool(house, "draft_role", role_args("cook"), who="Kim"))
    assert await lines(house, take) == ["You'll be down to cook for today's dinner call.", "You'll be in."]
    called = spy(monkeypatch, "offer_role", "join_late")
    assert await confirm(house, "Kim", take) == ActionConfirmed(
        event_id, "Confirmed. You're down to cook for today's dinner call."
    )
    give = draft_id(await tool(house, "draft_role", role_args("cook", offer=False), who="Kim"))
    assert await lines(house, give) == ["You'll no longer be down to cook for today's dinner call."]
    await confirm(house, "Kim", give)
    assert called == ["offer_role", "offer_role"]
    mine = (await house.context(event_id)).response_of(house["Kim"].id)
    assert mine is not None and mine.is_in and not mine.offers
    none = await tool(house, "draft_role", role_args("pickup", offer=False), who="Kim")
    assert none == ToolResult(call_drafts.NOTHING_TO_DO, is_error=True)


async def test_a_role_or_a_suggestion_is_refused_while_voting_as_the_service_would(house: House) -> None:
    await to_voting(house)
    closed = await tool(house, "draft_suggestion", CALL | {"name": "Taqueria"})
    assert closed == ToolResult("Responses are closed for this call.", is_error=True)
    given_up = await tool(house, "draft_role", role_args("pickup", offer=False))
    assert given_up == ToolResult("Responses are closed for this call.", is_error=True)
    on_time = await tool(house, "draft_role", role_args("order"), who="Kim")
    assert on_time == ToolResult("Responses are closed for this call.", is_error=True)
    assert await rows(house) == []


async def test_a_role_that_joins_late_rolls_the_late_join_back_if_the_role_is_refused(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``offer_role`` joins late and offers the role under one lock, so a refusal after the late
    join undoes it, leaves the draft waiting, and sends nothing; the draft confirms later."""
    event_id = await to_voting(house)
    draft = draft_id(await tool(house, "draft_role", role_args("pickup")))
    before = await house.outbox()
    real = calls.check_can_offer_role

    def refused(*args: Any, **kwargs: Any) -> None:
        raise EventRuleViolation("Responses are closed for this call.", code="responses_closed")

    monkeypatch.setattr(calls, "check_can_offer_role", refused)
    with pytest.raises(EventRuleViolation):
        await confirm(house, "Lee", draft)
    assert (await house.context(event_id)).response_of(house["Lee"].id) is None
    assert await house.outbox() == before
    [row] = await rows(house)
    assert row.used_at is None
    monkeypatch.setattr(calls, "check_can_offer_role", real)
    await confirm(house, "Lee", draft)
    mine = (await house.context(event_id)).response_of(house["Lee"].id)
    assert mine is not None and mine.late and RoleOffer.PICKUP in mine.offers


async def test_a_late_join_already_made_another_way_isnt_reported_as_new(house: House) -> None:
    event_id = await to_voting(house)
    joining = draft_id(await tool(house, "draft_answer", answer_args("in")))
    await act(house, calls.join_late, house["Lee"], event_id)
    assert await confirm(house, "Lee", joining) == ActionConfirmed(
        event_id, "You're in today's dinner call already, so nothing changed."
    )


# Taking back an offer ---------------------------------------------------------------------------


async def post(house: House, who: str, slot: Slot = Slot.LUNCH) -> int:
    data = OfferInput(
        slot=slot,
        day=DAY + timedelta(days=1),
        mode=Mode.PICKUP,
        at=time(12, 0),
        capacity=3,
        item_id=house.items["Taqueria"].id,
    )
    posted = await act(house, offers.post_offer, house[who], data)
    return int(posted.event_id)


async def test_taking_back_an_offer_calls_it_off_as_its_offerer(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    house.clock.now = OPENS
    offer_id = await post(house, "Sam")
    draft = draft_id(await tool(house, "draft_take_back_offer", {"slot": "", "day": ""}, who="Sam"))
    assert await lines(house, draft) == [
        "It calls off your offer of Taqueria for tomorrow's lunch.",
        "Dinnerbell tells everyone who joined it.",
    ]
    called = spy(monkeypatch, "cancel_call")
    assert await confirm(house, "Sam", draft) == ActionConfirmed(
        offer_id, "Confirmed. Your offer is called off.", offer=True
    )
    assert called == ["cancel_call"] and (await house.event(offer_id)).state is EventState.CANCELLED


async def test_only_the_members_own_offers_can_be_taken_back(house: House) -> None:
    house.clock.now = OPENS
    offer_id = await post(house, "Sam")
    for who in ("Kim", "Alex"):  # an admin may call it off on the web, but never through the assistant
        refused = await tool(house, "draft_take_back_offer", {"slot": "lunch", "day": "tomorrow"}, who=who)
        assert refused == ToolResult(call_drafts.NO_OPEN_OFFER, is_error=True)
    assert await rows(house) == []
    async with house.sessionmaker() as db:
        offer = await house.event(offer_id)
        spec = await call_drafts.offer_off(db, tools.tool_actor(house["Sam"]), offer, today=DAY)
        alexs = await drafts.store_action(db, house["Alex"], web_key(house["Alex"].id), spec, now=house.clock())
    with pytest.raises(EventRuleViolation, match="Only whoever made the offer, or an admin"):
        await confirm(house, "Alex", alexs.id)
    assert (await house.event(offer_id)).state.is_open


async def test_several_open_offers_ask_which(house: House) -> None:
    house.clock.now = OPENS
    await post(house, "Sam", Slot.LUNCH)
    await post(house, "Sam", Slot.BREAKFAST)
    which = await tool(house, "draft_take_back_offer", {"slot": "", "day": ""}, who="Sam")
    assert which.is_error and "several open offers" in which.content and "lunch" in which.content
    one = await tool(house, "draft_take_back_offer", {"slot": "breakfast", "day": "tomorrow"}, who="Sam")
    assert not one.is_error


# Looking up a call ------------------------------------------------------------------------------


async def test_look_up_call_gives_the_members_own_answer_and_the_picks_but_no_one_elses(house: House) -> None:
    event_id = await to_voting(house)
    siam = next(p for p in (await house.context(event_id)).picks if p.item_id == house.items["Siam Garden"].id)
    await act(house, calls.vote, house["Kim"], event_id, siam.id)
    result = await tool(house, "look_up_call", CALL, who="Sam")
    assert not result.is_error
    found = json.loads(result.content)
    assert found["you"] == {"answer": "in", "late": False, "roles": ["pickup"], "suggestions": []}
    assert {p["name"] for p in found["picks"]} == {"Siam Garden", "Taqueria"}
    assert not any(p["you_voted"] for p in found["picks"])
    for never in ("Kim", "Sam", "Lee", "Alex", "@example.com", "votes"):
        assert never not in result.content
    collecting = await open_dinner_tomorrow(house)
    early = json.loads((await tool(house, "look_up_call", {"slot": "dinner", "day": collecting})).content)
    assert early["you"]["answer"] == "none" and "picks" not in early


async def open_dinner_tomorrow(house: House) -> str:
    async with house.sessionmaker() as db:
        await calls.start_call(db, house["Kim"], Slot.DINNER, date(2026, 10, 23), now=house.clock())
    return "2026-10-23"


async def test_bad_flags_are_input_errors(house: House) -> None:
    await to_voting(house)
    result = await tool(
        house, "draft_vote", CALL | {"pick": 0, "name": "Siam Garden", "mode": "", "vote": "yes"}, who="Sam"
    )
    assert result == ToolResult("vote is true or false.", is_error=True)


async def test_look_up_call_lists_only_the_members_own_suggestions(house: House) -> None:
    event_id = await open_dinner(house)
    await act(
        house, calls.save_details, house["Kim"], event_id, Details(suggestion_item_ids=(house.items["Taqueria"].id,))
    )
    await act(
        house, calls.save_details, house["Sam"], event_id, Details(suggestion_item_ids=(house.items["Siam Garden"].id,))
    )
    found = json.loads((await tool(house, "look_up_call", CALL, who="Sam")).content)
    assert found["you"]["suggestions"] == ["Siam Garden"]
    lee = json.loads((await tool(house, "look_up_call", CALL)).content)
    assert lee["you"]["suggestions"] == []


IDEA = "Grandma Rosa's lasagna"


async def test_a_free_text_pick_reads_as_a_members_idea_and_takes_a_vote_by_its_number(house: House) -> None:
    event_id = await to_voting(house)
    mexican = next(t for t in (await house.context(event_id)).tags.values() if t.name == "Mexican")
    idea = await act(
        house, calls.add_proposal, house["Sam"], event_id, Mode.COOK, free_text=IDEA, cuisine_id=mexican.id
    )
    result = await tool(house, "look_up_call", CALL, who="Kim")
    assert IDEA not in result.content and "Grandma" not in result.content
    picks = json.loads(result.content)["picks"]
    assert {"pick": idea.id, "name": call_drafts.FREE_TEXT_PICK, "mode": "cook", "you_voted": False} in picks
    by_number = await tool(house, "draft_vote", vote_args(pick=idea.id), who="Kim")
    assert not by_number.is_error and "Grandma" not in by_number.content
    assert "Vote for a member's own idea (cook)" in by_number.content
    await confirm(house, "Kim", draft_id(by_number))
    assert house["Kim"].id in (await house.context(event_id)).votes[idea.id]
    by_name = await tool(house, "draft_vote", vote_args(IDEA.lower(), vote=False), who="Kim")  # the member's own words
    assert f"Take back your vote for {IDEA.lower()} (cook)" in by_name.content
    stranger = await tool(house, "draft_vote", vote_args(pick=idea.id + 1000), who="Kim")
    assert stranger == ToolResult(call_drafts.NO_SUCH_PROPOSAL, is_error=True)
# Guests (part 2) --------------------------------------------------------------------------------


def invite_args(name: str, *needs: str) -> dict[str, Any]:
    return CALL | {"name": name, "needs": list(needs)}


def guest_args(name: str, *, answer: str = "", role: str = "", offer: bool = True) -> dict[str, Any]:
    return CALL | {"name": name, "answer": answer, "role": role, "offer": offer}


async def test_inviting_a_new_guest_saves_them_with_the_needs_the_member_named(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    await house.tag("Vegetarian", TagKind.DIETARY)
    event_id = await open_dinner(house, "Kim")
    draft = draft_id(await tool(house, "draft_invite_guest", invite_args("Pat", "vegetarian"), who="Kim"))
    assert await lines(house, draft) == [
        "It saves Pat as a new guest and invites them to today's dinner call, and they're in.",
        "You answer for them from now on.",
        "Their dietary needs: Vegetarian.",
    ]
    assert [m.display_name for m in (await house.context(event_id)).members.values() if m.is_guest] == []
    called: list[str] = []
    real = guests.invite

    async def spied(*args: Any, **kwargs: Any) -> Any:
        called.append("invite")
        return await real(*args, **kwargs)

    monkeypatch.setattr(guests, "invite", spied)
    assert await confirm(house, "Kim", draft) == ActionConfirmed(
        event_id, "Confirmed. Pat is invited to today's dinner call."
    )
    assert called == ["invite"]
    ctx = await house.context(event_id)
    [pat] = [m for m in ctx.members.values() if m.is_guest]
    assert pat.display_name == "Pat" and ctx.invitations[pat.id] == house["Kim"].id
    assert pat.dietary_needs == frozenset({house.tags["Vegetarian"].id})
    mine = ctx.response_of(pat.id)
    assert mine is not None and mine.is_in


async def test_a_saved_guest_is_invited_by_name_and_refusals_follow_the_service(house: House) -> None:
    await house.tag("Vegetarian", TagKind.DIETARY)
    async with house.sessionmaker() as db:
        await guests.add_guest(db, house["Alex"], GuestInput("Robin"), now=house.clock())
    event_id = await open_dinner(house, "Kim", "Sam")
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    saved = draft_id(await tool(house, "draft_invite_guest", invite_args("robin"), who="Kim"))
    assert await lines(house, saved) == [
        "It invites robin to today's dinner call, and they're in.",
        "You answer for them from now on.",
    ]
    with_needs = await tool(house, "draft_invite_guest", invite_args("Robin", "Vegetarian"), who="Kim")
    assert with_needs.is_error and "new guest only" in with_needs.content
    unknown = await tool(house, "draft_invite_guest", invite_args("Pat", "Paleo"), who="Kim")
    assert unknown == ToolResult("Dietary needs are the household's dietary tags: Vegetarian.", is_error=True)
    await confirm(house, "Kim", saved)
    again = await tool(house, "draft_invite_guest", invite_args("Robin"), who="Sam")
    assert again == ToolResult(call_drafts.NOTHING_TO_DO, is_error=True)
    house.clock.now = RESPOND_BY
    await house.tick()
    closed = await tool(house, "draft_invite_guest", invite_args("Pat"), who="Kim")
    assert closed == ToolResult("Invite guests while answers are open.", is_error=True)


async def test_only_the_inviter_answers_for_a_guest_through_the_assistant(house: House) -> None:
    event_id = await open_dinner(house, "Kim")
    await act(house, guests.invite, house["Kim"], event_id, new_guest=GuestInput("Pat"))
    out = draft_id(await tool(house, "draft_guest_answer", guest_args("pat", answer="out"), who="Kim"))
    assert await lines(house, out) == ["pat will be out of today's dinner call."]
    for who in ("Sam", "Alex"):  # an admin may on the web, but never through the assistant
        refused = await tool(house, "draft_guest_answer", guest_args("Pat", answer="out"), who=who)
        assert refused == ToolResult(
            "Only whoever invited them, or an admin, answers for a guest without Slack.", is_error=True
        )
    missing = await tool(house, "draft_guest_answer", guest_args("Quinn", answer="in"), who="Kim")
    assert missing == ToolResult(call_drafts.NO_SUCH_GUEST, is_error=True)
    both = await tool(house, "draft_guest_answer", guest_args("Pat", answer="in", role="cook"), who="Kim")
    assert both.is_error and "not both" in both.content
    same = await tool(house, "draft_guest_answer", guest_args("Pat", answer="in"), who="Kim")
    assert same == ToolResult(call_drafts.NOTHING_TO_DO, is_error=True)
    role = draft_id(await tool(house, "draft_guest_answer", guest_args("Pat", role="pickup"), who="Kim"))
    assert await lines(house, role) == ["Pat will be down to pick up for today's dinner call, and in."]
    await confirm(house, "Kim", role)
    assert await confirm(house, "Kim", out) == ActionConfirmed(
        event_id, "Confirmed. pat's answer for today's dinner call is saved."
    )
    ctx = await house.context(event_id)
    [pat] = [m for m in ctx.members.values() if m.is_guest]
    answer = ctx.response_of(pat.id)
    assert answer is not None and answer.status is Status.OUT and not answer.offers


async def test_alexs_forged_answer_for_kims_guest_is_refused_at_confirm(house: House) -> None:
    """A draft can only be made by its member's own tools, but Confirm still acts as
    ``tool_actor``: a draft for Alex that answers for Kim's guest gets the service's refusal."""
    event_id = await open_dinner(house, "Kim")
    await act(house, guests.invite, house["Kim"], event_id, new_guest=GuestInput("Pat"))
    async with house.sessionmaker() as db:
        event = await house.event(event_id)
        spec = await call_drafts.guest_answer(
            db,
            tools.tool_actor(house["Kim"]),
            event,
            "Pat",
            status=Status.OUT,
            offered_role=None,
            offered=True,
            today=DAY,
            now=house.clock(),
        )
        alexs = await drafts.store_action(db, house["Alex"], web_key(house["Alex"].id), spec, now=house.clock())
    with pytest.raises(Forbidden):
        await confirm(house, "Alex", alexs.id)


# Ad-hoc calls (part 2) --------------------------------------------------------------------------


def call_args(slot: str, day: str) -> dict[str, Any]:
    return {"slot": slot, "day": day}


async def test_starting_a_call_drafts_then_starts_it_through_start_call(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    house.clock.now = OPENS
    draft = draft_id(await tool(house, "draft_call", call_args("lunch", "tomorrow"), who="Kim"))
    assert await lines(house, draft) == [
        "It starts a lunch call for tomorrow, which opens at the usual time.",
        "You'll be its starter, so you can call it off until it's decided.",
    ]
    async with house.sessionmaker() as db:
        assert await EventRepository(db).call_for(house.household.id, Slot.LUNCH, DAY + timedelta(days=1)) is None
    called = spy(monkeypatch, "start_call")
    done = await confirm(house, "Kim", draft)
    assert called == ["start_call"]
    assert isinstance(done, ActionConfirmed) and done.text == "Confirmed. The lunch call for tomorrow is started."
    event = await house.event(done.event_id)
    assert (event.slot, event.day, event.created_by) == (Slot.LUNCH, DAY + timedelta(days=1), house["Kim"].id)
    with pytest.raises(DraftUnavailable, match=USED):
        await confirm(house, "Kim", draft)
    again = await tool(house, "draft_call", call_args("lunch", "tomorrow"))
    assert again == ToolResult("There's already a lunch call for that day.", is_error=True)


async def test_a_call_someone_started_meanwhile_leaves_the_draft_unused(house: House) -> None:
    house.clock.now = OPENS
    draft = draft_id(await tool(house, "draft_call", call_args("breakfast", "tomorrow"), who="Kim"))
    await act(house, calls.start_call, house["Sam"], Slot.BREAKFAST, DAY + timedelta(days=1))
    with pytest.raises(EventRuleViolation, match="already a breakfast call"):
        await confirm(house, "Kim", draft)
    [row] = await rows(house)
    assert row.used_at is None


async def test_a_called_off_call_starts_again_on_its_run(house: House) -> None:
    event_id = await open_dinner(house, "Kim")
    await act(house, calls.cancel_call, house["Alex"], event_id)
    draft = draft_id(await tool(house, "draft_call", call_args("dinner", "today"), who="Kim"))
    assert (await lines(house, draft))[0] == "It starts the called-off dinner call for today again."
    await act(house, calls.restart_call, house["Sam"], event_id)
    await act(house, calls.cancel_call, house["Sam"], event_id)  # a newer run, called off too
    with pytest.raises(EventRuleViolation, match=STALE_CALL):
        await confirm(house, "Kim", draft)
    fresh = draft_id(await tool(house, "draft_call", call_args("dinner", "today"), who="Kim"))
    await confirm(house, "Kim", fresh)
    assert (await house.event(event_id)).state is EventState.COLLECTING


async def test_a_call_is_refused_as_start_call_would_refuse_it_without_naming_a_cook(house: House) -> None:
    house.clock.now = OPENS
    cook = OfferInput(
        slot=Slot.DINNER,
        day=DAY + timedelta(days=2),
        mode=Mode.COOK,
        at=time(18, 30),
        capacity=3,
        item_id=house.items["Sam's Tacos"].id,
    )
    await act(house, offers.post_offer, house["Sam"], cook)
    held = await tool(house, "draft_call", call_args("dinner", (DAY + timedelta(days=2)).isoformat()), who="Kim")
    assert held == ToolResult(call_drafts.COOKING_OFFER, is_error=True) and "Sam" not in held.content
    past = await tool(house, "draft_call", call_args("lunch", "2026-10-21"))
    assert past == ToolResult("You can start a call for today or a later day.", is_error=True)
    far = await tool(house, "draft_call", call_args("lunch", "2026-11-22"))
    assert far == ToolResult("You can start a call up to 7 days ahead.", is_error=True)
    assert await rows(house) == []


async def test_the_lines_for_each_guest_answer_and_call_say_what_confirm_does() -> None:
    from dinnerbell.domain.assistant_drafts import call_lines, guest_answer_lines, invite_lines

    common: dict[str, Any] = {"slot": Slot.DINNER, "when": "today"}
    assert invite_lines("Pat", saved=True, registered=True, needs=(), **common) == (
        "It invites Pat to today's dinner call. They're told, and answer themselves.",
    )
    answers: dict[str, Any] = {"role": None, "offered": True, "withdraws_roles": False, **common}
    assert guest_answer_lines("Pat", status=Status.IN, **answers) == ("Pat will be in for today's dinner call.",)
    out = guest_answer_lines("Pat", status=Status.OUT, **(answers | {"withdraws_roles": True}))
    assert out == ("Pat will be out of today's dinner call.", "Roles offered for them are withdrawn.")
    given_up = guest_answer_lines("Pat", status=None, **(answers | {"role": RoleOffer.ORDER, "offered": False}))
    assert given_up == ("Pat will no longer be down to order for today's dinner call.",)
    assert (
        call_lines(now=True, restart=False, **common)[0]
        == "It starts today's dinner call now, and Dinnerbell posts it."
    )


async def test_guest_input_errors_go_back_to_the_model(house: House) -> None:
    await open_dinner(house, "Kim")
    empty = await tool(house, "draft_invite_guest", invite_args("  "), who="Kim")
    assert empty == ToolResult("name is the guest's name, as the member wrote it.", is_error=True)
    many = await tool(house, "draft_invite_guest", invite_args("Pat", *"abcdef"), who="Kim")
    assert many == ToolResult("needs is a list of at most 5 strings.", is_error=True)
    long = await tool(house, "draft_invite_guest", invite_args("Pat", "x" * 81), who="Kim")
    assert long == ToolResult("Each of needs is at most 80 characters.", is_error=True)


async def test_two_saved_guests_with_one_name_are_left_to_the_call_page(house: House) -> None:
    async with house.sessionmaker() as db:
        for _ in range(2):
            await guests.add_guest(db, house["Alex"], GuestInput("Robin"), now=house.clock())
    await open_dinner(house, "Kim")
    assert await tool(house, "draft_invite_guest", invite_args("Robin"), who="Kim") == ToolResult(
        call_drafts.SAME_NAME, is_error=True
    )


async def test_a_dietary_tag_deleted_before_confirm_shows_on_the_card(house: House) -> None:
    vegetarian = await house.tag("Vegetarian", TagKind.DIETARY)
    await open_dinner(house, "Kim")
    draft = draft_id(await tool(house, "draft_invite_guest", invite_args("Pat", "Vegetarian"), who="Kim"))
    async with house.sessionmaker() as db:
        await db.execute(delete(TagRow).where(TagRow.id == vegetarian.id))
        await db.commit()
    with pytest.raises(EventRuleViolation, match="Dietary needs must be dietary tags"):
        await confirm(house, "Kim", draft)
    [row] = await rows(house)
    assert row.used_at is None
