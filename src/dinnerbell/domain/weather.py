# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dinnerbone's weather (#319, ADR 0011): what the den's sky can do, and local weather's rules.

``Weather`` is one of the den's skies (``Sky``), maybe adjusted by how it feels (``Feel``: hot,
cold, or windy). The seasonal choice is in ``domain.fun``; this module maps Open-Meteo's answer
to the same small set, and holds the rules for asking: which postal codes are valid, which hosts
Dinnerbell may call, how big an answer may be, how often to refresh, and how long an answer
stays good. It does no I/O: ``services.local_weather`` does the asking.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlencode

from dinnerbell.domain.fetch_policy import FetchError, parse_target


class Sky(StrEnum):
    CLEAR = "clear"
    PARTLY_CLOUDY = "partly-cloudy"
    OVERCAST = "overcast"
    FOG = "fog"
    DRIZZLE = "drizzle"
    RAIN = "rain"
    HEAVY_RAIN = "heavy-rain"
    THUNDERSTORM = "thunderstorm"
    SNOW = "snow"
    HEAVY_SNOW = "heavy-snow"
    SLEET = "sleet"
    """Sleet, freezing drizzle, or freezing rain."""


class Feel(StrEnum):
    HOT = "hot"
    COLD = "cold"
    WINDY = "windy"


WET = frozenset({Sky.DRIZZLE, Sky.RAIN, Sky.HEAVY_RAIN, Sky.THUNDERSTORM, Sky.SLEET})
"""Skies he holds an umbrella under."""
SNOWY = frozenset({Sky.SNOW, Sky.HEAVY_SNOW})
DRY = frozenset({Sky.CLEAR, Sky.PARTLY_CLOUDY, Sky.OVERCAST, Sky.FOG})


@dataclass(frozen=True, slots=True)
class Weather:
    sky: Sky
    feel: Feel | None = None
    local: bool = False
    """From Open-Meteo, not the seasonal choice."""


# Open-Meteo's answer --------------------------------------------------------------------------

WMO_SKIES: Mapping[int, Sky] = {
    0: Sky.CLEAR,
    1: Sky.CLEAR,
    2: Sky.PARTLY_CLOUDY,
    3: Sky.OVERCAST,
    45: Sky.FOG,
    48: Sky.FOG,
    51: Sky.DRIZZLE,
    53: Sky.DRIZZLE,
    55: Sky.DRIZZLE,
    56: Sky.SLEET,
    57: Sky.SLEET,
    61: Sky.RAIN,
    63: Sky.RAIN,
    65: Sky.HEAVY_RAIN,
    66: Sky.SLEET,
    67: Sky.SLEET,
    71: Sky.SNOW,
    73: Sky.SNOW,
    75: Sky.HEAVY_SNOW,
    77: Sky.SNOW,
    80: Sky.RAIN,
    81: Sky.RAIN,
    82: Sky.HEAVY_RAIN,
    85: Sky.SNOW,
    86: Sky.HEAVY_SNOW,
    95: Sky.THUNDERSTORM,
    96: Sky.THUNDERSTORM,
    97: Sky.THUNDERSTORM,
    99: Sky.THUNDERSTORM,
}
"""Every WMO weather interpretation code Open-Meteo documents, as a den sky. Any other code is an
answer that makes no sense."""

HOT_C = 28.0
"""At or above this, in °C, a dry sky feels hot: sunglasses by day, a fan in the evening."""
COLD_C = 0.0
"""At or below this, in °C, a dry sky feels cold."""
WINDY_KMH = 30.0
"""At or above this, in km/h at 10 m, the wind blows his scarf, unless it's storming."""


@dataclass(frozen=True, slots=True)
class Observation:
    """Current conditions, as Open-Meteo reported them."""

    code: int
    temperature_c: float
    wind_kmh: float


def weather_for(observation: Observation) -> Weather | None:
    """The den's weather for ``observation``, or None for a code outside the WMO table."""
    sky = WMO_SKIES.get(observation.code)
    if sky is None:
        return None
    feel = None
    if observation.wind_kmh >= WINDY_KMH and sky is not Sky.THUNDERSTORM:
        feel = Feel.WINDY
    elif sky in DRY and observation.temperature_c >= HOT_C:
        feel = Feel.HOT
    elif sky in DRY and observation.temperature_c <= COLD_C:
        feel = Feel.COLD
    return Weather(sky, feel, local=True)


