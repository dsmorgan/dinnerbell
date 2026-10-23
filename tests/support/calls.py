# SPDX-License-Identifier: AGPL-3.0-or-later
"""A household with members and a catalog, for driving dinner calls through the services with
a fake clock. Times are UTC; the household is in Chicago (UTC-5 in October), so the 15:00 dinner
call opens at 20:00 UTC, the reminder goes at 21:15, responses close at 21:30, votes at 22:15,
and the 18:30 default meal time is 23:30."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.catalog import CatalogRepository, ItemValues
from dinnerbell.db.events import EventRepository
from dinnerbell.db.models import AuditRow, MealLogRow, MemberRow
from dinnerbell.db.outbox import OutboxEntry, OutboxRepository
from dinnerbell.db.repositories import MemberRepository, to_member
from dinnerbell.domain.catalog import CatalogItem, ItemKind, Mode, Tag, TagKind
from dinnerbell.domain.events import Event
from dinnerbell.domain.household import Household
from dinnerbell.domain.members import Capabilities, Member, Role
from dinnerbell.domain.notifications import DeliveryResult, MessageRef, Notification
from dinnerbell.services import lifecycle, outbox
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.call_context import CallContext, load
from tests.support.clock import FakeClock

ZONE = "America/Chicago"
OPENS = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
REMIND = datetime(2026, 10, 22, 21, 15, tzinfo=UTC)
RESPOND_BY = datetime(2026, 10, 22, 21, 30, tzinfo=UTC)
VOTE_BY = datetime(2026, 10, 22, 22, 15, tzinfo=UTC)
MEAL = datetime(2026, 10, 22, 23, 30, tzinfo=UTC)
DONE_AT = datetime(2026, 10, 23, 1, 30, tzinfo=UTC)


@dataclass
class Sent:
    op: str
    kind: str
    key: str
    recipient: int | None
    thread: MessageRef | None
    ref: MessageRef | None


@dataclass
class RecordingChannel:
    """Records what a channel was asked to do. Set ``fail`` to make the next calls raise."""

    sent: list[Sent] = field(default_factory=list)
    fail: int = 0
    counter: int = 0

    async def deliver(self, notification: Notification, *, thread: MessageRef | None = None) -> DeliveryResult:
        self._maybe_fail()
        self.counter += 1
        ref = MessageRef("C1", f"{self.counter}.0")
        key = notification.idempotency_key
        self.sent.append(Sent("deliver", notification.kind, key, notification.recipient_id, thread, ref))
        return DeliveryResult(ref)

    async def update(self, ref: MessageRef, notification: Notification) -> None:
        self._maybe_fail()
        key = notification.idempotency_key
        self.sent.append(Sent("update", notification.kind, key, notification.recipient_id, None, ref))

    def _maybe_fail(self) -> None:
        if self.fail:
            self.fail -= 1
            raise ConnectionError("slack is down")

    @property
    def kinds(self) -> list[str]:
        return [s.kind for s in self.sent]


@dataclass
class House:
    """The household, its people (by first name), and its catalog (by item name)."""

    sessionmaker: async_sessionmaker[AsyncSession]
    clock: FakeClock
    household: Household
    people: dict[str, Member] = field(default_factory=dict)
    items: dict[str, CatalogItem] = field(default_factory=dict)
    tags: dict[str, Tag] = field(default_factory=dict)

    def __getitem__(self, name: str) -> Member:
        return self.people[name]

    async def add_member(self, name: str, *, admin: bool = False, **caps: bool) -> Member:
        async with self.sessionmaker() as db:
            member = await MemberRepository(db).add(
                household_id=self.household.id,
                display_name=name,
                email=f"{name.lower()}@example.com",
                role=Role.ADMIN if admin else Role.MEMBER,
                capabilities=Capabilities(**caps),
                now=self.clock(),
            )
            await db.commit()
        self.people[name] = member
        return member

    async def refresh(self, name: str) -> Member:
        async with self.sessionmaker() as db:
            row = await db.scalar(select(MemberRow).where(MemberRow.id == self.people[name].id))
            assert row is not None
            self.people[name] = to_member(row)
        return self.people[name]

    async def tag(self, name: str, kind: TagKind = TagKind.CUISINE) -> Tag:
        async with self.sessionmaker() as db:
            tag = await CatalogRepository(db).add_tag(self.household.id, kind, name, self.clock())
            await db.commit()
        self.tags[name] = tag
        return tag

    async def add_item(
        self,
        name: str,
        *modes: Mode,
        cuisine: str = "Thai",
        tags: tuple[str, ...] = (),
        cooks: tuple[str, ...] = (),
        url: str | None = None,
    ) -> CatalogItem:
        for tag_name in (cuisine, *tags):
            if tag_name not in self.tags:
                await self.tag(tag_name, TagKind.CUISINE if tag_name == cuisine else TagKind.DIETARY)
        modes = modes or (Mode.PICKUP,)
        kind = ItemKind.HOME if Mode.COOK in modes else ItemKind.RESTAURANT
        values = ItemValues(
            kind=kind,
            name=name,
            modes=frozenset(modes),
            tag_ids=frozenset(self.tags[t].id for t in (cuisine, *tags)),
            cook_ids=frozenset(self.people[c].id for c in cooks),
            url=url,
            order_url=None,
            notes=None,
            default_servings=None,
            active=True,
        )
        async with self.sessionmaker() as db:
            item = await CatalogRepository(db).add_item(self.household.id, values, created_by=None, now=self.clock())
            await db.commit()
        self.items[name] = item
        return item

    async def tick(self) -> list[Any]:
        return await lifecycle.tick(self.sessionmaker, self.clock())

    async def drain(self, channel: RecordingChannel) -> outbox.DrainResult:
        return await outbox.drain(self.sessionmaker, channel, self.clock)

    async def outbox(self) -> list[OutboxEntry]:
        async with self.sessionmaker() as db:
            return await OutboxRepository(db).for_household(self.household.id)

    async def kinds(self) -> list[str]:
        return [e.kind for e in await self.outbox()]

    async def event(self, event_id: int) -> Event:
        async with self.sessionmaker() as db:
            event = await EventRepository(db).get(self.household.id, event_id)
        assert event is not None
        return event

    async def context(self, event_id: int) -> CallContext:
        async with self.sessionmaker() as db:
            return await load(db, await self.event(event_id))

    async def audits(self, prefix: str = "event.") -> list[AuditRow]:
        async with self.sessionmaker() as db:
            rows = await db.scalars(select(AuditRow).where(AuditRow.action.startswith(prefix)).order_by(AuditRow.id))
            return list(rows)

    async def meals(self) -> list[MealLogRow]:
        async with self.sessionmaker() as db:
            return list(await db.scalars(select(MealLogRow).order_by(MealLogRow.id)))


async def make_house(sessionmaker: async_sessionmaker[AsyncSession], clock: FakeClock, admin_email: str) -> House:
    async with sessionmaker() as db:
        household = await bootstrap(db, household_name="Home", timezone=ZONE, admin_email=admin_email, now=clock())
        row = await db.scalar(select(MemberRow).where(MemberRow.email == admin_email))
        assert row is not None
        admin = await MemberRepository(db).update(household.id, row.id, clock(), display_name="Alex")
        await db.commit()
    house = House(sessionmaker, clock, household)
    house.people["Alex"] = admin
    return house
