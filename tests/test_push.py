# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web push from the queue to the push service (#138, spec 6.15): turning a device on and off,
which notifications queue pushes for whom, the drainer's checks and the push service's answers,
turning the switch off, and the pages. The rules themselves are in ``test_push_domain``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from dinnerbell.config import Settings
from dinnerbell.db.models import PushRow, PushSubscriptionRow
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.db.push import PushRepository, rejected_burst
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain import push as rules
from dinnerbell.domain.events import EventState
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import NotifyVia, Role
from dinnerbell.domain.push import PushResponse, PushTarget
from dinnerbell.services import outbox, push, web_app
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden, NotFound
from tests.conftest import BASE_URL, page, sign_in
from tests.support.calls import OPENS, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.pages import inline_code
from tests.support.slack import Slack, link, wire
from tests.support.web import submit
from tests.test_push_domain import AUTH, P256DH

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 22)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    """Nobody has a Slack user, so everyone is on the web app until linked."""
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def switch(house: House, on: bool) -> None:
    async with house.sessionmaker() as db:
        await web_app.set_web_app(db, house["Alex"], on, now=house.clock())


def endpoint(name: str, n: int = 1) -> str:
    return f"https://fcm.googleapis.com/fcm/send/{name.lower()}-{n}"


async def device(house: House, name: str, n: int = 1) -> int:
    async with house.sessionmaker() as db:
        return await push.subscribe(db, house[name], endpoint(name, n), P256DH, AUTH, now=house.clock())


def sql_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


async def a_call(house: House, state: str = "collecting") -> note.EventInfo:
    async with house.sessionmaker() as db:
        await db.execute(
            text(
                "INSERT INTO event (id, household_id, kind, slot, date, state, run, version, opens_at, "
                "respond_deadline, reminder_minutes, vote_minutes, created_at, updated_at) VALUES "
                "(1, :h, 'call', 'dinner', '2026-10-22', :s, 1, 1, :t, :d, 15, 45, :t, :t)"
            ),
            {
                "h": house.household.id,
                "s": state,
                "t": sql_time(house.clock()),
                "d": sql_time(house.clock() + timedelta(minutes=90)),
            },
        )
        await db.commit()
    now = house.clock()
    return note.EventInfo(
        1, house.household.id, Slot.DINNER, DAY, EventState(state), 1, "/events/1", "UTC", now + timedelta(minutes=90)
    )


async def set_event(house: House, **values: object) -> None:
    sets = ", ".join(f"{key} = :{key}" for key in values)
    async with house.sessionmaker() as db:
        await db.execute(text(f"UPDATE event SET {sets} WHERE id = 1"), values)  # noqa: S608 - names are ours
        await db.commit()


def who(house: House, *names: str) -> tuple[note.PersonInfo, ...]:
    return tuple(note.PersonInfo(house[n].id, n) for n in names)


async def enqueue(house: House, *notifications: note.Notification) -> None:
    async with house.sessionmaker() as db:
        for notification in notifications:
            event_id = notification.event_id
            await OutboxRepository(db).enqueue_with_push(house.household.id, event_id, notification, house.clock())
        await db.commit()


async def queued(house: House) -> list[tuple[str, int, str | None, bool, bool]]:
    """Each push: (whose, which device, last_error, sent, closed)."""
    names = {m.id: n for n, m in house.people.items()}
    async with house.sessionmaker() as db:
        rows = await db.scalars(select(PushRow).order_by(PushRow.id))
        return [
            (names[r.member_id], r.subscription_id, r.last_error, r.sent_at is not None, r.closed_at is not None)
            for r in rows
        ]


async def devices(house: House) -> list[int]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(PushSubscriptionRow.id).order_by(PushSubscriptionRow.id)))


