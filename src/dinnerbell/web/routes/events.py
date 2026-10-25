# SPDX-License-Identifier: AGPL-3.0-or-later
"""Today and event pages: respond, coordinate, propose, vote, lock, mark done, call it off, close
a window now, and start a called-off call again (spec 11, FR-56).

Every action posts a small form. With HTMX it swaps the event's card in place; without it, it
redirects back to the page it came from. Admin overrides live under ``/events/{id}/admin/`` and
check for an admin here as well as in the service.

On a labeled instance (``config.is_test_instance``), such as test, an admin can also discard a
call, and a called-off call can start again at any time (#60). Elsewhere the discard pages
don't exist (404).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import time
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.config import is_test_instance
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import (
    CALL_FINISHED,
    MAX_GUESTS,
    AssignmentRole,
    Details,
    EventRuleViolation,
    IllegalTransition,
    InvalidResponse,
    RoleOffer,
    StaleRun,
    Status,
    Window,
    WindowMoved,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.services import call_admin, calls, discard, event_views
from dinnerbell.services.errors import NotFound
from dinnerbell.web.auth import (
    AdminCSRFChecked,
    AdminPrincipal,
    Context,
    CSRFChecked,
    Database,
    Principal,
    SignedIn,
)
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import BadFormValue, RowId, choice, iso_date, optional_int, parse_id
from dinnerbell.web.render import is_htmx, render

router = APIRouter()

Field = Annotated[str, Form(max_length=200)]
Many = Annotated[list[str] | None, Form()]
STALE = "That can't happen anymore. Reload the page to see what changed."
RULE_ERRORS = (EventRuleViolation, InvalidResponse, BadFormValue)


def _any_time(context: AppContext) -> bool:
    """Whether a called-off call may start again at any time: on a test instance only (#60)."""
    return is_test_instance(context.settings.instance_label)


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
    status_code: int | None = None,
) -> Response:
    """The event's card for HTMX, or the event page with the error for a plain form post.
    An error is a 409 unless ``status_code`` says otherwise."""
    view = await event_views.event_view(
        db, principal.member, event_id, now=context.clock(), any_time=_any_time(context)
    )
    status = status_code or (409 if error else 200)
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
    except (StaleRun, WindowMoved):
        return await _card(request, principal, context, db, event_id, came_from=came_from, error=STALE)
    except RULE_ERRORS as exc:
        return await _card(request, principal, context, db, event_id, came_from=came_from, error=str(exc))
    except IllegalTransition as exc:
        error = STALE if exc.current.is_open else CALL_FINISHED
        return await _card(request, principal, context, db, event_id, came_from=came_from, error=error)
    if is_htmx(request):
        return await _card(request, principal, context, db, event_id, came_from=came_from)
    return RedirectResponse(_back(event_id, came_from), status_code=303)


# Pages ------------------------------------------------------------------------------------------


@router.get("/events/{event_id}")
async def event_page(
    request: Request, principal: SignedIn, context: Context, db: Database, event_id: RowId
) -> Response:
    view = await event_views.event_view(
        db, principal.member, event_id, now=context.clock(), any_time=_any_time(context)
    )
    return await render(request, "event.html", {"v": view, "from_page": "event"})


@router.get("/events/{event_id}/card")
async def event_card(
    request: Request,
    principal: SignedIn,
    context: Context,
    db: Database,
    event_id: RowId,
    came_from: Annotated[str, Query(alias="from", max_length=10)] = "",
) -> Response:
    """The event's card alone, for pages that refresh open calls in place (see app.js)."""
    view = await event_views.event_view(
        db, principal.member, event_id, now=context.clock(), any_time=_any_time(context)
    )
    page = {"v": view, "from_page": "today" if came_from == "today" else "event"}
    return await render(request, "_event.html", page)


@router.post("/events")
async def start_call(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    slot: Field = Slot.DINNER.value,
    day: Field = "",
    post_now: Field = "",
) -> Response:
    """FR-3: an ad-hoc call for a slot and date. A later day's call posts at that day's usual
    time, or now with ``post_now`` (issue #59)."""
    try:
        chosen = choice(Slot, slot)
        if chosen is None:
            raise BadFormValue("Pick lunch or dinner.")
        when = iso_date(day, "Enter the date as YYYY-MM-DD.")
        event = await calls.start_call(
            db, principal.member, chosen, when, now=context.clock(), instance_label=context.settings.instance_label
        )
        if post_now:
            await calls.post_now(db, principal.member, event.id, run=event.run, now=context.clock())
    except RULE_ERRORS as exc:
        view = await event_views.today_view(db, principal.member, now=context.clock(), any_time=_any_time(context))
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
        who = _member_id(member_id) if member_id else None
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


