# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Schedule page, where any member changes a slot's standing schedule (FR-44), and Admin >
Ranking: ranking weights, randomness, proposal count, and variety window (FR-43), and skipping a day's call
before it opens (FR-6)."""

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
from dinnerbell.domain.meals import CALL_ORDER, PICK_SLOT, Slot
from dinnerbell.domain.schedule import (
    MAX_REMINDER_MINUTES,
    MAX_WINDOW_MINUTES,
    MIN_WINDOW_MINUTES,
    WEIGHT_NAMES,
    InvalidSchedule,
    SlotSchedule,
    Weights,
    format_hhmm,
    parse_hhmm,
    slot_fingerprint,
)
from dinnerbell.services import activity, calls, event_views
from dinnerbell.services import schedule as schedule_service
from dinnerbell.services.errors import NotFound
from dinnerbell.services.schedule import RankingSettings, ScheduleMoved
from dinnerbell.web.auth import AdminCSRFChecked, AdminPrincipal, Context, CSRFChecked, Database, Principal, SignedIn
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import BadFormValue, choice, iso_date, optional_int
from dinnerbell.web.redirects import local_redirect
from dinnerbell.web.render import render

router = APIRouter()

WEIGHT_LABELS = {
    "soft_preference": "Soft preferences",
    "cuisine_variety": "Cuisine variety",
    "novelty": "Never had it",
    "suggested": "Suggested today",
    "default_mode": "Usual way to get it",
    "volunteer_fit": "Volunteer knows the dish",
}
MINUTE_FIELDS = (
    ("response_minutes", "Time to answer (minutes)", MIN_WINDOW_MINUTES, MAX_WINDOW_MINUTES),
    ("vote_minutes", "Time to vote (minutes)", MIN_WINDOW_MINUTES, MAX_WINDOW_MINUTES),
    ("reminder_minutes", "Reminder before answers close (minutes)", 0, MAX_REMINDER_MINUTES),
)
"""Each minutes field: its name, label, and the range the domain accepts (``check_schedule``)."""
OUT_OF_DATE = "This form is out of date. Here's the schedule now; make your change again."
MAX_FIELD = 40
SLOTS = CALL_ORDER


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value[:MAX_FIELD] if isinstance(value, str) else ""


def _whole(value: str, label: str) -> int:
    number = optional_int(value, f"{label} must be a whole number.")
    if number is None:
        raise BadFormValue(f"Enter {label.lower()}.")
    return number


# Schedule: any member (FR-44) -------------------------------------------------------------------


def _slot_values(slot: Slot, schedule: SlotSchedule) -> dict[str, Any]:
    """Form values for a stored slot schedule, keyed by input name."""
    p = slot.value
    values: dict[str, Any] = {
        f"{p}_enabled": schedule.enabled,
        f"{p}_call_time": format_hhmm(schedule.call_time),
        f"{p}_meal_time": format_hhmm(schedule.meal_time),
        f"{p}_modes": [m.value for m in schedule.modes],
        f"{p}_seen": slot_fingerprint(schedule),
    }
    return values | {f"{p}_{name}": str(getattr(schedule, name)) for name, *_ in MINUTE_FIELDS}


def _posted_slot(form: FormData, slot: Slot) -> dict[str, Any]:
    """One slot's form as typed, to redisplay after an error."""
    p = slot.value
    posted: dict[str, Any] = {
        f"{p}_enabled": form.get(f"{p}_enabled") is not None,
        f"{p}_modes": [v for v in form.getlist(f"{p}_modes") if isinstance(v, str)][: len(MODE_ORDER)],
    }
    for name in ("call_time", "meal_time", "seen", *(n for n, *_ in MINUTE_FIELDS)):
        posted[f"{p}_{name}"] = _text(form, f"{p}_{name}")
    return posted


def _parse_slot(posted: dict[str, Any], slot: Slot) -> SlotSchedule:
    p = slot.value
    try:
        modes = frozenset(Mode(v) for v in posted[f"{p}_modes"])
    except ValueError:
        raise BadFormValue("Pick cook, pickup, or delivery.") from None
    return SlotSchedule(
        enabled=posted[f"{p}_enabled"],
        call_time=parse_hhmm(posted[f"{p}_call_time"], "call time"),
        response_minutes=_whole(posted[f"{p}_response_minutes"], "The time to answer"),
        vote_minutes=_whole(posted[f"{p}_vote_minutes"], "The time to vote"),
        reminder_minutes=_whole(posted[f"{p}_reminder_minutes"], "The reminder time"),
        modes=modes,
        meal_time=parse_hhmm(posted[f"{p}_meal_time"], "meal time"),
    )


