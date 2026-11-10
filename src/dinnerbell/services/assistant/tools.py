# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's tools and the one wrapper that runs them (spec 6.14, **Scope**).

Every tool runs as ``tool_actor(member)`` (``acting``): the member it's answering, with a
member's role even when they're an admin, so no admin-only branch in a service ever passes for
the assistant. Rules that match on the member's ID still apply. Tools call the same services as
the forms, and read household settings directly, never through an admin-only use case.

A tool returns text for the model: data, never instructions (the system prompt says so). A
tool marked ``private_only`` is offered only in a private conversation (the web page, a DM).
The first build's tools work in any conversation: look up catalog items, find candidates, and
draft a proposal, an offer, or catalog items. A draft changes nothing until its member confirms
it (``drafts.confirm_draft``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.assistant import Pseudonyms, web_key
from dinnerbell.domain.assistant_drafts import Narrowing
from dinnerbell.domain.catalog import MODE_ORDER, InvalidCatalogInput, ItemKind, Meal, Mode, name_key
from dinnerbell.domain.events import EventRuleViolation, InvalidResponse
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.offers import DEFAULT_CAPACITY
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.assistant import drafts
from dinnerbell.services.assistant.acting import ToolRefusal, tool_actor
from dinnerbell.services.assistant.candidates import CallRef, call_basics, find_candidates, resolve_call
from dinnerbell.services.assistant.drafts import DraftView, OfferRequest
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.variety import household_today

log = logging.getLogger(__name__)

__all__ = ["TOOLS", "Tool", "ToolContext", "ToolInputError", "ToolResult", "ToolSession", "run_tool", "tool_actor"]

MAX_QUERY_LENGTH = 80
MAX_ITEMS = 25
"""The most items one lookup returns, so a broad query can't fill the request."""


MAX_CANDIDATES = 25
MAX_CATALOG_CHARS = 40_000
"""The most catalog file text one draft takes: far more than one answer's output can hold."""
CALL_SLOTS = ["", *(slot.value for slot in Slot)]
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME = re.compile(r"\d{1,2}:\d{2}")


@dataclass
class ToolSession:
    """What one message's tools share: the conversation's key, its call (the thread's call, for
    a Slack thread rooted under a Dinnerbell post), its pseudonyms, which tools add to as they
    name members, and the drafts made so far, for the reply."""

    key: str
    pseudonyms: Pseudonyms = field(default_factory=Pseudonyms)
    call_id: int | None = None
    drafts: list[DraftView] = field(default_factory=list)

    def label(self, member_id: int) -> str:
        self.pseudonyms = self.pseudonyms.with_member(member_id)
        return self.pseudonyms.label(member_id) or "a member"


@dataclass(frozen=True, slots=True)
class ToolContext:
    db: AsyncSession
    actor: Member
    """Always ``tool_actor`` of the member being answered."""
    private: bool
    now: datetime
    session: ToolSession


class ToolInputError(ToolRefusal):
    """The model sent input the tool can't use. The message goes back to the model."""


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    run: Callable[[ToolContext, Mapping[str, Any]], Awaitable[str]]
    private_only: bool = False

    def definition(self) -> dict[str, Any]:
        """The tool as the Messages API takes it. Strict, so the model's input matches the schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "strict": True,
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    is_error: bool = False


def _text(raw: Mapping[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str):
        raise ToolInputError(f"{name} must be a string; send an empty string for any.")
    if len(value) > MAX_QUERY_LENGTH:
        raise ToolInputError(f"{name} is at most {MAX_QUERY_LENGTH} characters.")
    return value.strip()


async def look_up_catalog_items(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    """Active catalog items whose name contains ``name`` and that have a tag containing ``tag``,
    in name order: name, kind, tags, modes, and meals. Never notes, links, cooks, or anything
    about people."""
    if set(raw) != {"name", "tag"}:
        raise ToolInputError("Send exactly two fields, name and tag.")
    name, tag = name_key(_text(raw, "name")), name_key(_text(raw, "tag"))
    data = await catalog_service.catalog(ctx.db, ctx.actor)
    tag_names = {t.id: t.name for t in data.tags}
    found = []
    for item in sorted(data.items, key=lambda i: (i.key, i.id)):
        names = sorted((tag_names[t] for t in item.tag_ids if t in tag_names), key=name_key)
        if not item.active or name not in item.key:
            continue
        if tag and not any(tag in name_key(t) for t in names):
            continue
        found.append(
            {
                "name": item.name,
                "kind": item.kind.value,
                "tags": names,
                "modes": [mode.value for mode in MODE_ORDER if mode in item.modes],
                "meals": [meal.value for meal in Meal if meal in item.meals],
            }
        )
    return json.dumps({"items": found[:MAX_ITEMS], "more": len(found) > MAX_ITEMS}, ensure_ascii=False)


LOOK_UP_CATALOG_ITEMS = Tool(
    name="look_up_catalog_items",
    description=(
        "Look up the household's catalog of places and home meals. Call this whenever the member "
        "asks what's in the catalog, whether a place or dish is there, or what has a tag such as a "
        "cuisine or a dietary tag. Returns active items in name order, at most 25, with each item's "
        "name, kind (home or restaurant), tags, modes (cook, pickup, delivery), and meals "
        "(breakfast, lunch, dinner); more is true when more items matched."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Text the item's name contains, ignoring case. An empty string matches every name.",
            },
            "tag": {
                "type": "string",
                "description": "Text one of the item's tags contains, such as Thai or vegetarian. "
                "An empty string matches any tags.",
            },
        },
        "required": ["name", "tag"],
        "additionalProperties": False,
    },
    run=look_up_catalog_items,
)

# Candidates and drafts --------------------------------------------------------------------------


def _fields(raw: Mapping[str, Any], *names: str) -> None:
    if set(raw) != set(names):
        raise ToolInputError(f"Send exactly these fields: {', '.join(names)}.")


def _enum(raw: Mapping[str, Any], name: str, allowed: list[str]) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or value not in allowed:
        raise ToolInputError(f"{name} is one of {', '.join(repr(a) for a in allowed)}.")
    return value


def _whole(raw: Mapping[str, Any], name: str, low: int, high: int) -> int:
    value = raw.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolInputError(f"{name} is a whole number from {low} to {high}.")
    return value


def _day(raw: Mapping[str, Any], name: str, today: date, *, required: bool) -> date | None:
    """``today``, ``tomorrow``, or ``YYYY-MM-DD``; an empty string for none when allowed."""
    value = _text(raw, name).lower()
    if not value:
        if required:
            raise ToolInputError(f"{name} is today, tomorrow, or a date such as 2026-11-12.")
        return None
    if value == "today":
        return today
    if value == "tomorrow":
        return today + timedelta(days=1)
    if _DAY.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ToolInputError(f"{name} is today, tomorrow, or a date such as 2026-11-12.")


async def _today(ctx: ToolContext) -> date:
    household = await HouseholdRepository(ctx.db).get(ctx.actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    return household_today(household, ctx.now)


async def _call(ctx: ToolContext, raw: Mapping[str, Any], today: date) -> Any:
    slot = _enum(raw, "slot", CALL_SLOTS)
    ref = CallRef(slot=Slot(slot) if slot else None, day=_day(raw, "day", today, required=False))
    return await resolve_call(ctx.db, ctx.actor, ref, default_call_id=ctx.session.call_id, today=today)


CALL_PROPERTIES: dict[str, Any] = {
    "slot": {
        "type": "string",
        "enum": CALL_SLOTS,
        "description": "The call's meal, when the member names one. An empty string lets Dinnerbell use the "
        "conversation's call, or the only open one.",
    },
    "day": {
        "type": "string",
        "description": "The call's day when the member names one: today, tomorrow, or YYYY-MM-DD. An empty "
        "string for today with a slot, or for any day without one.",
    },
}


async def find_candidates_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    """Candidates for a voting call, in name order, with the call's basics."""
    _fields(raw, "slot", "day", "not_eaten_in_days", "cuisine", "mode")
    today = await _today(ctx)
    event = await _call(ctx, raw, today)
    mode = _enum(raw, "mode", ["", *(m.value for m in MODE_ORDER)])
    narrowing = Narrowing(
        not_eaten_in_days=_whole(raw, "not_eaten_in_days", 0, 3650),
        cuisine=_text(raw, "cuisine"),
        mode=Mode(mode) if mode else None,
    )
    found = await find_candidates(ctx.db, ctx.actor, event, narrowing, label=ctx.session.label, now=ctx.now)
    listed = [
        {
            "name": c.item.name,
            "kind": (ItemKind.HOME if Mode.COOK in c.modes else ItemKind.RESTAURANT).value,
            "modes": [m.value for m in c.modes],
            "for_this_meal": c.for_meal,
            "reasons": list(c.reasons),
        }
        for c in found.found
    ]
    return json.dumps(
        {
            "call": call_basics(found.ctx, today),
            "candidates": listed[:MAX_CANDIDATES],
            "more": len(listed) > MAX_CANDIDATES,
            "note": "Every candidate works for everyone who's in. Only these can be drafted as proposals.",
        },
        ensure_ascii=False,
    )


FIND_CANDIDATES = Tool(
    name="find_candidates",
    description=(
        "Find what the household could propose for a call that's voting: the places and home meals that "
        "pass every filter for everyone who's in, at most 25. Those for the call's meal come first, then "
        "those usually had at other meals (for_this_meal false), each in name order, with short reasons. "
        "It also gives the call's meal, day, state, meal time, and the roles someone offered. Call this "
        "before draft_proposal, and whenever the member asks what fits, what's new, or what they haven't "
        "had in a while. Before voting opens it returns an error: tell the member to ask again once voting "
        "opens. Narrow with not_eaten_in_days (0 for any), cuisine (text a cuisine contains, or an empty "
        "string), and mode (cook, pickup, delivery, or an empty string). It never says why something is "
        "missing, and you mustn't guess."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **CALL_PROPERTIES,
            "not_eaten_in_days": {
                "type": "integer",
                "description": "Only items not eaten in at least this many days. 0 for any.",
            },
            "cuisine": {
                "type": "string",
                "description": "Text a cuisine contains, such as Thai. An empty string for any.",
            },
            "mode": {
                "type": "string",
                "enum": ["", *(m.value for m in MODE_ORDER)],
                "description": "Only this way of getting it. An empty string for any.",
            },
        },
        "required": ["slot", "day", "not_eaten_in_days", "cuisine", "mode"],
        "additionalProperties": False,
    },
    run=find_candidates_tool,
)


