# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures. Tests run with production security settings: HTTPS base URL, Secure
__Host- cookies, CSRF, and Origin checks all on. Browsers send Origin; so does the test client."""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey

from dinnerbell.app_factory import create_app
from dinnerbell.config import Settings
from dinnerbell.db.migrate import run_migrations
from dinnerbell.logs import JsonFormatter
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


MEMBER_TEXT = "Zq7x"
"""A marker in test data members write: notes, call-off and skip reasons, and feedback comments.
Logs carry their length, never their text (SEC-5, #75), so no log line may contain it."""
SECRET_SHAPES = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+"  # an email
    r"|xox[a-z]-\w|xapp-\w"  # a Slack token, but not a hint naming the kind, such as "(xoxb-)"
    r"|hooks\.slack\.com"  # a Slack response_url, a bearer capability for its message
    r"|1234\.5678\.trigger"  # the test trigger_id, which opens a modal for that person
    r"|[?&](code|state)=|__Host-"  # an OIDC code or state, or a session cookie
    r"|subject-admin"  # the test OIDC subject
    rf"|{MEMBER_TEXT}"
)
"""What no log line in the suite may contain (SEC-5, #75)."""


def leaks(line: str) -> bool:
    """Whether a formatted log line has something ``SECRET_SHAPES`` forbids. JSON escapes a
    newline as \\n; it's read as the space it is, so "\\nkim@example.com" still reads as an
    email, and "\\n@app.command" in a library's hint doesn't."""
    plain = line.replace("\\n", " ").replace("\\t", " ").replace("\\r", " ")
    return SECRET_SHAPES.search(plain) is not None


@pytest.fixture(autouse=True)
def _no_secrets_in_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """SEC-5 in every test: Dinnerbell logs everything down to debug, and no line, formatted as
    production writes it, has an email, a token, a capability, or text a member wrote."""
    caplog.set_level(logging.DEBUG, logger="dinnerbell")
    yield
    for when in ("setup", "call"):
        for record in caplog.get_records(when):
            line = JsonFormatter().format(record)
            assert not leaks(line), f"a log line leaks a secret: {line}"


@pytest.fixture(autouse=True)
def _no_leaked_scheduler_or_connection(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail the test that leaks, naming what it leaked, not whichever test runs when the leak
    dies (#93). A scheduler left running, or an aiosqlite connection left open, outlives its
    event loop: its thread or timer then fails with "Event loop is closed" in a later test, where
    pytest reports it as an unhandled thread exception. Cheap: one list and a thread scan."""
    started: list[AsyncIOScheduler] = []
    real_start = AsyncIOScheduler.start

    def recording_start(self: AsyncIOScheduler, *args: Any, **kwargs: Any) -> None:
        started.append(self)
        real_start(self, *args, **kwargs)

    monkeypatch.setattr(AsyncIOScheduler, "start", recording_start)
    yield
    leaks = [
        f"a scheduler still running, with jobs {sorted(job.id for job in s.get_jobs())}; stop it with "
        "stop_scheduler() or scheduler.shutdown() before the loop closes"
        for s in started
        if s.running
    ]
    deadline = time.monotonic() + 2  # a connection stopping on its own takes a moment
    while (threads := _connection_threads()) and time.monotonic() < deadline:
        time.sleep(0.01)
    leaks += [
        f"an aiosqlite connection still open ({t.name}); await Database.close() or engine.dispose() "
        "on the loop that opened it"
        for t in threads
    ]
    if leaks:
        pytest.fail("this test leaked:\n  " + "\n  ".join(leaks), pytrace=False)


def _connection_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.is_alive() and "_connection_worker_thread" in t.name]


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
