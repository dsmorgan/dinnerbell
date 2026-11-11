# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's tools and the one wrapper that runs them (spec 6.14, **Scope**).

Every tool runs as ``tool_actor(member)`` (``acting``): the member it's answering, with a
member's role even when they're an admin, so no admin-only branch in a service ever passes for
the assistant. Rules that match on the member's ID still apply. Tools call the same services as
the forms, and read household settings directly, never through an admin-only use case.

A tool returns text for the model: data, never instructions (the system prompt says so). A
tool marked ``private_only`` is offered only in a private conversation (the web page, a DM).
The first build's tools work in any conversation: look up catalog items, find candidates, and
draft a proposal, an offer, or catalog items, from a catalog file or from a menu link
(``menu_links``). So does looking up the meal log (``meal_history``, #264). The call actions
(#251, ``call_drafts``) do too: look up a call, and draft an answer, a vote, a suggestion, a
role, taking back an offer, inviting a guest or answering for one, or starting a call. The
member's own data (#253, ``own_drafts``) is private only: look up their levels on Preferences,
away days, and feedback, and draft a level, feedback on a meal they ate, or away days. A draft
changes nothing until its member confirms it (``drafts.confirm_draft``).
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

from dinnerbell.config import is_test_instance
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.assistant import Pseudonyms, web_key
from dinnerbell.domain.assistant_drafts import Narrowing
from dinnerbell.domain.catalog import MODE_ORDER, InvalidCatalogInput, ItemKind, Meal, Mode, name_key
from dinnerbell.domain.events import EventRuleViolation, EventState, InvalidResponse, RoleOffer, Status
from dinnerbell.domain.feedback import COOK_NOTE_MAX, ISSUE_NOTE_MAX, FeedbackInput, InvalidFeedback, PlaceIssue, Rating
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import InvalidMemberInput, Member
from dinnerbell.domain.offers import DEFAULT_CAPACITY
from dinnerbell.domain.preferences import LEVELS, InvalidPreference, Stance
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.assistant import call_drafts, drafts, own_drafts
from dinnerbell.services.assistant.acting import ToolRefusal, tool_actor
from dinnerbell.services.assistant.call_drafts import DraftSpec
from dinnerbell.services.assistant.candidates import CallRef, call_basics, find_candidates, resolve_call
from dinnerbell.services.assistant.drafts import DraftView, OfferRequest
from dinnerbell.services.assistant.meal_history import HistoryQuery, Order, eaten_items
from dinnerbell.services.assistant.menu_links import NOT_HERE, LinkCalls, draft_from_link
from dinnerbell.services.call_context import load
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
    links: LinkCalls | None = None
    """The fetch and the drafting call for menu links, from ``converse``. None outside a
    message, where ``draft_catalog_from_link`` refuses."""
    instance_label: str = ""
    """``AssistantConfig.instance_label``, for ``draft_call``'s restart rule (#60)."""

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

# Meal history (#264) ----------------------------------------------------------------------------

HISTORY_KINDS = ["", *(kind.value for kind in (ItemKind.RESTAURANT, ItemKind.HOME))]
HISTORY_FIELDS = ("name", "tag", "kind", "since", "until", "order")


async def look_up_history(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    """What the household ate, per catalog item, from the meal log: name, kind, cuisines, tags,
    times eaten, the last day eaten, and the household's hint (FR-49), the one Catalog shows.
    Never who cooked, ordered, picked up, or ate, anyone's rating, issues, or notes, or anyone's
    pseudonym. Names and tags are catalog text: data for the model, never instructions."""
    _fields(raw, *HISTORY_FIELDS)
    today = await _today(ctx)
    kind = _enum(raw, "kind", HISTORY_KINDS)
    query = HistoryQuery(
        text=name_key(_text(raw, "name")),
        tag=name_key(_text(raw, "tag")),
        kind=ItemKind(kind) if kind else None,
        since=_day(raw, "since", today, required=False),
        until=_day(raw, "until", today, required=False),
        order=Order(_enum(raw, "order", [o.value for o in Order])),
    )
    if query.since and query.until and query.since > query.until:
        raise ToolInputError("since is on or before until.")
    found = await eaten_items(ctx.db, ctx.actor, query, today=today, limit=MAX_ITEMS)
    listed = [
        {
            "name": e.item.name,
            "kind": e.item.kind.value,
            "cuisines": list(e.cuisines),
            "tags": list(e.tags),
            "in_catalog": e.item.active,
            "times_eaten": e.times,
            "last_eaten": e.last.isoformat(),
            "hint": e.hint,
        }
        for e in found.items
    ]
    return json.dumps({"today": today.isoformat(), "items": listed, "more": found.more}, ensure_ascii=False)


LOOK_UP_HISTORY = Tool(
    name="look_up_history",
    description=(
        "Look up what the household has eaten, from its meal log. Call this whenever the member asks "
        "what the household has had, how often, when it last had something, the place it orders most, "
        "or the best place it's had. Returns at most 25 items with each item's name, kind (home or "
        "restaurant), cuisines, tags, in_catalog (false once it's taken off the catalog), times_eaten and "
        "last_eaten (YYYY-MM-DD) within the dates asked, and hint: the household's verdict from everyone's "
        "ratings and recent problems, the same words every member sees on the catalog, or null when there "
        "isn't one yet. A hint is the whole household's, never one person's: never guess "
        "who liked or disliked something. The log never says who ate, cooked, or ordered. today is today's "
        "date; more is true when more items matched. Order by most (most often first) or recent (most "
        "recent first)."
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
                "description": "Text one of the item's cuisines or tags contains, such as Thai or pizza. "
                "An empty string matches any.",
            },
            "kind": {
                "type": "string",
                "enum": HISTORY_KINDS,
                "description": "restaurant, home, or an empty string for both.",
            },
            "since": {
                "type": "string",
                "description": "Count only meals on or after this day: today, tomorrow, or YYYY-MM-DD. "
                "An empty string for no start.",
            },
            "until": {
                "type": "string",
                "description": "Count only meals on or before this day: today, tomorrow, or YYYY-MM-DD. "
                "An empty string for no end.",
            },
            "order": {
                "type": "string",
                "enum": [o.value for o in Order],
                "description": "most for the most often eaten first, recent for the most recently eaten first.",
            },
        },
        "required": list(HISTORY_FIELDS),
        "additionalProperties": False,
    },
    run=look_up_history,
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
    """Candidates for a voting call, the call's meal first and then other meals, each in name
    order, with the call's basics."""
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


MAX_LINK_CHARS = 2_048
LINK_KINDS = ["", *(kind.value for kind in (ItemKind.RESTAURANT, ItemKind.HOME))]


async def draft_catalog_from_link_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "url", "kind")
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ToolInputError("url is the link the member gave, starting with https://.")
    if len(url) > MAX_LINK_CHARS:
        raise ToolInputError(f"url is at most {MAX_LINK_CHARS:,} characters.")
    kind = _enum(raw, "kind", LINK_KINDS)
    if ctx.session.links is None:
        raise ToolRefusal(NOT_HERE)
    drafted = await draft_from_link(
        ctx.db,
        ctx.actor,
        ctx.session.key,
        ctx.session.links,
        url.strip(),
        ItemKind(kind) if kind else None,
        label=ctx.session.label(ctx.actor.id),
        now=ctx.now,
    )
    ctx.session.drafts.append(drafted.view)
    return drafted.summary


DRAFT_CATALOG_FROM_LINK = Tool(
    name="draft_catalog_from_link",
    description=(
        "Draft new catalog items from a web page the member links to, such as a restaurant's menu or a recipe. "
        "Call this when the member gives a link and wants what's on it in the catalog. Dinnerbell reads the page "
        "itself and drafts the items in a separate step; you never see the page. Items the catalog has already "
        "are dropped and listed. Returns how many items the draft adds, the ones left out, and the draft's ID, "
        "or why the page couldn't be read. Dinnerbell shows the member the draft's items with Confirm, which "
        "opens the import preview; nothing changes until they import it there."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The link exactly as the member wrote it, starting with https://.",
            },
            "kind": {
                "type": "string",
                "enum": LINK_KINDS,
                "description": "restaurant for a place to eat, home for a recipe, or an empty string when unsure.",
            },
        },
        "required": ["url", "kind"],
        "additionalProperties": False,
    },
    run=draft_catalog_from_link_tool,
)


