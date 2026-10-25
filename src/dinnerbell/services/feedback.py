# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback after meals (spec 5.9, FR-45 to FR-49).

Members who ate a meal give feedback on it, from Slack or the web, and change it for 7 days.
Each save snapshots the meal: the item, its kind and mode, the cook, and who ate. Feedback has
no effect on ranking in version 1.

What anyone may see (FR-49) is decided here, not in the pages: a member sees their own answers,
everyone sees food and place feedback only as counts per catalog item, and only the cook sees
who thanked them and their notes. Nothing here logs or audits a rating, so no log line or
audit row can say who chose "not again".
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.events import AssignmentRepository, EventRepository
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.meals import MealLogRepository
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import ItemKind
from dinnerbell.domain.events import AssignmentRole, EventState, check_run
from dinnerbell.domain.feedback import (
    FEEDBACK_DAYS,
    CookThanks,
    Feedback,
    FeedbackInput,
    InvalidFeedback,
    ItemCounts,
    MealFacts,
    can_give,
    check_can_give,
    clean,
    closes_on,
    count,
    thanks_for,
)
from dinnerbell.domain.meals import MealLog, MemberRef, Slot
from dinnerbell.domain.members import Member
from dinnerbell.services import call_context as cc
from dinnerbell.services.call_context import NO_SUCH_CALL, CallContext
from dinnerbell.services.common import is_unique_violation
from dinnerbell.services.errors import NotFound
from dinnerbell.services.event_views import today_for

log = logging.getLogger(__name__)

NOT_DONE = "You can say how it was once the meal is done."
NO_MEAL = "This call didn't log a meal, so there's nothing to give feedback on."
MAX_AWAITING = 5
"""Meals App Home lists as awaiting feedback; History has the rest."""


@dataclass(frozen=True, slots=True)
class FeedbackForm:
    """What the feedback form shows ``member_id``: the meal, their answers so far, and the last
    day they can change them."""

    facts: MealFacts
    member_id: int
    mine: FeedbackInput | None
    closes_on: date

    @property
    def thanks_cook(self) -> bool:
        return self.facts.thanks_cook(self.member_id)


@dataclass(frozen=True, slots=True)
class Awaiting:
    """A meal from a done call that the member ate and hasn't given feedback on yet."""

    meal_id: int
    event_id: int
    run: int
    day: date
    slot: Slot
    item_name: str
    closes_on: date


@dataclass(frozen=True, slots=True)
class MealFeedback:
    """Feedback on one meal as the viewer may see it: their own answers, whether they can give
    or change them, and the thank-yous (with names and notes only for the cook)."""

    meal_id: int
    can_give: bool
    closes_on: date
    mine: FeedbackInput | None
    thanks: CookThanks


# The meal --------------------------------------------------------------------------------------


async def meal_facts(db: AsyncSession, household_id: int, meal: MealLog) -> MealFacts:
    """``meal`` with what feedback needs: the item's kind, and for a meal a call decided, its
    mode and cook. A logged meal has neither, so it has no cook to thank."""
    item = await CatalogRepository(db).item(household_id, meal.item_id)
    kind = item.kind if item is not None else ItemKind.RESTAURANT  # pragma: no cover - items are never deleted
    event = await EventRepository(db).get(household_id, meal.event_id) if meal.event_id is not None else None
    mode = event.decided_mode if event is not None else None
    cook = await _cook(db, household_id, meal) if kind is ItemKind.HOME else None
    return MealFacts(
        meal_id=meal.id,
        day=meal.day,
        slot=meal.slot,
        item_id=meal.item_id,
        item_name=meal.item_name,
        item_kind=kind,
        mode=mode,
        cook=cook,
        participants=meal.participants,
        event_id=meal.event_id,
    )


