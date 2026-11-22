# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dinnerbone's local weather (#319, ADR 0011): Admin's setting, the scheduler's refresh, and
what pages read.

Off by default. An admin turns it on with a postal code on Admin > Web app; the save geocodes it
once and stores the place rounded to 0.1°. The scheduler's ``refresh`` asks Open-Meteo for each
household that's due, and sets the next fetch to a random time before it asks, so fetches come
1 to 3 hours apart (``domain.weather.next_fetch``). Pages read only the stored answer
(``weather_now``), never the network, so a slow or failing weather service can't slow a page:
the den falls back to the seasonal weather.

Logs name the household and the failure's reason, never the postal code, place, or coordinates,
which locate the household: those go only to debug lines. So do tracebacks, since an exception's
message can quote a URL or a query's parameters. The first failure logs a warning, the ones after
it debug lines, and the first answer after them an info line.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.engine import write_transaction
from dinnerbell.db.local_weather import LocalWeatherRepository
from dinnerbell.domain.members import Member
from dinnerbell.domain.weather import (
    DEADLINE_SECONDS,
    DEFAULT_COUNTRY,
    InvalidLocation,
    LocalWeather,
    Observation,
    Place,
    Weather,
    WeatherError,
    WeatherFailure,
    next_fetch,
    normalize_country,
    normalize_location,
)
from dinnerbell.services.common import audit, require_admin

__all__ = [
    "InvalidLocation",
    "LocationNotSaved",
    "WeatherSource",
    "refresh",
    "save_setting",
    "setting",
    "weather_now",
]

log = logging.getLogger(__name__)

ADMINS_ONLY = "Only the household admin can change Dinnerbone's weather."
NOT_FOUND = "Dinnerbell couldn't find that postal code. Check it and the country, then save again."
UNREACHABLE = "Dinnerbell couldn't reach the weather service to look up that postal code. Try again in a few minutes."


class WeatherSource(Protocol):
    """Where local weather comes from: ``services.open_meteo.OpenMeteo``, or a fake in tests."""

    async def geocode(self, postal_code: str, country: str) -> Place: ...

    async def current(self, place: Place) -> Observation: ...


Draw = Callable[[], float]
"""A number from 0 to 1, for where in the 1-to-3-hour window the next fetch falls."""


class LocationNotSaved(Exception):
    """The postal code found no place, or the lookup failed: nothing was saved. The message is
    for the admin."""


@dataclass(frozen=True, slots=True)
class Saved:
    changed: bool
    """False when the setting was already this, and nothing was written or audited."""
    on: bool


async def setting(db: AsyncSession, household_id: int) -> LocalWeather | None:
    """The household's setting and last answer, or None if no admin has saved it."""
    return await LocalWeatherRepository(db).get(household_id)


async def weather_now(db: AsyncSession, household_id: int, now: datetime) -> tuple[Weather | None, bool]:
    """What a page shows: the fresh local weather or None, and whether the credit shows (local
    weather is on). Reads only the database, and never raises: if the read fails, the den shows
    the seasonal weather."""
    try:
        stored = await LocalWeatherRepository(db).get(household_id)
    except Exception as exc:
        log.warning("could not read local weather", extra={"household_id": household_id, "error": type(exc).__name__})
        log.debug("could not read local weather", exc_info=True, extra={"household_id": household_id})
        await db.rollback()
        return None, False
    if stored is None:
        return None, False
    return stored.weather(now), stored.on


async def save_setting(
    db: AsyncSession,
    actor: Member,
    *,
    on: bool,
    postal_code: str,
    country: str,
    source: WeatherSource,
    now: datetime,
) -> Saved:
    """Admin's save (ADR 0011). On, the postal code must be valid (``InvalidLocation``) and find
    a place (``LocationNotSaved``); the place is looked up only when the code or country changed,
    or it had none. Off, the code and country are kept, and the place and every answer are
    deleted. Audited as ``household.weather_updated`` with ``on`` and whether the postal code
    changed, never the code itself, unless nothing changed."""
    require_admin(actor, ADMINS_ONLY)
    household_id = actor.household_id
    repo = LocalWeatherRepository(db)
    current = await repo.get(household_id)
    if on:
        postal_code, country = normalize_location(postal_code, country)
    else:
        postal_code, country = _kept(postal_code, country, current)
        if current is None and not postal_code:
            return Saved(changed=False, on=False)
    moved = current is None or (current.postal_code, current.country) != (postal_code, country)
    if current is not None and current.enabled == on and not moved and (current.place is not None or not on):
        return Saved(changed=False, on=on)
    place = None
    if on:
        place = current.place if current is not None and current.place is not None and not moved else None
        if place is None:
            place = await _look_up(source, household_id, postal_code, country)
    async with write_transaction(db):
        await repo.save_setting(household_id, postal_code=postal_code, country=country, place=place, now=now)
        payload = {"on": on, "postal_code_changed": moved}
        await audit(db, household_id, actor, "household.weather_updated", f"household:{household_id}", payload, now)
        await db.commit()
    if on:
        log.info("local weather turned on", extra={"household_id": household_id, "member_id": actor.id})
    else:
        log.info("local weather turned off", extra={"household_id": household_id, "member_id": actor.id})
    return Saved(changed=True, on=on)


