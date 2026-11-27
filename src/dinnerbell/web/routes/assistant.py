# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Assistant (ADR 0009): the switch, the monthly cap, Conversation hours, Memory and its
rollup model (#367, ADR 0012), **Clear memory**, and this month's usage. The tab and the page
exist only while the operator has set ``DINNERBELL_ANTHROPIC_API_KEY``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData
from starlette.responses import Response

from dinnerbell.domain.assistant import (
    MAX_CAP_DOLLARS,
    MAX_CONVERSATION_HOURS,
    MIN_CAP_DOLLARS,
    MIN_CONVERSATION_HOURS,
    MODELS,
    AssistantSettings,
    InvalidAssistantSettings,
    format_dollars,
)
from dinnerbell.services.assistant import admin as assistant_admin
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.errors import NotFound
from dinnerbell.web.auth import AdminCSRFChecked, AdminPrincipal, Context, Database
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import BadFormValue, checked, optional_int
from dinnerbell.web.redirects import local_redirect
from dinnerbell.web.render import render

router = APIRouter(prefix="/admin")

MAX_FIELD = 40
NOTICES = {
    "saved": "Assistant settings saved.",
    "cleared": "Memory cleared.",
}
CHOOSE = "Choose whose memory to clear."
EVERYONE = "all"
"""**Clear memory**'s choice for every member's facts."""


def _config(context: AppContext) -> AssistantConfig:
    config = AssistantConfig.from_settings(context.settings)
    if not config.configured:
        raise NotFound("That page doesn't exist.")
    return config


def _field(form: FormData, name: str) -> str | None:
    value = form.get(name)
    return value[:MAX_FIELD] if isinstance(value, str) else None


def _whole(value: str | None, label: str) -> int:
    number = optional_int(value, f"{label} must be a whole number.")
    if number is None:
        raise BadFormValue(f"Enter {label.lower()}.")
    return number


def _values(settings: AssistantSettings) -> dict[str, Any]:
    return {
        "enabled": settings.enabled,
        "cap_dollars": str(settings.cap_dollars),
        "conversation_hours": str(settings.conversation_hours),
        "memory": settings.memory,
        "rollup_model": settings.rollup_model,
    }


def _dollars_down(micros: int) -> str:
    """Micro-dollars as dollars to the cent, rounded down: what's left never shows as more than it is."""
    cents = micros // 10_000
    return f"${cents // 100}.{cents % 100:02d}"


async def _page(
    request: Request,
    context: AppContext,
    db: Database,
    principal: Any,
    *,
    values: dict[str, Any] | None = None,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    view = await assistant_admin.assistant_admin(db, _config(context), principal.member, now=context.clock())
    page = {
        "values": values or _values(view.settings),
        "model": context.settings.assistant_model,
        "month": view.month,
        "spent": format_dollars(view.spent_micros),
        "cap": f"${view.settings.cap_dollars}",
        "by_member": [(usage.name, format_dollars(usage.micros)) for usage in view.by_member],
        "cap_below_one_call": view.cap_below_one_call,
        "cap_below_any_call": view.cap_below_any_call,
        "left_below_one_call": view.left_below_one_call,
        "left_below_any_call": view.left_below_any_call,
        "left": _dollars_down(view.left_micros),
        "least_call": format_dollars(view.least_call_micros),
        "least_channel_call": format_dollars(view.least_channel_call_micros),
        "least_cap": f"${-(-view.least_call_micros // 1_000_000)}",
        "memory": [(m.member_id, m.name, m.facts) for m in view.memory],
        "facts": view.facts,
        "models": list(MODELS),
        "everyone": EVERYONE,
        "limits": {
            "cap": (MIN_CAP_DOLLARS, MAX_CAP_DOLLARS),
            "hours": (MIN_CONVERSATION_HOURS, MAX_CONVERSATION_HOURS),
        },
        "error": error,
        "notice": notice,
    }
    return await render(request, "admin_assistant.html", page, status_code=status_code)


@router.get("/assistant")
async def assistant_page(
    request: Request, principal: AdminPrincipal, context: Context, db: Database, notice: str = ""
) -> Response:
    return await _page(request, context, db, principal, notice=NOTICES.get(notice))


@router.post("/assistant")
async def save_assistant(request: Request, principal: AdminCSRFChecked, context: Context, db: Database) -> Response:
    config = _config(context)
    form = await request.form()
    enabled, memory = checked(_field(form, "enabled")), checked(_field(form, "memory"))
    cap, hours = _field(form, "cap_dollars") or "", _field(form, "conversation_hours") or ""
    rollup_model = _field(form, "rollup_model")
    if rollup_model is None:  # a form from before Memory (#367): keep the model as it is
        current = await assistant_admin.assistant_admin(db, config, principal.member, now=context.clock())
        rollup_model = current.settings.rollup_model
    posted: dict[str, Any] = {
        "enabled": enabled,
        "cap_dollars": cap,
        "conversation_hours": hours,
        "memory": memory,
        "rollup_model": rollup_model,
    }
    try:
        values = AssistantSettings(
            enabled=enabled,
            cap_dollars=_whole(cap, "The monthly cap"),
            conversation_hours=_whole(hours, "Conversation hours"),
            memory=memory,
            rollup_model=rollup_model,
        )
        await assistant_admin.update_assistant(db, config, principal.member, values, now=context.clock())
    except (InvalidAssistantSettings, BadFormValue) as exc:
        return await _page(request, context, db, principal, values=posted, error=str(exc), status_code=400)
    return RedirectResponse("/admin/assistant?notice=saved", status_code=303)


@router.post("/assistant/memory/clear")
async def clear_memory(request: Request, principal: AdminCSRFChecked, context: Context, db: Database) -> Response:
    """**Clear memory** and **Delete all memory** (#367): one member's facts, or everyone's."""
    config = _config(context)
    form = await request.form()
    who = _field(form, "member") or ""
    try:
        member_id = None if who == EVERYONE else optional_int(who, CHOOSE)
        if member_id is None and who != EVERYONE:
            raise BadFormValue(CHOOSE)
    except BadFormValue as exc:
        return await _page(request, context, db, principal, error=str(exc), status_code=400)
    await assistant_admin.clear_memory(db, config, principal.member, member_id, now=context.clock())
    return local_redirect("/admin/assistant?notice=cleared#memory")
