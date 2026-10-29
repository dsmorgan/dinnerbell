# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime configuration, read once at start from DINNERBELL_* variables."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import Mapping
from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from dinnerbell import __version__
from dinnerbell.domain import flags
from dinnerbell.domain.members import InvalidMemberInput, normalize_email

ENV_PREFIX = "DINNERBELL_"
SECRETS_DIR = Path("/run/secrets")
# The settings that live in files under /run/secrets, one file per setting, named like the
# variable. Keep in step with the secrets in compose.yaml and DINNERBELL_SECRETS in
# deploy/dinnerbell-test (scripts/check-compose.sh checks both).
SECRET_FIELDS = ("secret_key", "slack_bot_token", "slack_app_token", "oidc_client_secret")
FILES_HELP = 'See "Keep secrets in files" in docs/setup.md.'
log = logging.getLogger(__name__)
# What to log for a secret that the environment still gives, one constant per setting. Nothing
# derived from a secret's value is ever passed to the logger: callers look a message up by field.
LEGACY_WARNINGS = {
    "secret_key": (
        "DINNERBELL_SECRET_KEY is set in the environment. Move it to the file "
        "/run/secrets/DINNERBELL_SECRET_KEY (secrets/ on the host) and remove it from .env: the next "
        "release ignores it there, and until then it shows on podman's command line. " + FILES_HELP
    ),
    "slack_bot_token": (
        "DINNERBELL_SLACK_BOT_TOKEN is set in the environment. Move it to the file "
        "/run/secrets/DINNERBELL_SLACK_BOT_TOKEN (secrets/ on the host) and remove it from .env: the next "
        "release ignores it there, and until then it shows on podman's command line. " + FILES_HELP
    ),
    "slack_app_token": (
        "DINNERBELL_SLACK_APP_TOKEN is set in the environment. Move it to the file "
        "/run/secrets/DINNERBELL_SLACK_APP_TOKEN (secrets/ on the host) and remove it from .env: the next "
        "release ignores it there, and until then it shows on podman's command line. " + FILES_HELP
    ),
    "oidc_client_secret": (
        "DINNERBELL_OIDC_CLIENT_SECRET is set in the environment. Move it to the file "
        "/run/secrets/DINNERBELL_OIDC_CLIENT_SECRET (secrets/ on the host) and remove it from .env: the next "
        "release ignores it there, and until then it shows on podman's command line. " + FILES_HELP
    ),
}
MIN_SECRET_KEY_LENGTH = 32
MIN_SECRET_KEY_DISTINCT = 8
# Text that only a placeholder or a sample contains. Compared with "-", "_", and spaces removed
# and the case folded, so "Change-Me" and "REPLACE_WITH_..." both match. Keep in step with the
# same list in deploy/check-secrets.
SECRET_KEY_PLACEHOLDER_MARKERS = ("replace", "changeme", "placeholder", "example", "yoursecret", "secretkeyhere")
# Environment variables that start with ENV_PREFIX and are not settings: the container's launch
# scripts and compose files read them. A variable in neither list is logged as a likely typo.
NON_SETTING_VARIABLES = frozenset(
    {
        "DINNERBELL_BIND",
        "DINNERBELL_COMPOSE",
        "DINNERBELL_ENGINE",
        "DINNERBELL_ENV_FILE",
        "DINNERBELL_PORT",
        "DINNERBELL_SECRETS",
        "DINNERBELL_TAG",
        "DINNERBELL_UPDATE_GOLDEN",
    }
)
MAX_SECRET_BYTES = 8192
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
BUILD_PATTERN = re.compile(r"[0-9a-f]{7,40}")
SLACK_ISSUER = "https://slack.com"

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