def _keep(ctx: ToolContext, draft: Any) -> str:
    ctx.session.drafts.append(drafts.view(draft))
    return drafts.summary(draft, ctx.session.label(ctx.actor.id))


async def draft_proposal_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "slot", "day", "name", "mode")
    today = await _today(ctx)
    event = await _call(ctx, raw, today)
    mode = Mode(_enum(raw, "mode", [m.value for m in MODE_ORDER]))
    name = _text(raw, "name")
    draft = await drafts.draft_proposal(
        ctx.db, ctx.actor, ctx.session.key, event, name, mode, label=ctx.session.label, now=ctx.now
    )
    return _keep(ctx, draft)


DRAFT_PROPOSAL = Tool(
    name="draft_proposal",
    description=(
        "Draft a proposal for the member: one of a voting call's candidates, by the name and in a mode "
        "find_candidates returned. Dinnerbell shows the member the draft with Confirm and Cancel, and nothing "
        "changes until they confirm. The draft says what Confirm does, such as joining late or taking the role "
        "the mode needs. Returns the draft's ID and what Confirm will do."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **CALL_PROPERTIES,
            "name": {"type": "string", "description": "The candidate's name, exactly as find_candidates returned it."},
            "mode": {"type": "string", "enum": [m.value for m in MODE_ORDER], "description": "One of its modes."},
        },
        "required": ["slot", "day", "name", "mode"],
        "additionalProperties": False,
    },
    run=draft_proposal_tool,
)


