# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guests in Slack (#38): joining from the channel (option A) with every refusal, the admins'
**Make member** and **Remove**, a guest's own buttons and Home, refused taps, a member's
**Invite a guest**, and answering for a guest without Slack on the card."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.interactions import JOIN_OFFER, JOINED, UNKNOWN_USER
from dinnerbell.channels.slack.messages import MOVED_ON
from dinnerbell.config import Settings
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.events import GUESTS_DONT, EventState, RoleOffer, Status
from dinnerbell.domain.guests import REFUSED, GuestAction
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member, Role
from dinnerbell.services import calls, guests
from dinnerbell.services.database import Database, open_database
from tests.support.calls import OPENS, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import CHANNEL, OTHER_TEAM, send, state, view_submission
from tests.support.slack import SLACK_IDS, Slack, wire

pytestmark = pytest.mark.anyio
NEW = "U09NOVA"
PAT = "U07PAT"


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    async with house.sessionmaker() as db:
        members = MemberRepository(db)
        for name, slack in (("Pat", "U07PAT"), ("Jo", None)):
            house.people[name] = await members.add_guest(
                household_id=house.household.id,
                display_name=name,
                dietary_needs=frozenset(),
                slack_user_id=slack,
                now=clock(),
            )
        await db.commit()
    return house


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    wired.fake.add_person(NEW, "Nova")
    wired.fake.add_person("U07PAT", "Pat")
    yield wired
    await wired.conn.stop()


async def member_for(house: House, slack_id: str) -> Member | None:
    async with house.sessionmaker() as db:
        return await MemberRepository(db).by_slack_user_id(house.household.id, slack_id)


def buttons(blocks: list[dict[str, Any]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for block in blocks:
        for element in block.get("elements", []) if block["type"] == "actions" else []:
            if element.get("type") == "button":
                found[element["action_id"]] = element.get("value", "")
    return found


async def open_dinner(house: House) -> int:
    house.clock.now = OPENS
    await house.tick()
    return await dinner_id(house)


# Joining from the channel ------------------------------------------------------------------------


async def test_someone_in_the_channel_joins_as_a_guest(slack: Slack, house: House) -> None:
    await slack.open_home(NEW)
    home = slack.last_home(NEW)
    assert JOIN_OFFER in json.dumps(home) and buttons(home["blocks"]) == {ids.JOIN_DINNERBELL: ids.JOIN_DINNERBELL}
    await slack.click(NEW, ids.JOIN_DINNERBELL, ids.JOIN_DINNERBELL)
    joined = await member_for(house, NEW)
    assert joined is not None and joined.is_guest and joined.display_name == "Nova"
    assert slack.ephemerals(NEW)[-1] == JOINED[guests.JoinedAs.GUEST]
    await slack.drain()
    (dm,) = [c for c in slack.fake.of("chat.postMessage") if c.args["channel"] == SLACK_IDS["Alex"]]
    dm_buttons = buttons(dm.args["blocks"] if isinstance(dm.args["blocks"], list) else json.loads(dm.args["blocks"]))
    assert dm_buttons == {ids.MAKE_MEMBER: str(joined.id), ids.REMOVE_GUEST: str(joined.id), "link_members": ""}

    await slack.click("Alex", ids.MAKE_MEMBER, str(joined.id), channel="D01ALEX")
    assert "Slack doesn't share their email" in slack.fake.of("chat.postEphemeral", "chat.postMessage")[-1].args["text"]
    assert (await member_for(house, NEW)).is_guest  # type: ignore[union-attr]
    slack.fake.add_person(NEW, "Nova", email="nova@example.com")
    await slack.click("Alex", ids.MAKE_MEMBER, str(joined.id), channel="D01ALEX")
    promoted = await member_for(house, NEW)
    assert promoted is not None and (promoted.role, promoted.email) == (Role.MEMBER, "nova@example.com")
    await slack.click("Sam", ids.REMOVE_GUEST, str(joined.id), channel="D02SAM")  # not an admin
    assert (await member_for(house, NEW)).active  # type: ignore[union-attr]


async def test_the_admin_removes_someone_who_joined(slack: Slack, house: House) -> None:
    await slack.click(NEW, ids.JOIN_DINNERBELL, ids.JOIN_DINNERBELL)
    joined = await member_for(house, NEW)
    assert joined is not None
    await slack.click("Alex", ids.REMOVE_GUEST, str(joined.id), channel="D01ALEX")
    removed = await member_for(house, NEW)
    assert removed is not None and not removed.active and removed.join_refused
    await slack.click(NEW, ids.JOIN_DINNERBELL, ids.JOIN_DINNERBELL)
    assert slack.ephemerals(NEW)[-1] == guests.JOIN_REFUSALS[guests.NotJoinable.REMOVED]


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"is_bot": True}, guests.NotJoinable.NOT_A_PERSON),
        ({"deleted": True}, guests.NotJoinable.NOT_A_PERSON),
        ({"is_stranger": True}, guests.NotJoinable.NOT_A_PERSON),
        ({"team_id": OTHER_TEAM}, guests.NotJoinable.NOT_A_PERSON),
        ({"in_channel": False}, guests.NotJoinable.NOT_IN_CHANNEL),
    ],
)
async def test_only_real_people_in_the_channel_can_join(
    slack: Slack, house: House, extra: dict[str, Any], reason: guests.NotJoinable
) -> None:
    slack.fake.channel_members.remove(NEW)
    slack.fake.add_person(NEW, "Nova", **extra)
    await slack.click(NEW, ids.JOIN_DINNERBELL, ids.JOIN_DINNERBELL)
    assert await member_for(house, NEW) is None
    assert slack.ephemerals(NEW)[-1] == guests.JOIN_REFUSALS[reason]
    (row,) = await house.audits("member.join_refused")
    assert row.payload["reason"] == reason.value


