# SPDX-License-Identifier: AGPL-3.0-or-later
"""Call use cases against a migrated database: abandon paths, the coordinator-less and no-vote
paths, ties, locking, proposals, every admin override, authorization, and household isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from dinnerbell.config import Settings
from dinnerbell.db.models import CatalogItemRow, ProposalRow, ResponseRow
from dinnerbell.domain.catalog import Mode, TagKind
from dinnerbell.domain.events import (
    AssignmentRole,
    Details,
    EndReason,
    EventRuleViolation,
    EventState,
    IllegalTransition,
    InvalidResponse,
    RoleOffer,
    Status,
    Step,
    Window,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.services import call_admin, calls, event_views
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.preferences import group_preferences, set_my_preference
from tests.support.calls import OPENS, RESPOND_BY, VOTE_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def act(house: House, use_case: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


async def open_dinner(house: House) -> int:
    house.clock.now = OPENS
    await house.tick()
    return await dinner_id(house)


async def to_voting(house: House, *, pickup: str = "Alex", eaters: tuple[str, ...] = ("Alex", "Sam")) -> int:
    event_id = await open_dinner(house)
    for name in eaters:
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house[pickup], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    return event_id


async def proposal_ids(house: House, event_id: int) -> dict[str, int]:
    ctx = await house.context(event_id)
    return {ctx.items[p.item_id].name if p.item_id else (p.free_text or ""): p.id for p in ctx.proposals}


# Abandon paths ---------------------------------------------------------------------------------


async def test_fewer_than_two_people_abandons(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.respond, house["Sam"], event_id, Status.MAYBE)  # maybes don't count
    house.clock.now = RESPOND_BY
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    event = await house.event(event_id)
    assert (event.state, event.end_reason) == (EventState.ABANDONED, EndReason.TOO_FEW)
    assert await house.kinds() == ["call_opened", "call_updated", "maybe_resolved", "call_abandoned"]
    assert await house.meals() == []
    assert (await house.audits())[-1].payload == {"reason": "too_few"}


async def test_one_person_with_a_guest_is_enough(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.save_details, house["Alex"], event_id, Details(guests=1))
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING


async def test_nothing_covered_abandons(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.respond, house["Kim"], event_id, Status.IN)  # nobody cooks, picks up, or orders
    house.clock.now = RESPOND_BY
    await house.tick()
    event = await house.event(event_id)
    assert (event.state, event.end_reason) == (EventState.ABANDONED, EndReason.NO_COVERAGE)
    assert "proposals_ready" not in await house.kinds()


async def test_delivery_needs_a_participant_who_can_order(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.respond, house["Lee"], event_id, Status.IN)  # Lee can order
    house.clock.now = RESPOND_BY
    await house.tick()
    assert list(await proposal_ids(house, event_id)) == ["Siam Garden"]
    ctx = await house.context(event_id)
    assert ctx.proposals[0].mode is Mode.DELIVERY


async def test_coverage_lost_before_the_vote_deadline_abandons(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.COOK, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert list(await proposal_ids(house, event_id)) == ["Sam's Tacos"]
    async with house.sessionmaker() as db:  # the only cook is deactivated mid-vote
        await db.execute(text("UPDATE member SET active = 0 WHERE id = :id"), {"id": house["Sam"].id})
        await db.commit()
    house.clock.now = VOTE_BY
    await house.tick()
    event = await house.event(event_id)
    assert (event.state, event.end_reason) == (EventState.ABANDONED, EndReason.NO_COVERAGE)
    entries = await house.outbox()
    assert [e.kind for e in entries][-2:] == ["call_abandoned", "proposals_updated"]
    # The call message's pending update was coalesced, so it now carries the final state.
    (call,) = [e for e in entries if e.kind == "call_updated"]
    assert call.payload["event"]["state"] == "abandoned"


# Deciding --------------------------------------------------------------------------------------


async def test_no_coordinator_and_no_votes_decides_the_top_ranked(house: House) -> None:
    event_id = await to_voting(house)
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.coordinator_id is None
    assert ctx.items[ctx.event.decided_item_id or 0].name == "Siam Garden"  # rank 1: alphabetical tie
    assert [(a.role, a.member_id) for a in ctx.assignments] == [(AssignmentRole.PICKUP, house["Alex"].id)]
    decided = (await house.audits())[-1]
    assert (decided.action, decided.actor_member_id, decided.payload["how"]) == ("event.decided", None, "vote")


async def test_a_tie_goes_to_the_coordinators_pick(house: House) -> None:
    event_id = await to_voting(house)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    ids = await proposal_ids(house, event_id)
    await act(house, calls.vote, house["Alex"], event_id, ids["Siam Garden"])
    await act(house, calls.vote, house["Sam"], event_id, ids["Taqueria"])
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.items[ctx.event.decided_item_id or 0].name == "Taqueria"
    assert {(a.role, a.member_id) for a in ctx.assignments} == {
        (AssignmentRole.PICKUP, house["Alex"].id),
        (AssignmentRole.COORDINATOR, house["Sam"].id),
    }


async def test_a_tie_goes_to_the_admins_pick_without_a_coordinator(house: House) -> None:
    event_id = await to_voting(house)
    ids = await proposal_ids(house, event_id)
    await act(house, calls.vote, house["Sam"], event_id, ids["Siam Garden"])
    await act(house, calls.vote, house["Alex"], event_id, ids["Taqueria"])  # Alex is the admin
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.items[ctx.event.decided_item_id or 0].name == "Taqueria"


async def test_most_votes_win_and_unvoting_counts(house: House) -> None:
    event_id = await to_voting(house)
    ids = await proposal_ids(house, event_id)
    await act(house, calls.vote, house["Sam"], event_id, ids["Taqueria"])
    await act(house, calls.vote, house["Alex"], event_id, ids["Taqueria"])
    await act(house, calls.vote, house["Alex"], event_id, ids["Taqueria"])  # twice: still one vote
    await act(house, calls.vote, house["Alex"], event_id, ids["Siam Garden"])  # several proposals is fine
    await act(house, calls.unvote, house["Sam"], event_id, ids["Taqueria"])
    await act(house, calls.unvote, house["Sam"], event_id, ids["Taqueria"])  # no vote to remove
    await act(house, calls.vote, house["Sam"], event_id, ids["Siam Garden"])
    ctx = await house.context(event_id)
    assert {p: sorted(v) for p, v in ctx.votes.items()} == {
        ids["Taqueria"]: [house["Alex"].id],
        ids["Siam Garden"]: sorted([house["Alex"].id, house["Sam"].id]),
    }
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.items[ctx.event.decided_item_id or 0].name == "Siam Garden"


async def test_who_may_vote(house: House) -> None:
    event_id = await to_voting(house)
    ids = await proposal_ids(house, event_id)
    with pytest.raises(EventRuleViolation, match="Only people who are in"):
        await act(house, calls.vote, house["Kim"], event_id, ids["Taqueria"])
    with pytest.raises(NotFound, match="proposal"):
        await act(house, calls.vote, house["Alex"], event_id, 999_999)
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Kim"].id)
    await act(house, calls.vote, house["Kim"], event_id, ids["Taqueria"])  # the coordinator votes


async def test_the_coordinator_locks_early(house: House) -> None:
    event_id = await to_voting(house)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    ids = await proposal_ids(house, event_id)
    with pytest.raises(EventRuleViolation, match="coordinator or an admin"):
        await act(house, calls.lock, house["Kim"], event_id, ids["Taqueria"])
    house.clock.now = RESPOND_BY + timedelta(minutes=10)
    await act(house, calls.lock, house["Sam"], event_id, ids["Taqueria"])
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.DECIDED and ctx.event.decided_at == house.clock()
    entries = await house.outbox()
    made = next(e for e in entries if e.kind == "decision_made")
    assert made.payload["locked_by"] == {"id": house["Sam"].id, "name": "Sam"}
    locked = (await house.audits())[-1]
    assert (locked.action, locked.actor_member_id, locked.payload["how"]) == (
        "event.decided",
        house["Sam"].id,
        "locked",
    )
    with pytest.raises(EventRuleViolation, match="Voting is closed"):
        await act(house, calls.lock, house["Sam"], event_id, ids["Siam Garden"])
    house.clock.now = VOTE_BY
    assert await house.tick() == []  # nothing left to decide


async def test_the_admin_locks_and_a_lock_respects_a_new_veto(house: House) -> None:
    event_id = await to_voting(house)
    ids = await proposal_ids(house, event_id)
    thai = house.tags["Thai"].id
    await act(house, set_my_preference, house["Sam"], TargetType.TAG, thai, Stance(veto=True))
    with pytest.raises(EventRuleViolation, match="doesn't work for everyone"):
        await act(house, calls.lock, house["Alex"], event_id, ids["Siam Garden"])
    await act(house, calls.lock, house["Alex"], event_id, ids["Taqueria"])
    assert (await house.event(event_id)).state is EventState.DECIDED


async def test_a_decision_skips_proposals_vetoed_since_ranking(house: House) -> None:
    event_id = await to_voting(house)
    ids = await proposal_ids(house, event_id)
    await act(house, calls.vote, house["Alex"], event_id, ids["Siam Garden"])
    await act(house, set_my_preference, house["Sam"], TargetType.ITEM, house.items["Siam Garden"].id, Stance(veto=True))
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.items[ctx.event.decided_item_id or 0].name == "Taqueria"


# Proposals -------------------------------------------------------------------------------------


async def test_members_add_catalog_and_free_text_proposals(house: House) -> None:
    event_id = await to_voting(house)
    taqueria = house.items["Taqueria"].id
    with pytest.raises(EventRuleViolation, match="already proposed"):
        await act(house, calls.add_proposal, house["Kim"], event_id, Mode.PICKUP, item_id=taqueria)
    with pytest.raises(EventRuleViolation, match="isn't available by delivery"):
        await act(house, calls.add_proposal, house["Kim"], event_id, Mode.DELIVERY, item_id=taqueria)
    with pytest.raises(EventRuleViolation, match="Nobody who's in offered to order"):
        await act(
            house, calls.add_proposal, house["Kim"], event_id, Mode.DELIVERY, item_id=house.items["Siam Garden"].id
        )
    with pytest.raises(EventRuleViolation, match="Pick a cuisine"):
        await act(house, calls.add_proposal, house["Kim"], event_id, Mode.PICKUP, free_text="Pho Place")
    with pytest.raises(EventRuleViolation, match="Enter a name"):
        await act(house, calls.add_proposal, house["Kim"], event_id, Mode.PICKUP, free_text="  ", cuisine_id=1)
    with pytest.raises(EventRuleViolation, match="isn't in the catalog"):
        await act(house, calls.add_proposal, house["Kim"], event_id, Mode.PICKUP, item_id=999_999)
    vietnamese = await house.tag("Vietnamese")
    added = await act(
        house,
        calls.add_proposal,
        house["Kim"],
        event_id,
        Mode.PICKUP,
        free_text=" Pho  Place ",
        cuisine_id=vietnamese.id,
    )
    assert (added.free_text, added.rank, added.reasons, added.proposed_by) == (
        "Pho Place",
        3,
        ("Added by Kim.",),
        house["Kim"].id,
    )
    with pytest.raises(EventRuleViolation, match="already proposed"):
        await act(
            house,
            calls.add_proposal,
            house["Sam"],
            event_id,
            Mode.PICKUP,
            free_text="pho place",
            cuisine_id=vietnamese.id,
        )
    # Free text naming a catalog item proposes that item.
    with pytest.raises(EventRuleViolation, match="already proposed"):
        await act(
            house,
            calls.add_proposal,
            house["Sam"],
            event_id,
            Mode.PICKUP,
            free_text="taqueria",
            cuisine_id=vietnamese.id,
        )
    assert (await house.kinds())[-1] == "proposals_updated"


async def test_a_winning_free_text_proposal_joins_the_catalog(house: House) -> None:
    event_id = await to_voting(house)
    vietnamese = await house.tag("Vietnamese")
    pho = await act(
        house, calls.add_proposal, house["Sam"], event_id, Mode.PICKUP, free_text="Pho Place", cuisine_id=vietnamese.id
    )
    await act(house, calls.vote, house["Sam"], event_id, pho.id)
    house.clock.now = VOTE_BY
    await house.tick()
    async with house.sessionmaker() as db:
        row = await db.scalar(select(CatalogItemRow).where(CatalogItemRow.name == "Pho Place"))
        proposal = await db.get(ProposalRow, pho.id)
    assert row is not None and proposal is not None
    assert (row.kind, row.modes, row.created_by) == ("restaurant", ["pickup"], house["Sam"].id)
    assert (proposal.item_id, proposal.free_text) == (row.id, None)
    event = await house.event(event_id)
    assert (event.decided_item_id, event.decided_mode) == (row.id, Mode.PICKUP)
    assert "catalog.item_created" in [a.action for a in await house.audits("catalog.")]
    decision = next(e for e in await house.outbox() if e.kind == "decision_made")
    assert decision.payload["decision"]["proposal"]["item"]["name"] == "Pho Place"


async def test_proposals_respect_vetoes_and_guest_needs_without_naming_anyone(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    """FR-29: every veto or dietary refusal reads the same, whichever tag, item, or member caused
    it, so a proposer can't probe their way to who vetoed what."""
    import logging

    caplog.set_level(logging.DEBUG, logger="dinnerbell")
    vegan = await house.tag("Vegan", TagKind.DIETARY)
    korean = await house.tag("Korean")
    await house.add_item("Green Bowl", Mode.PICKUP, cuisine="Thai", tags=("Vegan",))
    await house.add_item("Seoul Food", Mode.PICKUP, cuisine="Korean", tags=("Vegan",))
    event_id = await open_dinner(house)
    for name in ("Alex", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.save_details, house["Kim"], event_id, Details(guests=1, guest_needs=frozenset({vegan.id})))
    house.clock.now = RESPOND_BY
    await house.tick()
    assert sorted(await proposal_ids(house, event_id)) == ["Green Bowl", "Seoul Food"]
    # Added after ranking, so they aren't proposed yet; both meet the guest's need.
    await house.add_item("Kimchi House", Mode.PICKUP, cuisine="Korean", tags=("Vegan",))
    await house.add_item("Leaf", Mode.PICKUP, cuisine="Thai", tags=("Vegan",))
    await act(house, set_my_preference, house["Kim"], TargetType.TAG, korean.id, Stance(veto=True))
    await act(house, set_my_preference, house["Lee"], TargetType.ITEM, house.items["Leaf"].id, Stance(veto=True))
    refusals = [
        (house["Alex"], {"item_id": house.items["Taqueria"].id}),  # a guest's dietary need
        (house["Alex"], {"free_text": "Jade", "cuisine_id": house.tags["Thai"].id}),  # free text with a guest need
        (house["Alex"], {"item_id": house.items["Kimchi House"].id}),  # Kim's tag veto
        (house["Kim"], {"item_id": house.items["Leaf"].id}),  # Lee's item veto
        (house["Sam"], {"free_text": "Bibim", "cuisine_id": korean.id}),  # Kim's veto, on free text
    ]
    messages = set()
    for proposer, target in refusals:
        with pytest.raises(EventRuleViolation) as caught:
            await act(house, calls.add_proposal, proposer, event_id, Mode.PICKUP, **target)
        messages.add(str(caught.value))
    assert messages == {"That doesn't work for everyone who's in tonight."}
    ours = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records if r.name.startswith("dinnerbell"))
    for name in ("Kim", "Lee", "Korean", "Vegan", "Leaf"):
        assert name not in " ".join(messages)
        assert name not in ours
    # Coverage reasons stay specific: they reveal nothing private.
    curry = await house.add_item("Sprout Curry", Mode.COOK, cuisine="Thai", tags=("Vegan",))
    with pytest.raises(EventRuleViolation, match="Nobody who's in offered to cook"):
        await act(house, calls.add_proposal, house["Alex"], event_id, Mode.COOK, item_id=curry.id)


