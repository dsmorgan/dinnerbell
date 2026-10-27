# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured JSON logs on stdout.

Log member IDs and display names, never emails, tokens, or secrets (SEC-5). Request and member
IDs, and the channel the work came from, come from context variables that each entry point
sets (``start_unit``): the web middleware and auth dependencies, Slack handlers, scheduler jobs,
and the command line.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
member_id_var: ContextVar[int | None] = ContextVar("member_id", default=None)
channel_var: ContextVar[str | None] = ContextVar("channel", default=None)
"""Where the current work came from: ``slack``, ``web``, ``cli``, or ``scheduler`` (#75)."""
subject_var: ContextVar[dict[str, Any] | None] = ContextVar("subject", default=None)
"""What the current work is about, such as the call and the place, for a refusal's log line
(``services.activity``)."""


def start_unit(channel: str, request_id: str | None = None) -> Callable[[], None]:
    """Begin one unit of work, such as a web request, a Slack tap, or a scheduler job: a request
    ID (a new one unless given), ``channel``, and no member or subject yet. Every line it logs
    carries the request ID and channel, so one tap's lines group together. Returns a function
    that restores the context as it was."""
    tokens = (
        request_id_var.set(request_id or uuid.uuid4().hex),
        member_id_var.set(None),
        channel_var.set(channel),
        subject_var.set(None),
    )

    def end() -> None:
        request_id_var.reset(tokens[0])
        member_id_var.reset(tokens[1])
        channel_var.reset(tokens[2])
        subject_var.reset(tokens[3])

    return end


_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "color_message",  # uvicorn's ANSI-colored duplicate of msg
}


def _stamp_context(factory: Callable[..., logging.LogRecord]) -> Callable[..., logging.LogRecord]:
    """Copy the request and member IDs and the channel onto each record when it is created, so
    they survive handlers that format later (queues, test capture) after the context is gone."""

    def make(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = factory(*args, **kwargs)
        record._request_id = request_id_var.get()
        record._member_id = member_id_var.get()
        record._channel = channel_var.get()
        return record

    make.dinnerbell_context = True  # type: ignore[attr-defined]
    return make


if not getattr(logging.getLogRecordFactory(), "dinnerbell_context", False):
    logging.setLogRecordFactory(_stamp_context(logging.getLogRecordFactory()))


class JsonFormatter(logging.Formatter):
    def __init__(self, instance: str = "") -> None:
        super().__init__()
        self.instance = instance
        """The instance label, on every entry when set, so test and production logs can't mix."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if (request_id := getattr(record, "_request_id", None)) is not None:
            entry["request_id"] = request_id
        if (member_id := getattr(record, "_member_id", None)) is not None:
            entry["member_id"] = member_id
        if (channel := getattr(record, "_channel", None)) is not None:
            entry["channel"] = channel
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if self.instance:
            entry["instance"] = self.instance  # after the extras, so none can replace it
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str, instance: str = "") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(instance))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    quiet_third_party_loggers()


# Libraries that log SQL parameters, URLs, or other request data at INFO or DEBUG. Keeping them
# at WARNING even when DINNERBELL_LOG_LEVEL=DEBUG keeps emails and tokens out of logs (SEC-5).
QUIET_LOGGERS = (
    "sqlalchemy",
    "aiosqlite",
    "httpx",
    "httpcore",
    "apscheduler",
    "alembic",
    "multipart",
    # Slack's SDK logs request parameters and payloads (member names, emails from lookups) at
    # DEBUG, and Socket Mode traffic at INFO.
    "slack_sdk",
    "slack_bolt",
    "aiohttp",
)


def quiet_third_party_loggers() -> None:
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
