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


def test_config_check_accepts_a_valid_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    database = tmp_path / "dinnerbell.db"
    monkeypatch.setenv("DINNERBELL_DATABASE_URL", f"sqlite:///{database}")
    assert cli.main(["config", "check"]) == 0
    out, err = capsys.readouterr()
    assert out == "Configuration is valid.\n"
    assert err == ""
    assert not database.exists()  # a check never creates or migrates the database


def test_config_check_names_a_missing_setting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in REQUIRED.items():
        if key != "DINNERBELL_ADMIN_EMAIL":
            monkeypatch.setenv(key, value)
    assert cli.main(["config", "check"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "DINNERBELL_ADMIN_EMAIL is required but not set." in err
    assert err.count("  - ") == 1
    for value in REQUIRED.values():
        assert value not in err


def test_config_check_names_invalid_settings_without_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = {
        "DINNERBELL_SECRET_KEY": "short-secret-xyz",
        "DINNERBELL_SLACK_BOT_TOKEN": "xoxp-wrong-kind-of-token",
        "DINNERBELL_SLACK_APP_TOKEN": "xoxb-swapped-for-the-bot-token",
        "DINNERBELL_BASE_URL": "https://user:hunter2-password@dinner.example.com",
        "DINNERBELL_ADMIN_EMAIL": "not-an-email-address",
        "DINNERBELL_LOG_LEVEL": "loud-and-secret",
        "DINNERBELL_TRUSTED_PROXIES": "10.0.0.1,proxy-secret-name",
        "DINNERBELL_DATABASE_URL": "sqlite://user:db-password@host:PORTLEAK/x",
    }
    for key, value in (REQUIRED | secrets).items():
        monkeypatch.setenv(key, value)
    assert cli.main(["config", "check"]) == 2
    err = capsys.readouterr().err
    for name in secrets:
        assert f"  - {name}: " in err, name
    for key, value in (REQUIRED | secrets).items():
        assert value not in err, key
    for fragment in ("hunter2", "short-secret", "wrong-kind", "swapped", "loud", "proxy-secret", "PORTLEAK", "db-pass"):
        assert fragment not in err


@pytest.mark.parametrize("url", ["garbage", "sqlite://user:db-password@host:PORTLEAK/x", "postgresql://u:p@h/x"])
def test_config_check_rejects_a_bad_database_url_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str
) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DINNERBELL_DATABASE_URL", url)
    assert cli.main(["config", "check"]) == 2
    err = capsys.readouterr().err
    assert "  - DINNERBELL_DATABASE_URL: " in err
    for fragment in ("garbage", "PORTLEAK", "db-password", "Traceback"):
        assert fragment not in err


def _env_file_text(**overrides: str) -> str:
    return "".join(f"{key}={value}\n" for key, value in (REQUIRED | overrides).items())


def test_config_check_reads_an_env_file_like_compose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    env_file = tmp_path / ".env.test"
    env_file.write_text(
        "# comment\n"
        + _env_file_text(DINNERBELL_ADMIN_EMAIL="wrong@example.com")
        + 'export DINNERBELL_BASE_URL="https://dinner-test.example.com"  # quoted, with a comment\n'
        + "DINNERBELL_ADMIN_EMAIL='admin@example.com'\n"  # the last assignment wins
        + "DINNERBELL_OIDC_SCOPES=openid email profile\n"
        + "DINNERBELL_SLACK_CHANNEL\n"  # a bare name passes nothing through
    )
    monkeypatch.setenv("DINNERBELL_LOG_LEVEL", "INFO")
    assert cli.main(["config", "check", "--env-file", str(env_file)]) == 0
    assert capsys.readouterr().out == "Configuration is valid.\n"
    assert "DINNERBELL_BASE_URL" not in os.environ  # the file's values don't outlive the check
    assert os.environ["DINNERBELL_LOG_LEVEL"] == "INFO"


def test_config_check_env_file_overrides_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    env_file = tmp_path / ".env.test"
    env_file.write_text("DINNERBELL_SECRET_KEY=too-short-to-use\nDINNERBELL_ADMIN_EMAIL=\n")
    assert cli.main(["config", "check", "--env-file", str(env_file)]) == 2
    err = capsys.readouterr().err
    assert "DINNERBELL_SECRET_KEY: must be at least 32 characters" in err
    assert "DINNERBELL_ADMIN_EMAIL is required but not set." in err
    assert "too-short-to-use" not in err
    assert os.environ["DINNERBELL_SECRET_KEY"] == REQUIRED["DINNERBELL_SECRET_KEY"]


def test_config_check_reads_the_env_file_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    text = _env_file_text(DINNERBELL_ADMIN_EMAIL="")
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    assert cli.main(["config", "check", "--env-file", "-"]) == 2
    out, err = capsys.readouterr()
    assert "DINNERBELL_ADMIN_EMAIL is required but not set." in err
    for value in REQUIRED.values():
        assert value not in out + err

    monkeypatch.setattr("sys.stdin", io.StringIO(_env_file_text()))
    assert cli.main(["config", "check", "--env-file", "-"]) == 0


def test_config_check_reads_secret_files_from_a_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The test wrapper mounts the test instance's files and runs this, as the service would."""
    secrets = {"DINNERBELL_SECRET_KEY", "DINNERBELL_OIDC_CLIENT_SECRET"}
    directory = tmp_path / "secrets-test"
    directory.mkdir()
    for name in secrets:
        (directory / name).write_text(REQUIRED[name])
    env_text = "".join(f"{k}={v}\n" for k, v in REQUIRED.items() if k not in secrets)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(env_text))
    args = ["config", "check", "--env-file", "-", "--secrets-dir", str(directory)]
    assert cli.main(args) == 0
    out, err = capsys.readouterr()
    assert out == "Configuration is valid.\n"
    assert err == ""

    (directory / "DINNERBELL_SECRET_KEY").unlink()
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(env_text))
    assert cli.main(args) == 2
    err = capsys.readouterr().err
    assert (
        f"DINNERBELL_SECRET_KEY is required but not set. Set it in the env file, or put it in the file {directory}"
        in err
    )
    for name in secrets:
        assert REQUIRED[name] not in err