async def test_a_slack_guest_account_can_join_as_a_guest(slack: Slack, house: House) -> None:
    slack.fake.add_person("U09SKY", "Sky", is_restricted=True)
    await slack.click("U09SKY", ids.JOIN_DINNERBELL, ids.JOIN_DINNERBELL)
    joined = await member_for(house, "U09SKY")
    assert joined is not None and joined.is_guest


async def test_joining_while_off_says_to_ask_the_admin(slack: Slack, house: House) -> None:
    async with house.sessionmaker() as db:
        await guests.set_joining(db, house["Alex"], False, now=house.clock())
    await slack.open_home(NEW)
    assert UNKNOWN_USER in json.dumps(slack.last_home(NEW))
    await slack.click(NEW, ids.JOIN_DINNERBELL, ids.JOIN_DINNERBELL)  # an old button
    assert await member_for(house, NEW) is None
    assert slack.ephemerals(NEW)[-1] == guests.JOIN_REFUSALS[guests.NotJoinable.OFF]


async def test_joining_is_rate_limited_per_slack_user(slack: Slack, house: House) -> None:
    slack.fake.channel_members.remove(NEW)
    for _ in range(6):
        await slack.click(NEW, ids.JOIN_DINNERBELL, ids.JOIN_DINNERBELL)
    assert slack.ephemerals(NEW)[-1] == guests.JOIN_REFUSALS[guests.NotJoinable.TOO_MANY]
    assert len(slack.fake.of("users.info")) == 0  # never asked: not in the channel
    assert len(slack.fake.of("conversations.members")) == 5
    told = len(slack.ephemerals(NEW))
    for _ in range(3):  # told once a window; after that, nothing is answered or audited
        await slack.click(NEW, ids.JOIN_DINNERBELL, ids.JOIN_DINNERBELL)
    assert len(slack.ephemerals(NEW)) == told
    reasons = [r.payload["reason"] for r in await house.audits("member.join_refused")]
    assert reasons.count("rate_limited") == 1


async def test_a_join_from_another_workspace_is_ignored(slack: Slack, house: House) -> None:
    await slack.click(NEW, ids.JOIN_DINNERBELL, ids.JOIN_DINNERBELL, team=OTHER_TEAM)
    assert await member_for(house, NEW) is None and slack.fake.calls == []


# A registered guest in Slack -----------------------------------------------------------------------