async def test_no_proposals_outside_voting(house: House) -> None:
    event_id = await open_dinner(house)
    with pytest.raises(EventRuleViolation, match="only while voting"):
        await act(house, calls.add_proposal, house["Alex"], event_id, Mode.PICKUP, item_id=house.items["Taqueria"].id)


# Responding ------------------------------------------------------------------------------------


async def test_responses_and_details(house: House) -> None:
    event_id = await open_dinner(house)
    sam = house["Sam"]
    await act(house, calls.offer_role, sam, event_id, RoleOffer.COOK, True)
    await act(house, calls.offer_role, sam, event_id, RoleOffer.ORDER, False)
    await act(house, calls.offer_role, house["Kim"], event_id, RoleOffer.ORDER, False)  # nothing to withdraw
    vegan = await house.tag("Vegan", TagKind.DIETARY)
    details = Details(
        guests=2,
        guest_needs=frozenset({vegan.id}),
        eat_from=time(18),
        eat_until=time(19, 30),
        suggestion_item_ids=(house.items["Taqueria"].id,),
        suggestion_texts=("Ramen",),
        note="running late",
    )
    await act(house, calls.save_details, sam, event_id, details)
    await act(house, calls.save_details, sam, event_id, details)  # suggestions replace, not pile up
    ctx = await house.context(event_id)
    mine = ctx.response_of(sam.id)
    assert mine is not None
    assert (mine.status, mine.offers, mine.guests, mine.note) == (Status.IN, {RoleOffer.COOK}, 2, "running late")
    assert [(s.item_id, s.free_text) for s in ctx.suggestions] == [(house.items["Taqueria"].id, None), (None, "Ramen")]
    call = next(e for e in await house.outbox() if e.kind == "call_updated")
    (entry,) = call.payload["roster"]
    assert entry["guest_needs"] == ["Vegan"] and entry["suggestions"] == ["Taqueria", "Ramen"]
    with pytest.raises(InvalidResponse, match="dietary"):
        await act(house, calls.save_details, sam, event_id, Details(guest_needs=frozenset({house.tags["Thai"].id})))
    await act(house, calls.respond, sam, event_id, Status.OUT)
    assert (await house.context(event_id)).response_of(sam.id).offers == frozenset()  # type: ignore[union-attr]


