# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured JSON logs on stdout.

Log member IDs, never emails, tokens, or secrets (SEC-5). Request and member IDs come from
context variables that the web middleware and auth dependencies set.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
member_id_var: ContextVar[int | None] = ContextVar("member_id", default=None)

_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "color_message",  # uvicorn's ANSI-colored duplicate of msg
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if (request_id := request_id_var.get()) is not None:
            entry["request_id"] = request_id
        if (member_id := member_id_var.get()) is not None:
            entry["member_id"] = member_id
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
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
QUIET_LOGGERS = ("sqlalchemy", "aiosqlite", "httpx", "httpcore", "apscheduler", "alembic", "multipart")


def quiet_third_party_loggers() -> None:
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
