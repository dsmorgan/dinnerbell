# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's tools and the one wrapper that runs them (spec 6.14, **Scope**).

Every tool runs as ``tool_actor(member)``: the member it's answering, with a member's role even
when they're an admin, so no admin-only branch in a service ever passes for the assistant.
Rules that match on the member's ID still apply. Tools call the same services as the forms,
and read household settings directly, never through an admin-only use case.

A tool returns text for the model: data, never instructions (the system prompt says so). A
tool marked ``private_only`` is offered only in a private conversation (the web page, a DM).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.domain.catalog import MODE_ORDER, Meal, name_key
from dinnerbell.domain.members import Member, Role
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.errors import Forbidden, NotFound

log = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 80
MAX_ITEMS = 25
"""The most items one lookup returns, so a broad query can't fill the request."""


def tool_actor(member: Member) -> Member:
    """The actor every tool runs as: ``member`` with a member's role, never an admin's."""
    return replace(member, role=Role.MEMBER)


@dataclass(frozen=True, slots=True)
class ToolContext:
    db: AsyncSession
    actor: Member
    """Always ``tool_actor`` of the member being answered."""
    private: bool
    now: datetime


class ToolInputError(ValueError):
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

TOOLS: tuple[Tool, ...] = (LOOK_UP_CATALOG_ITEMS,)
"""Every tool, in the order requests list them. Drafts and candidates come in later issues."""


def tools_for(private: bool) -> tuple[Tool, ...]:
    return tuple(tool for tool in TOOLS if private or not tool.private_only)


async def run_tool(
    name: str, raw: object, *, db: AsyncSession, member: Member, private: bool, now: datetime
) -> ToolResult:
    """Run the tool ``name`` for the model, as ``tool_actor(member)``. A tool this conversation
    isn't offered, bad input, and a refusal from a service come back as error results for the
    model to read, never as exceptions."""
    tool = next((t for t in tools_for(private) if t.name == name), None)
    if tool is None:
        return ToolResult(f"There's no tool named {name[:40]!r} here.", is_error=True)
    if not isinstance(raw, Mapping):
        return ToolResult("Send the tool's input as an object.", is_error=True)
    ctx = ToolContext(db=db, actor=tool_actor(member), private=private, now=now)
    try:
        return ToolResult(await tool.run(ctx, raw))
    except ToolInputError as exc:
        return ToolResult(str(exc), is_error=True)
    except (Forbidden, NotFound) as exc:
        return ToolResult(str(exc) or "That isn't allowed.", is_error=True)
    except Exception:
        log.exception("assistant tool failed", extra={"tool": tool.name, "member_id": member.id})
        return ToolResult("That didn't work. Don't try it again.", is_error=True)