async def test_responses_close_at_the_deadline(house: House) -> None:
    event_id = await to_voting(house)
    with pytest.raises(EventRuleViolation, match="closed"):
        await act(house, calls.respond, house["Kim"], event_id, Status.IN)
    with pytest.raises(EventRuleViolation, match="closed"):
        await act(house, calls.save_details, house["Kim"], event_id, Details())


async def test_reminders_skip_maybes_and_muted_members_and_go_once(house: House) -> None:
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE member SET mute_dm_reminders = 1 WHERE id = :id"), {"id": house["Lee"].id})
        await db.commit()
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Kim"], event_id, Status.MAYBE)
    house.clock.now = RESPOND_BY - timedelta(minutes=15)
    await house.tick()
    await house.tick()
    reminders = [e for e in await house.outbox() if e.kind == "response_reminder"]
    assert sorted(e.payload["member"]["name"] for e in reminders) == ["Alex", "Sam"]


# Coordinating ----------------------------------------------------------------------------------


async def test_claim_hand_off_and_admin_assignment(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    with pytest.raises(EventRuleViolation, match="already coordinating"):
        await act(house, calls.claim_coordinator, house["Sam"], event_id)
    with pytest.raises(EventRuleViolation, match="Only the coordinator"):
        await act(house, calls.hand_off, house["Sam"], event_id, house["Sam"].id)
    with pytest.raises(NotFound, match="member"):
        await act(house, calls.hand_off, house["Kim"], event_id, 999_999)
    await act(house, calls.hand_off, house["Kim"], event_id, house["Sam"].id)
    assert (await house.event(event_id)).coordinator_id == house["Sam"].id
    with pytest.raises(Forbidden):
        await act(house, call_admin.assign_coordinator, house["Sam"], event_id, house["Kim"].id)
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, None)
    assert (await house.event(event_id)).coordinator_id is None
    with pytest.raises(NotFound):
        await act(house, call_admin.assign_coordinator, house["Alex"], event_id, 999_999)
    actions = [a.action for a in await house.audits()][1:]
    assert actions == ["event.coordinator_claimed", "event.coordinator_handed_off", "event.coordinator_assigned"]