# Calling it off and closing early (FR-56) -------------------------------------------------------

Reason = Annotated[str, Form(max_length=400)]
"""A call-off reason. The service cuts it to 140 characters rather than refusing a longer one."""
Run = Annotated[str, Form(max_length=20)]
"""The run of the call the card showed, so a card left open across a restart can't act on the
newer run. Required: every card that posts here writes it."""
WindowField = Annotated[str, Form(max_length=10)]
"""The window Close now's button closes (``respond`` or ``vote``), so a confirmation that arrives
after the call moved on, or races another, closes nothing. Required, like ``run``."""


async def _malformed(
    request: Request, principal: Principal, context: AppContext, db: Database, event_id: int, came_from: str
) -> Response:
    """A post missing a field the card always writes, or with one it never writes: a 400 with
    the fresh card and the reload message. Only a plain (no-JavaScript) post shows that body:
    HTMX drops 4xx responses other than 409 (``responseHandling`` in ``base.html``), so the page
    shows its "Not saved. Reload the page." banner instead."""
    return await _card(request, principal, context, db, event_id, came_from=came_from, error=STALE, status_code=400)


@router.post("/events/{event_id}/cancel")
async def cancel(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    reason: Reason = "",
    run: Run = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    """Call it off: the coordinator, the starter until it's decided, or an admin; the service checks which."""
    seen = parse_id(run)
    if seen is None:
        return await _malformed(request, principal, context, db, event_id, came_from)

    async def action() -> None:
        await calls.cancel_call(db, principal.member, event_id, note=reason or None, run=seen, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


@router.post("/events/{event_id}/close")
async def close(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    run: Run = "",
    window: WindowField = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    """Close now ("Start the vote now" or "Decide now"): the coordinator or an admin. ``window``
    is the one the card offered to close; the service refuses if another is open by now."""
    seen, expected = parse_id(run), choice(Window, window)
    if seen is None or expected is None:
        return await _malformed(request, principal, context, db, event_id, came_from)

    async def action() -> None:
        member = principal.member
        await calls.close_window(db, member, event_id, run=seen, expected_window=expected, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


@router.post("/events/{event_id}/post-now")
async def post_now(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    run: Run = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    """Post a scheduled call now instead of at its usual time (issue #59): any member."""
    seen = parse_id(run)
    if seen is None:
        return await _malformed(request, principal, context, db, event_id, came_from)

    async def action() -> None:
        await calls.post_now(db, principal.member, event_id, run=seen, now=context.clock())

    return await _run(request, principal, context, db, event_id, came_from, action)


@router.post("/events/{event_id}/restart")
async def restart(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    event_id: RowId,
    run: Run = "",
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
) -> Response:
    """Start a called-off call again: any member, until the usual meal time plus two hours, or at
    any time on a test instance."""
    seen = parse_id(run)
    if seen is None:
        return await _malformed(request, principal, context, db, event_id, came_from)

    async def action() -> None:
        label = context.settings.instance_label
        await calls.restart_call(db, principal.member, event_id, run=seen, now=context.clock(), instance_label=label)

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


# Discarding a call, on a test instance only (#60) ------------------------------------------------


async def labeled_instance_only(context: Context) -> None:
    """On an unlabeled instance these routes don't exist: 404, before the admin check."""
    if not is_test_instance(context.settings.instance_label):
        raise NotFound("That page doesn't exist.")


TEST_ONLY = [Depends(labeled_instance_only)]
ROW_LABELS = {
    "vote": "Votes",
    "proposal": "Proposals",
    "suggestion": "Suggestions",
    "response": "Answers",
    "assignment": "Assignments",
    "meal_log": "Meal log entries",
}


@router.get("/events/{event_id}/admin/discard", dependencies=TEST_ONLY)
async def discard_page(
    request: Request, principal: AdminPrincipal, context: Context, db: Database, event_id: RowId
) -> Response:
    """Confirm discarding a call, with what it would delete."""
    found = await discard.preview(db, principal.member.household_id, event_id)
    rows = [(ROW_LABELS.get(table, table), count) for table, count in found.rows.items()]
    return await render(request, "event_discard.html", {"p": found, "rows": rows})


@router.post("/events/{event_id}/admin/discard", dependencies=TEST_ONLY)
async def discard_call(principal: AdminCSRFChecked, context: Context, db: Database, event_id: RowId) -> Response:
    await discard.discard_call(
        db,
        principal.member.household_id,
        event_id,
        actor=principal.member,
        instance_label=context.settings.instance_label,
        now=context.clock(),
    )
    return RedirectResponse("/", status_code=303)
