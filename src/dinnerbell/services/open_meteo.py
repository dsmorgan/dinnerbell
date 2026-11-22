# SPDX-License-Identifier: AGPL-3.0-or-later
"""Open-Meteo, for Dinnerbone's local weather (#319, ADR 0011): a postal code's place, and the
current conditions there.

Every request goes to a host in ``domain.weather.ALLOWED_HOSTS``, over https on 443, to a URL
Dinnerbell built, through the menu fetcher's transport (``safe_fetch.new_transport``: bundled CAs,
no proxies from the environment, no retries, no kept connections). It follows no redirects,
allows ``DEADLINE_SECONDS`` for the whole request, reads at most ``MAX_ANSWER_BYTES``, and takes
only ``application/json``. Anything else is a ``WeatherError`` with its reason. It never logs:
the URL carries the postal code or the coordinates, and ``services.local_weather`` logs the
reason.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from dinnerbell.domain.weather import (
    DEADLINE_SECONDS,
    MAX_ANSWER_BYTES,
    Observation,
    Place,
    WeatherError,
    WeatherFailure,
    check_content_type,
    check_url,
    forecast_url,
    geocoding_url,
    parse_current,
    parse_place,
)
from dinnerbell.services.safe_fetch import USER_AGENT, new_transport

HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "identity"}


class OpenMeteo:
    """``transport`` replaces the network, for tests; each request otherwise gets a new one."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, *, deadline: float = DEADLINE_SECONDS):
        self._transport = transport
        self._deadline = deadline

    async def geocode(self, postal_code: str, country: str) -> Place:
        """The place ``postal_code`` names in ``country``, rounded to 0.1°."""
        return parse_place(await self._get(geocoding_url(postal_code, country)))

    async def current(self, place: Place) -> Observation:
        """The current conditions at ``place``."""
        return parse_current(await self._get(forecast_url(place.latitude, place.longitude)))

    async def _get(self, url: str) -> object:
        url = check_url(url)
        transport = self._transport if self._transport is not None else new_transport()
        client = httpx.AsyncClient(
            transport=transport,
            headers=HEADERS,
            timeout=httpx.Timeout(self._deadline),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            async with asyncio.timeout(self._deadline):
                body = await _read(client, url)
        except TimeoutError:
            raise WeatherError(WeatherFailure.TIMEOUT) from None
        except httpx.TimeoutException:
            raise WeatherError(WeatherFailure.TIMEOUT) from None
        except httpx.HTTPError:
            raise WeatherError(WeatherFailure.NETWORK) from None
        finally:
            await client.aclose()
        try:
            return json.loads(body)
        except ValueError:  # UnicodeDecodeError is one
            raise WeatherError(WeatherFailure.BAD_JSON) from None


async def _read(client: httpx.AsyncClient, url: str) -> bytes:
    async with client.stream("GET", url) as response:
        if response.is_redirect or 300 <= response.status_code < 400:
            raise WeatherError(WeatherFailure.REDIRECT)
        if response.status_code != 200:
            raise WeatherError(WeatherFailure.STATUS)
        check_content_type(response.headers.get("content-type"))
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_ANSWER_BYTES:
            raise WeatherError(WeatherFailure.TOO_BIG)
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_ANSWER_BYTES:
                raise WeatherError(WeatherFailure.TOO_BIG)
            chunks.append(chunk)
        return b"".join(chunks)