@dataclass
class FakeSender:
    """Answers each push with the next status, or 201; raises when the status is an exception."""

    answers: list[int | PushResponse | Exception] = field(default_factory=list)
    sent: list[tuple[PushTarget, bytes, int, str, str | None]] = field(default_factory=list)

    async def send(
        self, target: PushTarget, payload: bytes, *, ttl: int, urgency: str, topic: str | None
    ) -> PushResponse:
        self.sent.append((target, payload, ttl, urgency, topic))
        answer = self.answers.pop(0) if self.answers else 201
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, PushResponse) else PushResponse(answer)


async def drain(house: House, sender: FakeSender) -> push.PushDrainResult:
    return await push.drain(house.sessionmaker, sender, house.clock, BASE_URL)


# Devices ----------------------------------------------------------------------------------------


async def test_a_device_needs_the_switch_and_a_member(house: House) -> None:
    with pytest.raises(NotFound):
        await device(house, "Kim")
    await switch(house, True)
    first = await device(house, "Kim")
    assert await device(house, "Kim") == first  # the same device again
    guest = replace(house["Kim"], role=Role.GUEST, email=None)
    async with house.sessionmaker() as db:
        with pytest.raises(Forbidden, match="Guests"):
            await push.subscribe(db, guest, endpoint("Pat"), P256DH, AUTH, now=house.clock())
        with pytest.raises(rules.InvalidSubscription):
            await push.subscribe(db, house["Kim"], "https://dinnerbell.test/x", P256DH, AUTH, now=house.clock())
        assert await push.unsubscribe(db, house["Kim"], endpoint("Kim"))
        assert not await push.unsubscribe(db, house["Kim"], endpoint("Kim"))
    assert await devices(house) == []


async def test_a_member_keeps_at_most_ten_devices_and_the_oldest_goes(house: House, clock: FakeClock) -> None:
    await switch(house, True)
    first = await device(house, "Kim", 0)
    for n in range(1, rules.MAX_SUBSCRIPTIONS + 1):
        clock.advance(timedelta(minutes=1))
        await device(house, "Kim", n)
    found = await devices(house)
    assert len(found) == rules.MAX_SUBSCRIPTIONS and first not in found


async def test_a_device_that_signs_in_as_someone_else_moves_to_them(house: House) -> None:
    await switch(house, True)
    kims = await device(house, "Kim")
    async with house.sessionmaker() as db:
        moved = await push.subscribe(db, house["Lee"], endpoint("Kim"), P256DH, AUTH, now=house.clock())
        assert moved == kims
        assert [s.id for s in await PushRepository(db).for_member(house.household.id, house["Lee"].id)] == [kims]


# The queue --------------------------------------------------------------------------------------


async def test_a_call_opening_queues_a_push_per_device_of_each_web_app_member(house: House) -> None:
    await link(house, "Sam", "U02SAM")  # Slack, by default
    await switch(house, True)
    await device(house, "Kim", 1)
    await device(house, "Kim", 2)
    await device(house, "Sam")
    await device(house, "Lee")
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE member SET active = 0 WHERE id = :i"), {"i": house["Lee"].id})
        await db.commit()
    event = await a_call(house)
    await enqueue(house, note.CallOpened(event, who(house, "Alex", "Sam", "Kim", "Lee")))
    assert [(name, error) for name, _, error, _, _ in await queued(house)] == [("Kim", None), ("Kim", None)]


async def test_a_retried_enqueue_queues_nothing_more(house: House) -> None:
    await switch(house, True)
    await device(house, "Kim")
    event = await a_call(house)
    opened = note.CallOpened(event, who(house, "Kim", "Lee"))
    await enqueue(house, opened)
    await device(house, "Lee")  # a device added since: the retry still adds nothing
    await enqueue(house, opened)
    assert [name for name, *_ in await queued(house)] == ["Kim"]


async def test_only_the_kinds_that_push_queue_pushes(house: House) -> None:
    await switch(house, True)
    await device(house, "Kim")
    event = await a_call(house)
    kim = who(house, "Kim")[0]
    await enqueue(
        house,
        note.CallUpdated(event),
        note.MaybeResolved(event, kim),
        note.CallAbandoned(event, note.EndReason.TOO_FEW),  # type: ignore[attr-defined]
        note.ResponseReminder(event, kim),
    )
    assert len(await queued(house)) == 1
    async with house.sessionmaker() as db:
        kinds = [
            k for (k,) in await db.execute(text("SELECT o.kind FROM push_queue p JOIN outbox o ON o.id = p.outbox_id"))
        ]
    assert kinds == ["response_reminder"]


