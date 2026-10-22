# SPDX-License-Identifier: AGPL-3.0-or-later
"""History: meals by week, cuisines in the variety window, and logging meals (FR-30, FR-32)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.domain.catalog import ItemKind
from dinnerbell.domain.meals import InvalidMeal, MealInput, MealLog, Slot, can_change
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services import meals as meal_service
from dinnerbell.services.errors import Forbidden
from dinnerbell.services.variety import household_today, variety_window
from dinnerbell.web.auth import Context, CSRFChecked, Database, Principal, SignedIn
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import BadFormValue, choice, iso_date
from dinnerbell.web.render import render

router = APIRouter(prefix="/history")

NOTICES = {"logged": "Meal logged.", "updated": "Meal updated.", "deleted": "Meal deleted."}
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
            raise BadFormValue("Pick lunch or dinner.")
        if not self.item_id.isdigit():
            raise BadFormValue("Pick something from the catalog.")
        return MealInput(
            day=iso_date(self.day, "Enter the date as YYYY-MM-DD."),
            slot=slot,
            item_id=int(self.item_id),
            participant_ids=frozenset(self.participant_ids),
        )


def posted_meal(
    day: FormText = "",
    slot: FormText = Slot.DINNER.value,
    item_id: FormText = "",
    participant_ids: Annotated[list[str] | None, Form()] = None,
) -> MealForm:
    ids = {int(v) for v in participant_ids or [] if v.isdigit()}
    return MealForm(day=day, slot=slot, item_id=item_id, participant_ids=ids)


PostedMeal = Annotated[MealForm, Depends(posted_meal)]


def _today(context: AppContext, principal: Principal) -> date:
    return household_today(principal.household, context.clock())


async def _choices(db: Database, principal: Principal, keep_item: int | None = None) -> dict[str, Any]:
    data = await catalog_service.catalog(db, principal.member)
    usable = [i for i in data.items if i.active or i.id == keep_item]
    return {
        "restaurants": [i for i in usable if i.kind is ItemKind.RESTAURANT],
        "home_meals": [i for i in usable if i.kind is ItemKind.HOME],
        "people": [m for m in data.members if m.active],
        "member_names": {m.id: m.display_name for m in data.members},
        "slots": list(Slot),
    }


async def _history_page(
    request: Request,
    principal: Principal,
    context: AppContext,
    db: Database,
    *,
    page: int = 0,
    form: MealForm | None = None,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    today = _today(context, principal)
    history = await meal_service.history(db, principal.member, today=today, page=page)
    window = await variety_window(db, principal.household.id, today)
    blank = MealForm(day=today.isoformat(), participant_ids={principal.member.id})
    return await render(
        request,
        "history.html",
        {
            "history": history,
            "window": window,
            "today": today,
            "form": form or blank,
            "error": error,
            "notice": notice,
            "can_change": lambda meal: can_change(principal.member, meal),
        }
        | await _choices(db, principal),
        status_code=status_code,
    )


@router.get("")
async def history_page(
    request: Request, principal: SignedIn, context: Context, db: Database, page: int = 0, notice: str = ""
) -> Response:
    return await _history_page(request, principal, context, db, page=page, notice=NOTICES.get(notice))


@router.post("/meals")
async def log_meal(
    request: Request, principal: CSRFChecked, context: Context, db: Database, form: PostedMeal
) -> Response:
    try:
        await meal_service.log_meal(
            db, principal.member, form.to_input(), today=_today(context, principal), now=context.clock()
        )
    except (InvalidMeal, BadFormValue) as exc:
        return await _history_page(request, principal, context, db, form=form, error=str(exc), status_code=400)
    return RedirectResponse("/history?notice=logged", status_code=303)


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
        | await _choices(db, principal, keep_item=meal.item_id),
        status_code=status_code,
    )


@router.get("/meals/{meal_id}")
async def edit_meal_page(
    request: Request, principal: SignedIn, context: Context, db: Database, meal_id: int
) -> Response:
    meal = await meal_service.get_meal(db, principal.member, meal_id)
    if not can_change(principal.member, meal):
        raise Forbidden("You can change only meals you logged. Ask the admin to fix this one.")
    return await _edit_page(request, principal, context, db, meal, MealForm.from_meal(meal))


@router.post("/meals/{meal_id}")
async def update_meal(
    request: Request, principal: CSRFChecked, context: Context, db: Database, meal_id: int, form: PostedMeal
) -> Response:
    try:
        await meal_service.update_meal(
            db, principal.member, meal_id, form.to_input(), today=_today(context, principal), now=context.clock()
        )
    except (InvalidMeal, BadFormValue) as exc:
        meal = await meal_service.get_meal(db, principal.member, meal_id)
        return await _edit_page(request, principal, context, db, meal, form, error=str(exc), status_code=400)
    return RedirectResponse("/history?notice=updated", status_code=303)


@router.post("/meals/{meal_id}/delete")
async def delete_meal(principal: CSRFChecked, context: Context, db: Database, meal_id: int) -> Response:
    await meal_service.delete_meal(db, principal.member, meal_id, now=context.clock())
    return RedirectResponse("/history?notice=deleted", status_code=303)