async def _schedule_page(
    db: Database, principal: Principal, *, typed: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    stored = await schedule_service.slot_schedules_for(db, principal.member)
    values: dict[str, Any] = {}
    for slot in SLOTS:
        values |= _slot_values(slot, stored[slot])
    return {
        "values": values | (typed or {}),
        "slots": SLOTS,
        "modes": list(MODE_ORDER),
        "minute_fields": MINUTE_FIELDS,
    } | extra


@router.get("/schedule")
async def schedule_page(request: Request, principal: SignedIn, db: Database, saved: str = "") -> Response:
    page = await _schedule_page(db, principal, saved=choice(Slot, saved))
    return await render(request, "schedule.html", page)


@router.post("/schedule/{slot}")
async def save_slot(request: Request, slot: str, principal: CSRFChecked, context: Context, db: Database) -> Response:
    """FR-44: any active member saves one slot's standing schedule. A save that changes
    nothing says so, and announces nothing."""
    chosen = choice(Slot, slot)
    if chosen is None:
        raise NotFound("No such meal.")
    posted = _posted_slot(await request.form(), chosen)
    seen = posted[f"{chosen.value}_seen"]
    if not seen:  # every form we draw has one; without it, a save could undo someone else's
        page = await _schedule_page(db, principal, errors={chosen: OUT_OF_DATE})
        return await render(request, "schedule.html", page, status_code=400)
    try:
        changed = await schedule_service.update_slot(
            db, principal.member, chosen, _parse_slot(posted, chosen), seen=seen, now=context.clock()
        )
    except ScheduleMoved as exc:  # show what's there now, not what they typed over
        page = await _schedule_page(db, principal, errors={chosen: str(exc)})
        return await render(request, "schedule.html", page, status_code=409)
    except (InvalidSchedule, BadFormValue) as exc:
        page = await _schedule_page(db, principal, typed=posted, errors={chosen: str(exc)})
        return await render(request, "schedule.html", page, status_code=400)
    if not changed:
        page = await _schedule_page(db, principal, unchanged=chosen)
        return await render(request, "schedule.html", page)
    return local_redirect("/schedule?saved={}#{}", chosen, chosen)


# Admin > Ranking (FR-43) and Skip a day (FR-6) -------------------------------------------------


def _stored(values: RankingSettings) -> dict[str, Any]:
    """Form values for the stored ranking settings, keyed by input name."""
    form: dict[str, Any] = {f"weight_{name}": f"{getattr(values.weights, name):g}" for name in WEIGHT_NAMES}
    return form | {
        "temperature": f"{values.temperature:g}",
        "proposal_count": str(values.proposal_count),
        "variety_window_days": str(values.variety_window_days),
        "roles_choice_minutes": str(values.roles_choice_minutes),
    }


def _posted(form: FormData) -> dict[str, Any]:
    """The form as typed, to redisplay after an error."""
    return {
        name: _text(form, name)
        for name in (
            *(f"weight_{w}" for w in WEIGHT_NAMES),
            "temperature",
            "proposal_count",
            "variety_window_days",
            "roles_choice_minutes",
        )
    }


def _weight(value: str, label: str) -> float:
    try:
        number = float(value.strip())
    except ValueError:
        raise BadFormValue(f"{label} must be a number.") from None
    if not math.isfinite(number):
        raise BadFormValue(f"{label} must be a number.")
    return number


def _parse(posted: dict[str, Any]) -> RankingSettings:
    weights = Weights(**{w: _weight(posted[f"weight_{w}"], WEIGHT_LABELS[w]) for w in WEIGHT_NAMES})
    return RankingSettings(
        weights=weights,
        proposal_count=_whole(posted["proposal_count"], "The number of proposals"),
        variety_window_days=_whole(posted["variety_window_days"], "The variety window"),
        temperature=_weight(posted["temperature"], "Randomness"),
        roles_choice_minutes=_whole(posted["roles_choice_minutes"], "The time to choose roles"),
    )


async def _page(
    db: Database, principal: Principal, context: AppContext, values: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    today = await event_views.today_for(db, principal.member, now=context.clock())
    slots = await schedule_service.slot_schedules_for(db, principal.member)
    return {
        "values": values,
        "weights": [(name, WEIGHT_LABELS[name]) for name in WEIGHT_NAMES],
        "skip_slots": [slot for slot in SLOTS if slots[slot].enabled],
        "skip": {"slot": "", "day": (today + timedelta(days=1)).isoformat(), "reason": ""},
        "skip_days": (today, today + timedelta(days=AD_HOC_DAYS_AHEAD)),
    } | extra


NOTICES = {
    "saved": "Ranking saved. It applies from the next call.",
    "skipped": "Skipped. That call won't go out. Anyone can still start it from the Today page that day.",
}


@router.get("/admin/schedule")
async def admin_schedule_page(
    request: Request, principal: AdminPrincipal, context: Context, db: Database, notice: str = ""
) -> Response:
    stored = await schedule_service.ranking_settings(db, principal.member)
    page = await _page(db, principal, context, _stored(stored), notice=NOTICES.get(notice))
    return await render(request, "admin_schedule.html", page)


@router.post("/admin/schedule")
async def save_ranking(request: Request, principal: AdminCSRFChecked, context: Context, db: Database) -> Response:
    posted = _posted(await request.form())
    try:
        await schedule_service.update_ranking(db, principal.member, _parse(posted), now=context.clock())
    except (InvalidSchedule, BadFormValue) as exc:
        page = await _page(db, principal, context, posted, error=str(exc))
        return await render(request, "admin_schedule.html", page, status_code=400)
    return RedirectResponse("/admin/schedule?notice=saved", status_code=303)


Field = Annotated[str, Form(max_length=40)]
Reason = Annotated[str, Form(max_length=400)]
"""A skip reason. The service cuts it to 140 characters rather than refusing a longer one."""


@router.post("/admin/schedule/skip")
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
            raise BadFormValue(PICK_SLOT)
        when: date = iso_date(day, "Enter the date as YYYY-MM-DD.")
        await calls.skip_day(db, principal.member, chosen, when, note=reason or None, now=context.clock())
    except (EventRuleViolation, BadFormValue) as exc:
        activity.refused(exc, str(exc), principal.member, path=request.url.path)
        stored = await schedule_service.ranking_settings(db, principal.member)
        typed = {"slot": slot, "day": day, "reason": reason}
        page = await _page(db, principal, context, _stored(stored), skip_error=str(exc))
        status = 400 if isinstance(exc, BadFormValue) else 409  # a malformed form, or a rule refusal
        return await render(request, "admin_schedule.html", page | {"skip": typed}, status_code=status)
    return RedirectResponse("/admin/schedule?notice=skipped", status_code=303)