# Call actions (#251) -----------------------------------------------------------------------------


def _flag(raw: Mapping[str, Any], name: str) -> bool:
    value = raw.get(name)
    if not isinstance(value, bool):
        raise ToolInputError(f"{name} is true or false.")
    return value


ANSWER_WORDS = {"in": Status.IN, "out": Status.OUT, "maybe": Status.MAYBE}
ROLE_NAMES = {"cook": RoleOffer.COOK, "pickup": RoleOffer.PICKUP, "order": RoleOffer.ORDER}


async def look_up_call_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    """A call's basics, the member's own answer, and, once voting opens, its picks with whether
    the member votes for each. Nobody else's answer, vote, or name, and no tallies."""
    _fields(raw, "slot", "day")
    today = await _today(ctx)
    loaded = await load(ctx.db, await _call(ctx, raw, today))
    mine = loaded.response_of(ctx.actor.id)
    suggested = [
        loaded.items[s.item_id].name
        for s in loaded.suggestions
        if s.member_id == ctx.actor.id and s.item_id is not None and s.item_id in loaded.items
    ]
    you = {
        "answer": mine.status.value if mine is not None else "none",
        "late": bool(mine and mine.late),
        "roles": [name for name, role in ROLE_NAMES.items() if mine is not None and role in mine.offers],
        "suggestions": suggested,
    }
    found: dict[str, Any] = {"call": call_basics(loaded, today), "you": you}
    if loaded.event.state not in {EventState.SCHEDULED, EventState.COLLECTING}:
        found["picks"] = [
            {
                "pick": p.id,
                "name": call_drafts.pick_name(loaded, p),
                "mode": p.mode.value,
                "you_voted": ctx.actor.id in loaded.votes.get(p.id, []),
            }
            for p in loaded.picks
        ]
    return json.dumps(found, ensure_ascii=False)