def _kept(postal_code: str, country: str, current: LocalWeather | None) -> tuple[str, str]:
    """What an off save keeps for when it's on again: the code as typed if it's valid, nothing if
    it's blank, and otherwise what was saved before."""
    fallback = (current.postal_code, current.country) if current is not None else ("", DEFAULT_COUNTRY)
    try:
        if not postal_code.strip():
            return "", normalize_country(country)
        return normalize_location(postal_code, country)
    except InvalidLocation:
        return ("", fallback[1]) if not postal_code.strip() else fallback


async def _look_up(source: WeatherSource, household_id: int, postal_code: str, country: str) -> Place:
    try:
        place = await source.geocode(postal_code, country)
    except WeatherError as exc:
        log.warning("postal code lookup failed", extra={"household_id": household_id, "reason": exc.reason.value})
        raise LocationNotSaved(NOT_FOUND if exc.reason is WeatherFailure.NOT_FOUND else UNREACHABLE) from None
    except Exception:
        log.warning("postal code lookup failed", extra={"household_id": household_id, "reason": "error"})
        log.debug("postal code lookup raised", exc_info=True, extra={"household_id": household_id})
        raise LocationNotSaved(UNREACHABLE) from None
    log.debug("postal code found", extra={"household_id": household_id, "place": place.name})
    return place


# The scheduler's refresh -------------------------------------------------------------------------


async def refresh(
    sessionmaker: async_sessionmaker[AsyncSession],
    source: WeatherSource,
    now: datetime,
    *,
    draw: Draw = random.random,
) -> int:
    """Ask for every household that's due, and return how many asked. Never raises: a failure,
    whatever it is, is stored and logged, and the den shows the last good answer or the seasonal
    weather."""
    try:
        async with sessionmaker() as db:
            due = await LocalWeatherRepository(db).due(now)
    except Exception as exc:
        log.warning("could not read which local weather is due", extra={"error": type(exc).__name__})
        log.debug("could not read which local weather is due", exc_info=True)
        return 0
    asked = 0
    for stored in due:
        try:
            if await _refresh_one(sessionmaker, source, stored, now, draw):
                asked += 1
        except Exception as exc:
            fields = {"household_id": stored.household_id}
            log.warning("local weather refresh failed", extra=fields | {"error": type(exc).__name__})
            log.debug("local weather refresh failed", exc_info=True, extra=fields)
    return asked


async def _refresh_one(
    sessionmaker: async_sessionmaker[AsyncSession],
    source: WeatherSource,
    stored: LocalWeather,
    now: datetime,
    draw: Draw,
) -> bool:
    place = stored.place
    if place is None:  # pragma: no cover - ``due`` reads only rows with a place
        return False
    household_id = stored.household_id
    next_at = next_fetch(now, min(max(draw(), 0.0), 1.0))
    async with sessionmaker() as db, write_transaction(db):
        claimed = await LocalWeatherRepository(db).claim(household_id, place, now, next_at)
        await db.commit()
    if not claimed:
        return False  # turned off, moved, or another process asked first
    try:
        async with asyncio.timeout(DEADLINE_SECONDS + 1):  # whatever the source, it can't hold the job
            observation = await source.current(place)
    except TimeoutError:
        await _failed(sessionmaker, stored, place, WeatherFailure.TIMEOUT, now)
        return True
    except WeatherError as exc:
        await _failed(sessionmaker, stored, place, exc.reason, now)
        return True
    except Exception:
        log.debug("local weather fetch raised", exc_info=True, extra={"household_id": household_id})
        await _failed(sessionmaker, stored, place, WeatherFailure.ERROR, now)
        return True
    async with sessionmaker() as db, write_transaction(db):
        recorded = await LocalWeatherRepository(db).record_answer(household_id, place, observation, now)
        await db.commit()
    if recorded and stored.failing_since is not None:
        minutes = max(0, round((now - stored.failing_since).total_seconds() / 60))
        log.info("local weather recovered", extra={"household_id": household_id, "failing_minutes": minutes})
    log.debug(
        "local weather fetched",
        extra={"household_id": household_id, "place": place.name, "code": observation.code, "next_fetch_at": next_at},
    )
    return True


async def _failed(
    sessionmaker: async_sessionmaker[AsyncSession],
    stored: LocalWeather,
    place: Place,
    reason: WeatherFailure,
    now: datetime,
) -> None:
    household_id = stored.household_id
    since = stored.failing_since or now
    async with sessionmaker() as db, write_transaction(db):
        recorded = await LocalWeatherRepository(db).record_failure(household_id, place, reason, since, now)
        await db.commit()
    fields = {"household_id": household_id, "reason": reason.value}
    if recorded and stored.failing_since is None:
        log.warning("local weather failed; the den shows the seasonal weather", extra=fields)
    else:
        log.debug("local weather still failing", extra=fields)