async def test_coordinator_changes_after_the_decision_update_the_assignment(house: House) -> None:
    event_id = await to_voting(house)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = VOTE_BY
    await house.tick()
    await act(house, calls.hand_off, house["Sam"], event_id, house["Kim"].id)
    ctx = await house.context(event_id)
    assert {(a.role, a.member_id) for a in ctx.assignments} >= {(AssignmentRole.COORDINATOR, house["Kim"].id)}
    dms = [e for e in await house.outbox() if e.kind == "assignment_made"]
    assert [d.payload["member"]["name"] for d in dms][-1] == "Kim"
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, None)
    ctx = await house.context(event_id)
    assert AssignmentRole.COORDINATOR not in {a.role for a in ctx.assignments}


# After the decision -----------------------------------------------------------------------------


async def decided(house: House) -> int:
    event_id = await to_voting(house)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = VOTE_BY
    await house.tick()
    return event_id


async def test_meal_time_and_assignment_changes(house: House) -> None:
    event_id = await decided(house)
    await act(house, calls.set_meal_time, house["Sam"], event_id, time(19, 10))
    event = await house.event(event_id)
    assert event.meal_time is not None and event.meal_time.hour == 0 and event.meal_time.minute == 10  # 19:10 CDT
    with pytest.raises(EventRuleViolation, match="coordinator or an admin"):
        await act(house, calls.set_meal_time, house["Kim"], event_id, time(20))
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Sam"].id)
    with pytest.raises(NotFound, match="member"):
        await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, 999_999)
    with pytest.raises(EventRuleViolation, match="hand off"):
        await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.COORDINATOR, house["Sam"].id)
    ctx = await house.context(event_id)
    assert (AssignmentRole.PICKUP, house["Sam"].id) in {(a.role, a.member_id) for a in ctx.assignments}
    assert (await house.outbox())[-1].payload["member"]["name"] == "Sam"  # the new assignee's message
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, None)
    assert AssignmentRole.PICKUP not in {a.role for a in (await house.context(event_id)).assignments}
    actions = [a.action for a in await house.audits()]
    assert actions[-3:] == ["event.meal_time_changed", "event.assignment_changed", "event.assignment_changed"]


async def test_marking_done_logs_the_meal_once(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(EventRuleViolation, match="someone at the meal"):
        await act(house, calls.mark_done, house["Kim"], event_id)
    with pytest.raises(EventRuleViolation, match="once it's meal time"):
        await act(house, calls.mark_done, house["Sam"], event_id)
    view = await act(house, event_views.event_view, house["Sam"], event_id)
    assert not view.can.mark_done
    await act(house, calls.mark_done, house["Alex"], event_id)
    with pytest.raises(EventRuleViolation, match="decided"):
        await act(house, calls.mark_done, house["Sam"], event_id)
    with pytest.raises(EventRuleViolation, match="already done"):
        await act(house, calls.set_meal_time, house["Alex"], event_id, time(20))
    (meal,) = await house.meals()
    assert meal.event_id == event_id
    done = (await house.audits())[-1]
    assert (done.action, done.actor_member_id) == ("event.done", house["Alex"].id)
    house.clock.now += timedelta(days=1)
    assert await house.tick() == []
    # The admin corrects an assignment after the fact; the coordinator no longer can.
    with pytest.raises(EventRuleViolation, match="no decision"):
        await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Sam"].id)
    await act(house, calls.change_assignment, house["Alex"], event_id, AssignmentRole.PICKUP, house["Sam"].id)
    assert (await house.outbox())[-1].kind == "decision_updated"  # no assignment message for a past meal


async def test_the_meal_log_rejects_a_second_meal_for_one_event(house: House) -> None:
    from sqlalchemy.exc import IntegrityError

    event_id = await decided(house)
    await act(house, calls.mark_done, house["Alex"], event_id)
    async with house.sessionmaker() as db:
        with pytest.raises(IntegrityError, match="UNIQUE"):
            await db.execute(
                text(
                    "INSERT INTO meal_log (household_id, date, slot, item_id, cuisine_tags, participants, event_id, "
                    "created_at, updated_at) SELECT household_id, date, slot, item_id, cuisine_tags, participants, "
                    "event_id, created_at, updated_at FROM meal_log"
                )
            )


# Admin overrides (FR-6, spec 5.7) --------------------------------------------------------------


async def test_extend_shorten_and_close_windows(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.RESPOND, 30)
    assert (await house.event(event_id)).respond_deadline == RESPOND_BY + timedelta(minutes=30)
    await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.RESPOND, -15)
    assert (await house.event(event_id)).respond_deadline == RESPOND_BY + timedelta(minutes=15)
    with pytest.raises(EventRuleViolation, match="isn't open"):
        await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.VOTE, 15)
    with pytest.raises(Forbidden):
        await act(house, call_admin.move_deadline, house["Sam"], event_id, Window.RESPOND, 15)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = OPENS + timedelta(minutes=20)
    await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.RESPOND, None)  # close now
    event = await house.event(event_id)
    assert event.state is EventState.VOTING  # advanced immediately, not at the next tick
    assert event.vote_deadline == house.clock() + timedelta(minutes=45)
    await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.VOTE, -240)  # in the past: closes
    assert (await house.event(event_id)).state is EventState.DECIDED
    moves = [a.payload for a in await house.audits() if a.action == "event.deadline_moved"]
    assert [m["window"] for m in moves] == ["respond", "respond", "respond", "vote"]


