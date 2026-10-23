# SPDX-License-Identifier: AGPL-3.0-or-later
"""Today and event pages: respond, coordinate, propose, vote, lock, and mark done (spec 11).

Every action posts a small form. With HTMX it swaps the event's card in place; without it, it
redirects back to the page it came from. Admin overrides live under ``/events/{id}/admin/`` and
check for an admin here as well as in the service.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import time
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import (
    MAX_GUESTS,
    AssignmentRole,
    Details,
    EventRuleViolation,
    IllegalTransition,
    InvalidResponse,
    RoleOffer,
    Status,
    Window,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.services import call_admin, calls, event_views
from dinnerbell.web.auth import AdminCSRFChecked, Context, CSRFChecked, Database, Principal, SignedIn
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import BadFormValue, RowId, choice, iso_date, optional_int, parse_id
from dinnerbell.web.render import is_htmx, render

router = APIRouter()

Field = Annotated[str, Form(max_length=200)]
Many = Annotated[list[str] | None, Form()]
STALE = "That can't happen anymore. Reload the page to see what changed."
RULE_ERRORS = (EventRuleViolation, InvalidResponse, BadFormValue)


def _back(event_id: int, came_from: str) -> str:
    """Where a form posted without HTMX returns: Today or the event page, never anywhere else."""
    return "/" if came_from == "today" else f"/events/{event_id}"


async def _card(
    request: Request,
    principal: Principal,
    context: AppContext,
    db: Database,
    event_id: int,
    *,
    came_from: str,
    error: str | None = None,
) -> Response:
    """The event's card for HTMX, or the event page with the error for a plain form post."""
    view = await event_views.event_view(db, principal.member, event_id, now=context.clock())
    status = 409 if error else 200
    page = {"v": view, "from_page": came_from, "error": error}
    if is_htmx(request):
        return await render(request, "_event.html", page, status_code=status)
    return await render(request, "event.html", page | {"from_page": "event"}, status_code=status)


Action = Callable[[], Awaitable[object]]


async def _run(
    request: Request,
    principal: Principal,
    context: AppContext,
    db: Database,
    event_id: int,
    came_from: str,
    action: Action,
) -> Response:
    try:
        await action()
    except RULE_ERRORS as exc:
        return await _card(request, principal, context, db, event_id, came_from=came_from, error=str(exc))
    except IllegalTransition:
        return await _card(request, principal, context, db, event_id, came_from=came_from, error=STALE)
    if is_htmx(request):
        return await _card(request, principal, context, db, event_id, came_from=came_from)
    return RedirectResponse(_back(event_id, came_from), status_code=303)


# Pages ------------------------------------------------------------------------------------------


@router.get("/events/{event_id}")
async def event_page(
    request: Request, principal: SignedIn, context: Context, db: Database, event_id: RowId
) -> Response:
    view = await event_views.event_view(db, principal.member, event_id, now=context.clock())
    return await render(request, "event.html", {"v": view, "from_page": "event"})


@router.post("/events")
async def start_call(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    slot: Field = Slot.DINNER.value,
    day: Field = "",
) -> Response:
    """FR-3: an ad-hoc call for a slot and date."""
    try:
        chosen = choice(Slot, slot)
        if chosen is None:
            raise BadFormValue("Pick lunch or dinner.")
        when = iso_date(day, "Enter the date as YYYY-MM-DD.")
        event = await calls.start_call(db, principal.member, chosen, when, now=context.clock())
    except RULE_ERRORS as exc:
        view = await event_views.today_view(db, principal.member, now=context.clock())
        page = {"today": view, "from_page": "today", "start_error": str(exc)}
        return await render(request, "today.html", page, status_code=409)
    return RedirectResponse(f"/events/{event.id}", status_code=303)


# Responding -------------------------------------------------------------------------------------


