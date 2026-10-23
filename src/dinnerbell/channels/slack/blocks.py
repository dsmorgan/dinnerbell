# SPDX-License-Identifier: AGPL-3.0-or-later
"""Block Kit building blocks: text objects, buttons, escaping, truncation, and times.

Limits are from Slack's Block Kit reference (docs.slack.dev/reference/block-kit): 50 blocks
per message and 100 per modal or Home tab; header text 150 characters; section text 3,000 and
each field 2,000; button text 75, value 2,000, URL 3,000; option text 75 and value 150.
Everything a person typed goes through ``escape`` before it reaches mrkdwn, so a name like
``<!channel>`` can't ping anyone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from dinnerbell.domain.notifications import PersonInfo

Block = dict[str, Any]

MAX_MESSAGE_BLOCKS = 50
MAX_VIEW_BLOCKS = 100
MAX_HEADER = 150
MAX_SECTION = 3000
MAX_FIELD = 2000
MAX_FIELDS = 10
MAX_BUTTON_TEXT = 75
MAX_BUTTON_VALUE = 2000
MAX_URL = 3000
MAX_ACTIONS = 25
MAX_CONTEXT_ELEMENTS = 10
MAX_OPTION_TEXT = 75
MAX_OPTION_VALUE = 150
MAX_OPTIONS = 100
MAX_MODAL_TITLE = 24
MAX_FALLBACK = 4000
"""Slack recommends keeping a message's top-level ``text`` under 4,000 characters."""
ELLIPSIS = "…"

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# Text ------------------------------------------------------------------------------------------


def escape(text: str) -> str:
    """Escape the three characters Slack treats as control sequences in mrkdwn and text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clip(text: str, limit: int) -> str:
    """Plain text cut to ``limit`` characters, with an ellipsis when cut."""
    return text if len(text) <= limit else text[: limit - 1] + ELLIPSIS


def clip_mrkdwn(text: str, limit: int) -> str:
    """mrkdwn cut to ``limit`` without splitting a ``<...>`` link or mention or an ``&...;``
    escape, which Slack would render as garbage or reject."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    open_angle, open_amp = cut.rfind("<"), cut.rfind("&")
    if open_angle > cut.rfind(">"):
        cut = cut[:open_angle]
    if open_amp > cut.rfind(";"):
        cut = cut[:open_amp]
    return cut + ELLIPSIS


def join_lines(lines: Iterable[str], limit: int = MAX_SECTION, more: str = "…and more on the web.") -> str:
    """Join lines until the next one would pass ``limit``, then say there's more."""
    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if out else 0)
        if used + cost > limit - len(more) - 1:
            out.append(more)
            break
        out.append(line)
        used += cost
    return clip_mrkdwn("\n".join(out), limit)


def safe_url(url: str | None) -> str | None:
    """An http(s) URL safe inside ``<url|label>`` and within the URL limit, or None."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or len(url) > MAX_URL:
        return None
    return quote(url, safe=":/?#[]@!$&'()*+,;=%-._~")


def link(url: str | None, label: str) -> str:
    """mrkdwn for a link, or the escaped label alone when the URL isn't usable."""
    safe = safe_url(url)
    return f"<{safe}|{escape(label)}>" if safe else escape(label)


@dataclass(frozen=True)
class Directory:
    """Who is who on Slack: member ID to Slack user ID."""

    slack_ids: Mapping[int, str] = field(default_factory=dict)

    def mention(self, person: PersonInfo) -> str:
        """``<@U…>`` when the member's Slack user is known, else their escaped name."""
        slack_id = self.slack_ids.get(person.id)
        return f"<@{slack_id}>" if slack_id else escape(person.name)

    def mentions(self, people: Iterable[PersonInfo]) -> str:
        return ", ".join(self.mention(p) for p in people)


def names(people: Iterable[PersonInfo]) -> str:
    """Plain names, never mentions: for lists that shouldn't ping anyone."""
    return ", ".join(escape(p.name) for p in people)


def plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


# Times -----------------------------------------------------------------------------------------


def clock(at: time) -> str:
    """A friendly 12-hour time: 5:15 PM."""
    hour = at.hour % 12 or 12
    return f"{hour}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"


def local_clock(at: datetime, zone: str) -> str:
    """A UTC time shown in the household's time zone."""
    return clock(at.astimezone(ZoneInfo(zone)).time())


def day_label(day: date) -> str:
    """Thursday, Oct 22. English names, whatever the server's locale."""
    return f"{DAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day}"


# Block Kit objects -----------------------------------------------------------------------------


def plain(text: str, limit: int = MAX_SECTION) -> dict[str, Any]:
    return {"type": "plain_text", "text": clip(text, limit), "emoji": True}


def mrkdwn(text: str, limit: int = MAX_SECTION) -> dict[str, Any]:
    return {"type": "mrkdwn", "text": clip_mrkdwn(text, limit)}


def header(text: str) -> Block:
    return {"type": "header", "text": plain(text, MAX_HEADER)}


def section(text: str, *, block_id: str | None = None) -> Block:
    block: Block = {"type": "section", "text": mrkdwn(text)}
    if block_id:
        block["block_id"] = block_id
    return block


def fields_section(fields: Iterable[str]) -> Block:
    return {"type": "section", "fields": [mrkdwn(f, MAX_FIELD) for f in list(fields)[:MAX_FIELDS]]}


def context(*texts: str) -> Block:
    return {"type": "context", "elements": [mrkdwn(t) for t in texts[:MAX_CONTEXT_ELEMENTS]]}


def divider() -> Block:
    return {"type": "divider"}


def actions(*elements: Block, block_id: str) -> Block:
    return {"type": "actions", "block_id": block_id, "elements": list(elements)[:MAX_ACTIONS]}


def button(
    text: str,
    action_id: str,
    value: str | None = None,
    *,
    style: str | None = None,
    url: str | None = None,
    confirm: dict[str, Any] | None = None,
) -> Block:
    element: Block = {"type": "button", "text": plain(text, MAX_BUTTON_TEXT), "action_id": action_id}
    if value is not None:
        element["value"] = value[:MAX_BUTTON_VALUE]
    if url is not None:
        element["url"] = url
    if style is not None:
        element["style"] = style
    if confirm is not None:
        element["confirm"] = confirm
    return element


def confirm(title: str, text: str, yes: str, no: str = "Not yet") -> dict[str, Any]:
    return {
        "title": plain(title, 100),
        "text": plain(text, 300),
        "confirm": plain(yes, 30),
        "deny": plain(no, 30),
    }


def option(text: str, value: str) -> dict[str, Any]:
    return {"text": plain(text, MAX_OPTION_TEXT), "value": value[:MAX_OPTION_VALUE]}
