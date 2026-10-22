# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuration validation."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import pytest

from dinnerbell.config import ConfigError, Settings, load_settings
from tests.conftest import make_settings

REQUIRED = {
    "DINNERBELL_BASE_URL": "https://dinner.example.com",
    "DINNERBELL_SECRET_KEY": "s3cr3t-value-that-is-long-enough-for-validation",
    "DINNERBELL_ADMIN_EMAIL": "Admin@Example.com",
    "DINNERBELL_OIDC_ISSUER": "https://slack.com",
    "DINNERBELL_OIDC_CLIENT_ID": "123.456",
    "DINNERBELL_OIDC_CLIENT_SECRET": "very-secret-client-secret",
    "DINNERBELL_SLACK_TEAM_ID": "T0123ABCD",
}


def set_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in (REQUIRED | values).items():
        monkeypatch.setenv(key, value)


def error_for(monkeypatch: pytest.MonkeyPatch, **values: str) -> str:
    set_env(monkeypatch, **values)
    with pytest.raises(ConfigError) as caught:
        load_settings()
    return str(caught.value)


def test_loads_from_environment_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch)
    settings = load_settings()
    assert settings.database_url == "sqlite:////data/dinnerbell.db"
    assert settings.database_path == Path("/data/dinnerbell.db")
    assert settings.backup_dir == Path("/data/backups")
    assert settings.async_database_url == "sqlite+aiosqlite:////data/dinnerbell.db"
    assert settings.household_name == "Home"
    assert settings.oidc_scopes == "openid email profile"
    assert settings.admin_email == "admin@example.com"
    assert settings.log_level == "INFO"
    assert settings.timezone == "UTC"
    assert settings.trusted_proxies == ()
    assert settings.slack_team_id == "T0123ABCD"
    assert settings.cookie_secure


def test_missing_required_settings_are_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings()
    message = str(caught.value)
    for name in set(REQUIRED) - {"DINNERBELL_SLACK_TEAM_ID"}:
        assert f"{name} is required" in message
    assert ".env.example" in message


def test_empty_values_count_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    message = error_for(monkeypatch, DINNERBELL_SECRET_KEY="", DINNERBELL_SLACK_CHANNEL="")
    assert "DINNERBELL_SECRET_KEY is required" in message
    assert "SLACK_CHANNEL" not in message


def test_errors_never_echo_values(monkeypatch: pytest.MonkeyPatch) -> None:
    message = error_for(
        monkeypatch,
        DINNERBELL_SECRET_KEY="short-secret-xyz",
        DINNERBELL_SLACK_BOT_TOKEN="bad-token-abc123",
        DINNERBELL_SLACK_APP_TOKEN="bad-app-token-def456",
    )
    assert "DINNERBELL_SECRET_KEY: must be at least 32 characters" in message
    assert "DINNERBELL_SLACK_BOT_TOKEN: must be a Slack bot token starting with xoxb-" in message
    assert "DINNERBELL_SLACK_APP_TOKEN: must be a Slack app-level token starting with xapp-" in message
    for secret in ("short-secret-xyz", "bad-token-abc123", "bad-app-token-def456"):
        assert secret not in message


def test_slack_tokens_with_right_prefixes_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, DINNERBELL_SLACK_BOT_TOKEN="xoxb-1-2-3", DINNERBELL_SLACK_APP_TOKEN="xapp-1-A-2")
    settings = load_settings()
    assert settings.slack_bot_token is not None
    assert "xoxb" not in repr(settings)


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("http://dinner.example.com", "must use https"),
        ("ftp://dinner.example.com", "absolute http(s) URL"),
        ("dinner.example.com", "absolute http(s) URL"),
        ("https://dinner.example.com/app", "must not include a path"),
        ("https://user:pw@dinner.example.com", "must not contain credentials"),
    ],
)
def test_base_url_validation(monkeypatch: pytest.MonkeyPatch, url: str, fragment: str) -> None:
    assert fragment in error_for(monkeypatch, DINNERBELL_BASE_URL=url)


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000/"])
def test_local_http_base_url_turns_off_secure_cookies(tmp_path: Path, url: str) -> None:
    settings = make_settings(tmp_path, base_url=url)
    assert not settings.cookie_secure
    assert not settings.base_url.endswith("/")