def test_config_check_reports_an_unreadable_env_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["config", "check", "--env-file", str(tmp_path / "missing")]) == 2
    assert capsys.readouterr().err == "Can't read the env file (FileNotFoundError).\n"
    binary = tmp_path / "binary"
    binary.write_bytes(b"DINNERBELL_SECRET_KEY=\xff\xfe-secret\n")
    assert cli.main(["config", "check", "--env-file", str(binary)]) == 2
    err = capsys.readouterr().err
    assert err == "Can't read the env file (UnicodeDecodeError).\n"


def test_config_check_needs_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["config"])
    assert "required: COMMAND" in capsys.readouterr().err


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

        from dinnerbell.db.migrate import IntegrityCheckFailed

        def broken(*_args: object) -> None:
            raise IntegrityCheckFailed("rows in meal_log refer to rows that don't exist")

        monkeypatch.setattr(cli, "run_migrations", broken)
        assert cli.main(["migrate"]) == 1  # a clean refusal, not a traceback
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


def test_secret_warnings_are_logged_after_logging_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A secret still in the environment warns, through the app logger and without its value."""
    import logging

    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("dinnerbell.config.SECRETS_DIR", tmp_path / "no-secrets")
    events: list[str] = []

    class Recorder(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            events.append(record.getMessage())

    logger = logging.getLogger("dinnerbell.config")
    recorder = Recorder()
    logger.addHandler(recorder)
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: events.append("configure_logging"))
    monkeypatch.setattr(cli, "_command", lambda args, settings: 0)
    try:
        assert cli.main(["migrate"]) == 0
    finally:
        logger.removeHandler(recorder)
    assert events[0] == "configure_logging"
    named = [e for e in events[1:] if "is set in the environment" in e]
    assert len(named) == 2  # REQUIRED sets the key and the client secret
    for value in REQUIRED.values():
        assert not any(value in e for e in events)
