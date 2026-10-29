# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuration validation."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import pytest

from dinnerbell import __version__
from dinnerbell.config import SECRET_FIELDS, ConfigError, Settings, load_settings
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
    assert settings.build == ""
    assert settings.version_label == __version__
    assert settings.timezone == "America/New_York"
    assert settings.trusted_proxies == ()
    assert settings.slack_team_id == "T0123ABCD"
    assert settings.cookie_secure
    assert settings.slack_command == "/dinnerbell"
    assert settings.instance_label == ""
    assert settings.features == frozenset()


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


@pytest.mark.parametrize("url", ["http://localhost:8013", "http://127.0.0.1:8013/"])
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
        ("DINNERBELL_BUILD", "abc123", "7 to 40 lowercase hex"),
        ("DINNERBELL_BUILD", "ABC1234", "7 to 40 lowercase hex"),
        ("DINNERBELL_BUILD", "a" * 41, "7 to 40 lowercase hex"),
        ("DINNERBELL_BUILD", "abc1234<script>", "7 to 40 lowercase hex"),
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


SECRET_FILES = {
    "DINNERBELL_SECRET_KEY": "file-secret-key-that-is-long-enough-to-pass",
    "DINNERBELL_SLACK_BOT_TOKEN": "xoxb-from-a-file",
    "DINNERBELL_SLACK_APP_TOKEN": "xapp-from-a-file",
    "DINNERBELL_OIDC_CLIENT_SECRET": "client-secret-from-a-file",
}


def secrets_from_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **files: str) -> Path:
    """Point the app at a secrets directory holding ``files`` and no secret in the environment."""
    from dinnerbell import config

    directory = tmp_path / "secrets"
    directory.mkdir()
    for name, text in files.items():
        (directory / name).write_text(text)
    set_env(monkeypatch)
    for name in SECRET_FILES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "SECRETS_DIR", directory)
    return directory


def test_all_four_secrets_come_from_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # printf without a trailing newline and echo with one must both work.
    files = {name: value + ("\n" if name.endswith("_KEY") else "") for name, value in SECRET_FILES.items()}
    secrets_from_files(monkeypatch, tmp_path, **files)
    settings = load_settings()
    assert settings.secret_key.get_secret_value() == SECRET_FILES["DINNERBELL_SECRET_KEY"]
    assert settings.slack_bot_token is not None
    assert settings.slack_bot_token.get_secret_value() == "xoxb-from-a-file"
    assert settings.slack_app_token is not None
    assert settings.slack_app_token.get_secret_value() == "xapp-from-a-file"
    assert settings.oidc_client_secret.get_secret_value() == "client-secret-from-a-file"


def test_an_explicit_secrets_directory_is_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    directory = secrets_from_files(monkeypatch, tmp_path, **SECRET_FILES)
    monkeypatch.setattr("dinnerbell.config.SECRETS_DIR", tmp_path / "elsewhere")
    assert load_settings(directory).slack_bot_token is not None


def test_an_empty_secret_file_counts_as_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secrets_from_files(monkeypatch, tmp_path, **(SECRET_FILES | {"DINNERBELL_SLACK_BOT_TOKEN": "\n"}))
    assert load_settings().slack_bot_token is None


def test_a_missing_secret_names_its_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    directory = secrets_from_files(monkeypatch, tmp_path, **(SECRET_FILES | {"DINNERBELL_SECRET_KEY": ""}))
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert (
        f"DINNERBELL_SECRET_KEY is required but not set. Set it in the env file, or put it in the file {directory}"
        in str(caught.value)
    )


def test_an_unreadable_secret_file_is_a_clear_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("root reads every file")
    directory = secrets_from_files(monkeypatch, tmp_path, **SECRET_FILES)
    (directory / "DINNERBELL_OIDC_CLIENT_SECRET").chmod(0)
    with pytest.raises(ConfigError) as caught:
        load_settings()
    message = str(caught.value)
    assert "Can't read the secret file" in message
    assert "DINNERBELL_OIDC_CLIENT_SECRET" in message
    assert "uid 10001" in message
    assert SECRET_FILES["DINNERBELL_OIDC_CLIENT_SECRET"] not in message


def test_a_secret_file_that_is_a_directory_is_a_clear_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    directory = secrets_from_files(monkeypatch, tmp_path, **SECRET_FILES)
    (directory / "DINNERBELL_SLACK_APP_TOKEN").unlink()
    (directory / "DINNERBELL_SLACK_APP_TOKEN").mkdir()
    with pytest.raises(ConfigError, match="is a directory, not a file"):
        load_settings()