async def test_a_guest_sees_only_their_invitations_and_answers_in_or_out(slack: Slack, house: House) -> None:
    event_id = await open_dinner(house)
    await slack.open_home(PAT)
    assert "No invitations right now" in json.dumps(slack.last_home(PAT))
    async with house.sessionmaker() as db:
        await guests.invite(db, house["Kim"], event_id, guest_id=house["Pat"].id, now=house.clock())
    await slack.drain()
    (dm,) = [c for c in slack.fake.of("chat.postMessage") if c.args["channel"] == "U07PAT"]
    invitation = dm.args["blocks"] if isinstance(dm.args["blocks"], list) else json.loads(dm.args["blocks"])
    assert set(buttons(invitation)) == {
        ids.CARD_ACTION_FOR_STATUS[Status.IN],
        ids.CARD_ACTION_FOR_STATUS[Status.OUT],
        *ids.CARD_OFFER,  # the role offers, as on their card and Home (#149)
        ids.CARD_COORDINATE,
        "link_event",
    }
    await slack.open_home(PAT)
    home = buttons(slack.last_home(PAT)["blocks"])
    # Home offers what their card does (#84): In, Out, and the roles, and nothing a member has.
    assert set(home) == {
        ids.CARD_ACTION_FOR_STATUS[Status.IN],
        ids.CARD_ACTION_FOR_STATUS[Status.OUT],
        *ids.CARD_OFFER,
        ids.CARD_COORDINATE,
        "link_event",
    }
    await slack.click(PAT, ids.CARD_ACTION_FOR_STATUS[Status.IN], ids.card_value(event_id, 1), home=True)
    await slack.click(PAT, "card_offer_order", ids.card_value(event_id, 1), home=True)
    mine = (await house.context(event_id)).response_of(house["Pat"].id)
    assert mine is not None and mine.status is Status.IN and mine.offers == {RoleOffer.ORDER}
    assert "card_withdraw_order" in buttons(slack.last_home(PAT)["blocks"])
    await slack.click(PAT, ids.ACTION_FOR_STATUS[Status.OUT], str(event_id), home=True)  # a Home from before #84
    assert (await house.context(event_id)).response_of(house["Pat"].id).status is Status.OUT  # type: ignore[union-attr]
    await slack.click(PAT, ids.CARD_ACTION_FOR_STATUS[Status.IN], ids.card_value(event_id, 1), home=True)

    await slack.click(PAT, ids.ANSWER, ids.card_value(event_id, 1))
    card = slack.cards(PAT)[-1]
    card_buttons = buttons(card["blocks"])
    assert ids.CARD_ACTION_FOR_STATUS[Status.MAYBE] not in card_buttons and ids.CARD_DETAILS not in card_buttons
    assert ids.INVITE_GUEST not in card_buttons and "card_offer_pickup" in card_buttons
    await slack.tap_card(PAT, "card_offer_pickup", card)
    assert RoleOffer.PICKUP in (await house.context(event_id)).response_of(house["Pat"].id).offers  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("action_id", "value"),
    [
        (ids.ACTION_FOR_STATUS[Status.MAYBE], None),
        (ids.DETAILS, None),
        (ids.VOTE, "proposal"),
        (ids.LOCK, "proposal"),
        (ids.ADD_PROPOSAL, None),
        (ids.ACTION_FOR_START[Slot.LUNCH], "lunch"),
        (ids.MAKE_OFFER, ids.MAKE_OFFER),
        (ids.INVITE_GUEST, "card"),
        (ids.CANCEL_CALL, "cancel"),
        (ids.FEEDBACK, "card"),
        (ids.POST_NOW, "card"),
        (ids.JOIN_OFFER, "card"),
        (ids.MAKE_MEMBER, "jo"),
    ],
)
async def test_every_other_button_is_refused_to_a_guest(
    slack: Slack, house: House, action_id: str, value: str | None
) -> None:
    event_id = await open_dinner(house)
    async with house.sessionmaker() as db:
        await guests.invite(db, house["Kim"], event_id, guest_id=house["Pat"].id, now=house.clock())
        await calls.respond(db, house["Pat"], event_id, Status.IN, now=house.clock())
    values = {
        None: str(event_id),
        "proposal": ids.proposal_value(event_id, 1),
        "card": ids.card_value(event_id, 1),
        "cancel": ids.menu_value(ids.MenuChoice.CANCEL, event_id, 1),
        "jo": str(house["Jo"].id),
    }
    before = await house.kinds()
    await slack.click(PAT, action_id, values.get(value, value))
    # Maybe reaches the service, which says why; the rest stop at the door.
    expected = REFUSED[GuestAction.MAYBE] if action_id == ids.ACTION_FOR_STATUS[Status.MAYBE] else GUESTS_DONT
    assert slack.ephemerals(PAT)[-1] == expected
    assert await house.kinds() == before and slack.fake.of("views.open") == []