LOOK_UP_CALL = Tool(
    name="look_up_call",
    description=(
        "Look up a call: its meal, day, state, meal time, and the roles someone offered; the member's own answer "
        "(in, out, maybe, or none), whether they joined late, the roles they offered, and their suggestions; and, "
        "once voting opens, its picks, each with a number for draft_vote, a name (a pick a member typed reads as "
        "a member's own idea), a mode, and whether the member votes for each. Call this before "
        "draft_vote, and whenever the member asks where they stand. It never says how anyone else answered or voted."
    ),
    input_schema={
        "type": "object",
        "properties": dict(CALL_PROPERTIES),
        "required": ["slot", "day"],
        "additionalProperties": False,
    },
    run=look_up_call_tool,
)


async def _keep_action(ctx: ToolContext, spec: DraftSpec) -> str:
    return _keep(ctx, await drafts.store_action(ctx.db, ctx.actor, ctx.session.key, spec, now=ctx.now))


async def draft_answer_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "slot", "day", "answer")
    today = await _today(ctx)
    event = await _call(ctx, raw, today)
    status = ANSWER_WORDS[_enum(raw, "answer", list(ANSWER_WORDS))]
    return await _keep_action(ctx, await call_drafts.answer(ctx.db, ctx.actor, event, status, today=today, now=ctx.now))


