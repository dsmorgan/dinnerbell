# SPDX-License-Identifier: AGPL-3.0-or-later
"""`dinnerbell init`: its questions, the generated key, the file's mode, --test, and that no secret
it reads ever reaches standard error."""

from __future__ import annotations

import io
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from dotenv import dotenv_values

from dinnerbell import init_cli
from dinnerbell import main as cli
from dinnerbell.config import generate_secret_key, secret_key_problem

BOT = "xoxb-1111-2222-botsecretvalue"
APP = "xapp-1-A0001-3333-appsecretvalue"
CLIENT_SECRET = "clientsecretvalue0123456789abcdef"
SECRETS = [BOT, APP, CLIENT_SECRET]
# Hostname, admin email, time zone, household name (default), team, channel, client ID.
ANSWERS = ["dinner.example.com", "Admin@Example.com", "America/Chicago", "", "T0123ABCD", "C0123ABCD", "1234.5678"]


class FakeTerminal(io.StringIO):
    """Answers on standard input, as a terminal: secrets then go through getpass."""

    def isatty(self) -> bool:
        return True


def answer(monkeypatch: pytest.MonkeyPatch, lines: list[str], secrets: list[str]) -> list[str]:
    """Feed ``lines`` to the plain questions and ``secrets`` to getpass; return getpass's prompts."""
    monkeypatch.setattr(sys, "stdin", FakeTerminal("".join(f"{line}\n" for line in lines)))
    prompts: list[str] = []
    remaining = iter(secrets)

    def fake_getpass(prompt: str = "", stream: object = None) -> str:
        prompts.append(prompt)
        try:
            return next(remaining)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(init_cli.getpass, "getpass", fake_getpass)
    return prompts


def assert_no_secret(text: str, *values: str) -> None:
    for value in values:
        assert value not in text


