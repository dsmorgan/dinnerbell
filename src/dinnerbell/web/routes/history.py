# SPDX-License-Identifier: AGPL-3.0-or-later
"""History: meals by week, cuisines in the variety window, logging meals, and feedback on them
(FR-30, FR-32, FR-45 to FR-49)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request
from starlette.responses import Response

from dinnerbell.domain.catalog import ItemKind
from dinnerbell.domain.feedback import (
    COOK_NOTE_MAX,
    ISSUE_NOTE_MAX,
    FeedbackInput,
    InvalidFeedback,
    PlaceIssue,
    Rating,
)
from dinnerbell.domain.meals import PICK_SLOT, InvalidMeal, MealInput, MealLog, Slot, can_change
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services import feedback as feedback_service
from dinnerbell.services import meals as meal_service
from dinnerbell.services.errors import Forbidden
from dinnerbell.services.variety import household_today, variety_window
from dinnerbell.web.auth import Context, CSRFChecked, Database, Principal, SignedIn
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import BadFormValue, RowId, choice, iso_date, parse_id
from dinnerbell.web.redirects import local_path, local_redirect
from dinnerbell.web.render import render

router = APIRouter(prefix="/history")

NOTICES = {
    "logged": "Meal logged.",
    "updated": "Meal updated.",
    "deleted": "Meal deleted.",
    "feedback": "Thanks for the feedback! Only you see it.",
    "no_feedback": "Got it: no feedback from you on that meal.",
}
FormText = Annotated[str, Form(max_length=40)]


@dataclass
class MealForm:
    """The log form's fields as posted, kept to redisplay the form after an error."""

    day: str = ""
    slot: str = Slot.DINNER.value
    item_id: str = ""
    participant_ids: set[int] = field(default_factory=set)

    @classmethod
    def from_meal(cls, meal: MealLog) -> MealForm:
        return cls(
            day=meal.day.isoformat(),
            slot=meal.slot.value,
            item_id=str(meal.item_id),
            participant_ids={p.id for p in meal.participants},
        )

    def to_input(self) -> MealInput:
        slot = choice(Slot, self.slot)
        if slot is None:
            raise BadFormValue(PICK_SLOT)
        item_id = parse_id(self.item_id)
        if item_id is None:
            raise BadFormValue("Pick something from the catalog.")
        return MealInput(
            day=iso_date(self.day, "Enter the date as YYYY-MM-DD."),
            slot=slot,
            item_id=item_id,
            participant_ids=frozenset(self.participant_ids),
        )


def posted_meal(
    day: FormText = "",
    slot: FormText = Slot.DINNER.value,
    item_id: FormText = "",
    participant_ids: Annotated[list[str] | None, Form()] = None,
) -> MealForm:
    ids = {i for v in participant_ids or [] if (i := parse_id(v)) is not None}
    return MealForm(day=day, slot=slot, item_id=item_id, participant_ids=ids)


PostedMeal = Annotated[MealForm, Depends(posted_meal)]


def _today(context: AppContext, principal: Principal) -> date:
    return household_today(principal.household, context.clock())


async def _history_path(db: Database, principal: Principal, meal: MealLog, notice: str = "") -> str:
    """The History page that lists ``meal``, scrolled to it, with ``notice`` (a ``NOTICES`` key)."""
    page = await meal_service.history_page_of(db, principal.member, meal)
    query = "&".join(([f"page={page}"] if page > 1 else []) + ([f"notice={notice}"] if notice else []))
    return local_path(f"/history{'?' if query else ''}{query}#meal-{{}}", meal.id)


async def _choices(db: Database, principal: Principal, editing: MealLog | None = None) -> dict[str, Any]:
    data = await catalog_service.catalog(db, principal.member)
    keep_item = editing.item_id if editing is not None else None
    usable = [i for i in data.items if i.active or i.id == keep_item]
    return {
        "restaurants": [i for i in usable if i.kind is ItemKind.RESTAURANT],
        "home_meals": [i for i in usable if i.kind is ItemKind.HOME],
        "people": await meal_service.participant_choices(db, principal.member, editing),
        "member_names": {m.id: m.display_name for m in data.members},
        "slots": list(Slot),
    }