DRAFT_ANSWER = Tool(
    name="draft_answer",
    description=(
        "Draft the member's answer to a call: in, out, or maybe, while answers are open. While the call is voting, "
        "in means joining late; out and maybe aren't possible then. Dinnerbell shows the member the draft with "
        "Confirm and Cancel, and nothing changes until they confirm. Returns the draft's ID and what Confirm does."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **CALL_PROPERTIES,
            "answer": {"type": "string", "enum": list(ANSWER_WORDS), "description": "The member's answer."},
        },
        "required": ["slot", "day", "answer"],
        "additionalProperties": False,
    },
    run=draft_answer_tool,
)


async def draft_vote_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "slot", "day", "pick", "name", "mode", "vote")
    today = await _today(ctx)
    event = await _call(ctx, raw, today)
    mode = _enum(raw, "mode", ["", *(m.value for m in MODE_ORDER)])
    spec = await call_drafts.vote(
        ctx.db,
        ctx.actor,
        event,
        _text(raw, "name"),
        Mode(mode) if mode else None,
        handle=_whole(raw, "pick", 0, 2**62),
        voting=_flag(raw, "vote"),
        today=today,
        now=ctx.now,
    )
    return await _keep_action(ctx, spec)


DRAFT_VOTE = Tool(
    name="draft_vote",
    description=(
        "Draft the member's vote for one of a voting call's picks, or taking their vote back. Send the pick's "
        "number from look_up_call, or 0 with a name and mode the member gave. Someone who isn't in joins late "
        "first; the draft says so. "
        "Dinnerbell shows the member the draft with Confirm and Cancel, and nothing changes until they confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **CALL_PROPERTIES,
            "pick": {"type": "integer", "description": "The pick's number from look_up_call, or 0 to use name."},
            "name": {"type": "string", "description": "With pick 0, the pick's name. An empty string otherwise."},
            "mode": {
                "type": "string",
                "enum": ["", *(m.value for m in MODE_ORDER)],
                "description": "The pick's mode, or an empty string when only one pick has that name.",
            },
            "vote": {"type": "boolean", "description": "true to vote for it, false to take the vote back."},
        },
        "required": ["slot", "day", "pick", "name", "mode", "vote"],
        "additionalProperties": False,
    },
    run=draft_vote_tool,
)


async def draft_suggestion_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "slot", "day", "name")
    today = await _today(ctx)
    event = await _call(ctx, raw, today)
    spec = await call_drafts.suggestion(ctx.db, ctx.actor, event, _text(raw, "name"), today=today, now=ctx.now)
    return await _keep_action(ctx, spec)


DRAFT_SUGGESTION = Tool(
    name="draft_suggestion",
    description=(
        "Draft a suggestion for a call while answers are open: a catalog item the member would like, by its name "
        "exactly as look_up_catalog_items returned it. A member suggests up to 3. Dinnerbell shows the member the "
        "draft with Confirm and Cancel, and nothing changes until they confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {**CALL_PROPERTIES, "name": {"type": "string", "description": "The catalog item's name."}},
        "required": ["slot", "day", "name"],
        "additionalProperties": False,
    },
    run=draft_suggestion_tool,
)


async def draft_role_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "slot", "day", "role", "offer")
    today = await _today(ctx)
    event = await _call(ctx, raw, today)
    chosen = ROLE_NAMES[_enum(raw, "role", list(ROLE_NAMES))]
    spec = await call_drafts.role(
        ctx.db, ctx.actor, event, chosen, offered=_flag(raw, "offer"), today=today, now=ctx.now
    )
    return await _keep_action(ctx, spec)


DRAFT_ROLE = Tool(
    name="draft_role",
    description=(
        'Draft the member taking a role on a call, such as "put me down for pickup", or giving one up: cook, '
        "pickup, or order. Taking one counts them in. While the call is voting, someone who isn't in joins late "
        "first; the draft says so. Dinnerbell shows the member the draft with Confirm and Cancel, and nothing "
        "changes until they confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **CALL_PROPERTIES,
            "role": {"type": "string", "enum": list(ROLE_NAMES), "description": "The role."},
            "offer": {"type": "boolean", "description": "true to take the role, false to give it up."},
        },
        "required": ["slot", "day", "role", "offer"],
        "additionalProperties": False,
    },
    run=draft_role_tool,
)