async def test_a_muted_member_gets_no_reminder_push_and_slack_only_members_get_none(house: House) -> None:
    await link(house, "Sam", "U02SAM")
    await switch(house, True)
    async with house.sessionmaker() as db:
        await web_app.set_my_notify_via(db, house["Sam"], NotifyVia.BOTH, now=house.clock())
        await db.execute(text("UPDATE member SET mute_dm_reminders = 1 WHERE id = :i"), {"i": house["Kim"].id})
        await db.commit()
    for name in ("Kim", "Sam", "Alex"):
        await device(house, name)
    await link(house, "Alex", "U01ALEX")  # Slack by default now
    event = await a_call(house)
    sam, kim, alex = who(house, "Sam", "Kim", "Alex")
    await enqueue(house, *(note.ResponseReminder(event, p) for p in (sam, kim, alex)))
    assert [name for name, *_ in await queued(house)] == ["Sam"]


async def test_the_switch_off_queues_nothing(house: House) -> None:
    await switch(house, True)
    await device(house, "Kim")
    async with house.sessionmaker() as db:  # off, without the delete, to see the enqueue's own check
        await db.execute(text("UPDATE household SET settings = json_set(settings, '$.web_app', json('false'))"))
        await db.commit()
    event = await a_call(house)
    await enqueue(house, note.CallOpened(event, who(house, "Kim")))
    assert await queued(house) == []
    assert [e.kind for e in await house.outbox()] == ["call_opened"]


async def test_a_real_call_queues_its_pushes_through_publish(house: House, clock: FakeClock) -> None:
    await switch(house, True)
    await device(house, "Kim")
    clock.now = OPENS
    await house.tick()
    await dinner_id(house)
    assert [name for name, *_ in await queued(house)] == ["Kim"]


async def test_turning_the_switch_off_deletes_every_device_and_push_and_leaves_slack_alone(house: House) -> None:
    await switch(house, True)
    await device(house, "Kim")
    await device(house, "Lee")
    event = await a_call(house)
    await enqueue(house, note.CallOpened(event, who(house, "Kim", "Lee")))
    async with house.sessionmaker() as db:
        await rejected_burst(db, house.household.id, house.clock())
        await db.commit()
    before = await house.outbox()
    await switch(house, False)
    assert await devices(house) == [] and await queued(house) == []
    assert await house.outbox() == before
    async with house.sessionmaker() as db:
        settings = (await db.execute(text("SELECT settings FROM household"))).scalar_one()
        status = await push.status(db, house.household.id, house.clock())
    assert "push_rejections" not in str(settings) and status == push.PushStatus(0, 0, None)


# The drainer ------------------------------------------------------------------------------------


