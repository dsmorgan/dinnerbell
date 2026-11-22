# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake Open-Meteo for tests (ADR 0011): an httpx transport handler, so no test reaches the
network. It knows one postal code, answers the forecast with ``current``, and records every
request. Set ``respond`` to answer with anything else, or raise, for a failure."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from dinnerbell.domain.weather import FORECAST_HOST, GEOCODING_HOST

POSTAL_CODE = "12345"
PLACE = {"name": "Elmford", "admin1": "Northshire", "latitude": 40.1234, "longitude": -75.2468, "country_code": "US"}
"""A made-up place: rounded, it's 40.1, -75.2."""


def current(code: int = 61, temperature: float = 12.5, wind: float = 8.0) -> dict[str, Any]:
    return {
        "latitude": 40.1,
        "longitude": -75.2,
        "current": {"time": "2026-10-22T18:00", "interval": 900, "weather_code": code,
                    "temperature_2m": temperature, "wind_speed_10m": wind},
    }  # fmt: skip


def json_response(
    body: object, status: int = 200, content_type: str = "application/json; charset=utf-8"
) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers={"content-type": content_type})


class FakeOpenMeteo:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer: dict[str, Any] = current()
        self.respond: Callable[[httpx.Request], httpx.Response] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.respond is not None:
            return self.respond(request)
        if request.url.host == GEOCODING_HOST:
            found = request.url.params.get("name") == POSTAL_CODE and request.url.params.get("countryCode") == "US"
            return json_response({"results": [PLACE]} if found else {"generationtime_ms": 0.1})
        if request.url.host == FORECAST_HOST:
            return json_response(self.answer)
        raise httpx.ConnectError("not an Open-Meteo host", request=request)

    def forecasts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == FORECAST_HOST]

    def lookups(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == GEOCODING_HOST]