async def draft_take_back_offer_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "slot", "day")
    today = await _today(ctx)
    slot = _enum(raw, "slot", CALL_SLOTS)
    offer = await call_drafts.own_offer(
        ctx.db, ctx.actor, Slot(slot) if slot else None, _day(raw, "day", today, required=False), today=today
    )
    return await _keep_action(ctx, await call_drafts.offer_off(ctx.db, ctx.actor, offer, today=today))


DRAFT_TAKE_BACK_OFFER = Tool(
    name="draft_take_back_offer",
    description=(
        "Draft taking back an offer the member made, which calls it off. Only the member's own offers. With "
        "several open, it says which and you ask. Dinnerbell shows the member the draft with Confirm and Cancel, "
        "and nothing changes until they confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "slot": {
                "type": "string",
                "enum": CALL_SLOTS,
                "description": "The offer's meal, or an empty string for the member's only open offer.",
            },
            "day": {
                "type": "string",
                "description": "The offer's day: today, tomorrow, or YYYY-MM-DD. An empty string for today with a "
                "slot, or for any day without one.",
            },
        },
        "required": ["slot", "day"],
        "additionalProperties": False,
    },
    run=draft_take_back_offer_tool,
)


MAX_NEEDS = 5
GUEST_ANSWERS = {"": None, "in": Status.IN, "out": Status.OUT}
GUEST_ROLES = {"": None, **ROLE_NAMES}


def _names(raw: Mapping[str, Any], name: str) -> list[str]:
    value = raw.get(name)
    if not isinstance(value, list) or len(value) > MAX_NEEDS or not all(isinstance(v, str) for v in value):
        raise ToolInputError(f"{name} is a list of at most {MAX_NEEDS} strings.")
    if any(len(v) > MAX_QUERY_LENGTH for v in value):
        raise ToolInputError(f"Each of {name} is at most {MAX_QUERY_LENGTH} characters.")
    return [v.strip() for v in value if v.strip()]


async def draft_invite_guest_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "slot", "day", "name", "needs")
    today = await _today(ctx)
    event = await _call(ctx, raw, today)
    name = _text(raw, "name")
    if not name:
        raise ToolInputError("name is the guest's name, as the member wrote it.")
    spec = await call_drafts.invite_guest(ctx.db, ctx.actor, event, name, _names(raw, "needs"), today=today)
    return await _keep_action(ctx, spec)


DRAFT_INVITE_GUEST = Tool(
    name="draft_invite_guest",
    description=(
        "Draft inviting a guest to a call while answers are open: the household's saved guest with that exact "
        "name, or a new guest. Use the name exactly as the member wrote it; never make one up. A new guest can "
        "have dietary needs the member named, from the household's dietary tags. A guest without Slack is in at "
        "once, and the member answers for them. Dinnerbell shows the member the draft with Confirm and Cancel, "
        "and nothing changes until they confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **CALL_PROPERTIES,
            "name": {"type": "string", "description": "The guest's name, as the member wrote it."},
            "needs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "A new guest's dietary needs the member named, such as vegetarian. Empty for none.",
            },
        },
        "required": ["slot", "day", "name", "needs"],
        "additionalProperties": False,
    },
    run=draft_invite_guest_tool,
)


async def draft_guest_answer_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "slot", "day", "name", "answer", "role", "offer")
    today = await _today(ctx)
    event = await _call(ctx, raw, today)
    status = GUEST_ANSWERS[_enum(raw, "answer", list(GUEST_ANSWERS))]
    chosen = GUEST_ROLES[_enum(raw, "role", list(GUEST_ROLES))]
    if (status is None) == (chosen is None):
        raise ToolInputError("Send an answer or a role, not both: the other is an empty string.")
    spec = await call_drafts.guest_answer(
        ctx.db,
        ctx.actor,
        event,
        _text(raw, "name"),
        status=status,
        offered_role=chosen,
        offered=_flag(raw, "offer"),
        today=today,
        now=ctx.now,
    )
    return await _keep_action(ctx, spec)