async def test_the_drainer_sends_a_declarative_push_with_its_headers(house: House) -> None:
    await switch(house, True)
    kims = await device(house, "Kim")
    event = await a_call(house)
    await enqueue(house, note.CallOpened(event, who(house, "Kim")))
    sender = FakeSender()
    result = await drain(house, sender)
    assert (result.sent, result.closed, result.failed) == (1, 0, 0)
    ((target, body, ttl, urgency, topic),) = sender.sent
    assert target == PushTarget(kims, endpoint("Kim"), P256DH, AUTH)
    assert b'"web_push":8030' in body and f'"navigate":"{BASE_URL}/events/1"'.encode() in body
    assert (ttl, urgency, topic) == (90 * 60, "high", rules.topic(house.household.id, 1, 1))
    assert await queued(house) == [("Kim", kims, None, True, False)]
    assert (await drain(house, sender)).sent == 0  # sent once
    async with house.sessionmaker() as db:
        row = await db.get(PushSubscriptionRow, kims)
    assert row is not None and row.last_success_at == house.clock()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"state": "voting"}, note.SUPERSEDED_BY_PROGRESS),
        ({"state": "cancelled"}, note.SUPERSEDED_BY_CALL_OFF),
        ({"run": 2}, note.SUPERSEDED_BY_CALL_OFF),
    ],
)
async def test_a_direct_message_the_call_moved_past_closes_unsent(
    house: House, change: dict[str, object], reason: str
) -> None:
    """The outbox's own rules (``outbox.overtaken_by``): "Are you in?" never follows the vote."""
    await switch(house, True)
    await device(house, "Kim")
    event = await a_call(house)
    await enqueue(house, note.ResponseReminder(event, who(house, "Kim")[0]))
    await set_event(house, **change)
    sender = FakeSender()
    assert (await drain(house, sender)).closed == 1
    assert sender.sent == [] and [error for _, _, error, *_ in await queued(house)] == [reason]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"state": "voting"}, rules.MOVED_ON),
        ({"state": "cancelled"}, note.SUPERSEDED_BY_CALL_OFF),
        ({"run": 2}, rules.STARTED_AGAIN),
    ],
)
async def test_a_channel_post_the_call_moved_past_closes_unsent(
    house: House, change: dict[str, object], reason: str
) -> None:
    await switch(house, True)
    await device(house, "Kim")
    event = await a_call(house)
    await enqueue(house, note.CallOpened(event, who(house, "Kim")))
    await set_event(house, **change)
    sender = FakeSender()
    assert (await drain(house, sender)).closed == 1
    assert sender.sent == [] and [error for _, _, error, *_ in await queued(house)] == [reason]


async def test_each_push_checks_the_switch_and_the_members_choice_again(house: House) -> None:
    await link(house, "Sam", "U02SAM")
    await switch(house, True)
    async with house.sessionmaker() as db:
        await web_app.set_my_notify_via(db, house["Sam"], NotifyVia.WEB, now=house.clock())
    await device(house, "Sam")
    await device(house, "Kim")
    event = await a_call(house)
    await enqueue(house, note.CallOpened(event, who(house, "Sam", "Kim")))
    await house.refresh("Sam")
    async with house.sessionmaker() as db:
        await web_app.set_my_notify_via(db, house["Sam"], NotifyVia.SLACK, now=house.clock())
    result = await drain(house, FakeSender())
    assert (result.sent, result.closed) == (1, 1)
    assert sorted((n, e) for n, _, e, *_ in await queued(house)) == [("Kim", None), ("Sam", push.NOT_WANTED)]


async def test_a_push_queued_before_the_switch_went_off_never_goes(house: House) -> None:
    await switch(house, True)
    await device(house, "Kim")
    event = await a_call(house)
    await enqueue(house, note.CallOpened(event, who(house, "Kim")))
    async with house.sessionmaker() as db:  # off as another process sees it before the delete commits
        await db.execute(text("UPDATE household SET settings = json_set(settings, '$.web_app', json('false'))"))
        await db.commit()
    sender = FakeSender()
    assert (await drain(house, sender)).closed == 1 and sender.sent == []
    assert [e for _, _, e, *_ in await queued(house)] == [push.SWITCHED_OFF]


async def test_a_push_waiting_a_day_expires(house: House, clock: FakeClock) -> None:
    await switch(house, True)
    await device(house, "Kim")
    await enqueue(house, note.GuestJoined(house.household.id, who(house, "Kim")[0], note.PersonInfo(9, "Pat"), "web"))
    clock.advance(push.MAX_AGE + timedelta(seconds=1))
    sender = FakeSender()
    assert (await drain(house, sender)).closed == 1 and sender.sent == []
    assert [e for _, _, e, *_ in await queued(house)] == [push.EXPIRED]