MAX_ROW = 5
"""Slack folds an actions row past about five buttons into "+ N more" (#84)."""


async def test_no_home_row_has_more_buttons_than_slack_shows(slack: Slack, house: House) -> None:
    """Every Home an admin, the coordinator, a member, and a guest see, as a call goes from
    answers to done and another is called off, keeps each row to five buttons (#84)."""
    event_id = await open_dinner(house)
    async with house.sessionmaker() as db:
        await guests.invite(db, house["Kim"], event_id, guest_id=house["Pat"].id, now=house.clock())
    await slack.click("Sam", "respond_in", str(event_id))
    await slack.click("Sam", "coordinate", str(event_id))
    await slack.click("Alex", "offer_pickup", str(event_id))
    await slack.click("Kim", "respond_in", str(event_id))
    await slack.click(PAT, "respond_in", str(event_id))
    seen: set[tuple[EventState, str]] = set()

    async def check(call: int, state: EventState) -> None:
        assert (await house.event(call)).state is state
        for who in ("Alex", "Sam", "Kim", PAT):
            await slack.open_home(who)
            rows = {b["block_id"]: b["elements"] for b in slack.last_home(who)["blocks"] if b["type"] == "actions"}
            for block_id, elements in rows.items():
                assert len(elements) <= MAX_ROW, (state, who, block_id, [e["action_id"] for e in elements])
            seen.add((state, who))

    await check(event_id, EventState.COLLECTING)
    assert "close_window" in buttons(slack.last_home("Sam")["blocks"])  # Close now still shows
    house.clock.now = RESPOND_BY
    await house.tick()
    await check(event_id, EventState.VOTING)
    first = (await house.context(event_id)).proposals[0].id
    await slack.click("Alex", "lock", ids.proposal_value(event_id, first))
    await check(event_id, EventState.DECIDED)
    async with house.sessionmaker() as db:
        await calls.mark_done(db, house["Alex"], event_id, now=house.clock())
    await check(event_id, EventState.DONE)
    async with house.sessionmaker() as db:
        lunch = await calls.start_call(db, house["Kim"], Slot.LUNCH, house.clock().date(), now=house.clock())
        await calls.cancel_call(db, house["Kim"], lunch.id, now=house.clock())
    await check(lunch.id, EventState.CANCELLED)
    assert len(seen) == 20


# A member inviting, and answering for a guest without Slack ------------------------------------------


async def test_a_member_invites_from_the_card_and_answers_for_their_guest(slack: Slack, house: House) -> None:
    event_id = await open_dinner(house)
    await slack.click("Kim", ids.ANSWER, ids.card_value(event_id, 1))
    card = slack.cards("Kim")[-1]
    assert buttons(card["blocks"])[ids.INVITE_GUEST] == ids.card_value(event_id, 1)
    await slack.tap_card("Kim", ids.INVITE_GUEST, card)
    (opened,) = slack.fake.of("views.open")
    view = opened.args["view"]
    assert view["callback_id"] == ids.INVITE_VIEW
    origin = ids.ViewOrigin.decode(view["private_metadata"])
    assert origin.run == 1
    values = state(guest={"selected_option": {"value": str(house["Jo"].id)}})
    await send(
        slack.conn.app, "interactive", view_submission(SLACK_IDS["Kim"], ids.INVITE_VIEW, values, origin.encode())
    )
    await slack.conn.interactions.settle()
    assert "Jo is invited and counted in" in slack.ephemerals("Kim")[-1]

    await slack.click("Kim", ids.ANSWER, ids.card_value(event_id, 1))
    card = slack.cards("Kim")[-1]
    guest_value = ids.guest_value(event_id, 1, house["Jo"].id)
    assert buttons(card["blocks"])["guest_offer_cook"] == guest_value
    await slack.click("Kim", "guest_offer_cook", guest_value, ephemeral=True)
    jo = (await house.context(event_id)).response_of(house["Jo"].id)
    assert jo is not None and RoleOffer.COOK in jo.offers
    replaced = slack.cards("Kim")[-1]
    assert buttons(replaced["blocks"])["guest_withdraw_cook"] == guest_value  # the card shows it offered

    await slack.click("Sam", "guest_out", guest_value, ephemeral=True)  # not who invited Jo
    assert (await house.context(event_id)).response_of(house["Jo"].id).status is Status.IN  # type: ignore[union-attr]
    await slack.click("Kim", "guest_out", guest_value)  # on the shared call message, not a card
    assert (await house.context(event_id)).response_of(house["Jo"].id).status is Status.IN  # type: ignore[union-attr]
    await slack.click("Kim", "guest_out", ids.guest_value(event_id, 2, house["Jo"].id), ephemeral=True)  # another run
    assert (await house.context(event_id)).response_of(house["Jo"].id).status is Status.IN  # type: ignore[union-attr]