async def test_cancel_from_every_open_state(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(Forbidden):
        await act(house, call_admin.cancel, house["Sam"], event_id)
    await act(house, call_admin.cancel, house["Alex"], event_id)
    event = await house.event(event_id)
    assert event.state is EventState.CANCELLED
    cancelled = next(e for e in await house.outbox() if e.kind == "event_cancelled")
    assert cancelled.payload["cancelled_by"]["name"] == "Alex"
    assert [p["name"] for p in cancelled.payload["participants"]] == ["Alex", "Sam"]
    assert cancelled.thread_key == f"event:{event_id}:run1:call"
    with pytest.raises(IllegalTransition):
        await act(house, call_admin.cancel, house["Alex"], event_id)
    house.clock.now = VOTE_BY + timedelta(hours=6)
    assert await house.tick() == []
    assert await house.meals() == []


async def test_cancel_a_scheduled_ad_hoc_call(house: House) -> None:
    house.clock.now = OPENS - timedelta(hours=3)
    tomorrow = date(2026, 10, 23)
    event = await act(house, calls.start_call, house["Kim"], Slot.DINNER, tomorrow)
    assert event.state is EventState.SCHEDULED
    await act(house, call_admin.cancel, house["Alex"], event.id)
    assert (await house.event(event.id)).state is EventState.CANCELLED


async def test_rerun_an_abandoned_call(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.respond, house["Kim"], event_id, Status.MAYBE)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.ABANDONED
    with pytest.raises(Forbidden):
        await act(house, call_admin.rerun, house["Sam"], event_id)
    house.clock.now = RESPOND_BY + timedelta(minutes=5)
    await act(house, call_admin.rerun, house["Alex"], event_id)
    ctx = await house.context(event_id)
    event = ctx.event
    assert (event.state, event.run, event.end_reason, event.reminder_sent_at) == (EventState.COLLECTING, 2, None, None)
    assert event.respond_deadline == house.clock() + timedelta(minutes=90)
    assert ctx.response_of(house["Kim"].id).status is Status.MAYBE  # type: ignore[union-attr]
    entries = await house.outbox()
    assert (entries[-1].kind, entries[-1].idempotency_key) == ("call_opened", f"event:{event_id}:run2:call_opened")
    assert entries[-1].message_key == f"event:{event_id}:run2:call"


async def test_rerun_a_decided_call_clears_the_decision(house: House) -> None:
    event_id = await decided(house)
    await act(house, call_admin.rerun, house["Alex"], event_id)
    ctx = await house.context(event_id)
    assert ctx.proposals == [] and ctx.assignments == [] and ctx.votes == {}
    assert (ctx.event.decided_item_id, ctx.event.meal_time, ctx.event.vote_deadline) == (None, None, None)
    house.clock.now = ctx.event.respond_deadline
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING


async def test_rerun_rules(house: House) -> None:
    event_id = await open_dinner(house)
    with pytest.raises(EventRuleViolation, match="re-run"):
        await act(house, call_admin.rerun, house["Alex"], event_id)
    await act(house, call_admin.cancel, house["Alex"], event_id)
    house.clock.now += timedelta(days=1)
    with pytest.raises(EventRuleViolation, match="today's"):
        await act(house, call_admin.rerun, house["Alex"], event_id)


async def test_no_admin_override_on_a_finished_call(house: House) -> None:
    event_id = await decided(house)
    await act(house, calls.mark_done, house["Alex"], event_id)
    with pytest.raises(EventRuleViolation, match="over"):
        await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Kim"].id)


# Ad-hoc calls (FR-3) ---------------------------------------------------------------------------


async def test_ad_hoc_calls(house: House) -> None:
    house.clock.now = OPENS - timedelta(hours=2)  # before the standing call
    event = await act(house, calls.start_call, house["Kim"], Slot.DINNER, date(2026, 10, 22))
    assert (event.state, event.opens_at, event.created_by) == (EventState.COLLECTING, house.clock(), house["Kim"].id)
    with pytest.raises(EventRuleViolation, match="already a dinner call"):
        await act(house, calls.start_call, house["Sam"], Slot.DINNER, date(2026, 10, 22))
    with pytest.raises(EventRuleViolation, match="today or a later"):
        await act(house, calls.start_call, house["Sam"], Slot.DINNER, date(2026, 10, 21))
    lunch = await act(house, calls.start_call, house["Sam"], Slot.LUNCH, date(2026, 10, 24))
    assert lunch.state is EventState.SCHEDULED
    house.clock.now = OPENS
    await house.tick()
    assert (await house.kinds()).count("call_opened") == 1  # the standing call didn't post again
    house.clock.now = lunch.opens_at
    assert await house.tick() == [Step.OPEN]
    opened = (await house.audits())[-1]
    assert opened.action == "event.opened"


async def test_inactive_members_cannot_start_calls(house: House) -> None:
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE member SET active = 0 WHERE id = :id"), {"id": house["Kim"].id})
        await db.commit()
    kim = await house.refresh("Kim")
    with pytest.raises(EventRuleViolation, match="active"):
        await act(house, calls.start_call, kim, Slot.DINNER, date(2026, 10, 22))


# Privacy and household isolation ---------------------------------------------------------------


async def test_group_preferences_combine_without_a_breakdown(house: House) -> None:
    thai, mexican = house.tags["Thai"].id, house.tags["Mexican"].id
    taqueria = house.items["Taqueria"].id
    await act(house, set_my_preference, house["Sam"], TargetType.TAG, thai, Stance(veto=True))
    await act(house, set_my_preference, house["Kim"], TargetType.TAG, mexican, Stance(score=2))
    await act(house, set_my_preference, house["Lee"], TargetType.TAG, mexican, Stance(score=-1))
    await act(house, set_my_preference, house["Lee"], TargetType.ITEM, taqueria, Stance(veto=True))
    ids = frozenset(house[n].id for n in ("Sam", "Kim", "Lee"))
    async with house.sessionmaker() as db:
        group = await group_preferences(db, house.household.id, ids)
        alone = await group_preferences(db, house.household.id, frozenset({house["Kim"].id}))
        nobody = await group_preferences(db, house.household.id, frozenset())
    assert (group.vetoed_tags, group.vetoed_items) == ({thai}, {taqueria})
    assert dict(group.tag_scores) == {mexican: 1} and dict(group.item_scores) == {}
    assert (alone.vetoed_tags, dict(alone.tag_scores)) == (frozenset(), {mexican: 2})
    assert nobody.vetoed_tags == frozenset() and dict(nobody.tag_scores) == {}


async def test_tag_merges_repoint_guest_needs_and_free_text_cuisines(house: House) -> None:
    vegan = await house.tag("Vegan", TagKind.DIETARY)
    plant = await house.tag("Plant based", TagKind.DIETARY)
    event_id = await to_voting(house)
    await act(
        house,
        calls.add_proposal,
        house["Sam"],
        event_id,
        Mode.PICKUP,
        free_text="Pho",
        cuisine_id=house.tags["Thai"].id,
    )
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE response SET guest_needs = :needs"), {"needs": f"[{plant.id}]"})
        await db.commit()
    lao = await house.tag("Lao")
    async with house.sessionmaker() as db:
        await catalog_service.merge_tags(db, house["Alex"], plant.id, vegan.id, now=house.clock())
        await catalog_service.merge_tags(db, house["Alex"], house.tags["Thai"].id, lao.id, now=house.clock())
        needs = list(await db.scalars(select(ResponseRow.guest_needs)))
        cuisine = await db.scalar(select(ProposalRow.free_cuisine_id).where(ProposalRow.free_text == "Pho"))
    assert needs == [[vegan.id], [vegan.id]] and cuisine == lao.id


async def other_household_event(database: Database, clock: FakeClock) -> tuple[Member, int, int]:
    """A second household with its own admin and an open call with one proposal."""
    from dinnerbell.db.events import EventRepository, ProposalRepository
    from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
    from dinnerbell.domain.events import ProposalSource
    from dinnerbell.domain.members import Capabilities, Role

    async with database.sessionmaker() as db:
        household = await HouseholdRepository(db).create("Other", "UTC", clock())
        admin = await MemberRepository(db).add(
            household_id=household.id,
            display_name="Mallory",
            email="mallory@example.com",
            role=Role.ADMIN,
            capabilities=Capabilities(),
            now=clock(),
        )
        event = await EventRepository(db).add(
            household_id=household.id,
            slot=Slot.DINNER,
            day=date(2026, 10, 22),
            state=EventState.VOTING,
            opens_at=clock(),
            respond_deadline=clock(),
            reminder_minutes=15,
            vote_minutes=45,
            created_by=admin.id,
            now=clock(),
        )
        # Voting until late evening, so the tick in this test has nothing to do to it.
        event = await EventRepository(db).save(replace(event, vote_deadline=clock() + timedelta(hours=5)), clock())
        item = CatalogItemRow(
            household_id=household.id,
            kind="restaurant",
            name="Theirs",
            name_key="theirs",
            modes=["pickup"],
            active=True,
            created_at=clock(),
            updated_at=clock(),
        )
        db.add(item)
        await db.flush()
        proposal = await ProposalRepository(db).add(
            household_id=household.id,
            event_id=event.id,
            item_id=item.id,
            mode=Mode.PICKUP,
            rank=1,
            source=ProposalSource.SYSTEM,
            reasons=(),
            now=clock(),
        )
        await db.commit()
    return admin, event.id, proposal.id


async def test_nothing_reaches_another_households_call(house: House, database: Database, clock: FakeClock) -> None:
    mallory, theirs, their_proposal = await other_household_event(database, clock)
    alex = house["Alex"]  # an admin, so every admin check passes and only isolation is tested
    attempts: list[tuple[Callable[..., Awaitable[Any]], tuple[Any, ...]]] = [
        (calls.respond, (alex, theirs, Status.IN)),
        (calls.offer_role, (alex, theirs, RoleOffer.COOK, True)),
        (calls.save_details, (alex, theirs, Details())),
        (calls.claim_coordinator, (alex, theirs)),
        (calls.hand_off, (alex, theirs, mallory.id)),
        (calls.add_proposal, (alex, theirs, Mode.PICKUP)),
        (calls.vote, (alex, theirs, their_proposal)),
        (calls.unvote, (alex, theirs, their_proposal)),
        (calls.lock, (alex, theirs, their_proposal)),
        (calls.set_meal_time, (alex, theirs, time(19))),
        (calls.change_assignment, (alex, theirs, AssignmentRole.COOK, alex.id)),
        (calls.mark_done, (alex, theirs)),
        (call_admin.move_deadline, (alex, theirs, Window.VOTE, 15)),
        (call_admin.cancel, (alex, theirs)),
        (call_admin.rerun, (alex, theirs)),
        (call_admin.assign_coordinator, (alex, theirs, alex.id)),
        (event_views.event_view, (alex, theirs)),
    ]
    for use_case, args in attempts:
        with pytest.raises(NotFound):
            await act(house, use_case, *args)
    # Their proposal ID is not found through our own call either.
    ours = await to_voting(house)
    with pytest.raises(NotFound):
        await act(house, calls.vote, alex, ours, their_proposal)
    with pytest.raises(NotFound):
        await act(house, calls.lock, alex, ours, their_proposal)
    # And our members can't be made coordinator of their call, nor theirs of ours.
    with pytest.raises(NotFound):
        await act(house, call_admin.assign_coordinator, alex, ours, mallory.id)
    async with database.sessionmaker() as db:
        from dinnerbell.db.events import EventRepository

        untouched = await EventRepository(db).get(mallory.household_id, theirs)
    assert untouched is not None and untouched.state is EventState.VOTING and untouched.coordinator_id is None


# Read models -----------------------------------------------------------------------------------


async def test_event_views_offer_only_what_the_rules_allow(house: House) -> None:
    event_id = await to_voting(house)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    views = {name: await act(house, event_views.event_view, house[name], event_id) for name in ("Alex", "Sam", "Kim")}
    assert (views["Sam"].can.lock, views["Sam"].can.vote, views["Sam"].can.hand_off) == (True, True, True)
    assert (views["Kim"].can.lock, views["Kim"].can.vote, views["Kim"].can.propose) == (False, False, True)
    assert (views["Alex"].can.admin, views["Alex"].can.lock, views["Alex"].can.move_window) == (True, True, Window.VOTE)
    assert views["Alex"].can.rerun and not views["Kim"].can.rerun
    assert [p.info.item.name for p in views["Sam"].proposals] == ["Siam Garden", "Taqueria"]
    today = await act(house, event_views.today_view, house["Kim"])
    assert [e.id for e in today.events] == [event_id] and today.today == date(2026, 10, 22)


async def test_free_text_naming_a_switched_off_item_is_checked_as_that_item(house: House) -> None:
    """Regression: free text matching an inactive item skipped its tag and item vetoes."""
    event_id = await to_voting(house)
    await house.add_item("Old Spot", Mode.PICKUP, cuisine="Thai")
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE catalog_item SET active = 0 WHERE name = 'Old Spot'"))
        await db.commit()
    with pytest.raises(EventRuleViolation, match="isn't in the catalog"):
        await act(house, calls.add_proposal, house["Sam"], event_id, Mode.PICKUP, free_text="old spot", cuisine_id=1)


async def test_a_free_text_winner_is_checked_as_the_catalog_item_it_names(house: House) -> None:
    """Regression: an item added mid-vote under the free text's name could carry a vetoed tag."""
    event_id = await to_voting(house)
    vietnamese = await house.tag("Vietnamese")
    pho = await act(
        house, calls.add_proposal, house["Sam"], event_id, Mode.PICKUP, free_text="Pho Place", cuisine_id=vietnamese.id
    )
    await act(house, calls.vote, house["Sam"], event_id, pho.id)
    await act(house, calls.vote, house["Alex"], event_id, pho.id)
    await house.add_item("Pho Place", Mode.PICKUP, cuisine="Mexican")
    await act(house, set_my_preference, house["Sam"], TargetType.TAG, house.tags["Mexican"].id, Stance(veto=True))
    with pytest.raises(EventRuleViolation, match="doesn't work for everyone"):
        await act(house, calls.lock, house["Alex"], event_id, pho.id)
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.items[ctx.event.decided_item_id or 0].name == "Siam Garden"  # the vetoed winner was skipped


async def test_guest_needs_without_guests_are_ignored(house: House) -> None:
    """Regression: needs left ticked after guests went back to 0 still filtered every item."""
    vegan = await house.tag("Vegan", TagKind.DIETARY)
    event_id = await open_dinner(house)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.save_details, house["Sam"], event_id, Details(guests=0, guest_needs=frozenset({vegan.id})))
    assert (await house.context(event_id)).response_of(house["Sam"].id).guest_needs == frozenset()  # type: ignore[union-attr]
    async with house.sessionmaker() as db:  # a row saved before the fix
        await db.execute(text("UPDATE response SET guest_needs = :needs"), {"needs": f"[{vegan.id}]"})
        await db.commit()
    house.clock.now = RESPOND_BY
    await house.tick()
    assert sorted(await proposal_ids(house, event_id)) == ["Siam Garden", "Taqueria"]