DRAFT_GUEST_ANSWER = Tool(
    name="draft_guest_answer",
    description=(
        "Draft an answer for a guest the member invited who isn't on Slack: in or out, or a role (cook, pickup, "
        "or order) taken or given up for them. Use the guest's name as the member wrote it. Only the member who "
        "invited a guest answers for them. Dinnerbell shows the member the draft with Confirm and Cancel, and "
        "nothing changes until they confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **CALL_PROPERTIES,
            "name": {"type": "string", "description": "The guest's name, as the member wrote it."},
            "answer": {
                "type": "string",
                "enum": list(GUEST_ANSWERS),
                "description": "in or out, or an empty string when this is a role.",
            },
            "role": {
                "type": "string",
                "enum": list(GUEST_ROLES),
                "description": "A role for the guest, or an empty string when this is an answer.",
            },
            "offer": {"type": "boolean", "description": "For a role: true to take it, false to give it up."},
        },
        "required": ["slot", "day", "name", "answer", "role", "offer"],
        "additionalProperties": False,
    },
    run=draft_guest_answer_tool,
)


async def draft_call_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _fields(raw, "slot", "day")
    today = await _today(ctx)
    slot = Slot(_enum(raw, "slot", [s.value for s in Slot]))
    day = _day(raw, "day", today, required=True) or today
    any_time = is_test_instance(ctx.session.instance_label)
    spec = await call_drafts.start_call(ctx.db, ctx.actor, slot, day, today=today, now=ctx.now, any_time=any_time)
    return await _keep_action(ctx, spec)


DRAFT_CALL = Tool(
    name="draft_call",
    description=(
        "Draft starting a call for a meal on a day, today or up to a week ahead, when the member wants one that "
        "isn't scheduled, or starting a called-off one again. Today's opens at once; a later day's opens at the "
        "usual time. Dinnerbell shows the member the draft with Confirm and Cancel, and nothing changes until "
        "they confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "slot": {"type": "string", "enum": [s.value for s in Slot], "description": "The meal."},
            "day": {"type": "string", "description": "today, tomorrow, or YYYY-MM-DD."},
        },
        "required": ["slot", "day"],
        "additionalProperties": False,
    },
    run=draft_call_tool,
)


# The member's own data, in private only (#253) --------------------------------------------------

PRIVATE_ONLY = (
    "That works only in a private conversation: a direct message with Dinnerbell, or the Assistant page on the "
    "web. Tell the member to ask there."
)
"""``run_tool`` offers a private-only tool nowhere else, so the model never sees this; it's the
tool's own check, in case it's ever reached another way."""
LEVEL_NAMES = list(LEVELS)
RATING_NAMES = ["", *(r.value for r in Rating)]
ISSUE_NAMES = [i.value for i in PlaceIssue]
OWN = "The member's own only, and only when they ask. "
CONFIRMS = " Nothing changes until the member confirms the draft."


def _private(ctx: ToolContext) -> None:
    if not ctx.private:
        raise ToolRefusal(PRIVATE_ONLY)


def _note(raw: Mapping[str, Any], name: str, most: int) -> str:
    """A note the member wrote, or empty. As the feedback form takes up to twice ``most``, the
    service refuses a longer note once it's trimmed, in its own words."""
    value = raw.get(name)
    if not isinstance(value, str):
        raise ToolInputError(f"{name} must be a string; send an empty string for none.")
    if len(value) > most * 2:
        raise ToolInputError(f"{name} is at most {most} characters.")
    return value


async def look_up_my_preferences_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _private(ctx)
    _fields(raw, "name")
    found = await own_drafts.my_levels(ctx.db, ctx.actor, _text(raw, "name"), today=await _today(ctx))
    return json.dumps(found, ensure_ascii=False)


