# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Schedule: slot schedules, ranking weights, proposal count, and variety window (FR-43),
and skipping a day's call before it opens (FR-6)."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData
from starlette.responses import Response

from dinnerbell.domain.catalog import MODE_ORDER, Mode
from dinnerbell.domain.events import AD_HOC_DAYS_AHEAD, EventRuleViolation
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.schedule import WEIGHT_NAMES, InvalidSchedule, SlotSchedule, Weights, format_hhmm, parse_hhmm
from dinnerbell.services import calls, event_views
from dinnerbell.services import schedule as schedule_service
from dinnerbell.services.schedule import ScheduleSettings
from dinnerbell.web.auth import AdminCSRFChecked, AdminPrincipal, Context, Database, Principal
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import BadFormValue, choice, iso_date, optional_int
from dinnerbell.web.render import render

router = APIRouter(prefix="/admin")

WEIGHT_LABELS = {
    "soft_preference": "Soft preferences",
    "cuisine_variety": "Cuisine variety",
    "novelty": "Never had it",
    "suggested": "Suggested today",
    "default_mode": "Usual way to get it",
    "volunteer_fit": "Volunteer knows the dish",
}
MINUTE_FIELDS = (
    ("response_minutes", "Response window (minutes)"),
    ("vote_minutes", "Vote window (minutes)"),
    ("reminder_minutes", "Reminder before the response deadline (minutes)"),
)
MAX_FIELD = 40


def _stored(values: ScheduleSettings) -> dict[str, Any]:
    """Form values for the stored settings, keyed by input name."""
    form: dict[str, Any] = {}
    for slot, schedule in values.slots.items():
        p = slot.value
        form |= {
            f"{p}_enabled": schedule.enabled,
            f"{p}_call_time": format_hhmm(schedule.call_time),
            f"{p}_meal_time": format_hhmm(schedule.meal_time),
            f"{p}_modes": [m.value for m in schedule.modes],
        }
        form |= {f"{p}_{name}": str(getattr(schedule, name)) for name, _ in MINUTE_FIELDS}
    form |= {f"weight_{name}": f"{getattr(values.weights, name):g}" for name in WEIGHT_NAMES}
    form |= {"proposal_count": str(values.proposal_count), "variety_window_days": str(values.variety_window_days)}
    return form


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value[:MAX_FIELD] if isinstance(value, str) else ""


def _posted(form: FormData) -> dict[str, Any]:
    """The form as typed, to redisplay after an error."""
    posted: dict[str, Any] = {}
    for slot in Slot:
        p = slot.value
        posted[f"{p}_enabled"] = form.get(f"{p}_enabled") is not None
        posted[f"{p}_modes"] = [v for v in form.getlist(f"{p}_modes") if isinstance(v, str)][: len(MODE_ORDER)]
        for name in ("call_time", "meal_time", *(n for n, _ in MINUTE_FIELDS)):
            posted[f"{p}_{name}"] = _text(form, f"{p}_{name}")
    for name in (*(f"weight_{w}" for w in WEIGHT_NAMES), "proposal_count", "variety_window_days"):
        posted[name] = _text(form, name)
    return posted


def _whole(value: str, label: str) -> int:
    number = optional_int(value, f"{label} must be a whole number.")
    if number is None:
        raise BadFormValue(f"Enter {label.lower()}.")
    return number


def _weight(value: str, label: str) -> float:
    try:
        number = float(value.strip())
    except ValueError:
        raise BadFormValue(f"{label} must be a number.") from None
    if not math.isfinite(number):
        raise BadFormValue(f"{label} must be a number.")
    return number