async def test_a_stored_endpoint_off_the_allowlist_is_never_posted_to(house: House) -> None:
    await one_push(house)
    async with house.sessionmaker() as db:  # as if stored before the allowlist changed
        await db.execute(text("UPDATE push_subscription SET endpoint = 'https://169.254.169.254/x'"))
        await db.commit()
    sender = FakeSender()
    assert (await drain(house, sender)).closed == 1 and sender.sent == []
    assert [e for _, _, e, *_ in await queued(house)] == [push.UNKNOWN_SERVICE]


# The push service's answers ---------------------------------------------------------------------


async def one_push(house: House) -> int:
    await switch(house, True)
    kims = await device(house, "Kim")
    event = await a_call(house)
    await enqueue(house, note.CallOpened(event, who(house, "Kim")))
    return kims


@pytest.mark.parametrize("status", [404, 410])
async def test_a_device_that_is_gone_is_deleted_at_once(house: House, status: int) -> None:
    await one_push(house)
    result = await drain(house, FakeSender(answers=[status]))
    assert result.devices_deleted == 1 and await devices(house) == [] and await queued(house) == []


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_signature_deletes_the_device_after_three_in_a_day(
    house: House, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    """Not at once, so a clock or signing bug can't wipe every device in one pass; the third
    refusal in a household within the hour also logs an error and warns on Admin."""
    await switch(house, True)
    kims = await device(house, "Kim")
    event = await a_call(house)
    kim, sam = who(house, "Kim", "Sam")
    now = house.clock()
    await enqueue(house, *(note.CoordinatingGiven(event, kim, sam, now + timedelta(seconds=n)) for n in range(3)))
    async with house.sessionmaker() as db:
        assert (await push.status(db, house.household.id, now)).rejected_at is None
    caplog.set_level(logging.INFO, logger="dinnerbell.services.push")
    for strike in (1, 2):
        result = await push.drain(house.sessionmaker, FakeSender(answers=[status]), house.clock, BASE_URL, limit=1)
        assert (result.failed, result.devices_deleted) == (1, 0), strike
        assert await devices(house) == [kims]
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    result = await push.drain(house.sessionmaker, FakeSender(answers=[status]), house.clock, BASE_URL, limit=1)
    assert result.devices_deleted == 1 and await devices(house) == [] and await queued(house) == []
    burst = [r for r in caplog.records if r.levelname == "ERROR"]
    assert [r.getMessage() for r in burst] == [
        "push services are refusing Dinnerbell's signature; check the clock and the secret key"
    ]
    async with house.sessionmaker() as db:
        assert (await push.status(db, house.household.id, now)).rejected_at == now


async def test_refusals_more_than_a_day_apart_start_counting_again(house: House, clock: FakeClock) -> None:
    kims = await one_push(house)
    async with house.sessionmaker() as db:
        repo = PushRepository(db)
        assert await repo.rejected(kims, clock()) == 1
        assert await repo.rejected(kims, clock() + timedelta(hours=1)) == 2
        assert await repo.rejected(kims, clock() + timedelta(hours=25)) == 1
        await db.commit()


@pytest.mark.parametrize("status", [413, 400])
async def test_a_push_the_service_wont_take_fails_for_good(house: House, status: int) -> None:
    kims = await one_push(house)
    result = await drain(house, FakeSender(answers=[status]))
    assert result.failed == 1 and await devices(house) == [kims]
    assert await queued(house) == [("Kim", kims, f"failed: {status}", False, True)]


@pytest.mark.parametrize(
    ("answer", "wait", "note_"),
    [
        (PushResponse(429, retry_after=120), timedelta(seconds=120), "status 429"),
        (PushResponse(503), timedelta(seconds=30), "status 503"),
        (httpx.ConnectTimeout("timed out"), timedelta(seconds=30), "ConnectTimeout"),
    ],
)
async def test_busy_or_unreachable_retries_with_backoff_honoring_retry_after(
    house: House, clock: FakeClock, answer: PushResponse | Exception, wait: timedelta, note_: str
) -> None:
    kims = await one_push(house)
    result = await drain(house, FakeSender(answers=[answer]))
    assert result.retrying == 1 and await queued(house) == [("Kim", kims, note_, False, False)]
    clock.advance(wait - timedelta(seconds=1))
    assert (await drain(house, FakeSender())).sent == 0  # not yet
    clock.advance(timedelta(seconds=1))
    assert (await drain(house, FakeSender())).sent == 1


async def test_a_push_that_keeps_failing_gives_up(house: House, clock: FakeClock) -> None:
    await one_push(house)
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE push_queue SET attempts = :a"), {"a": push.MAX_ATTEMPTS - 1})
        await db.commit()
    result = await drain(house, FakeSender(answers=[500]))
    assert result.failed == 1 and [e for _, _, e, *_ in await queued(house)] == ["gave up: status 500"]


async def test_logs_name_the_device_never_its_endpoint(house: House, caplog: pytest.LogCaptureFixture) -> None:
    """The autouse leak check refuses any push host in a log line; this makes sure there are lines."""
    caplog.set_level(logging.DEBUG, logger="dinnerbell")
    kims = await one_push(house)
    await drain(house, FakeSender(answers=[503]))
    lines = [r for r in caplog.records if r.name == "dinnerbell.services.push" and hasattr(r, "push_id")]
    assert lines and {getattr(r, "subscription_id", None) for r in lines} == {kims}


# The outbox, unchanged ----------------------------------------------------------------------------


async def test_a_member_read_that_fails_mid_drain_sends_through_slack_once(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``outbox._skipped`` fails open: if reading the member raises, the direct message goes to
    Slack, delivered exactly once, as if the switch were off."""
    slack: Slack = await wire(house, linked=("Alex", "Kim"))
    await switch(house, True)
    async with house.sessionmaker() as db:
        await web_app.set_my_notify_via(db, house["Kim"], NotifyVia.WEB, now=house.clock())
    event = await a_call(house)
    async with house.sessionmaker() as db:
        reminder = note.ResponseReminder(event, who(house, "Kim")[0])
        await OutboxRepository(db).enqueue(house.household.id, event.id, reminder, house.clock())
        await db.commit()
    real_get = MemberRepository.get

    async def unreadable(self: MemberRepository, household_id: int, member_id: int) -> object:
        if member_id == house["Kim"].id:
            raise RuntimeError("database is locked")
        return await real_get(self, household_id, member_id)

    monkeypatch.setattr(MemberRepository, "get", unreadable)
    result = await outbox.drain(house.sessionmaker, slack.conn.channel, house.clock)
    assert (result.delivered, result.skipped, result.failed) == (1, 0, 0)
    assert [c.args["channel"] for c in slack.fake.of("chat.postMessage")] == ["U03KIM"]
    again = await outbox.drain(house.sessionmaker, slack.conn.channel, house.clock)
    assert again.delivered == 0 and len(slack.fake.of("chat.postMessage")) == 1
    (row,) = await house.outbox()
    async with house.sessionmaker() as db:
        state = (await db.execute(text("SELECT delivered_at IS NOT NULL, last_error FROM outbox"))).one()
    assert tuple(state) == (1, None) and row.attempts == 1
    assert any(r.getMessage().startswith("web app switch or member unreadable") for r in caplog.records)


# The pages --------------------------------------------------------------------------------------


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    sign_in(client, provider)
    return client


def admin_switch(client: TestClient, on: bool) -> None:
    assert submit(client, "/admin/web-app", {"on": "on"} if on else {}).status_code == 303


FORM = {"endpoint": endpoint("Alex"), "p256dh": P256DH, "auth": AUTH}


def subscriptions(settings: Settings) -> list[str]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(settings.sync_database_url)
    try:
        with Session(engine) as db:
            return list(db.scalars(select(PushSubscriptionRow.endpoint)))
    finally:
        engine.dispose()


def test_the_routes_need_the_switch_and_a_csrf_token(admin: TestClient, migrated: Settings) -> None:
    for path, data in (("/app/notifications", FORM), ("/app/notifications/off", {"endpoint": FORM["endpoint"]})):
        assert submit(admin, path, data).status_code == 404, path  # off: no such page
    admin_switch(admin, True)
    assert admin.post("/app/notifications", data=FORM).status_code == 403  # no CSRF token
    assert admin.post("/app/notifications", data=FORM, headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert subscriptions(migrated) == []
    assert submit(admin, "/app/notifications", FORM).status_code == 204
    assert subscriptions(migrated) == [FORM["endpoint"]]
    bad = submit(admin, "/app/notifications", FORM | {"endpoint": "https://169.254.169.254/latest"})
    assert bad.status_code == 400 and "push service" in bad.text
    assert submit(admin, "/app/notifications/off", {"endpoint": FORM["endpoint"]}).status_code == 204
    assert subscriptions(migrated) == []
    signed_out = TestClient(admin.app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False)
    assert signed_out.post("/app/notifications", data=FORM).status_code in (303, 401, 403)


def test_get_the_app_offers_notifications_to_a_signed_in_member(admin: TestClient) -> None:
    admin_switch(admin, True)
    html = admin.get("/app").text
    scripts, problems = inline_code(html)
    assert problems == [] and any(s.startswith("/static/push.js") for s in scripts)
    card = html.split('id="notifications"', 1)[1].split("</section>", 1)[0]
    assert "hidden" in card.split(">", 1)[0] and "Turn on notifications" in card and "Turn off" in card
    key = card.split('data-key="', 1)[1].split('"', 1)[0]
    assert key == admin.app.state.dinnerbell.vapid_public_key and len(key) == 87  # type: ignore[attr-defined]
    signed_out = TestClient(admin.app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False)
    assert 'id="notifications"' not in signed_out.get("/app").text
    js = admin.get("/static/push.js")
    assert js.status_code == 200 and "applicationServerKey" in js.text and "X-CSRF-Token" in js.text


def test_the_service_worker_keeps_its_policy_and_icon_line(admin: TestClient) -> None:
    from dinnerbell.web.routes.web_app import WORKER_CONTENT_SECURITY_POLICY, WORKER_ICON

    admin_switch(admin, True)
    worker = admin.get("/sw.js")
    assert worker.headers["content-security-policy"] == WORKER_CONTENT_SECURITY_POLICY
    assert WORKER_ICON in worker.text and "showNotification" in worker.text and "navigate" in worker.text


def test_admin_shows_devices_and_warns_after_a_burst_of_refusals(admin: TestClient, migrated: Settings) -> None:
    admin_switch(admin, True)
    assert submit(admin, "/app/notifications", FORM).status_code == 204
    html = page(admin.get("/admin/web-app"))
    assert "1 device gets notifications." in html and 'id="push-rejected"' not in html
    assert 'id="not-built-yet"' in html  # the owner removes it once both lanes land
    from sqlalchemy import create_engine

    engine = create_engine(migrated.sync_database_url)
    try:
        with engine.begin() as c:
            stamp = admin.app.state.dinnerbell.clock().isoformat()  # type: ignore[attr-defined]
            c.execute(
                text("UPDATE household SET settings = json_set(settings, '$.push_rejected_at', :t)"), {"t": stamp}
            )
    finally:
        engine.dispose()
    warned = page(admin.get("/admin/web-app"))
    assert "Push services are refusing notifications." in warned and "DINNERBELL_SECRET_KEY" in warned


def test_every_use_case_queues_notifications_through_the_push_hook() -> None:
    """Only ``db/outbox.py`` calls ``enqueue`` itself; everything else calls ``enqueue_with_push``,
    so a new kind that pushes can't be queued without its pushes (spec 6.15)."""
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "src" / "dinnerbell"
    direct = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if ".enqueue(" in path.read_text() and path.name != "outbox.py"
    )
    assert direct == []
    assert ".enqueue(" not in (package / "services" / "outbox.py").read_text()
    users = {p.name for p in package.rglob("*.py") if ".enqueue_with_push(" in p.read_text()}
    assert {"call_context.py", "guests.py", "recap.py", "discard.py", "schedule.py", "announcements.py"} <= users
