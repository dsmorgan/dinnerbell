# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures. Tests run with production security settings: HTTPS base URL, Secure
__Host- cookies, CSRF, and Origin checks all on. Browsers send Origin; so does the test client."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey

from dinnerbell.config import Settings
from dinnerbell.db.migrate import run_migrations
from dinnerbell.web.app import create_app
from tests.support.clock import FakeClock
from tests.support.fake_oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeProvider, generate_key

BASE_URL = "https://dinnerbell.test"
ADMIN_EMAIL = "admin@example.com"
SECRET_KEY = "test-secret-key-that-is-long-enough-to-pass-validation"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": f"sqlite:///{tmp_path}/data/dinnerbell.db",
        "base_url": BASE_URL,
        "secret_key": SECRET_KEY,
        "admin_email": ADMIN_EMAIL,
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
        "timezone": "America/Chicago",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith("DINNERBELL_"):
            monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def signing_key() -> RSAKey:
    return generate_key("key-1")


@pytest.fixture(scope="session")
def other_key() -> RSAKey:
    return generate_key("key-1")  # same kid, different key material


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def provider(signing_key: RSAKey, clock: FakeClock) -> FakeProvider:
    return FakeProvider(key=signing_key, clock=clock)


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {}


@pytest.fixture
def settings(tmp_path: Path, settings_overrides: dict[str, Any]) -> Settings:
    return make_settings(tmp_path, **settings_overrides)


@pytest.fixture
def migrated(settings: Settings) -> Settings:
    run_migrations(settings.sync_database_url, settings.database_path)
    return settings


@pytest.fixture
def client(migrated: Settings, clock: FakeClock, provider: FakeProvider) -> Iterator[TestClient]:
    app = create_app(migrated, clock=clock, oidc_transport=httpx.ASGITransport(app=provider.app))
    with TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False) as test_client:
        yield test_client


def start_login(client: TestClient) -> dict[str, str]:
    response = client.get("/auth/login")
    assert response.status_code == 302, response.text
    return dict(parse_qsl(urlsplit(response.headers["location"]).query))


def sign_in(client: TestClient, provider: FakeProvider, **authorize: Any) -> httpx.Response:
    """Run the whole browser round trip: /auth/login, the provider, /auth/callback."""
    params = start_login(client)
    code = provider.authorize(params, **authorize)
    return client.get("/auth/callback", params={"code": code, "state": params["state"]})


def csrf_from(client: TestClient) -> str:
    """Read the CSRF token the way HTMX does: from hx-headers on <body>."""
    import json
    import re

    page = client.get("/")
    match = re.search(r"hx-headers='([^']+)'", page.text)
    assert match, "no hx-headers on body; not signed in?"
    token: str = json.loads(match.group(1))["X-CSRF-Token"]
    return token


def invite(client: TestClient, email: str, name: str, role: str = "member", **flags: bool) -> httpx.Response:
    data = {"display_name": name, "email": email, "role": role, "csrf_token": csrf_from(client)}
    data.update({key: "on" for key, value in flags.items() if value})
    return client.post("/admin/members", data=data)


def page(response: httpx.Response) -> str:
    """Response text with HTML entities decoded, for readable assertions."""
    import html

    return html.unescape(response.text)