async def draft_offer_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "name", "mode", "slot", "day", "time", "spare_servings")
    today = await _today(ctx)
    at_text = _text(raw, "time")
    at: time | None = None
    if at_text:
        if not _TIME.fullmatch(at_text):
            raise ToolInputError("time is HH:MM on a 24-hour clock, such as 18:30, or an empty string.")
        try:
            at = time.fromisoformat(at_text.zfill(5))
        except ValueError:
            raise ToolInputError("time is HH:MM on a 24-hour clock, such as 18:30, or an empty string.") from None
    spare = _whole(raw, "spare_servings", 0, 20)
    request = OfferRequest(
        name=_text(raw, "name"),
        mode=Mode(_enum(raw, "mode", [m.value for m in MODE_ORDER])),
        slot=Slot(_enum(raw, "slot", [s.value for s in Slot])),
        day=_day(raw, "day", today, required=True) or today,
        at=at,
        capacity=spare or DEFAULT_CAPACITY,
    )
    return _keep(ctx, await drafts.draft_offer(ctx.db, ctx.actor, ctx.session.key, request, now=ctx.now))


DRAFT_OFFER = Tool(
    name="draft_offer",
    description=(
        "Draft an offer for the member: they cook, pick up, or order something already in the catalog, for a "
        "meal on a day, with servings to spare. Use an item's name exactly as look_up_catalog_items returned it; "
        "something new needs draft_catalog_items first. Dinnerbell shows the member the draft with Confirm and "
        "Cancel, and nothing changes until they confirm. The draft says what Confirm does: join an open call as "
        "a pick, replace a scheduled call, keep the day's call from opening, or stand on its own."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The catalog item's name."},
            "mode": {"type": "string", "enum": [m.value for m in MODE_ORDER], "description": "How it comes."},
            "slot": {"type": "string", "enum": [s.value for s in Slot], "description": "The meal."},
            "day": {"type": "string", "description": "today, tomorrow, or YYYY-MM-DD."},
            "time": {
                "type": "string",
                "description": "When it's ready, HH:MM on a 24-hour clock. An empty string for the meal's usual time.",
            },
            "spare_servings": {
                "type": "integer",
                "description": "How many others it feeds, guests included, 1 to 20. 0 for the usual 3.",
            },
        },
        "required": ["name", "mode", "slot", "day", "time", "spare_servings"],
        "additionalProperties": False,
    },
    run=draft_offer_tool,
)