async def test_a_guest_tap_on_a_card_after_responses_close_shows_why(
    slack: Slack, house: House, caplog: pytest.LogCaptureFixture
) -> None:
    """#74: the card says the call moved on, and the refusal it used to swallow follows in a
    private line, logged at info."""
    caplog.set_level(logging.INFO, logger="dinnerbell")
    event_id = await open_dinner(house)
    for name in ("Alex", "Kim"):
        await slack.click(name, "respond_in", str(event_id))
    await slack.click("Alex", "offer_pickup", str(event_id))
    async with house.sessionmaker() as db:
        await guests.invite(db, house["Kim"], event_id, guest_id=house["Jo"].id, now=house.clock())
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    slack.fake.clear()
    await slack.click("Kim", "guest_out", ids.guest_value(event_id, 1, house["Jo"].id), ephemeral=True)
    assert (await house.context(event_id)).response_of(house["Jo"].id).status is Status.IN  # type: ignore[union-attr]
    assert f"*{MOVED_ON}*" in json.dumps(slack.cards("Kim")[-1])
    refusal = slack.ephemerals("Kim")[-1]
    assert refusal == "Responses are closed for this call."
    (logged,) = [r for r in caplog.records if r.getMessage() == "refused"]
    fields = (logged.levelno, logged.member_id, logged.event_id, logged.reason, logged.refusal, logged.moved_on)  # type: ignore[attr-defined]
    assert fields == (logging.INFO, house["Kim"].id, event_id, "responses_closed", refusal, True)


async def test_an_invite_modal_naming_someone_new(slack: Slack, house: House) -> None:
    event_id = await open_dinner(house)
    origin = ids.ViewOrigin(event_id, CHANNEL, run=1)
    both = state(guest={"selected_option": {"value": str(house["Jo"].id)}}, name={"value": "Riley"})
    response = await send(
        slack.conn.app, "interactive", view_submission(SLACK_IDS["Kim"], ids.INVITE_VIEW, both, origin.encode())
    )
    assert "name" in json.loads(response.body)["errors"]
    new = state(name={"value": "Riley"})
    await send(slack.conn.app, "interactive", view_submission(SLACK_IDS["Kim"], ids.INVITE_VIEW, new, origin.encode()))
    await slack.conn.interactions.settle()
    ctx = await house.context(event_id)
    assert [m.display_name for m in ctx.guests] == ["Riley"]


# Who Slack says may join -------------------------------------------------------------------------


async def test_channel_people_lists_only_real_people_in_the_channel(slack: Slack) -> None:
    slack.fake.add_person("U09BOT", "Botty", is_bot=True)
    slack.fake.add_person("U09OUT", "Outside", in_channel=False)
    slack.fake.add_person("U09SG", "Sky", email="sky@example.com", is_ultra_restricted=True)
    slack.fake.channel_members.append("U09GONE")  # users.info doesn't know them
    people = await slack.conn.people.channel_people()
    found = {p.slack_user_id: p for p in people}
    assert set(found) == {NEW, "U07PAT", "U09SG"}
    assert found["U09SG"].full_member is False and found["U09SG"].email == "sky@example.com"
    assert found[NEW] == guests.SlackPerson(NEW, "Nova", None, full_member=True)


