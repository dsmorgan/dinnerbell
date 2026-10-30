# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell init``: ask for the settings a new install needs, and write its env file.

Questions and messages go to standard error, and the env file to standard output or ``--output``
(mode 600). Secrets are read without echo and never printed: not in a prompt, an error, or a log.
It needs no configuration and no database, so it runs in a one-off container of the release image.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from dinnerbell import __version__
from dinnerbell.config import ENV_PREFIX, SLACK_ISSUER, Settings, format_validation_error, generate_secret_key
from dinnerbell.domain.members import InvalidMemberInput, normalize_email

EXIT_OK = 0
EXIT_STOPPED = 1
EXIT_INVALID = 2

# Every value init writes matches this, so the file needs no quotes: podman-compose 1.1.0 and older
# pass env_file to podman, which would keep quotes as part of the value.
PLAIN_VALUE = re.compile(r"[A-Za-z0-9._@:/+-]+")
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME = re.compile(rf"{LABEL}(?:\.{LABEL})*(?::[0-9]{{1,5}})?")
HOUSEHOLD_NAME = re.compile(r"[\w ,.&()!?+-]{1,80}")
DEFAULT_TIMEZONE = "America/New_York"
# Settings test must never share with production. The first four are deploy/dinnerbell-test's SECRETS.
NOT_SHARED_WITH_PRODUCTION = (
    "DINNERBELL_SECRET_KEY",
    "DINNERBELL_SLACK_BOT_TOKEN",
    "DINNERBELL_SLACK_APP_TOKEN",
    "DINNERBELL_OIDC_CLIENT_SECRET",
    "DINNERBELL_OIDC_CLIENT_ID",
    "DINNERBELL_BASE_URL",
)


def add_parser(commands: Any) -> None:
    init = commands.add_parser(
        "init",
        help="write a new env file, asking for each setting",
        description="Ask for the hostname, admin email, time zone, household name, and the Slack app's values, "
        "generate DINNERBELL_SECRET_KEY, write the env file, and check it as `config check` does. Questions go "
        "to standard error; the file goes to standard output or FILE. Secrets are never shown. Press Enter "
        "to skip a Slack value and fill it in later.",
    )
    init.add_argument(
        "--output", "-o", type=Path, metavar="FILE", help="write to FILE, with mode 600, instead of standard output"
    )
    init.add_argument("--force", action="store_true", help="replace FILE if it exists")
    init.add_argument(
        "--test",
        action="store_true",
        help="write a test instance's file (.env.test): label TEST, command /dinnerbell-test, household Test",
    )
    init.add_argument(
        "--production-env",
        type=Path,
        metavar="FILE",
        help="with --test, production's env file, whose secrets, client ID, and URL test must not reuse "
        "(default: .env next to the --output FILE, when there is one)",
    )


@dataclass
class Terminal:
    """Where questions go and answers come from. Tests replace it."""

    ask: Callable[[str], str]
    ask_secret: Callable[[str], str]
    say: Callable[[str], None]