async def test_the_meal_time_cannot_precede_the_decision(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(EventRuleViolation, match="after the decision"):
        await act(house, calls.set_meal_time, house["Sam"], event_id, time(6, 30))  # meant 18:30
    assert (await house.event(event_id)).state is EventState.DECIDED


# Stale calls after an outage ---------------------------------------------------------------------

LOCAL = ZoneInfo("America/Chicago")


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=LOCAL).astimezone(UTC)


async def test_a_call_stuck_through_the_evening_expires_quietly(house: House) -> None:
    """Regression: after a long outage the tick decided the call and messaged everyone hours late."""
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.respond, house["Kim"], event_id, Status.MAYBE)
    house.clock.now = local(24, 9)  # down from 15:10 until Saturday morning
    assert await house.tick() == [Step.EXPIRE]
    event = await house.event(event_id)
    assert (event.state, event.end_reason) == (EventState.ABANDONED, EndReason.MISSED)
    entries = await house.outbox()
    assert [e.kind for e in entries] == ["call_opened", "call_updated"]  # no reminder, maybe, or decision
    assert entries[-1].payload["event"]["end_reason"] == "missed"
    assert (await house.audits())[-1].payload == {"reason": "missed", "from": "collecting"}
    assert await house.tick() == []


async def test_voting_left_past_the_evening_expires_without_a_decision(house: House) -> None:
    event_id = await to_voting(house)
    house.clock.now = local(22, 20, 31)  # past the usual 18:30 meal plus two hours
    assert await house.tick() == [Step.EXPIRE]
    assert (await house.event(event_id)).end_reason is EndReason.MISSED
    kinds = await house.kinds()
    assert "decision_made" not in kinds and "assignment_made" not in kinds and "call_abandoned" not in kinds
    assert kinds[-1] == "proposals_updated"