async def test_channel_people_fails_closed_when_slack_does_not_answer(slack: Slack) -> None:
    from tests.support.fake_slack import Failure

    slack.fake.fail("conversations.members", Failure("ratelimited", status=429, headers={"Retry-After": "1"}))
    assert await slack.conn.people.person(NEW) is guests.NotJoinable.UNAVAILABLE
    slack.fake.fail("conversations.members", Failure("channel_not_found"))
    assert await slack.conn.people.channel_people() == []
    slack.fake.fail("users.info", Failure("fatal_error", status=500))
    assert await slack.conn.people.person(NEW) is guests.NotJoinable.UNAVAILABLE
    assert await slack.conn.people.person("not-an-id") is guests.NotJoinable.NOT_A_PERSON
    slack.fake.fail("users.info", Failure("fatal_error", status=500))
    assert await slack.conn.people.channel_people() == []  # stops at the first failure


async def test_nobody_joins_before_the_workspace_is_known(slack: Slack) -> None:
    slack.conn.team_id = None
    assert await slack.conn.people.person(NEW) is guests.NotJoinable.NOT_A_PERSON


async def test_a_guest_cant_probe_for_calls_with_the_command(slack: Slack, house: House) -> None:
    """Whether or not there's a call that day, a guest gets the same answer, before any lookup."""
    await open_dinner(house)
    for text in ("cancel", "cancel dinner", "cancel lunch 2026-10-25"):
        await slack.command(PAT, text)
    replies = set(slack.ephemerals(PAT))
    assert replies == {REFUSED[GuestAction.CALL_OFF]}


def test_a_card_with_many_guests_and_a_guest_home_with_many_invitations_fit_slack() -> None:
    from dataclasses import replace as copy

    from dinnerbell.channels.slack import preview as samples
    from dinnerbell.channels.slack.blocks import MAX_VIEW_BLOCKS, with_version_footer
    from dinnerbell.channels.slack.messages import MAX_CARD_GUESTS, CardGuest, RenderContext, your_answer
    from dinnerbell.channels.slack.views import HomeLinks, home_view
    from dinnerbell.domain.events import Response
    from dinnerbell.services.event_views import Permissions, TodayView

    ctx = RenderContext(samples.BASE_URL, samples.DIRECTORY)
    many = tuple(
        CardGuest(100 + i, f"Guest {i}", Response(100 + i, Status.IN, offers=frozenset(RoleOffer))) for i in range(12)
    )
    card = your_answer(samples.event(), samples.KIM_IN, samples.KIM.id, ctx, run=1, can_invite=True, guests=many)
    blocks = card.payload()["blocks"]
    assert len(blocks) <= 50 and all(len(b["elements"]) <= 25 for b in blocks if b["type"] == "actions")
    assert sum(1 for b in blocks if str(b.get("block_id", "")).startswith("guest-actions-")) == MAX_CARD_GUESTS
    assert "Answer for your other guests on the web." in json.dumps(blocks)

    view = copy(samples.COLLECTING_VIEW, mine=None, can=Permissions(respond=True))
    views = [copy(view, event=copy(view.event, id=i)) for i in range(80)]
    home = home_view(TodayView(samples.DAY, views, tuple(Slot)), HomeLinks(samples.BASE_URL), guest=True)
    home = with_version_footer(home, "0.5.0")
    assert len(home["blocks"]) <= MAX_VIEW_BLOCKS
    assert home["blocks"][-1]["elements"][0]["text"].startswith("Dinnerbell 0.5.0")


async def test_past_the_paging_cap_someone_unlisted_is_asked_to_ask_the_admin(
    slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dinnerbell.channels.slack import people as people_module

    monkeypatch.setattr(people_module, "PAGE_SIZE", 1)
    monkeypatch.setattr(people_module, "MAX_PAGES", 1)  # reads one member of several
    slack.fake.add_person("U09ZED", "Zed")
    assert await slack.conn.people.person("U09ZED") is guests.NotJoinable.ASK_ADMIN