LOOK_UP_MY_PREFERENCES = Tool(
    name="look_up_my_preferences",
    description=(
        OWN + "Look up the member's levels on Preferences (veto, or -2 to 2; 0 is no opinion) for tags and catalog "
        "items, and their away days (first, last, back: YYYY-MM-DD, or null). With a name, every tag and item whose "
        "name contains it; with an empty string, only those they set. from_your_ratings: what their own ratings did "
        "to their score, or null."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Text the name contains, or an empty string.",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    run=look_up_my_preferences_tool,
    private_only=True,
)


async def draft_preference_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _private(ctx)
    _fields(raw, "what", "name", "level")
    what = _enum(raw, "what", list(own_drafts.WHAT))
    name = _text(raw, "name")
    level = Stance.parse(_enum(raw, "level", LEVEL_NAMES))
    spec = await own_drafts.stance_draft(ctx.db, ctx.actor, what, name, level, today=await _today(ctx))
    return await _keep_action(ctx, spec)


DRAFT_PREFERENCE = Tool(
    name="draft_preference",
    description=(
        OWN + 'Draft one of the member\'s levels on Preferences, such as "veto mushrooms": on a tag or catalog item, '
        "by its name as look_up_my_preferences or look_up_catalog_items returned it." + CONFIRMS
    ),
    input_schema={
        "type": "object",
        "properties": {
            "what": {
                "type": "string",
                "enum": list(own_drafts.WHAT),
                "description": "An empty string for whichever has that name.",
            },
            "name": {"type": "string", "description": "The name."},
            "level": {"type": "string", "enum": LEVEL_NAMES, "description": "-2 dislike to 2 love."},
        },
        "required": ["what", "name", "level"],
        "additionalProperties": False,
    },
    run=draft_preference_tool,
    private_only=True,
)


async def look_up_my_feedback_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _private(ctx)
    _fields(raw, "name")
    return json.dumps(await own_drafts.my_meals(ctx.db, ctx.actor, _text(raw, "name"), now=ctx.now), ensure_ascii=False)


LOOK_UP_MY_FEEDBACK = Tool(
    name="look_up_my_feedback",
    description=(
        OWN + "Look up meals the member ate that still take their feedback (open 7 days), newest first, each with "
        "a meal number for draft_feedback, and yours: their feedback so far, or null. cook_to_thank is true for a "
        "home meal someone else cooked. Call it before draft_feedback."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Text the name contains, or an empty string.",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    run=look_up_my_feedback_tool,
    private_only=True,
)


async def draft_feedback_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _private(ctx)
    _fields(raw, "meal", "rating", "went_wrong", "what_went_wrong", "thank_cook", "cook_note")
    rating = _enum(raw, "rating", RATING_NAMES)
    wrong = raw.get("went_wrong")
    if not isinstance(wrong, list) or not all(isinstance(w, str) and w in ISSUE_NAMES for w in wrong):
        raise ToolInputError(f"went_wrong is a list of {', '.join(ISSUE_NAMES)}; empty for none.")
    data = FeedbackInput(
        rating=Rating(rating) if rating else None,
        place_issues=frozenset(PlaceIssue(w) for w in wrong),
        issue_note=_note(raw, "what_went_wrong", ISSUE_NOTE_MAX) or None,
        cook_thanks=_flag(raw, "thank_cook"),
        cook_note=_note(raw, "cook_note", COOK_NOTE_MAX) or None,
    )
    meal = _whole(raw, "meal", 1, 2**62)
    return await _keep_action(ctx, await own_drafts.feedback_draft(ctx.db, ctx.actor, meal, data, now=ctx.now))


DRAFT_FEEDBACK = Tool(
    name="draft_feedback",
    description=(
        OWN + "Draft the member's feedback on a meal from look_up_my_feedback, every part optional. It replaces "
        "theirs, so keep what they didn't change from yours; all empty takes it back." + CONFIRMS
    ),
    input_schema={
        "type": "object",
        "properties": {
            "meal": {"type": "integer", "description": "The meal's number from look_up_my_feedback."},
            "rating": {
                "type": "string",
                "enum": RATING_NAMES,
                "description": "An empty string for none.",
            },
            "went_wrong": {
                "type": "array",
                "items": {"type": "string", "enum": ISSUE_NAMES},
                "description": "A restaurant's problems.",
            },
            "what_went_wrong": {
                "type": "string",
                "description": f"For other, the member's words, at most {ISSUE_NOTE_MAX} characters, or empty.",
            },
            "thank_cook": {"type": "boolean", "description": "Only with cook_to_thank."},
            "cook_note": {
                "type": "string",
                "description": f"With thank_cook, the member's words, at most {COOK_NOTE_MAX} characters, or empty.",
            },
        },
        "required": ["meal", "rating", "went_wrong", "what_went_wrong", "thank_cook", "cook_note"],
        "additionalProperties": False,
    },
    run=draft_feedback_tool,
    private_only=True,
)


async def draft_away_tool(ctx: ToolContext, raw: Mapping[str, Any]) -> str:
    _private(ctx)
    _fields(raw, "first", "last", "clear")
    today = await _today(ctx)
    clear = _flag(raw, "clear")
    first = _day(raw, "first", today, required=not clear)
    last = _day(raw, "last", today, required=not clear)
    if clear and (first or last):
        raise ToolInputError("To clear the away days, send empty strings for first and last.")
    spec = await own_drafts.away_draft(ctx.db, ctx.actor, first, last, clear=clear, today=today)
    return await _keep_action(ctx, spec)


DRAFT_AWAY = Tool(
    name="draft_away",
    description=(
        OWN + "Draft the member's away days, first and last day included, replacing theirs; calls then answer them "
        'Out. Or clear them, as "I\'m back" does.' + CONFIRMS
    ),
    input_schema={
        "type": "object",
        "properties": {
            "first": {
                "type": "string",
                "description": "today, tomorrow, or YYYY-MM-DD; empty to clear.",
            },
            "last": {
                "type": "string",
                "description": "today, tomorrow, or YYYY-MM-DD; empty to clear.",
            },
            "clear": {"type": "boolean", "description": "true to clear them."},
        },
        "required": ["first", "last", "clear"],
        "additionalProperties": False,
    },
    run=draft_away_tool,
    private_only=True,
)


TOOLS: tuple[Tool, ...] = (
    LOOK_UP_CATALOG_ITEMS,
    LOOK_UP_HISTORY,
    FIND_CANDIDATES,
    DRAFT_PROPOSAL,
    DRAFT_OFFER,
    DRAFT_CATALOG_ITEMS,
    DRAFT_CATALOG_FROM_LINK,
    LOOK_UP_CALL,
    DRAFT_ANSWER,
    DRAFT_VOTE,
    DRAFT_SUGGESTION,
    DRAFT_ROLE,
    DRAFT_TAKE_BACK_OFFER,
    DRAFT_INVITE_GUEST,
    DRAFT_GUEST_ANSWER,
    DRAFT_CALL,
    LOOK_UP_MY_PREFERENCES,
    DRAFT_PREFERENCE,
    LOOK_UP_MY_FEEDBACK,
    DRAFT_FEEDBACK,
    DRAFT_AWAY,
)
"""Every tool, in the order requests list them. Later tools come in follow-up issues."""


def tools_for(private: bool) -> tuple[Tool, ...]:
    return tuple(tool for tool in TOOLS if private or not tool.private_only)


REFUSALS = (
    ToolRefusal,
    Forbidden,
    NotFound,
    EventRuleViolation,
    InvalidResponse,
    InvalidCatalogInput,
    InvalidMemberInput,
    InvalidFeedback,
    InvalidPreference,
)
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