async def _history_page(
    request: Request,
    principal: Principal,
    context: AppContext,
    db: Database,
    *,
    page: int = 1,
    form: MealForm | None = None,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    today = _today(context, principal)
    history = await meal_service.history(db, principal.member, page=page)
    window = await variety_window(db, principal.household.id, today)
    shown = [meal for _, meals in history.weeks for meal in meals]
    feedback = await feedback_service.for_meals(db, principal.member, shown, today=today)
    blank = MealForm(day=today.isoformat(), participant_ids={principal.member.id})
    return await render(
        request,
        "history.html",
        {
            "history": history,
            "history_url": lambda number: f"/history?page={number}" if number > 1 else "/history",
            "window": window,
            "today": today,
            "form": form or blank,
            "error": error,
            "notice": notice,
            "can_change": lambda meal: can_change(principal.member, meal),
            "feedback": feedback,
        }
        | await _choices(db, principal),
        status_code=status_code,
    )


@router.get("")
async def history_page(
    request: Request, principal: SignedIn, context: Context, db: Database, page: int = 1, notice: str = ""
) -> Response:
    """History, ``MEALS_PER_PAGE`` meals at a time. A page past either end shows the nearest one."""
    return await _history_page(request, principal, context, db, page=page, notice=NOTICES.get(notice))


@router.post("/meals")
async def log_meal(
    request: Request, principal: CSRFChecked, context: Context, db: Database, form: PostedMeal
) -> Response:
    try:
        meal = await meal_service.log_meal(
            db, principal.member, form.to_input(), today=_today(context, principal), now=context.clock()
        )
    except (InvalidMeal, BadFormValue) as exc:
        return await _history_page(request, principal, context, db, form=form, error=str(exc), status_code=400)
    return local_redirect(await _history_path(db, principal, meal, "logged"))


async def _edit_page(
    request: Request,
    principal: Principal,
    context: AppContext,
    db: Database,
    meal: MealLog,
    form: MealForm,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return await render(
        request,
        "meal_edit.html",
        {"meal": meal, "form": form, "error": error, "today": _today(context, principal)}
        | await _choices(db, principal, editing=meal),
        status_code=status_code,
    )


@router.get("/meals/{meal_id}")
async def edit_meal_page(
    request: Request, principal: SignedIn, context: Context, db: Database, meal_id: RowId
) -> Response:
    meal = await meal_service.get_meal(db, principal.member, meal_id)
    if not can_change(principal.member, meal):
        raise Forbidden("You can change only meals you logged. Ask the admin to fix this one.")
    return await _edit_page(request, principal, context, db, meal, MealForm.from_meal(meal))


@router.post("/meals/{meal_id}")
async def update_meal(
    request: Request, principal: CSRFChecked, context: Context, db: Database, meal_id: RowId, form: PostedMeal
) -> Response:
    try:
        meal = await meal_service.update_meal(
            db, principal.member, meal_id, form.to_input(), today=_today(context, principal), now=context.clock()
        )
    except (InvalidMeal, BadFormValue) as exc:
        meal = await meal_service.get_meal(db, principal.member, meal_id)
        return await _edit_page(request, principal, context, db, meal, form, error=str(exc), status_code=400)
    return local_redirect(await _history_path(db, principal, meal, "updated"))


@router.post("/meals/{meal_id}/delete")
async def delete_meal(principal: CSRFChecked, context: Context, db: Database, meal_id: RowId) -> Response:
    meal = await meal_service.get_meal(db, principal.member, meal_id)
    page = await meal_service.history_page_of(db, principal.member, meal)  # where it was: its neighbors stay there
    await meal_service.delete_meal(db, principal.member, meal_id, now=context.clock())
    if page == 1:
        return local_redirect("/history?notice=deleted")
    return local_redirect("/history?page={}&notice=deleted", page)


# Feedback (FR-45 to FR-49) -------------------------------------------------------------------------

FROM_PAGES = {"history", "event"}


def posted_feedback(
    rating: FormText = "",
    place_issues: Annotated[list[str] | None, Form()] = None,
    issue_note: Annotated[str, Form(max_length=ISSUE_NOTE_MAX * 2)] = "",
    cook_thanks: FormText = "",
    cook_note: Annotated[str, Form(max_length=COOK_NOTE_MAX * 2)] = "",
) -> FeedbackInput:
    """The feedback form as posted. Any rating but the five, including "No answer", is no answer.
    ``feedback.give`` checks the rest against the meal, including each note's length after
    trimming."""
    issues = frozenset(i for v in place_issues or [] if (i := choice(PlaceIssue, v)) is not None)
    return FeedbackInput(
        rating=choice(Rating, rating),
        place_issues=issues,
        issue_note=issue_note or None,
        cook_thanks=cook_thanks == "1",
        cook_note=cook_note or None,
    )


PostedFeedback = Annotated[FeedbackInput, Depends(posted_feedback)]


async def _back(db: Database, principal: Principal, meal: MealLog, came_from: str, notice: str = "") -> str:
    """Where the form returns: the meal's event page when it came from there, else the History
    page that lists the meal."""
    if came_from == "event" and meal.event_id is not None:
        return local_path("/events/{}#feedback", meal.event_id)
    return await _history_path(db, principal, meal, notice)


async def _feedback_page(
    request: Request,
    principal: Principal,
    context: AppContext,
    db: Database,
    meal_id: int,
    *,
    came_from: str,
    answers: FeedbackInput | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    try:
        form = await feedback_service.form(db, principal.member, meal_id, now=context.clock())
    except InvalidFeedback as exc:
        page = {"title": "Can't give feedback", "message": str(exc)}
        return await render(request, "error.html", page, status_code=403)
    meal = await meal_service.get_meal(db, principal.member, meal_id)
    return await render(
        request,
        "feedback.html",
        {
            "form": form,
            "answers": answers or form.mine or FeedbackInput(),
            "came_from": came_from if came_from in FROM_PAGES else "history",
            "back": await _back(db, principal, meal, came_from),
            "ratings": list(Rating),
            "issues": list(PlaceIssue),
            "note_max": COOK_NOTE_MAX,
            "issue_note_max": ISSUE_NOTE_MAX,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/meals/{meal_id}/feedback")
async def feedback_page(
    request: Request,
    principal: SignedIn,
    context: Context,
    db: Database,
    meal_id: RowId,
    came_from: Annotated[str, Query(alias="from", max_length=10)] = "",
) -> Response:
    return await _feedback_page(request, principal, context, db, meal_id, came_from=came_from)


LEGACY_FORM = "This form changed since you opened it. Check your answers and save again."


@router.post("/meals/{meal_id}/feedback")
async def give_feedback(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    meal_id: RowId,
    answers: PostedFeedback,
    came_from: Annotated[str, Form(alias="from", max_length=10)] = "",
    legacy: Annotated[str | None, Form(alias="food", max_length=40)] = None,
) -> Response:
    if legacy is not None:
        # A form opened before 0020 (one rating) posts "food" and no rating; saving it would drop
        # the member's rating. Show their saved answers instead. Remove after one release.
        return await _feedback_page(
            request, principal, context, db, meal_id, came_from=came_from, error=LEGACY_FORM, status_code=409
        )
    try:
        saved = await feedback_service.give(db, principal.member, meal_id, answers, now=context.clock())
    except InvalidFeedback as exc:
        error = str(exc)
        return await _feedback_page(
            request, principal, context, db, meal_id, came_from=came_from, answers=answers, error=error, status_code=400
        )
    meal = await meal_service.get_meal(db, principal.member, meal_id)
    notice = "feedback" if saved is not None else "no_feedback"
    return local_redirect(await _back(db, principal, meal, came_from, notice))
