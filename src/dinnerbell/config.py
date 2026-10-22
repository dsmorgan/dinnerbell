# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime configuration, read once at start from DINNERBELL_* variables."""

from __future__ import annotations

import ipaddress
from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

from dinnerbell.domain.members import InvalidMemberInput, normalize_email

ENV_PREFIX = "DINNERBELL_"
SECRETS_DIR = Path("/run/secrets")
MIN_SECRET_KEY_LENGTH = 32
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
SLACK_ISSUER = "https://slack.com"

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


def _is_local_http(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "http" and parts.hostname in LOCAL_HOSTS


def _require_web_url(value: str, name: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    if parts.scheme == "http" and not _is_local_http(value):
        raise ValueError(f"{name} must use https unless the host is localhost or 127.0.0.1")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError(f"{name} must not contain credentials, a query, or a fragment")
    return value.rstrip("/")


class Settings(BaseSettings):
    """Every setting Dinnerbell reads from the environment.

    Secrets use ``SecretStr`` so they never appear in reprs or logs.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    database_url: str = "sqlite:////data/dinnerbell.db"
    base_url: str
    secret_key: SecretStr
    admin_email: str
    household_name: str = Field(default="Home", min_length=1, max_length=80)
    oidc_issuer: str
    oidc_client_id: str = Field(min_length=1)
    oidc_client_secret: SecretStr
    oidc_scopes: str = "openid email profile"
    slack_team_id: str | None = None
    slack_bot_token: SecretStr | None = None
    slack_app_token: SecretStr | None = None
    slack_channel: str | None = None
    timezone: str = "UTC"
    seed_file: Path | None = None
    log_level: LogLevel = "INFO"
    trusted_proxies: Annotated[tuple[IPNetwork, ...], NoDecode] = ()

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        url = make_url(value)
        if url.get_backend_name() != "sqlite":
            raise ValueError("only sqlite:/// URLs are supported")
        if not url.database or url.database == ":memory:":
            raise ValueError("must point to a database file, for example sqlite:////data/dinnerbell.db")
        return value

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        url = _require_web_url(value, "base URL")
        if urlsplit(url).path not in {"", "/"}:
            raise ValueError("base URL must not include a path; serve Dinnerbell at the root of its hostname")
        return url

    @field_validator("oidc_issuer")
    @classmethod
    def _check_issuer(cls, value: str) -> str:
        _require_web_url(value, "OIDC issuer")
        return value

    @field_validator("secret_key")
    @classmethod
    def _check_secret_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"must be at least {MIN_SECRET_KEY_LENGTH} characters; "
                "generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        return value

    @field_validator("admin_email")
    @classmethod
    def _check_admin_email(cls, value: str) -> str:
        try:
            return normalize_email(value)
        except InvalidMemberInput as exc:
            raise ValueError(str(exc)) from None

    @field_validator("oidc_scopes")
    @classmethod
    def _check_scopes(cls, value: str) -> str:
        scopes = value.split()
        missing = {"openid", "email"} - set(scopes)
        if missing:
            raise ValueError(f"must include {' and '.join(sorted(missing))}")
        return " ".join(scopes)

    @field_validator("slack_bot_token")
    @classmethod
    def _check_bot_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith("xoxb-"):
            raise ValueError("must be a Slack bot token starting with xoxb-")
        return value

    @field_validator("slack_app_token")
    @classmethod
    def _check_app_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith("xapp-"):
            raise ValueError("must be a Slack app-level token starting with xapp-")
        return value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("must be an IANA time zone name such as America/Chicago") from None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _parse_proxies(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return tuple(ipaddress.ip_network(part.strip()) for part in value.split(",") if part.strip())
        except ValueError:
            raise ValueError("must be a comma-separated list of IP addresses or CIDR ranges") from None

    @model_validator(mode="after")
    def _require_team_for_slack(self) -> Settings:
        # Slack is one issuer for every workspace. Without a team check, an account in any
        # workspace whose verified email matches an unlinked invite could claim it.
        if self.oidc_issuer.rstrip("/") == SLACK_ISSUER and not self.slack_team_id:
            raise ValueError(
                f"{ENV_PREFIX}SLACK_TEAM_ID is required when {ENV_PREFIX}OIDC_ISSUER is {SLACK_ISSUER}. "
                "Set it to your workspace's team ID (it starts with T); without it, an account in any "
                "Slack workspace could claim an invitation."
            )
        return self

    @cached_property
    def database_path(self) -> Path:
        return Path(make_url(self.database_url).database or "")

    @cached_property
    def async_database_url(self) -> str:
        return make_url(self.database_url).set(drivername="sqlite+aiosqlite").render_as_string(hide_password=False)

    @cached_property
    def sync_database_url(self) -> str:
        return make_url(self.database_url).set(drivername="sqlite").render_as_string(hide_password=False)

    @cached_property
    def backup_dir(self) -> Path:
        return self.database_path.parent / "backups"

    @cached_property
    def cookie_secure(self) -> bool:
        return urlsplit(self.base_url).scheme == "https"

    @cached_property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class ConfigError(Exception):
    """Configuration is missing or invalid. The message is safe to print."""


def format_validation_error(error: ValidationError) -> str:
    """Render a settings error without echoing any submitted value."""
    lines = ["Dinnerbell cannot start because its configuration is incomplete or invalid:"]
    for item in error.errors(include_input=False, include_url=False):
        message = str(item["msg"]).removeprefix("Value error, ")
        if not item["loc"]:
            lines.append(f"  - {message}")
            continue
        name = f"{ENV_PREFIX}{str(item['loc'][0]).upper()}"
        if item["type"] == "missing":
            lines.append(f"  - {name} is required but not set.")
        else:
            lines.append(f"  - {name}: {message}")
    lines.append("See .env.example for every variable and what it does.")
    return "\n".join(lines)


def load_settings() -> Settings:
    """Read settings from the environment and, if present, /run/secrets."""
    secrets_dir = SECRETS_DIR if SECRETS_DIR.is_dir() else None
    try:
        return Settings(_secrets_dir=secrets_dir)  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc)) from None