def _ask(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\r\n")


_warned_about_echo = False


def _ask_secret(prompt: str) -> str:
    """Read a secret with echo off. Without a terminal on standard input (piped answers, or a container
    run with -i but not -t), read it like any answer, after one warning that typing may show."""
    global _warned_about_echo
    if sys.stdin.isatty():
        return getpass.getpass(prompt, stream=sys.stderr)
    if not _warned_about_echo:
        _warned_about_echo = True
        _say("  Standard input isn't a terminal, so a secret you type may show. In a container, run with -it.")
    return _ask(prompt)


def _say(message: str) -> None:
    print(message, file=sys.stderr)


TERMINAL = Terminal(ask=_ask, ask_secret=_ask_secret, say=_say)


class Stop(Exception):
    """Init can't go on. The message is safe to print."""


@dataclass(frozen=True)
class Question:
    name: str
    prompt: str
    hint: str
    check: Callable[[str], str | None]
    """Return the error for an answer, or None. An error never quotes the answer."""
    secret: bool = False


def _check_plain(what: str) -> Callable[[str], str | None]:
    def check(value: str) -> str | None:
        return None if PLAIN_VALUE.fullmatch(value) else f"That doesn't look like {what}."

    return check


def _check_prefix(prefix: str, what: str) -> Callable[[str], str | None]:
    def check(value: str) -> str | None:
        if not value.startswith(prefix) or not PLAIN_VALUE.fullmatch(value):
            return f"That doesn't look like {what}, which starts with {prefix}."
        return None

    return check


def _check_pattern(pattern: str, what: str) -> Callable[[str], str | None]:
    compiled = re.compile(pattern)

    def check(value: str) -> str | None:
        return None if compiled.fullmatch(value) else f"That doesn't look like {what}."

    return check


SLACK_QUESTIONS = (
    Question(
        "DINNERBELL_SLACK_TEAM_ID",
        "Slack team ID",
        "Open Slack in a browser: the address is app.slack.com/client/T.../, and the T... part is the ID.",
        _check_pattern(r"T[A-Z0-9]{4,}", "a team ID, such as T0123ABCD"),
    ),
    Question(
        "DINNERBELL_SLACK_CHANNEL",
        "Slack channel ID",
        "In the channel, select its name; the ID (C...) is at the bottom of the About tab.",
        _check_pattern(r"[CG][A-Z0-9]{4,}", "a channel ID, such as C0123ABCD"),
    ),
    Question(
        "DINNERBELL_SLACK_BOT_TOKEN",
        "Bot token",
        "In the app's settings: Install App > Bot User OAuth Token (xoxb-...).",
        _check_prefix("xoxb-", "a bot token"),
        secret=True,
    ),
    Question(
        "DINNERBELL_SLACK_APP_TOKEN",
        "App-level token",
        "Basic Information > App-Level Tokens: generate one with the connections:write scope (xapp-...).",
        _check_prefix("xapp-", "an app-level token"),
        secret=True,
    ),
    Question(
        "DINNERBELL_OIDC_CLIENT_ID",
        "Sign-in client ID",
        "Basic Information > App Credentials > Client ID.",
        _check_plain("a client ID"),
    ),
    Question(
        "DINNERBELL_OIDC_CLIENT_SECRET",
        "Sign-in client secret",
        "Basic Information > App Credentials > Client Secret (select Show).",
        _check_plain("a client secret"),
        secret=True,
    ),
)


def _check_hostname(value: str) -> str | None:
    if len(value) > 253 or not HOSTNAME.fullmatch(value):
        return "Enter a hostname, such as dinner.example.com, with no https:// and no path."
    return None


def _check_email(value: str) -> str | None:
    try:
        normalize_email(value)
    except InvalidMemberInput as exc:
        return str(exc)
    return None if PLAIN_VALUE.fullmatch(value) else "Enter a valid email address."


def _check_timezone(value: str) -> str | None:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return "Enter an IANA time zone name, such as America/Chicago or Europe/Berlin."
    return None if PLAIN_VALUE.fullmatch(value) else "Enter an IANA time zone name, such as America/Chicago."


def _check_household(value: str) -> str | None:
    if not HOUSEHOLD_NAME.fullmatch(value) or value != value.strip():
        return "Use 1 to 80 letters, digits, spaces, and simple punctuation (no quotes, #, or $)."
    return None


def _hostname_answer(value: str) -> str:
    """What people type as a hostname, without a scheme or a trailing slash they pasted."""
    value = value.strip()
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
    return value.rstrip("/").lower()


class Interview:
    def __init__(self, terminal: Terminal, production: Mapping[str, str]) -> None:
        self.terminal = terminal
        self.production = production

    def shared(self, name: str, value: str) -> bool:
        return name in NOT_SHARED_WITH_PRODUCTION and bool(value) and self.production.get(name, "") == value

    def ask(self, question: Question, *, default: str = "", optional: bool = False) -> str:
        """Ask until the answer passes ``question.check``, or is blank where blank is allowed."""
        self.terminal.say(f"  {question.hint}")
        suffix = f" [{default}]" if default else (" (Enter to skip)" if optional else "")
        while True:
            prompt = f"{question.prompt}{suffix}: "
            raw = self.terminal.ask_secret(prompt) if question.secret else self.terminal.ask(prompt)
            value = raw.strip()
            if question.name == "DINNERBELL_BASE_URL":
                value = _hostname_answer(value)
            if not value:
                if default:
                    return default
                if optional:
                    return ""
                self.terminal.say("  This one is required.")
                continue
            if (problem := question.check(value)) is not None:
                self.terminal.say(f"  {problem}")
                continue
            stored = f"https://{value}" if question.name == "DINNERBELL_BASE_URL" else value
            if self.shared(question.name, stored):
                noun = question.prompt[0].lower() + question.prompt[1:]
                self.terminal.say(f"  That's production's {noun}. Test needs its own.")
                continue
            return stored


def _read_production(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    try:
        with path.open(encoding="utf-8") as stream:
            parsed = dotenv_values(stream=stream)
    except (OSError, UnicodeDecodeError) as exc:
        # Never the exception text: a decode error quotes bytes from the file.
        raise Stop(f"Can't read production's env file {path} ({type(exc).__name__}). Nothing written.") from None
    values = {name: value.strip() for name, value in parsed.items() if value}
    if url := values.get("DINNERBELL_BASE_URL"):
        values["DINNERBELL_BASE_URL"] = url.rstrip("/").lower()
    return values


def _production_values(args: argparse.Namespace, terminal: Terminal) -> dict[str, str]:
    if not args.test:
        if args.production_env is not None:
            raise Stop("--production-env goes with --test. Nothing written.")
        return {}
    path: Path | None = args.production_env
    if path is None and args.output is not None and (args.output.parent / ".env").is_file():
        path = args.output.parent / ".env"
    if path is None:
        terminal.say(
            "Not comparing with production's env file. Pass --production-env FILE to check that test's "
            "secrets differ; deploy/dinnerbell-test checks again before every start."
        )
        return {}
    if args.output is not None and args.output.exists() and _same_file(args.output, path):
        raise Stop(f"{args.output} is production's env file. Write test's to .env.test. Nothing written.")
    values = _read_production(path)
    terminal.say(f"Test's secrets, client ID, and URL must differ from production's in {path}.")
    return values


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def interview(terminal: Terminal, *, test: bool, production: Mapping[str, str]) -> dict[str, str]:
    """Ask every question and return the settings, in the order they're written."""
    asking = Interview(terminal, production)
    which = "the test instance" if test else "Dinnerbell"
    terminal.say(f"Answer a few questions to write {which}'s env file. Press Ctrl+C to stop; nothing is written.")
    values: dict[str, str] = {}
    values["DINNERBELL_BASE_URL"] = asking.ask(
        Question(
            "DINNERBELL_BASE_URL",
            "Hostname",
            "The public hostname people open, such as dinner.example.com"
            + (" (test's own, never production's)." if test else "."),
            _check_hostname,
        )
    )
    secret_key = generate_secret_key()
    while asking.shared("DINNERBELL_SECRET_KEY", secret_key):  # pragma: no cover - a 1-in-2**384 chance
        secret_key = generate_secret_key()
    values["DINNERBELL_SECRET_KEY"] = secret_key
    values["DINNERBELL_ADMIN_EMAIL"] = normalize_email(
        asking.ask(
            Question(
                "DINNERBELL_ADMIN_EMAIL",
                "Admin email",
                "The first admin: the email address of your Slack account.",
                _check_email,
            )
        )
    )
    zone = production.get("DINNERBELL_TIMEZONE", "")  # test defaults to production's
    values["DINNERBELL_TIMEZONE"] = asking.ask(
        Question(
            "DINNERBELL_TIMEZONE",
            "Time zone",
            "The household's IANA time zone, such as America/Chicago. Calls and backups follow it.",
            _check_timezone,
        ),
        default=zone if _check_timezone(zone) is None else DEFAULT_TIMEZONE,
    )
    values["DINNERBELL_HOUSEHOLD_NAME"] = asking.ask(
        Question(
            "DINNERBELL_HOUSEHOLD_NAME",
            "Household name",
            "Shown in the app. Like the time zone, it's read on the first start only.",
            _check_household,
        ),
        default="Test" if test else "Home",
    )
    terminal.say("")
    terminal.say(
        "Now the Slack app's values. Secrets don't show as you type or paste. Press Enter to skip one and "
        "fill it in later."
    )
    values["DINNERBELL_OIDC_ISSUER"] = SLACK_ISSUER
    for question in SLACK_QUESTIONS:
        values[question.name] = asking.ask(question, optional=True)
    if test:
        values["DINNERBELL_INSTANCE_LABEL"] = "TEST"
        values["DINNERBELL_SLACK_COMMAND"] = "/dinnerbell-test"
    return values


def render(values: Mapping[str, str], *, test: bool) -> str:
    kind = "test instance" if test else "Dinnerbell"
    lines = [
        f"# {kind} settings, written by `dinnerbell init` {__version__}. .env.example explains each one.",
        "# Keep this file private (mode 600) and out of version control. Empty values count as unset.",
        *(f"{name}={value}" for name, value in values.items()),
    ]
    if not test:
        lines += [
            "",
            "# Compose only: the host address that port 8013 is published on. The default, 127.0.0.1, works",
            "# when the reverse proxy runs on this host. Otherwise use this host's LAN address, such as:",
            "# DINNERBELL_BIND=192.168.1.20",
        ]
    return "\n".join(lines) + "\n"


@contextmanager
def _only(values: Mapping[str, str]) -> Iterator[None]:
    """Make ``values`` the only DINNERBELL_* settings in ``os.environ`` for the duration."""
    replaced = {name: "" for name in os.environ if name.upper().startswith(ENV_PREFIX)} | dict(values)
    saved = {name: os.environ.get(name) for name in replaced}
    os.environ.update(replaced)
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous


def check(values: Mapping[str, str]) -> str | None:
    """Validate ``values`` as `dinnerbell config check` validates an env file with no secret files,
    ignoring the rest of the environment. Returns the problems, which name settings, never values."""
    with _only(values):
        try:
            Settings()  # type: ignore[call-arg]
        except ValidationError as exc:
            return format_validation_error(exc, Path("secrets"))
    return None


def _write_file(output: Path, text: str, *, force: bool) -> None:
    """Write ``text`` to ``output`` with mode 600: created exclusively, or with ``force`` written to a
    private temporary file next to it and renamed over it, so no other user can ever read it."""
    data = text.encode("utf-8")
    try:
        if not force:
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as file:
                os.fchmod(file.fileno(), 0o600)
                file.write(data)
            return
        mode = os.lstat(output).st_mode if os.path.lexists(output) else stat.S_IFREG
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise Stop(f"{output} isn't a regular file, so --force won't replace it. Nothing written.")
        fd, temporary = tempfile.mkstemp(dir=output.parent, prefix=f".{output.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as file:
                os.fchmod(file.fileno(), 0o600)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, output)
        except BaseException:
            with suppress(OSError):
                os.unlink(temporary)
            raise
    except FileExistsError:
        raise Stop(f"{output} already exists. Pass --force to replace it. Nothing written.") from None
    except OSError as exc:
        raise Stop(f"Can't write {output}: {exc.strerror or type(exc).__name__}. Nothing written.") from None


def _write_stdout(text: str, terminal: Terminal) -> None:
    """Write to standard output. When the shell redirected it to a file, make that file private first."""
    try:
        fd = sys.stdout.fileno()
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except (OSError, ValueError, AttributeError):
        fd, regular = -1, False
    if regular:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            terminal.say("Can't make the output file private. Run chmod 600 on it.")
    sys.stdout.write(text)
    sys.stdout.flush()
    if not regular:
        terminal.say("Make the file you saved this to private: chmod 600 FILE.")


def run(args: argparse.Namespace, terminal: Terminal = TERMINAL) -> int:
    if args.output is None and sys.stdout.isatty():
        terminal.say(
            "Standard output is a terminal, where the env file's secrets would show. Pass --output FILE "
            "(in a container, a file in a mounted directory), or redirect standard output to a file."
        )
        return EXIT_INVALID
    try:
        if args.output is not None and not args.force and os.path.lexists(args.output):
            raise Stop(f"{args.output} already exists. Pass --force to replace it. Nothing written.")
        production = _production_values(args, terminal)
        try:
            values = interview(terminal, test=args.test, production=production)
        except EOFError:
            raise Stop("No more answers. Nothing written.") from None
        text = render(values, test=args.test)
        if args.output is None:
            _write_stdout(text, terminal)
        else:
            _write_file(args.output, text, force=args.force)
    except Stop as exc:
        terminal.say(str(exc))
        return EXIT_STOPPED
    except KeyboardInterrupt:
        terminal.say("\nStopped. Nothing written.")
        return EXIT_STOPPED
    where = "the env file" if args.output is None else str(args.output)
    terminal.say("")
    if (problems := check(values)) is not None:
        terminal.say(f"Wrote {where}, but it isn't complete yet.")
        terminal.say(problems)
        terminal.say(
            "Fill in the blanks, then check the file again with: "
            f"podman run --rm -i ghcr.io/dsmorgan/dinnerbell:{__version__} config check --env-file - < FILE"
        )
        return EXIT_INVALID
    terminal.say(f"Wrote {where}. Configuration is valid.")
    if args.test:
        terminal.say("Point the test hostname at port 8014, then start it with: deploy/dinnerbell-test up -d")
    else:
        terminal.say("Point your hostname at port 8013, then start it with: podman-compose up -d")
        terminal.say('Optional, with podman: to start it at boot, see "Start at boot" in docs/setup.md.')
    return EXIT_OK