async def _cook(db: AsyncSession, household_id: int, meal: MealLog) -> MemberRef | None:
    """Who cooks a meal now: its call's cook assignment. A logged meal has none."""
    if meal.event_id is None:
        return None
    assignments = await AssignmentRepository(db).for_event(household_id, meal.event_id)
    cook_id = next((a.member_id for a in assignments if a.role is AssignmentRole.COOK), None)
    if cook_id is None:
        return None
    member = await MemberRepository(db).get(household_id, cook_id)
    return MemberRef(cook_id, member.display_name if member else "A former member")


async def _meal(db: AsyncSession, actor: Member, meal_id: int) -> MealLog:
    meal = await MealLogRepository(db).get(actor.household_id, meal_id)
    if meal is None:
        raise NotFound("No such meal.")
    return meal


async def meal_for_event(db: AsyncSession, actor: Member, event_id: int, *, run: int | None = None) -> int:
    """The meal a done call logged. ``run`` is the run the person saw, when their surface says."""
    event = await EventRepository(db).get(actor.household_id, event_id)
    if event is None:
        raise NotFound(NO_SUCH_CALL)
    check_run(event, run)
    if event.state is not EventState.DONE:
        raise InvalidFeedback(NOT_DONE)
    meal_id = await MealLogRepository(db).for_event(actor.household_id, event_id)
    if meal_id is None:  # pragma: no cover - marking a call done logs its meal
        raise InvalidFeedback(NO_MEAL)
    return meal_id


# Giving feedback -------------------------------------------------------------------------------


async def form(db: AsyncSession, actor: Member, meal_id: int, *, now: datetime) -> FeedbackForm:
    """The feedback form for ``meal_id``. Raises InvalidFeedback unless ``actor`` ate it and
    feedback on it is still open, or NotFound."""
    facts = await meal_facts(db, actor.household_id, await _meal(db, actor, meal_id))
    check_can_give(actor, facts, await today_for(db, actor, now=now))
    mine = await FeedbackRepository(db).get(actor.household_id, meal_id, actor.id)
    return FeedbackForm(facts, actor.id, mine.answers if mine else None, closes_on(facts.day))


async def form_for_event(db: AsyncSession, actor: Member, event_id: int, *, run: int, now: datetime) -> FeedbackForm:
    """The feedback form for the meal of a done call, from **How was it?** on Slack."""
    return await form(db, actor, await meal_for_event(db, actor, event_id, run=run), now=now)


async def give(db: AsyncSession, actor: Member, meal_id: int, data: FeedbackInput, *, now: datetime) -> Feedback | None:
    """Save ``actor``'s feedback on ``meal_id``, replacing any they gave before. Empty feedback
    takes theirs back, and returns None. Raises InvalidFeedback or NotFound."""
    facts = await meal_facts(db, actor.household_id, await _meal(db, actor, meal_id))
    check_can_give(actor, facts, await today_for(db, actor, now=now))
    answers = clean(data, facts, actor.id)
    repo = FeedbackRepository(db)
    if answers.is_empty:
        removed = await repo.delete(actor.household_id, meal_id, actor.id)
        await db.commit()
        if removed:
            log.info("feedback removed", extra={"member_id": actor.id, "meal_id": meal_id})
        return None
    try:
        saved = await repo.save(actor.household_id, actor.id, answers, facts, now)
        await db.commit()
    except IntegrityError as exc:
        # A double submit inserted it first; this one replaces it.
        await db.rollback()
        if not is_unique_violation(exc):
            raise
        saved = await repo.save(actor.household_id, actor.id, answers, facts, now)
        await db.commit()
    log.info("feedback saved", extra={"member_id": actor.id, "meal_id": meal_id})
    return saved


async def give_for_event(
    db: AsyncSession, actor: Member, event_id: int, data: FeedbackInput, *, run: int, now: datetime
) -> Feedback | None:
    """``give`` for the meal of a done call, from the Slack modal that names the call's run."""
    return await give(db, actor, await meal_for_event(db, actor, event_id, run=run), data, now=now)


# Reading it back -------------------------------------------------------------------------------


