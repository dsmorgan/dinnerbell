# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures. Tests run with production security settings: HTTPS base URL, Secure
__Host- cookies, CSRF, and Origin checks all on. Browsers send Origin; so does the test client."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey

from dinnerbell.app_factory import create_app
from dinnerbell.config import Settings
from dinnerbell.db.migrate import run_migrations
from tests.support.clock import FakeClock
from tests.support.fake_oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeProvider, generate_key
from tests.support.flags import disagreements, features_for, item_problems

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


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Refuse to run when a ``flag`` mark could count as coverage without the test running in
    that state (tests/support/flags.py)."""
    problems = [problem for item in items for problem in item_problems(item)]
    if problems:
        raise pytest.UsageError("Bad feature flag marks:\n" + "\n".join(problems))


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
def settings(request: pytest.FixtureRequest, tmp_path: Path, settings_overrides: dict[str, Any]) -> Settings:
    """Settings for one test. ``@pytest.mark.flag`` marks set the feature flags, and the test
    fails if anything else changed them (tests/support/flags.py)."""
    marks = list(request.node.iter_markers("flag"))
    features = features_for(marks)
    flagged = {} if features is None else {"features": features}
    settings = make_settings(tmp_path, **(flagged | settings_overrides))
    if problems := disagreements(marks, settings.features):
        pytest.fail("; ".join(problems), pytrace=False)
    return settings


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database migrated once per session (once per xdist worker), for ``migrated`` to copy.
    tests/test_migrations.py still runs real migrations against fresh and old databases."""
    path = tmp_path_factory.mktemp("template") / "dinnerbell.db"
    run_migrations(f"sqlite:///{path}", path)
    # The last connection closing checkpoints the WAL and deletes it, so the one file is the database.
    assert not path.with_name(path.name + "-wal").exists()
    return path


@pytest.fixture
def migrated(settings: Settings, migrated_template: Path) -> Settings:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(migrated_template, settings.database_path)
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
