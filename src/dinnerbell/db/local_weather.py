# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dinnerbone's local weather (#319, ADR 0011): Admin's setting and the scheduler's answers, in
``local_weather``. The scheduler's writes apply only while the place it fetched for is still the
one saved and the setting is on, so a fetch that finishes after the admin turned it off or moved
it changes nothing."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import LocalWeatherRow
from dinnerbell.domain.weather import LocalWeather, Observation, Place, WeatherFailure


def _entity(row: LocalWeatherRow) -> LocalWeather:
    place = None
    if row.latitude is not None and row.longitude is not None and row.place is not None:
        place = Place(row.latitude, row.longitude, row.place)
    observation = None
    if row.code is not None and row.temperature_c is not None and row.wind_kmh is not None:
        observation = Observation(row.code, row.temperature_c, row.wind_kmh)
    failure = None
    if row.failure is not None:
        failure = WeatherFailure(row.failure) if row.failure in WeatherFailure else WeatherFailure.ERROR
    return LocalWeather(
        household_id=row.household_id,
        enabled=row.enabled,
        postal_code=row.postal_code,
        country=row.country,
        place=place,
        observation=observation,
        observed_at=row.observed_at,
        next_fetch_at=row.next_fetch_at,
        failing_since=row.failing_since,
        failure=failure,
    )


_CLEARED_ANSWER: dict[str, Any] = {
    "code": None,
    "temperature_c": None,
    "wind_kmh": None,
    "observed_at": None,
    "failing_since": None,
    "failure": None,
}


class LocalWeatherRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, household_id: int) -> LocalWeather | None:
        row = await self._session.get(LocalWeatherRow, household_id, populate_existing=True)
        return None if row is None else _entity(row)

    async def save_setting(
        self, household_id: int, *, postal_code: str, country: str, place: Place | None, now: datetime
    ) -> None:
        """Admin's save: on at ``place``, which fetches at once, or off with ``place`` None, which
        clears the place and every answer. The postal code and country are kept either way."""
        values: dict[str, Any] = {
            "enabled": place is not None,
            "postal_code": postal_code,
            "country": country,
            "latitude": None if place is None else place.latitude,
            "longitude": None if place is None else place.longitude,
            "place": None if place is None else place.name,
            "next_fetch_at": None if place is None else now,
            "updated_at": now,
            **_CLEARED_ANSWER,
        }
        statement = insert(LocalWeatherRow).values(household_id=household_id, **values)
        await self._session.execute(statement.on_conflict_do_update(index_elements=["household_id"], set_=values))

    async def due(self, now: datetime) -> list[LocalWeather]:
        """Every household whose next fetch is due, oldest first."""
        rows = await self._session.scalars(
            select(LocalWeatherRow)
            .where(
                LocalWeatherRow.enabled.is_(True),
                LocalWeatherRow.latitude.is_not(None),
                LocalWeatherRow.next_fetch_at <= now,
            )
            .order_by(LocalWeatherRow.next_fetch_at, LocalWeatherRow.household_id)
        )
        return [_entity(row) for row in rows]

    async def claim(self, household_id: int, place: Place, now: datetime, next_at: datetime) -> bool:
        """Move the next fetch to ``next_at`` before fetching, if it's still due for ``place``:
        a crash or a second process can't fetch again before then."""
        return await self._update(household_id, place, {"next_fetch_at": next_at}, LocalWeatherRow.next_fetch_at <= now)

    async def record_answer(self, household_id: int, place: Place, observation: Observation, now: datetime) -> bool:
        values = {
            "code": observation.code,
            "temperature_c": observation.temperature_c,
            "wind_kmh": observation.wind_kmh,
            "observed_at": now,
            "failing_since": None,
            "failure": None,
            "updated_at": now,
        }
        return await self._update(household_id, place, values)

    async def record_failure(
        self, household_id: int, place: Place, reason: WeatherFailure, failing_since: datetime, now: datetime
    ) -> bool:
        values = {"failing_since": failing_since, "failure": reason.value, "updated_at": now}
        return await self._update(household_id, place, values)

    async def _update(
        self, household_id: int, place: Place, values: dict[str, Any], *extra: ColumnElement[bool]
    ) -> bool:
        result = await self._session.execute(
            update(LocalWeatherRow)
            .where(
                and_(
                    LocalWeatherRow.household_id == household_id,
                    LocalWeatherRow.enabled.is_(True),
                    LocalWeatherRow.latitude == place.latitude,
                    LocalWeatherRow.longitude == place.longitude,
                    *extra,
                )
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return bool(getattr(result, "rowcount", 0))
