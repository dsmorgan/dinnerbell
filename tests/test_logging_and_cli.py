# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured logs and the command line."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dinnerbell import main as cli
from dinnerbell.logs import (
    QUIET_LOGGERS,
    JsonFormatter,
    configure_logging,
    member_id_var,
    quiet_third_party_loggers,
    request_id_var,
)
from tests.conftest import sign_in
from tests.support.fake_oidc import FakeProvider
from tests.test_config import REQUIRED


@pytest.fixture(autouse=True)
def _restore_umask() -> Iterator[None]:
    import os

    previous = os.umask(0o022)
    os.umask(previous)
    yield
    os.umask(previous)


def test_json_formatter_includes_context_and_extras() -> None:
    request_token = request_id_var.set("req-1")
    member_token = member_id_var.set(7)
    try:
        record = logging.getLogRecordFactory()("dinnerbell.x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    finally:
        request_id_var.reset(request_token)
        member_id_var.reset(member_token)
    record.status = 200
    entry = json.loads(JsonFormatter().format(record))  # formatted after the context is gone
    assert entry["msg"] == "hello world"
    assert entry["level"] == "INFO"
    assert entry["request_id"] == "req-1"
    assert entry["member_id"] == 7
    assert entry["status"] == 200


def test_json_formatter_includes_exceptions() -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    assert "ValueError: bad" in json.loads(JsonFormatter().format(record))["exc"]


def test_configure_logging_installs_json_handler() -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
    try:
        configure_logging("DEBUG")
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.DEBUG
        assert logging.getLogger("uvicorn.access").disabled
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)
        logging.getLogger("uvicorn.access").disabled = False
        for name, saved_level in levels.items():
            logging.getLogger(name).setLevel(saved_level)


@pytest.fixture
def quiet_loggers() -> Iterator[None]:
    saved = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
    quiet_third_party_loggers()
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.usefixtures("quiet_loggers")
def test_debug_logs_never_contain_email_or_tokens(
    client: TestClient, provider: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        sign_in(client, provider, claims={"sub": "x", "email": "stranger@example.com"})
        sign_in(client, provider)
        client.get("/")
    formatted = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert "@example.com" not in formatted
    assert client.cookies["__Host-dinnerbell_session"] not in formatted
    assert "code=" not in formatted
    access = [r for r in caplog.records if r.getMessage() == "request" and getattr(r, "path", "") == "/"]
    assert access and access[-1].status == 200  # type: ignore[attr-defined]


def test_cli_reports_missing_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["migrate"]) == 2
    err = capsys.readouterr().err
    assert "DINNERBELL_SECRET_KEY is required" in err


def test_cli_migrate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    database = tmp_path / "data" / "dinnerbell.db"
    monkeypatch.setenv("DINNERBELL_DATABASE_URL", f"sqlite:///{database}")
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        assert cli.main(["migrate"]) == 0
        assert database.exists()
        assert database.stat().st_mode & 0o077 == 0  # owner-only: the database holds emails

        import sqlite3

        with sqlite3.connect(database) as db:
            db.execute("UPDATE alembic_version SET version_num = 'future'")
        assert cli.main(["migrate"]) == 1
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)


def test_cli_serve_runs_uvicorn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import uvicorn

    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DINNERBELL_DATABASE_URL", f"sqlite:///{tmp_path}/db.sqlite")
    seen: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs, app=app))
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        assert cli.main(["serve", "--port", "8123"]) == 0
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)
    assert seen["port"] == 8123
    assert seen["host"] == "0.0.0.0"  # noqa: S104
    assert seen["proxy_headers"] is False


def test_cli_requires_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main([])