@router.post("/events/{event_id}/respond")
async def respond(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    status: Field = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    async def action() -> None:
        chosen = choice(Status, status)
        if chosen is None:
            raise BadFormValue("Pick in, out, or maybe.")
        await calls.respond(db, principal.member, event_id, chosen, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


@router.post("/events/{event_id}/offer")
async def offer(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    role: Field = "",
    offered: Field = "1",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    async def action() -> None:
        chosen = choice(RoleOffer, role)
        if chosen is None:
            raise BadFormValue("Pick cooking, picking up, or ordering.")
        await calls.offer_role(db, principal.member, event_id, chosen, offered == "1", now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


def _wall_time(value: str, message: str) -> time | None:
    text = value.strip()
    if not text:
        return None
    try:
        return time.fromisoformat(text)
    except ValueError:
        raise BadFormValue(message) from None


@router.post("/events/{event_id}/details")
async def details(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    guests: Field = "0",
    guest_needs: Many = None,
    eat_from: Field = "",
    eat_until: Field = "",
    suggestion_item: Field = "",
    suggestion_text: Field = "",
    note: Annotated[str, Form(max_length=400)] = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    """FR-8: guests, their dietary needs, an eat window, a suggestion, and a note."""

    async def action() -> None:
        count = optional_int(guests, f"Guests is a whole number from 0 to {MAX_GUESTS}.") or 0
        item = parse_id(suggestion_item)
        parsed = Details(
            guests=count,
            guest_needs=frozenset(i for v in guest_needs or [] if (i := parse_id(v)) is not None),
            eat_from=_wall_time(eat_from, "Enter the earliest time as HH:MM."),
            eat_until=_wall_time(eat_until, "Enter the latest time as HH:MM."),
            suggestion_item_ids=(item,) if item is not None else (),
            suggestion_texts=(suggestion_text,) if suggestion_text.strip() else (),
            note=note,
        )
        await calls.save_details(db, principal.member, event_id, parsed, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


# Coordinating -----------------------------------------------------------------------------------


@router.post("/events/{event_id}/coordinate")
async def coordinate(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    async def action() -> None:
        await calls.claim_coordinator(db, principal.member, event_id, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


def _member_id(value: str) -> int:
    member_id = parse_id(value)
    if member_id is None:
        raise BadFormValue("Pick a member.")
    return member_id


@router.post("/events/{event_id}/hand-off")
async def hand_off(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    member_id: Field = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    async def action() -> None:
        await calls.hand_off(db, principal.member, event_id, _member_id(member_id), now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


# Proposals and votes ----------------------------------------------------------------------------


@router.post("/events/{event_id}/proposals")
async def add_proposal(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    mode: Field = "",
    item_id: Field = "",
    free_text: Field = "",
    cuisine_id: Field = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    """FR-13: a catalog item, or a new place or dish by name with its cuisine."""

    async def action() -> None:
        chosen = choice(Mode, mode)
        if chosen is None:
            raise BadFormValue("Pick cook, pickup, or delivery.")
        item = parse_id(item_id)
        if item is None and not free_text.strip():
            raise BadFormValue("Pick something from the catalog or type a name.")
        await calls.add_proposal(
            db,
            principal.member,
            event_id,
            chosen,
            item_id=item,
            free_text=None if item is not None else free_text,
            cuisine_id=parse_id(cuisine_id),
            now=context.clock(),
        )

    return await _run(request, principal, context, db, event_id, came_from, action)


ProposalUseCase = Callable[..., Awaitable[None]]


async def _on_proposal(
    request: Request,
    principal: Principal,
    context: AppContext,
    db: Database,
    event_id: int,
    proposal_id: int,
    came_from: str,
    use_case: ProposalUseCase,
) -> Response:
    async def action() -> None:
        await use_case(db, principal.member, event_id, proposal_id, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


@router.post("/events/{event_id}/proposals/{proposal_id}/vote")
async def vote(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    proposal_id: RowId,
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    return await _on_proposal(request, principal, context, db, event_id, proposal_id, came_from, calls.vote)


@router.post("/events/{event_id}/proposals/{proposal_id}/unvote")
async def unvote(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    proposal_id: RowId,
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    return await _on_proposal(request, principal, context, db, event_id, proposal_id, came_from, calls.unvote)


@router.post("/events/{event_id}/proposals/{proposal_id}/lock")
async def lock(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    proposal_id: RowId,
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    """FR-15: the coordinator or an admin; the service checks which."""
    return await _on_proposal(request, principal, context, db, event_id, proposal_id, came_from, calls.lock)


# After the decision -----------------------------------------------------------------------------


@router.post("/events/{event_id}/meal-time")
async def meal_time(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    at: Field = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    async def action() -> None:
        wall = _wall_time(at, "Enter the meal time as HH:MM.")
        if wall is None:
            raise BadFormValue("Enter the meal time as HH:MM.")
        await calls.set_meal_time(db, principal.member, event_id, wall, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


@router.post("/events/{event_id}/assignments")
async def assignment(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    role: Field = "",
    member_id: Field = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    """The coordinator picks who does a role; the admin can also correct it after the meal."""

    async def action() -> None:
        chosen = choice(AssignmentRole, role)
        if chosen is None:
            raise BadFormValue("Pick a role.")
        who = parse_id(member_id) if member_id else None
        await calls.change_assignment(db, principal.member, event_id, chosen, who, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


@router.post("/events/{event_id}/done")
async def done(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    async def action() -> None:
        await calls.mark_done(db, principal.member, event_id, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


# Admin overrides (FR-6, spec 5.7) ---------------------------------------------------------------

DEADLINE_SHIFTS = {"plus30": 30, "plus15": 15, "minus15": -15, "close": None}


@router.post("/events/{event_id}/admin/deadline")
async def move_deadline(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    window: Field = "",
    shift: Field = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    async def action() -> None:
        chosen = choice(Window, window)
        if chosen is None or shift not in DEADLINE_SHIFTS:
            raise BadFormValue("Pick a window and how to move it.")
        await call_admin.move_deadline(
            db, principal.member, event_id, chosen, DEADLINE_SHIFTS[shift], now=context.clock()
        )

    return await _run(request, principal, context, db, event_id, came_from, action)


@router.post("/events/{event_id}/admin/cancel")
async def cancel(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    async def action() -> None:
        await call_admin.cancel(db, principal.member, event_id, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


@router.post("/events/{event_id}/admin/rerun")
async def rerun(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    async def action() -> None:
        await call_admin.rerun(db, principal.member, event_id, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


@router.post("/events/{event_id}/admin/coordinator")
async def assign_coordinator(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    member_id: Field = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    async def action() -> None:
        who = _member_id(member_id) if member_id else None
        await call_admin.assign_coordinator(db, principal.member, event_id, who, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)