async def test_a_scheduled_call_whose_window_passed_never_posts(house: House) -> None:
    """Regression: FR-2. It was posted and abandoned in the same tick."""
    house.clock.now = OPENS - timedelta(hours=3)
    event = await act(house, calls.start_call, house["Kim"], Slot.DINNER, date(2026, 10, 23))
    house.clock.now = local(23, 16, 31)  # down from Thursday until after Friday's response window
    assert await house.tick() == [Step.EXPIRE]
    stored = await house.event(event.id)
    assert (stored.state, stored.end_reason) == (EventState.ABANDONED, EndReason.MISSED)
    assert "call_opened" not in await house.kinds()


async def test_a_decision_that_would_land_after_midnight_expires(house: House) -> None:
    house.clock.now = local(22, 22)
    event = await act(house, calls.start_call, house["Alex"], Slot.DINNER, date(2026, 10, 22))
    await act(house, calls.respond, house["Sam"], event.id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event.id, RoleOffer.PICKUP, True)
    house.clock.now = local(22, 22, 10)
    await act(house, call_admin.move_deadline, house["Alex"], event.id, Window.RESPOND, None)
    ids = await proposal_ids(house, event.id)
    house.clock.now = local(22, 23, 53)  # rounds up to 00:00, which isn't the call's day
    await act(house, calls.lock, house["Alex"], event.id, ids["Siam Garden"])
    stored = await house.event(event.id)
    assert (stored.state, stored.end_reason, stored.decided_item_id) == (EventState.ABANDONED, EndReason.MISSED, None)
    assert "decision_made" not in await house.kinds()


async def test_rerun_is_refused_late_in_the_evening_and_keeps_a_later_days_timing(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, call_admin.cancel, house["Alex"], event_id)
    house.clock.now = local(22, 20, 31)
    with pytest.raises(EventRuleViolation, match="too late to re-run"):
        await act(house, call_admin.rerun, house["Alex"], event_id)
    tomorrow = await act(house, calls.start_call, house["Kim"], Slot.DINNER, date(2026, 10, 23))
    await act(house, call_admin.cancel, house["Alex"], tomorrow.id)
    before = await house.kinds()
    await act(house, call_admin.rerun, house["Alex"], tomorrow.id)
    rerun = await house.event(tomorrow.id)
    assert (rerun.state, rerun.run, rerun.opens_at) == (EventState.SCHEDULED, 2, local(23, 15))
    assert await house.kinds() == before  # posted when it opens, not now
    house.clock.now = local(23, 15)
    assert await house.tick() == [Step.OPEN]


