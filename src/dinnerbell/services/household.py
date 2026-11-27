# SPDX-License-Identifier: AGPL-3.0-or-later
"""The household row: the source of truth for its name and time zone.

DINNERBELL_HOUSEHOLD_NAME and DINNERBELL_TIMEZONE only seed it on first start. After that,
``dinnerbell set-timezone`` and ``dinnerbell set-household-name`` change it (#334), each audited
with no actor, as ``admin add`` is: whoever runs them has a shell on the host.

Everything reads the zone from the row when it needs it, except the scheduler, which reads it at
start for the nightly backup and outbox prune; ``scheduler.runner.follow_zone`` moves both to a
changed zone within a minute, so nobody needs to restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import EventRepository
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.household import Household, check_name, check_timezone
from dinnerbell.services.common import audit
from dinnerbell.services.reset import OpenCall

log = logging.getLogger(__name__)

OPEN_CALLS = (
    "A call is still open, so nothing changed: its times were set in the household's current time zone. "
    "Let each one below finish, or call it off (Call it off on its card, or in Slack), then run set-timezone again."
)


class NoHousehold(LookupError):
    """The database has no household yet: bootstrap hasn't run."""


class TimezoneRefused(Exception):
    """The time zone didn't change because a call is open. The message lists them."""


async def current_household(db: AsyncSession) -> Household:
    household = await HouseholdRepository(db).first()
    if household is None:
        raise NoHousehold("no household; bootstrap has not run")
    return household


@dataclass(frozen=True, slots=True)
class Changed:
    """What a ``set-`` command did: ``before`` and ``after`` are equal when nothing changed."""

    before: str
    after: str

    @property
    def changed(self) -> bool:
        return self.before != self.after


async def set_timezone(db: AsyncSession, zone: str, *, now: datetime) -> Changed:
    """Change the household's time zone to ``zone``, an IANA name. Raises ``InvalidHouseholdInput``
    for a name that isn't one, ``NoHousehold`` before the first start, and ``TimezoneRefused``
    while any call is open, scheduled ones included: their times were set in the old zone. It
    checks and writes under the write lock, so no call opens in between. Audited as
    ``household.timezone_changed``; nothing is written when the zone is the same."""
    zone = check_timezone(zone)
    async with write_transaction(db):
        household = await current_household(db)
        if household.timezone == zone:
            return Changed(zone, zone)
        events = EventRepository(db)
        open_calls = [await events.get(household.id, event_id) for event_id in await events.open_ids(household.id)]
        lines = [OpenCall(e.id, e.slot, e.day, e.state).line() for e in open_calls if e is not None]
        if lines:
            raise TimezoneRefused("\n".join([OPEN_CALLS, *(f"  {line}" for line in lines)]))
        await HouseholdRepository(db).set_timezone(household.id, zone)
        payload = {"from": household.timezone, "to": zone}
        await audit(db, household.id, None, "household.timezone_changed", f"household:{household.id}", payload, now)
        await db.commit()
    extra = {"household_id": household.id, "from_zone": household.timezone, "to_zone": zone}
    log.info("household time zone changed from the command line", extra=extra)
    return Changed(household.timezone, zone)


async def set_name(db: AsyncSession, name: str, *, now: datetime) -> Changed:
    """Rename the household. Raises ``InvalidHouseholdInput`` for an empty or too long name and
    ``NoHousehold`` before the first start. Pages read the name on each load, so it shows at
    once. Audited as ``household.renamed``; the log line names neither name (SEC-5)."""
    name = check_name(name)
    async with write_transaction(db):
        household = await current_household(db)
        if household.name == name:
            return Changed(name, name)
        await HouseholdRepository(db).rename(household.id, name)
        payload = {"from": household.name, "to": name}
        await audit(db, household.id, None, "household.renamed", f"household:{household.id}", payload, now)
        await db.commit()
    log.info("household renamed from the command line", extra={"household_id": household.id})
    return Changed(household.name, name)