@pytest.mark.parametrize(
    ("name", "value", "fragment"),
    [
        ("DINNERBELL_OIDC_ISSUER", "http://idp.example.com", "must use https"),
        ("DINNERBELL_ADMIN_EMAIL", "not-an-email", "valid email"),
        ("DINNERBELL_OIDC_SCOPES", "openid profile", "must include email"),
        ("DINNERBELL_TIMEZONE", "Mars/Olympus", "IANA time zone"),
        ("DINNERBELL_DATABASE_URL", "postgresql://db/dinnerbell", "only sqlite"),
        ("DINNERBELL_DATABASE_URL", "sqlite://", "database file"),
        ("DINNERBELL_TRUSTED_PROXIES", "10.0.0.1, not-an-ip", "comma-separated list"),
        ("DINNERBELL_LOG_LEVEL", "chatty", "DINNERBELL_LOG_LEVEL"),
        ("DINNERBELL_HOUSEHOLD_NAME", "x" * 81, "DINNERBELL_HOUSEHOLD_NAME"),
    ],
)
def test_invalid_values(monkeypatch: pytest.MonkeyPatch, name: str, value: str, fragment: str) -> None:
    assert fragment in error_for(monkeypatch, **{name: value})


@pytest.mark.parametrize("issuer", ["https://slack.com", "https://slack.com/"])
def test_slack_issuer_requires_team_id(monkeypatch: pytest.MonkeyPatch, issuer: str) -> None:
    """Regression: without a team ID, any Slack workspace's account could claim an invitation."""
    message = error_for(monkeypatch, DINNERBELL_OIDC_ISSUER=issuer, DINNERBELL_SLACK_TEAM_ID="")
    assert "  - DINNERBELL_SLACK_TEAM_ID is required when DINNERBELL_OIDC_ISSUER is https://slack.com" in message


def test_other_issuers_do_not_need_a_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, DINNERBELL_OIDC_ISSUER="https://accounts.google.com", DINNERBELL_SLACK_TEAM_ID="")
    assert load_settings().slack_team_id is None


def test_trusted_proxies_and_log_level_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, DINNERBELL_TRUSTED_PROXIES="10.0.0.5, 172.16.0.0/12,fd00::/8", DINNERBELL_LOG_LEVEL="debug")
    settings = load_settings()
    assert settings.trusted_proxies == (
        ipaddress.ip_network("10.0.0.5/32"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("fd00::/8"),
    )
    assert settings.log_level == "DEBUG"


def test_scopes_are_normalized(tmp_path: Path) -> None:
    assert make_settings(tmp_path, oidc_scopes="  email   openid ").oidc_scopes == "email openid"


def test_secrets_are_hidden_in_repr(tmp_path: Path) -> None:
    settings: Any = make_settings(tmp_path)
    text = repr(settings) + str(settings.model_dump())
    assert settings.secret_key.get_secret_value() not in text
    assert settings.oidc_client_secret.get_secret_value() not in text


def test_settings_are_immutable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(ValueError, match="frozen"):
        settings.base_url = "https://other.example"  # type: ignore[misc]


def test_secret_files_are_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dinnerbell import config

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "DINNERBELL_SECRET_KEY").write_text("from-a-secret-file-and-long-enough-to-pass")
    set_env(monkeypatch)
    monkeypatch.delenv("DINNERBELL_SECRET_KEY")
    monkeypatch.setattr(config, "SECRETS_DIR", secrets_dir)
    settings: Settings = load_settings()
    assert settings.secret_key.get_secret_value() == "from-a-secret-file-and-long-enough-to-pass"


def test_env_example_documents_every_setting() -> None:
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    for field in Settings.model_fields:
        assert f"\nDINNERBELL_{field.upper()}=" in text, field