async def awaiting(db: AsyncSession, actor: Member, *, now: datetime) -> list[Awaiting]:
    """Meals from done calls in the feedback window that ``actor`` ate and hasn't given
    feedback on, newest first, for App Home. Logged meals take feedback on the History page."""
    if not actor.active:
        return []
    today = await today_for(db, actor, now=now)
    meals = await MealLogRepository(db).between(actor.household_id, today - timedelta(days=FEEDBACK_DAYS), today)
    mine = [m for m in meals if m.event_id is not None and any(p.id == actor.id for p in m.participants)]
    given = await FeedbackRepository(db).given_by(actor.household_id, actor.id, (m.id for m in mine))
    events = EventRepository(db)
    found: list[Awaiting] = []
    for meal in sorted(mine, key=lambda m: (m.day, m.slot is Slot.DINNER, m.id), reverse=True):
        event = await events.get(actor.household_id, meal.event_id) if meal.event_id is not None else None
        if meal.id in given or event is None or event.state is not EventState.DONE:
            continue
        found.append(Awaiting(meal.id, event.id, event.run, meal.day, meal.slot, meal.item_name, closes_on(meal.day)))
    return found


async def for_meals(
    db: AsyncSession, actor: Member, meals: Iterable[MealLog], *, today: date
) -> dict[int, MealFeedback]:
    """Feedback on each of ``meals`` as ``actor`` may see it, for History and the event page."""
    meals = list(meals)
    every = await FeedbackRepository(db).for_meals(actor.household_id, (m.id for m in meals))
    names = {m.id: m.display_name for m in await MemberRepository(db).list(actor.household_id)}
    by_meal: dict[int, list[Feedback]] = {}
    for f in every:
        by_meal.setdefault(f.meal_id, []).append(f)
    kinds = {item.id: item.kind for item in await CatalogRepository(db).items(actor.household_id)}
    views: dict[int, MealFeedback] = {}
    for meal in meals:
        on_meal = by_meal.get(meal.id, [])
        mine = next((f.answers for f in on_meal if f.member_id == actor.id), None)
        # The public count is about the meal's cook now; each note reaches only the cook it named.
        thanked = any(f.answers.cook_thanks for f in on_meal) and kinds.get(meal.item_id) is ItemKind.HOME
        cook = await _cook(db, actor.household_id, meal) if thanked else None
        views[meal.id] = MealFeedback(
            meal.id,
            can_give(actor, meal.participants, meal.day, today),
            closes_on(meal.day),
            mine,
            thanks_for(on_meal, actor.id, names, cook),
        )
    return views


async def event_meal(
    db: AsyncSession, actor: Member, event_id: int, *, now: datetime
) -> tuple[MealLog, MealFeedback] | None:
    """A done call's meal and its feedback as ``actor`` may see it, for the event page."""
    meals = MealLogRepository(db)
    meal_id = await meals.for_event(actor.household_id, event_id)
    meal = await meals.get(actor.household_id, meal_id) if meal_id is not None else None
    if meal is None:
        return None
    views = await for_meals(db, actor, [meal], today=await today_for(db, actor, now=now))
    return meal, views[meal.id]


async def item_counts(db: AsyncSession, actor: Member, item_ids: Iterable[int] | None = None) -> dict[int, ItemCounts]:
    """Food and place feedback per catalog item, as counts only (FR-49)."""
    return count(await FeedbackRepository(db).for_items(actor.household_id, item_ids))


# The reminder (FR-48) --------------------------------------------------------------------------


def reminders(ctx: CallContext, item_name: str) -> list[note.FeedbackReminder]:
    """One direct message to each active member who ate a done call's meal and turned the
    feedback reminder on. ``ctx`` is the call after it was marked done."""
    info = cc.event_info(ctx)
    closes = closes_on(ctx.event.day)
    people = (ctx.members.get(r.member_id) for r in ctx.participants)
    return [
        note.FeedbackReminder(info, ctx.person(m.id), item_name, closes)
        for m in sorted((m for m in people if m is not None), key=lambda m: m.display_name.casefold())
        if m.active and m.feedback_reminder
    ]