def is_test_instance(instance_label: str) -> bool:
    """Whether ``DINNERBELL_INSTANCE_LABEL`` (``Settings.instance_label``) marks a labeled
    instance, such as test (ADR 0006). The one gate for what only test may do: ``scrub-test``,
    discarding a call, and starting a called-off call again at any time (#60)."""
    return bool(instance_label)


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
    slack_command: str = Field(default="/dinnerbell", pattern=r"^/[a-z0-9_-]{1,31}$")
    instance_label: str = Field(default="", max_length=16, pattern=r"^[A-Za-z0-9 -]*$")
    features: Annotated[frozenset[str], NoDecode] = frozenset()
    timezone: str = "America/New_York"
    seed_file: Path | None = None
    log_level: LogLevel = "INFO"
    build: str = ""
    # Optional. One proxy address, never a range or an address other clients share (a container gateway).
    trusted_proxies: Annotated[tuple[IPNetwork, ...], NoDecode] = ()

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        try:
            url = make_url(value)
        except (ArgumentError, ValueError):
            # Never the parser's message: it can quote part of the URL, such as a password.
            raise ValueError("must be a SQLAlchemy URL such as sqlite:////data/dinnerbell.db") from None
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
        key = value.get_secret_value()
        how = "generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        if len(key) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(f"must be at least {MIN_SECRET_KEY_LENGTH} characters; {how}")
        folded = re.sub(r"[-_\s]", "", key.lower())
        if any(marker in folded for marker in SECRET_KEY_PLACEHOLDER_MARKERS):
            raise ValueError(f"is still a placeholder from an example file; {how}")
        if len(set(key)) < MIN_SECRET_KEY_DISTINCT:
            raise ValueError(f"is too repetitive to be random; {how}")
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

    @field_validator("build")
    @classmethod
    def _check_build(cls, value: str) -> str:
        if value and not BUILD_PATTERN.fullmatch(value):
            raise ValueError("must be empty or a git commit hash of 7 to 40 lowercase hex characters")
        return value

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _parse_proxies(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return tuple(ipaddress.ip_network(part.strip()) for part in value.split(",") if part.strip())
        except ValueError:
            raise ValueError("must be a comma-separated list of IP addresses or CIDR ranges") from None

    @field_validator("features", mode="before")
    @classmethod
    def _parse_features(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("features")
    @classmethod
    def _check_features(cls, value: frozenset[str]) -> frozenset[str]:
        try:
            return flags.check(value)
        except flags.UnknownFlag as exc:
            raise ValueError(f"{exc}; src/dinnerbell/domain/flags.py lists the known flags") from None

    @field_validator("instance_label")
    @classmethod
    def _trim_label(cls, value: str) -> str:
        # "TEST " and "TEST" are the same label; an all-space label is no label.
        return value.strip()

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
    def version_label(self) -> str:
        """The version people see: 0.2.0 on a release image, 0.2.0+abc1234 on a main build."""
        return f"{__version__}+{self.build}" if self.build else __version__

    @property
    def slack_sign_in(self) -> bool:
        """Whether people sign in with Slack: the configured issuer is Slack's, so ID tokens
        carry a Slack user ID Dinnerbell may match on (#38)."""
        return self.oidc_issuer.rstrip("/") == SLACK_ISSUER

    @cached_property
    def cookie_secure(self) -> bool:
        return urlsplit(self.base_url).scheme == "https"


class ConfigError(Exception):
    """Configuration is missing or invalid. The message is safe to print."""


def format_validation_error(error: ValidationError, secrets_dir: Path | None = None) -> str:
    """Render a settings error without echoing any submitted value."""
    lines = ["Dinnerbell cannot start because its configuration is incomplete or invalid:"]
    for item in error.errors(include_input=False, include_url=False):
        message = str(item["msg"]).removeprefix("Value error, ")
        if not item["loc"]:
            lines.append(f"  - {message}")
            continue
        name = f"{ENV_PREFIX}{str(item['loc'][0]).upper()}"
        if item["type"] == "missing":
            hint = (
                f" Put it in the file {(secrets_dir or SECRETS_DIR) / name}."
                if name.lower().removeprefix(ENV_PREFIX.lower()) in SECRET_FIELDS
                else ""
            )
            lines.append(f"  - {name} is required but not set.{hint}")
        else:
            lines.append(f"  - {name}: {message}")
    lines.append("See .env.example for every variable and what it does.")
    return "\n".join(lines)


def _read_secret_file(path: Path) -> str:
    """The trimmed text of secret file ``path``, or "" when there's no file. Raises ConfigError, naming
    the path and never a value, for a file that's there but unusable. A symlink to a readable file
    works, because that's what a secret store may mount."""
    try:
        if path.is_symlink() and not path.exists():
            raise ConfigError(f"{path} is a symlink to a file that doesn't exist. {FILES_HELP}")
        if path.is_dir():
            raise ConfigError(
                f"{path} is a directory, not a file. Container engines create a directory when the "
                f"file they mount is missing: remove it and create the file. {FILES_HELP}"
            )
        if not path.exists():
            return ""
        with path.open("rb") as stream:
            raw = stream.read(MAX_SECRET_BYTES + 1)
        text = raw.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"Can't read the secret file {path} ({type(exc).__name__}). The user Dinnerbell runs "
            f"as (uid 10001 in the image) must own it, with mode 600. {FILES_HELP}"
        ) from None
    if len(raw) > MAX_SECRET_BYTES:
        raise ConfigError(f"{path} is larger than {MAX_SECRET_BYTES} bytes, too big for a secret. {FILES_HELP}")
    if "\n" in text or "\r" in text:
        raise ConfigError(
            f"{path} has more than one line. A secret is one line; a trailing newline is fine. {FILES_HELP}"
        )
    return text


def _set_in_environment(name: str) -> bool:
    return os.environ.get(name, "") != ""


def _environment_differs(name: str, file_text: str) -> bool:
    """Whether a secret file and the environment both give ``name`` and disagree. Returns only a
    boolean, so nothing derived from either value reaches a message or a log."""
    return bool(file_text) and os.environ.get(name, "").strip() != file_text


def _read_secret_files(directory: Path, warnings: list[str] | None = None) -> dict[str, str]:
    """Read the four secrets from ``directory``, one file each, named like the variable.

    A file that's empty counts as absent. Compose can't skip a secret, so an install without Slack
    keeps an empty file. A secret that is also in the environment is the old layout: the
    environment keeps working for one release, with a warning, and must agree with the file.
    ``warnings`` collects the setting names (not values) that the environment still gives, for the CLI
    to log through LEGACY_WARNINGS once logging is set up; without it they go to the log directly.
    """
    values: dict[str, str] = {}
    for field in SECRET_FIELDS:
        name = f"{ENV_PREFIX}{field.upper()}"
        path = directory / name
        text = _read_secret_file(path)
        if _set_in_environment(name):
            if _environment_differs(name, text):
                raise ConfigError(
                    f"{name} is set in the environment (probably in .env) and in {path}, with different "
                    f"values. Remove it from .env. {FILES_HELP}"
                )
            if warnings is None:
                log.warning(LEGACY_WARNINGS[field])
            else:
                warnings.append(field)
        elif text:
            values[field] = text
    return values


def unknown_setting_names(environ: Mapping[str, str] | None = None) -> list[str]:
    """The ``DINNERBELL_*`` variables in ``environ`` (default: the process environment) that are
    neither a setting nor a variable the launch scripts use: likely typos, since ``Settings``
    ignores unknown names. Names only, sorted; never a value."""
    names = set(Settings.model_fields)
    known = {f"{ENV_PREFIX}{name.upper()}" for name in names} | NON_SETTING_VARIABLES
    source = os.environ if environ is None else environ
    return sorted(name for name in source if name.upper().startswith(ENV_PREFIX) and name.upper() not in known)


def load_settings(secrets_dir: Path | None = None, warnings: list[str] | None = None) -> Settings:
    """Read settings from the environment and the secret files in ``secrets_dir``
    (default /run/secrets, if there is one). Warnings about secrets still in the environment go to
    ``warnings`` if given, else to the log."""
    directory = secrets_dir if secrets_dir is not None else SECRETS_DIR
    try:
        # Init values win over the environment, and _read_secret_files leaves out any secret the
        # environment gives, so each setting has one source.
        secrets = _read_secret_files(directory, warnings)
        return Settings(**secrets)  # type: ignore[arg-type]
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc, directory)) from None