async def test_proposals_are_capped_per_call_and_per_member(house: House) -> None:
    """Regression: nothing stopped one member, or everyone, from flooding a call with proposals."""
    event_id = await to_voting(house)  # two ranked proposals
    thai = house.tags["Thai"].id

    async def propose(name: str, text: str) -> Any:
        return await act(house, calls.add_proposal, house[name], event_id, Mode.PICKUP, free_text=text, cuisine_id=thai)

    for n in range(3):
        await propose("Kim", f"Kim's {n}")
    with pytest.raises(EventRuleViolation, match="You've added 3"):
        await propose("Kim", "One more")
    for name in ("Sam", "Lee"):
        for n in range(3 if name == "Sam" else 2):
            await propose(name, f"{name}'s {n}")
    assert len((await house.context(event_id)).proposals) == 10
    with pytest.raises(EventRuleViolation, match="already has 10 proposals"):
        await propose("Alex", "Eleventh")


async def test_toggles_flip_the_actors_own_state(house: House) -> None:
    """Shared Slack buttons can't know who tapped them, so the service reads and flips."""
    event_id = await open_dinner(house)
    assert await act(house, calls.toggle_role, house["Sam"], event_id, RoleOffer.COOK) is True
    mine = (await house.context(event_id)).response_of(house["Sam"].id)
    assert mine is not None and mine.status is Status.IN and mine.offers == {RoleOffer.COOK}
    assert await act(house, calls.toggle_role, house["Sam"], event_id, RoleOffer.COOK) is False
    assert (await house.context(event_id)).response_of(house["Sam"].id).offers == frozenset()  # type: ignore[union-attr]
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    ids = await proposal_ids(house, event_id)
    assert await act(house, calls.toggle_vote, house["Sam"], event_id, ids["Taqueria"]) is True
    assert await act(house, calls.toggle_vote, house["Alex"], event_id, ids["Taqueria"]) is True
    assert await act(house, calls.toggle_vote, house["Sam"], event_id, ids["Taqueria"]) is False
    assert (await house.context(event_id)).votes == {ids["Taqueria"]: [house["Alex"].id]}
    with pytest.raises(EventRuleViolation, match="Only people who are in"):
        await act(house, calls.toggle_vote, house["Kim"], event_id, ids["Taqueria"])
    with pytest.raises(EventRuleViolation, match="closed"):
        await act(house, calls.toggle_role, house["Kim"], event_id, RoleOffer.COOK)


async def test_a_free_text_proposal_in_a_vetoed_cuisine_is_refused(house: House) -> None:
    event_id = await to_voting(house)
    mexican = house.tags["Mexican"].id
    await act(house, set_my_preference, house["Sam"], TargetType.TAG, mexican, Stance(veto=True))
    with pytest.raises(EventRuleViolation, match="doesn't work for everyone"):
        await act(
            house, calls.add_proposal, house["Alex"], event_id, Mode.PICKUP, free_text="Tortas", cuisine_id=mexican
        )
    added = await act(
        house,
        calls.add_proposal,
        house["Alex"],
        event_id,
        Mode.PICKUP,
        free_text="Tortas",
        cuisine_id=house.tags["Thai"].id,
    )
    assert added.free_text == "Tortas"


async def test_no_call_is_posted_for_a_day_that_has_ended(house: House) -> None:
    """A 23:30 call is stale at midnight (it can't finish on its day), so a restart at 00:10
    must not post it only to expire it in the same tick (FR-2)."""
    async with house.sessionmaker() as db:
        await db.execute(
            text("UPDATE household SET settings = json_set(settings, '$.slots.dinner.call_time', '23:30')")
        )
        await db.commit()
    house.clock.now = local(23, 0, 10)  # 00:10 on Friday, 40 minutes into Thursday's window
    await house.tick()
    assert await house.outbox() == []
    house.clock.now = local(23, 23, 30)  # Friday's own call still opens on time
    await house.tick()
    assert await house.kinds() == ["call_opened"]


async def test_suggestions_from_people_who_are_out_earn_nothing(house: House) -> None:
    event_id = await open_dinner(house)
    taqueria = house.items["Taqueria"].id
    await act(house, calls.save_details, house["Kim"], event_id, Details(suggestion_item_ids=(taqueria,)))
    await act(house, calls.respond, house["Kim"], event_id, Status.OUT)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    reasons = {ctx.items[p.item_id or 0].name: p.reasons for p in ctx.proposals}
    assert not any("Kim" in r for r in reasons["Taqueria"])
    assert ctx.proposals[0].item_id == house.items["Siam Garden"].id  # no suggestion bonus for Taqueria


async def test_assignments_fit_the_decision_and_repeat_quietly(house: House) -> None:
    """Regression: any role could be given to anyone, and reassigning the same person sent
    them a second assignment message."""
    event_id = await decided(house)  # Siam Garden by pickup, picked up by Alex
    with pytest.raises(EventRuleViolation, match="This meal is pickup"):
        await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.COOK, house["Sam"].id)
    with pytest.raises(EventRuleViolation, match="someone who's in"):
        await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Kim"].id)
    before = await house.outbox()
    pickup = next(a for a in (await house.context(event_id)).assignments if a.role is AssignmentRole.PICKUP)
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Alex"].id)
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Sam"].id)  # already Sam
    assert await house.outbox() == before
    after = next(a for a in (await house.context(event_id)).assignments if a.role is AssignmentRole.PICKUP)
    assert after.id == pickup.id


async def test_suggestion_credit_counts_members_not_names(house: House) -> None:
    """Regression: two members with the same display name were credited as one suggester."""
    twin = await house.add_member("Sam Two")
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE member SET display_name = 'Sam' WHERE id = :id"), {"id": twin.id})
        await db.commit()
    event_id = await open_dinner(house)
    taqueria = house.items["Taqueria"].id
    for member in (house["Sam"], twin):
        await act(house, calls.save_details, member, event_id, Details(suggestion_item_ids=(taqueria,)))
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.proposals[0].item_id == taqueria
    assert ctx.proposals[0].reasons[0] == "Suggested by Sam and Sam."