def test_an_undecodable_secret_file_does_not_echo_its_bytes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    directory = secrets_from_files(monkeypatch, tmp_path, **SECRET_FILES)
    (directory / "DINNERBELL_SECRET_KEY").write_bytes(b"\xff\xfe-hunter2")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert "UnicodeDecodeError" in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_a_secret_in_the_environment_still_works_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secrets_from_files(monkeypatch, tmp_path, **{k: v for k, v in SECRET_FILES.items() if not k.endswith("_KEY")})
    monkeypatch.setenv("DINNERBELL_SECRET_KEY", "env-secret-key-that-is-long-enough-to-pass")
    with caplog.at_level("WARNING", logger="dinnerbell.config"):
        settings = load_settings()
    assert settings.secret_key.get_secret_value() == "env-secret-key-that-is-long-enough-to-pass"
    assert "DINNERBELL_SECRET_KEY is set in the environment (the env file). That works" in caplog.text
    assert "env-secret-key" not in caplog.text


def test_all_four_secrets_from_the_environment_start_without_a_secrets_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The default layout (0.9.1): compose.yaml mounts nothing at /run/secrets, and .env holds the
    four secrets. Dinnerbell starts and only warns, once per secret, without a value."""
    from dinnerbell import config

    set_env(monkeypatch, DINNERBELL_SLACK_BOT_TOKEN="xoxb-env-only", DINNERBELL_SLACK_APP_TOKEN="xapp-env-only")
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path / "absent")
    with caplog.at_level("WARNING", logger="dinnerbell.config"):
        settings = load_settings()
    assert settings.secret_key.get_secret_value() == REQUIRED["DINNERBELL_SECRET_KEY"]
    assert settings.slack_bot_token is not None
    assert settings.slack_bot_token.get_secret_value() == "xoxb-env-only"
    for field in SECRET_FIELDS:
        assert config.LEGACY_WARNINGS[field] in caplog.messages
    for value in (REQUIRED["DINNERBELL_SECRET_KEY"], REQUIRED["DINNERBELL_OIDC_CLIENT_SECRET"], "env-only"):
        assert value not in caplog.text
    assert "next release" not in " ".join(config.LEGACY_WARNINGS.values())


def test_the_same_secret_in_the_environment_and_a_file_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secrets_from_files(monkeypatch, tmp_path, **SECRET_FILES)
    monkeypatch.setenv("DINNERBELL_SECRET_KEY", SECRET_FILES["DINNERBELL_SECRET_KEY"])
    with caplog.at_level("WARNING", logger="dinnerbell.config"):
        load_settings()
    assert "DINNERBELL_SECRET_KEY is set in the environment" in caplog.text


def test_different_secrets_in_the_environment_and_a_file_are_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secrets_from_files(monkeypatch, tmp_path, **SECRET_FILES)
    monkeypatch.setenv("DINNERBELL_SECRET_KEY", "stale-env-secret-key-that-is-long-enough")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert "with different values. Remove it from .env." in str(caught.value)
    assert "stale-env" not in str(caught.value)
    assert SECRET_FILES["DINNERBELL_SECRET_KEY"] not in str(caught.value)


def test_secrets_in_the_environment_alone_warn_once_each(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    set_env(monkeypatch, DINNERBELL_SLACK_BOT_TOKEN="xoxb-1-2-3")
    with caplog.at_level("WARNING", logger="dinnerbell.config"):
        load_settings()
    names = [r.getMessage().split()[0] for r in caplog.records if r.name == "dinnerbell.config"]
    assert names == ["DINNERBELL_SECRET_KEY", "DINNERBELL_SLACK_BOT_TOKEN", "DINNERBELL_OIDC_CLIENT_SECRET"]


@pytest.mark.parametrize("build", ["abc1234", "0123456789abcdef0123456789abcdef01234567"])
def test_build_stamp_follows_the_version(monkeypatch: pytest.MonkeyPatch, build: str) -> None:
    set_env(monkeypatch, DINNERBELL_BUILD=build)
    settings = load_settings()
    assert settings.build == build
    assert settings.version_label == f"{__version__}+{build}"


def test_empty_build_stamp_shows_the_bare_version(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, DINNERBELL_BUILD="")
    assert load_settings().version_label == __version__


# The image sets these. An uncommented line in .env, even an empty one, would replace the
# image's value, so .env.example documents them only as comments.
SET_BY_IMAGE = {"build"}


def test_env_example_documents_every_setting() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / ".env.example").read_text()
    for field in Settings.model_fields:
        line = f"\nDINNERBELL_{field.upper()}="
        if field in SECRET_FIELDS:
            # A secret is a line in .env by default; secrets/ keeps a placeholder for the optional file.
            assert line in text, field
            assert (root / "secrets" / f"DINNERBELL_{field.upper()}.example").is_file(), field
        elif field in SET_BY_IMAGE:
            assert line not in text, field
            assert f"\n# DINNERBELL_{field.upper()}=" in text, field
        else:
            assert line in text, field


# Test instance settings (ADR 0006) ----------------------------------------------------------------


def test_test_instance_settings_load(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, DINNERBELL_SLACK_COMMAND="/dinnerbell-test", DINNERBELL_INSTANCE_LABEL=" TEST ")
    settings = load_settings()
    assert settings.slack_command == "/dinnerbell-test"
    assert settings.instance_label == "TEST"


@pytest.mark.parametrize(
    "command",
    ["dinnerbell", "/", "/Dinnerbell", "/dinner bell", "/dinnerbell;rm", "/" + "a" * 32, "/dinnerbell\n"],
)
def test_bad_slash_commands_are_refused(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    message = error_for(monkeypatch, DINNERBELL_SLACK_COMMAND=command)
    assert "DINNERBELL_SLACK_COMMAND" in message


def test_the_longest_slash_command_slack_allows_is_accepted(tmp_path: Path) -> None:
    command = "/" + "a" * 31
    assert make_settings(tmp_path, slack_command=command).slack_command == command


@pytest.mark.parametrize("label", ["T" * 17, "TEST<b>", "TEST:", "TÉST", "TEST\nprod", "TEST_1"])
def test_bad_instance_labels_are_refused(monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    message = error_for(monkeypatch, DINNERBELL_INSTANCE_LABEL=label)
    assert "DINNERBELL_INSTANCE_LABEL" in message


def test_a_blank_label_is_no_label(tmp_path: Path) -> None:
    assert make_settings(tmp_path, instance_label="   ").instance_label == ""
    assert make_settings(tmp_path, instance_label="Test 2-b").instance_label == "Test 2-b"


def test_an_unknown_flag_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    message = error_for(monkeypatch, DINNERBELL_FEATURES="no_such_flag")
    assert "  - DINNERBELL_FEATURES: unknown feature flag 'no_such_flag'" in message


def test_features_parse_a_comma_separated_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dinnerbell.domain import flags

    known = {name: flags.Flag(name, "test", "0.0.0") for name in ("alpha", "beta")}
    monkeypatch.setattr(flags, "FLAGS", known)
    set_env(monkeypatch, DINNERBELL_FEATURES=" alpha, ,beta,alpha ")
    assert load_settings().features == frozenset({"alpha", "beta"})
    assert "unknown feature flag 'gamma'" in error_for(monkeypatch, DINNERBELL_FEATURES="alpha,gamma")
    with pytest.raises(ValueError, match="unknown feature flag 'gamma'"):
        make_settings(tmp_path, features=frozenset({"gamma"}))


def test_a_symlink_to_a_good_file_works(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    directory = secrets_from_files(monkeypatch, tmp_path, **SECRET_FILES)
    target = tmp_path / "store" / "key"
    target.parent.mkdir()
    target.write_text(SECRET_FILES["DINNERBELL_SECRET_KEY"])
    (directory / "DINNERBELL_SECRET_KEY").unlink()
    (directory / "DINNERBELL_SECRET_KEY").symlink_to(target)
    assert load_settings().secret_key.get_secret_value() == SECRET_FILES["DINNERBELL_SECRET_KEY"]


def test_a_broken_symlink_is_an_error_not_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    directory = secrets_from_files(monkeypatch, tmp_path, **SECRET_FILES)
    (directory / "DINNERBELL_SLACK_APP_TOKEN").unlink()
    (directory / "DINNERBELL_SLACK_APP_TOKEN").symlink_to(tmp_path / "gone")
    with pytest.raises(ConfigError, match="DINNERBELL_SLACK_APP_TOKEN is a symlink to a file that doesn't exist"):
        load_settings()


def test_a_multi_line_secret_is_refused_but_a_trailing_newline_is_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = secrets_from_files(monkeypatch, tmp_path, **SECRET_FILES)
    (directory / "DINNERBELL_SECRET_KEY").write_text(SECRET_FILES["DINNERBELL_SECRET_KEY"] + "\n\n")
    assert load_settings().secret_key.get_secret_value() == SECRET_FILES["DINNERBELL_SECRET_KEY"]
    (directory / "DINNERBELL_SECRET_KEY").write_text("first-line-of-the-key\nsecond-line-hunter2\n")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert "has more than one line" in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_an_oversized_secret_file_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dinnerbell.config import MAX_SECRET_BYTES

    directory = secrets_from_files(monkeypatch, tmp_path, **SECRET_FILES)
    (directory / "DINNERBELL_SECRET_KEY").write_text(("k7Qz9xWv" * MAX_SECRET_BYTES)[: MAX_SECRET_BYTES + 1])
    with pytest.raises(ConfigError, match="is larger than 8192 bytes"):
        load_settings()
    (directory / "DINNERBELL_SECRET_KEY").write_text(("k7Qz9xWv" * MAX_SECRET_BYTES)[:MAX_SECRET_BYTES])
    assert len(load_settings().secret_key.get_secret_value()) == MAX_SECRET_BYTES


def test_a_directory_the_service_cannot_search_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("root searches every directory")
    directory = secrets_from_files(monkeypatch, tmp_path, **SECRET_FILES)
    inner = directory / "inner"
    inner.mkdir()
    (inner / "DINNERBELL_SECRET_KEY").write_text(SECRET_FILES["DINNERBELL_SECRET_KEY"])
    inner.chmod(0)
    try:
        with pytest.raises(ConfigError) as caught:
            load_settings(inner)
    finally:
        inner.chmod(0o700)
    assert "Can't read the secret file" in str(caught.value)
    assert "PermissionError" in str(caught.value)


@pytest.mark.parametrize(
    "key",
    [
        "replace-with-48-random-characters",
        "Please_CHANGE-ME_to_something_long_enough_now",
        "your-secret-key-goes-here-please-xxxx",
        "a" * 40,
        "abababababababababababababababab",
    ],
)
def test_placeholder_and_repetitive_secret_keys_are_refused(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    message = error_for(monkeypatch, DINNERBELL_SECRET_KEY=key)
    assert "DINNERBELL_SECRET_KEY" in message
    assert key not in message


def test_the_example_secret_key_file_fails_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    example = Path(__file__).resolve().parents[1] / "secrets" / "DINNERBELL_SECRET_KEY.example"
    (tmp_path / "DINNERBELL_SECRET_KEY").write_text(example.read_text())
    set_env(monkeypatch)
    monkeypatch.delenv("DINNERBELL_SECRET_KEY")
    with pytest.raises(ConfigError) as caught:
        load_settings(tmp_path)
    assert "DINNERBELL_SECRET_KEY" in str(caught.value)
    assert example.read_text().strip() not in str(caught.value)


def test_unknown_setting_names_are_found_without_values() -> None:
    from dinnerbell.config import unknown_setting_names

    environ = {
        "DINNERBELL_TRUSTED_PROXY": "10.0.0.1",
        "DINNERBELL_TRUSTED_PROXIES": "10.0.0.0/8",
        "DINNERBELL_SECRET_KEY": "x",
        "DINNERBELL_SLACK_APP_TOKEN": "x",
        "DINNERBELL_TAG": "0.8.0",
        "DINNERBELL_BUILD": "abc1234",
        "DINNERBELL_COMPOSE": "podman-compose",
        "DINNERBELL_ENV_FILE": ".env",
        "PATH": "/bin",
    }
    assert unknown_setting_names(environ) == ["DINNERBELL_TRUSTED_PROXY"]


def test_every_dinnerbell_variable_in_the_repo_is_a_setting_or_listed() -> None:
    import re

    from dinnerbell.config import NON_SETTING_VARIABLES, unknown_setting_names

    root = Path(__file__).resolve().parents[1]
    found: set[str] = set()
    for path in [
        *root.glob("compose*.yaml"),
        *root.glob(".env*example"),
        *(root / "deploy").iterdir(),
        *(root / "scripts").iterdir(),
    ]:
        if path.is_file():
            found |= set(re.findall(r"DINNERBELL_[A-Z_]+", path.read_text(errors="ignore")))
    found -= {"DINNERBELL_"}
    assert unknown_setting_names(dict.fromkeys(found, "x")) == []
    assert "DINNERBELL_TAG" in NON_SETTING_VARIABLES


def test_serve_logs_one_warning_for_unknown_names(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from dinnerbell import main as main_module

    set_env(monkeypatch, DINNERBELL_TRUSTED_PROXY="10.9.8.7")
    monkeypatch.setattr(main_module, "_command", lambda args, settings: 0)
    monkeypatch.setattr(main_module, "configure_logging", lambda *args: None)  # keep caplog's handler
    with caplog.at_level("WARNING", logger="dinnerbell.config"):
        assert main_module.main(["migrate"]) == 0
    messages = [r.getMessage() for r in caplog.records if "match no setting" in r.getMessage()]
    assert len(messages) == 1
    assert "DINNERBELL_TRUSTED_PROXY" in messages[0]
    assert "10.9.8.7" not in caplog.text