class WeatherFailure(StrEnum):
    """Why local weather failed. The value is what logs and the database record."""

    TIMEOUT = "timeout"
    NETWORK = "network"
    STATUS = "status"
    REDIRECT = "redirect"
    CONTENT_TYPE = "content_type"
    TOO_BIG = "too_big"
    BAD_JSON = "bad_json"
    BAD_ANSWER = "bad_answer"
    """JSON that parsed but makes no sense: a missing field, a number out of range, or an unknown code."""
    NOT_FOUND = "not_found"
    """Geocoding found no place for the postal code."""
    REFUSED = "refused"
    """A URL outside the allowlist: a bug, never sent."""
    ERROR = "error"
    """Anything else, such as a database error while storing the answer."""


class WeatherError(Exception):
    def __init__(self, reason: WeatherFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


def _number(value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise WeatherError(WeatherFailure.BAD_ANSWER)
    if not low <= value <= high:
        raise WeatherError(WeatherFailure.BAD_ANSWER)
    return float(value)


def parse_current(answer: object) -> Observation:
    """The ``current`` block of a forecast answer, checked, or ``WeatherError(BAD_ANSWER)``."""
    current = answer.get("current") if isinstance(answer, dict) else None
    if not isinstance(current, dict):
        raise WeatherError(WeatherFailure.BAD_ANSWER)
    code = current.get("weather_code")
    if isinstance(code, float) and code.is_integer():
        code = int(code)
    if isinstance(code, bool) or not isinstance(code, int) or code not in WMO_SKIES:
        raise WeatherError(WeatherFailure.BAD_ANSWER)
    return Observation(
        code=code,
        temperature_c=_number(current.get("temperature_2m"), -90.0, 60.0),
        wind_kmh=_number(current.get("wind_speed_10m"), 0.0, 500.0),
    )


@dataclass(frozen=True, slots=True)
class Place:
    """Where a postal code is, rounded (``round_coordinate``), and what the place is called."""

    latitude: float
    longitude: float
    name: str


PLACE_NAME_LENGTH = 120


def round_coordinate(value: float) -> float:
    """To 0.1°, about 11 km: Dinnerbell never keeps or sends anything finer."""
    return round(value, 1)


def parse_place(answer: object) -> Place:
    """The first result of a geocoding answer, rounded, or ``WeatherError``: NOT_FOUND for no
    results, BAD_ANSWER for one that makes no sense."""
    if not isinstance(answer, dict):
        raise WeatherError(WeatherFailure.BAD_ANSWER)
    results = answer.get("results")
    if results is None or results == []:
        raise WeatherError(WeatherFailure.NOT_FOUND)
    if not isinstance(results, list) or not isinstance(results[0], dict):
        raise WeatherError(WeatherFailure.BAD_ANSWER)
    first = results[0]
    latitude = _number(first.get("latitude"), -90.0, 90.0)
    longitude = _number(first.get("longitude"), -180.0, 180.0)
    parts = [first.get("name"), first.get("admin1")]
    names = [" ".join(p.split()) for p in parts if isinstance(p, str) and p.strip()]
    if not names:
        raise WeatherError(WeatherFailure.BAD_ANSWER)
    name = ", ".join(dict.fromkeys(names))[:PLACE_NAME_LENGTH]
    return Place(round_coordinate(latitude), round_coordinate(longitude), name)


# The setting ------------------------------------------------------------------------------------

DEFAULT_COUNTRY = "US"
_COUNTRY = re.compile(r"[A-Z]{2}")
_POSTAL = re.compile(r"[A-Z0-9](?:[A-Z0-9 -]{0,8}[A-Z0-9])?")
_US_ZIP = re.compile(r"([0-9]{5})(?:-?[0-9]{4})?")


class InvalidLocation(ValueError):
    """A postal code or country that can't be one. The message is for the admin."""


def normalize_location(postal_code: str, country: str) -> tuple[str, str]:
    """The postal code and country as Dinnerbell stores and sends them, or ``InvalidLocation``.

    The country is two letters (ISO 3166-1), US when blank. A US ZIP code is 5 digits; ZIP+4
    is accepted and sent as its first 5. Elsewhere, 2 to 10 letters, digits, spaces, or hyphens,
    in capitals."""
    country = country.strip().upper() or DEFAULT_COUNTRY
    if not _COUNTRY.fullmatch(country):
        raise InvalidLocation("Enter the country as two letters, such as US or CA.")
    code = " ".join(postal_code.split()).upper()
    if not code:
        raise InvalidLocation("Enter a ZIP or postal code to show local weather.")
    if country == "US":
        match = _US_ZIP.fullmatch(code)
        if match is None:
            raise InvalidLocation("Enter a 5-digit ZIP code, such as 12345.")
        return match.group(1), country
    if len(code) < 2 or not _POSTAL.fullmatch(code):
        raise InvalidLocation("Enter a postal code of 2 to 10 letters, digits, spaces, or hyphens.")
    return code, country


# Egress ----------------------------------------------------------------------------------------

GEOCODING_HOST = "geocoding-api.open-meteo.com"
FORECAST_HOST = "api.open-meteo.com"
ALLOWED_HOSTS = frozenset({GEOCODING_HOST, FORECAST_HOST})
"""The only hosts local weather calls (ADR 0011)."""
MAX_ANSWER_BYTES = 64 * 1024
DEADLINE_SECONDS = 5.0
"""For a whole request: connecting, waiting, and reading."""
CREDIT_URL = "https://open-meteo.com/"


def geocoding_url(postal_code: str, country: str) -> str:
    query = urlencode({"name": postal_code, "countryCode": country, "count": 1, "language": "en", "format": "json"})
    return f"https://{GEOCODING_HOST}/v1/search?{query}"


def forecast_url(latitude: float, longitude: float) -> str:
    """Current conditions at a rounded place, in °C and km/h."""
    query = urlencode(
        {
            "latitude": f"{round_coordinate(latitude):.1f}",
            "longitude": f"{round_coordinate(longitude):.1f}",
            "current": "weather_code,temperature_2m,wind_speed_10m",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "timezone": "GMT",
        }
    )
    return f"https://{FORECAST_HOST}/v1/forecast?{query}"


def check_url(url: str) -> str:
    """``url`` if local weather may fetch it: https on 443 with no user info
    (``fetch_policy.parse_target``), to a host in ``ALLOWED_HOSTS``. Else ``WeatherError(REFUSED)``."""
    try:
        target = parse_target(url)
    except FetchError:
        raise WeatherError(WeatherFailure.REFUSED) from None
    if target.address is not None or target.host not in ALLOWED_HOSTS:
        raise WeatherError(WeatherFailure.REFUSED)
    return target.url


def check_content_type(value: str | None) -> None:
    mime = (value or "").partition(";")[0].strip().lower()
    if mime != "application/json":
        raise WeatherError(WeatherFailure.CONTENT_TYPE)


# When to ask ------------------------------------------------------------------------------------

REFRESH_MIN = timedelta(hours=1)
REFRESH_MAX = timedelta(hours=3)
FRESH_FOR = timedelta(hours=6)
"""How long an answer stays good: past this, the den falls back to the seasonal weather."""


def next_fetch(now: datetime, draw: float) -> datetime:
    """When to ask next, ``draw`` (from 0 to 1) of the way from 1 to 3 hours after ``now``, to
    the second, so households' requests never line up on the clock."""
    if not 0.0 <= draw <= 1.0:
        raise ValueError("draw must be from 0 to 1")
    seconds = REFRESH_MIN.total_seconds() + draw * (REFRESH_MAX - REFRESH_MIN).total_seconds()
    return now + timedelta(seconds=round(seconds))


def is_fresh(observed_at: datetime | None, now: datetime) -> bool:
    """Whether an answer fetched at ``observed_at`` is still good at ``now``. One from the future,
    after the clock moved back, isn't."""
    return observed_at is not None and timedelta(0) <= now - observed_at <= FRESH_FOR


# The household's setting and its last answer ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalWeather:
    """Admin's local weather setting, the place it found, and the last answer (``local_weather``)."""

    household_id: int
    enabled: bool
    postal_code: str
    country: str
    place: Place | None
    observation: Observation | None
    observed_at: datetime | None
    """When the last good answer arrived."""
    next_fetch_at: datetime | None
    failing_since: datetime | None
    failure: WeatherFailure | None

    @property
    def on(self) -> bool:
        """Turned on, with a place to ask about: only then does anything fetch or show the credit."""
        return self.enabled and self.place is not None

    def weather(self, now: datetime) -> Weather | None:
        """The local weather to show at ``now``: the last good answer while it's fresh, else None,
        and the den shows the seasonal weather."""
        if not self.on or self.observation is None or not is_fresh(self.observed_at, now):
            return None
        return weather_for(self.observation)