async def draft_catalog_items_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "catalog_file")
    text = raw.get("catalog_file")
    if not isinstance(text, str) or not text.strip():
        raise ToolInputError("catalog_file is the catalog file's text.")
    if len(text) > MAX_CATALOG_CHARS:
        raise ToolInputError(f"catalog_file is at most {MAX_CATALOG_CHARS:,} characters. Draft fewer items at once.")
    return _keep(ctx, await drafts.draft_catalog_items(ctx.db, ctx.actor, ctx.session.key, text, now=ctx.now))


DRAFT_CATALOG_ITEMS = Tool(
    name="draft_catalog_items",
    description=(
        "Draft new catalog items for the member, as a Dinnerbell catalog file: YAML starting with version: 1, "
        "then restaurants and home_meals lists. A restaurant has name, cuisine (a list), and optionally tags, "
        "modes (pickup, delivery), meals (breakfast, lunch, dinner), url, order_url, and notes; a home meal has "
        "name, cuisine, and optionally tags, meals, servings, url, and notes. Leave out cooks and active. "
        "Items the catalog has already are dropped and listed. Dinnerbell shows the member the draft with "
        "Confirm, which opens the import preview; nothing changes until they import it there."
    ),
    input_schema={
        "type": "object",
        "properties": {"catalog_file": {"type": "string", "description": "The catalog file's text."}},
        "required": ["catalog_file"],
        "additionalProperties": False,
    },
    run=draft_catalog_items_tool,
)


TOOLS: tuple[Tool, ...] = (LOOK_UP_CATALOG_ITEMS, FIND_CANDIDATES, DRAFT_PROPOSAL, DRAFT_OFFER, DRAFT_CATALOG_ITEMS)
"""Every tool, in the order requests list them. Later tools come in follow-up issues."""


def tools_for(private: bool) -> tuple[Tool, ...]:
    return tuple(tool for tool in TOOLS if private or not tool.private_only)


REFUSALS = (ToolRefusal, Forbidden, NotFound, EventRuleViolation, InvalidResponse, InvalidCatalogInput)
"""Refusals whose message is safe to send back: written for the person, or for the model."""


async def run_tool(
    name: str,
    raw: object,
    *,
    db: AsyncSession,
    member: Member,
    private: bool,
    now: datetime,
    session: ToolSession | None = None,
) -> ToolResult:
    """Run the tool ``name`` for the model, as ``tool_actor(member)``. A tool this conversation
    isn't offered, bad input, and a refusal from a service come back as error results for the
    model to read, never as exceptions. ``session`` is the message's; a lone call gets one for
    the member's web conversation."""
    tool = next((t for t in tools_for(private) if t.name == name), None)
    if tool is None:
        return ToolResult(f"There's no tool named {name[:40]!r} here.", is_error=True)
    if not isinstance(raw, Mapping):
        return ToolResult("Send the tool's input as an object.", is_error=True)
    shared = session or ToolSession(key=web_key(member.id), pseudonyms=Pseudonyms().with_member(member.id))
    ctx = ToolContext(db=db, actor=tool_actor(member), private=private, now=now, session=shared)
    try:
        return ToolResult(await tool.run(ctx, raw))
    except REFUSALS as exc:
        return ToolResult(str(exc) or "That isn't allowed.", is_error=True)
    except Exception:
        log.exception("assistant tool failed", extra={"tool": tool.name, "member_id": member.id})
        return ToolResult("That didn't work. Don't try it again.", is_error=True)