def test_init_writes_a_complete_env_file_with_mode_600(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_prompts = answer(monkeypatch, ANSWERS, SECRETS)
    output = tmp_path / ".env"
    assert cli.main(["init", "--output", str(output)]) == 0
    out, err = capsys.readouterr()
    assert out == ""  # the file went to --output; nothing to standard output
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    values = dotenv_values(output)
    assert values["DINNERBELL_BASE_URL"] == "https://dinner.example.com"
    assert values["DINNERBELL_ADMIN_EMAIL"] == "admin@example.com"
    assert values["DINNERBELL_TIMEZONE"] == "America/Chicago"
    assert values["DINNERBELL_HOUSEHOLD_NAME"] == "Home"
    assert values["DINNERBELL_OIDC_ISSUER"] == "https://slack.com"
    assert values["DINNERBELL_SLACK_BOT_TOKEN"] == BOT
    assert values["DINNERBELL_SLACK_APP_TOKEN"] == APP
    assert values["DINNERBELL_OIDC_CLIENT_SECRET"] == CLIENT_SECRET
    assert "DINNERBELL_INSTANCE_LABEL" not in values
    key = values["DINNERBELL_SECRET_KEY"]
    assert key and secret_key_problem(key) is None
    # Each secret was read through getpass, and its prompt carries a hint but never a value.
    assert secret_prompts == [
        "Bot token (Enter to skip): ",
        "App-level token (Enter to skip): ",
        "Sign-in client secret (Enter to skip): ",
    ]
    assert "Bot User OAuth Token" in err and "App-Level Tokens" in err and "Client Secret" in err
    assert "Configuration is valid." in err
    assert_no_secret(err, BOT, APP, CLIENT_SECRET, key)
    # The file passes the real check, read as compose reads it.
    assert cli.main(["config", "check", "--env-file", str(output)]) == 0


def test_generated_keys_pass_every_check_and_differ() -> None:
    keys = {generate_secret_key() for _ in range(20)}
    assert len(keys) == 20
    assert all(len(key) >= 64 and secret_key_problem(key) is None for key in keys)
    assert secret_key_problem("REPLACE-ME-with-48-random-characters-and-more") is not None
    assert secret_key_problem("short") is not None
    assert secret_key_problem("a" * 40) is not None


def test_init_asks_again_after_a_bad_answer_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wrong_secret = "not-a-token-but-a-secret-anyway"
    lines = [
        "",  # the hostname is required
        "https://Dinner.Example.com/",  # a pasted URL becomes its hostname
        "not an email",
        "admin@example.com",
        "Mars/Olympus_Mons",
        "",  # the default time zone
        'The "Smiths"',
        "The Smiths",
        "nope",
        "T0123ABCD",
        "C0123ABCD",
        "1234.5678",
    ]
    answer(monkeypatch, lines, [wrong_secret, BOT, APP, CLIENT_SECRET])
    output = tmp_path / ".env"
    assert cli.main(["init", "-o", str(output)]) == 0
    err = capsys.readouterr().err
    assert "This one is required." in err
    assert "Enter a valid email address." in err
    assert "IANA time zone" in err
    assert "no quotes" in err
    assert "That doesn't look like a team ID" in err
    assert "That doesn't look like a bot token, which starts with xoxb-." in err
    assert_no_secret(err, wrong_secret, BOT, APP, CLIENT_SECRET)
    values = dotenv_values(output)
    assert values["DINNERBELL_BASE_URL"] == "https://dinner.example.com"
    assert values["DINNERBELL_TIMEZONE"] == "America/New_York"
    assert values["DINNERBELL_HOUSEHOLD_NAME"] == "The Smiths"
    assert values["DINNERBELL_SLACK_BOT_TOKEN"] == BOT


def test_skipped_slack_values_leave_blanks_and_fail_the_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answer(monkeypatch, [*ANSWERS[:4], "", "", ""], ["", "", ""])
    assert cli.main(["init"]) == 2  # written to standard output, but incomplete
    out, err = capsys.readouterr()
    values = dotenv_values(stream=io.StringIO(out))
    assert values["DINNERBELL_SLACK_BOT_TOKEN"] == ""
    assert values["DINNERBELL_OIDC_CLIENT_ID"] == ""
    assert "DINNERBELL_OIDC_CLIENT_ID is required but not set" in err
    assert "DINNERBELL_OIDC_CLIENT_SECRET is required but not set" in err
    assert "chmod 600" in err  # standard output isn't a file it can make private itself
    assert_no_secret(err, values["DINNERBELL_SECRET_KEY"] or "missing key")


def test_the_check_ignores_the_rest_of_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DINNERBELL_FEATURES", "no-such-flag")
    monkeypatch.setenv("DINNERBELL_SLACK_BOT_TOKEN", "xoxb-from-the-environment")
    answer(monkeypatch, ANSWERS, SECRETS)
    assert cli.main(["init", "-o", str(tmp_path / ".env")]) == 0
    assert "Configuration is valid." in capsys.readouterr().err
    assert os.environ["DINNERBELL_FEATURES"] == "no-such-flag"  # put back afterward


def test_init_refuses_to_print_the_file_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts = answer(monkeypatch, ANSWERS, SECRETS)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert cli.main(["init"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "Pass --output FILE" in err
    assert prompts == []  # refused before asking anything


def test_init_makes_a_redirected_output_file_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    answer(monkeypatch, ANSWERS, SECRETS)
    target = tmp_path / ".env"
    with target.open("w", encoding="utf-8") as stream:  # as `dinnerbell init > .env` opens it
        os.chmod(target, 0o644)
        monkeypatch.setattr(sys, "stdout", stream)
        assert cli.main(["init"]) == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert dotenv_values(target)["DINNERBELL_SLACK_APP_TOKEN"] == APP
    assert "chmod" not in capsys.readouterr().err


def test_init_keeps_an_existing_file_unless_forced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / ".env"
    output.write_text("DINNERBELL_ADMIN_EMAIL=old@example.com\n")
    prompts = answer(monkeypatch, ANSWERS, SECRETS)
    assert cli.main(["init", "-o", str(output)]) == 1
    assert "already exists. Pass --force" in capsys.readouterr().err
    assert prompts == []
    assert output.read_text() == "DINNERBELL_ADMIN_EMAIL=old@example.com\n"

    output.chmod(0o644)
    assert cli.main(["init", "-o", str(output), "--force"]) == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert dotenv_values(output)["DINNERBELL_ADMIN_EMAIL"] == "admin@example.com"
    assert [path.name for path in tmp_path.iterdir()] == [".env"]  # no temporary file left


def test_init_writes_nothing_when_the_answers_run_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    answer(monkeypatch, ANSWERS[:2], [])
    output = tmp_path / ".env"
    assert cli.main(["init", "-o", str(output)]) == 1
    assert "Nothing written." in capsys.readouterr().err
    assert not output.exists()


def test_init_stops_cleanly_on_ctrl_c(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(prompt: str) -> str:
        raise KeyboardInterrupt

    terminal = init_cli.Terminal(ask=interrupted, ask_secret=interrupted, say=lambda message: None)
    args = cli._parser().parse_args(["init", "-o", str(tmp_path / ".env")])
    assert init_cli.run(args, terminal) == 1
    assert not (tmp_path / ".env").exists()


def test_secrets_are_read_as_plain_answers_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(init_cli, "_warned_about_echo", False)
    lines = [*ANSWERS[:6], BOT, APP, ANSWERS[6], CLIENT_SECRET]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(f"{line}\n" for line in lines)))
    output = tmp_path / ".env"
    assert cli.main(["init", "-o", str(output)]) == 0
    err = capsys.readouterr().err
    assert err.count("a secret you type may show") == 1
    assert_no_secret(err, BOT, APP, CLIENT_SECRET)
    assert dotenv_values(output)["DINNERBELL_OIDC_CLIENT_SECRET"] == CLIENT_SECRET


def write_production(path: Path) -> None:
    path.write_text(
        "DINNERBELL_BASE_URL=https://Dinner.Example.com/\n"
        "DINNERBELL_SECRET_KEY=production-key-0123456789-abcdefghijklmnop\n"
        f"DINNERBELL_SLACK_BOT_TOKEN={BOT}\n"
        f"DINNERBELL_SLACK_APP_TOKEN={APP}\n"
        "DINNERBELL_OIDC_CLIENT_ID=1234.5678\n"
        f"DINNERBELL_OIDC_CLIENT_SECRET={CLIENT_SECRET}\n"
        "DINNERBELL_SLACK_TEAM_ID=T0123ABCD\n"
        "DINNERBELL_TIMEZONE=America/Denver\n"
    )


@pytest.mark.parametrize("explicit", [False, True])
def test_init_test_refuses_production_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], explicit: bool
) -> None:
    production = tmp_path / ("production.env" if explicit else ".env")
    write_production(production)
    test_bot, test_app, test_secret = "xoxb-9-9-testbot", "xapp-9-A9-9-testapp", "testclientsecret99"
    lines = [
        "dinner.example.com",  # production's hostname: refused
        "dinnerbell-test.example.com",
        "admin@example.com",
        "",  # defaults to production's time zone
        "",  # defaults to Test
        "T0123ABCD",  # the same workspace as production: allowed
        "C0TEST1234",
        "1234.5678",  # production's client ID: refused
        "9999.0000",
    ]
    answer(monkeypatch, lines, [BOT, test_bot, APP, test_app, CLIENT_SECRET, test_secret])
    output = tmp_path / ".env.test"
    argv = ["init", "--test", "-o", str(output)]
    if explicit:
        argv += ["--production-env", str(production)]
    assert cli.main(argv) == 0
    err = capsys.readouterr().err
    for noun in ("hostname", "bot token", "app-level token", "sign-in client ID", "sign-in client secret"):
        assert f"That's production's {noun}. Test needs its own." in err
    assert_no_secret(err, BOT, APP, CLIENT_SECRET, test_bot, test_app, test_secret)
    assert "port 8014" in err
    values = dotenv_values(output)
    assert values["DINNERBELL_INSTANCE_LABEL"] == "TEST"
    assert values["DINNERBELL_SLACK_COMMAND"] == "/dinnerbell-test"
    assert values["DINNERBELL_HOUSEHOLD_NAME"] == "Test"
    assert values["DINNERBELL_TIMEZONE"] == "America/Denver"
    assert values["DINNERBELL_BASE_URL"] == "https://dinnerbell-test.example.com"
    assert values["DINNERBELL_SLACK_BOT_TOKEN"] == test_bot
    assert values["DINNERBELL_SECRET_KEY"] != "production-key-0123456789-abcdefghijklmnop"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_init_test_without_production_says_it_did_not_compare(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answer(monkeypatch, ANSWERS, SECRETS)
    assert cli.main(["init", "--test"]) == 0
    out, err = capsys.readouterr()
    assert "Not comparing with production's env file" in err
    assert dotenv_values(stream=io.StringIO(out))["DINNERBELL_INSTANCE_LABEL"] == "TEST"


@pytest.mark.parametrize(
    ("setup", "argv", "message"),
    [
        (lambda tmp: None, ["--production-env", "{tmp}/.env"], "--production-env goes with --test"),
        (lambda tmp: None, ["--test", "--production-env", "{tmp}/missing.env"], "Can't read production's env file"),
        (
            lambda tmp: write_production(tmp / ".env"),
            ["--test", "--force", "-o", "{tmp}/.env"],
            "is production's env file",
        ),
    ],
)
def test_init_option_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    setup: Callable[[Path], None],
    argv: list[str],
    message: str,
) -> None:
    setup(tmp_path)
    prompts = answer(monkeypatch, ANSWERS, SECRETS)
    assert cli.main(["init", *(arg.format(tmp=tmp_path) for arg in argv)]) == 1
    assert message in capsys.readouterr().err
    assert prompts == []