def _parse(posted: dict[str, Any]) -> ScheduleSettings:
    slots: dict[Slot, SlotSchedule] = {}
    for slot in Slot:
        p = slot.value
        try:
            modes = frozenset(Mode(v) for v in posted[f"{p}_modes"])
        except ValueError:
            raise BadFormValue("Pick cook, pickup, or delivery.") from None
        slots[slot] = SlotSchedule(
            enabled=posted[f"{p}_enabled"],
            call_time=parse_hhmm(posted[f"{p}_call_time"], f"{p} call time"),
            response_minutes=_whole(posted[f"{p}_response_minutes"], "The response window"),
            vote_minutes=_whole(posted[f"{p}_vote_minutes"], "The vote window"),
            reminder_minutes=_whole(posted[f"{p}_reminder_minutes"], "The reminder time"),
            modes=modes,
            meal_time=parse_hhmm(posted[f"{p}_meal_time"], f"{p} meal time"),
        )
    weights = Weights(**{w: _weight(posted[f"weight_{w}"], WEIGHT_LABELS[w]) for w in WEIGHT_NAMES})
    return ScheduleSettings(
        slots=slots,
        weights=weights,
        proposal_count=_whole(posted["proposal_count"], "The number of proposals"),
        variety_window_days=_whole(posted["variety_window_days"], "The variety window"),
    )


async def _page(
    db: Database, principal: Principal, context: AppContext, values: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    today = await event_views.today_for(db, principal.member, now=context.clock())
    stored = await schedule_service.schedule_settings(db, principal.member)
    skip_slots = [slot for slot in (Slot.DINNER, Slot.LUNCH) if stored.slots[slot].enabled]
    return {
        "values": values,
        "slots": [Slot.DINNER, Slot.LUNCH],
        "modes": list(MODE_ORDER),
        "minute_fields": MINUTE_FIELDS,
        "weights": [(name, WEIGHT_LABELS[name]) for name in WEIGHT_NAMES],
        "skip_slots": skip_slots,
        "skip": {"slot": "", "day": (today + timedelta(days=1)).isoformat(), "reason": ""},
        "skip_days": (today, today + timedelta(days=AD_HOC_DAYS_AHEAD)),
    } | extra


NOTICES = {
    "saved": "Schedule saved. It applies from the next call.",
    "skipped": "Skipped. That call won't go out. Anyone can still start it from the Today page that day.",
}


@router.get("/schedule")
async def schedule_page(
    request: Request, principal: AdminPrincipal, context: Context, db: Database, notice: str = ""
) -> Response:
    stored = await schedule_service.schedule_settings(db, principal.member)
    page = await _page(db, principal, context, _stored(stored), notice=NOTICES.get(notice))
    return await render(request, "admin_schedule.html", page)


@router.post("/schedule")
async def save_schedule(request: Request, principal: AdminCSRFChecked, context: Context, db: Database) -> Response:
    posted = _posted(await request.form())
    try:
        await schedule_service.update_schedule(db, principal.member, _parse(posted), now=context.clock())
    except (InvalidSchedule, BadFormValue) as exc:
        page = await _page(db, principal, context, posted, error=str(exc))
        return await render(request, "admin_schedule.html", page, status_code=400)
    return RedirectResponse("/admin/schedule?notice=saved", status_code=303)


Field = Annotated[str, Form(max_length=40)]
Reason = Annotated[str, Form(max_length=400)]
"""A skip reason. The service cuts it to 140 characters rather than refusing a longer one."""


@router.post("/schedule/skip")
async def skip_day(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    slot: Field = "",
    day: Field = "",
    reason: Reason = "",
) -> Response:
    """FR-6: skip a day's call, today or up to a week ahead, before it opens."""
    try:
        chosen = choice(Slot, slot)
        if chosen is None:
            raise BadFormValue("Pick lunch or dinner.")
        when: date = iso_date(day, "Enter the date as YYYY-MM-DD.")
        await calls.skip_day(db, principal.member, chosen, when, note=reason or None, now=context.clock())
    except (EventRuleViolation, BadFormValue) as exc:
        stored = await schedule_service.schedule_settings(db, principal.member)
        typed = {"slot": slot, "day": day, "reason": reason}
        page = await _page(db, principal, context, _stored(stored), skip_error=str(exc))
        status = 400 if isinstance(exc, BadFormValue) else 409  # a malformed form, or a rule refusal
        return await render(request, "admin_schedule.html", page | {"skip": typed}, status_code=status)
    return RedirectResponse("/admin/schedule?notice=skipped", status_code=303)
