# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catalog edits and the meal log through the assistant (#254, spec 6.14, **Tools** and
**Drafts**), in any conversation: edit a catalog item, take one off the catalog or put it back
(FR-23), and log, edit, or delete a meal the member logged (spec 5.6).

Each draft checks now what its use case would refuse, as ``tool_actor`` of the member, and the
use case checks again on **Confirm**: ``catalog.update_item`` and ``catalog.set_item_active``, which
the Catalog item form calls, and ``meals.log_meal``, ``meals.update_meal``, and
``meals.delete_meal``, which History's forms call. None of them is an admin's: a member edits any
item, and the meals they logged, as on the web.

People are named only by the conversation's labels, such as "Member 2", and resolved to members at
draft time from its pseudonym map. Someone named as a cook, or as having eaten, must be an active
member: a label nobody has, a guest's, and a deactivated member's get the same refusal, which names
nobody. A stored line marks each person by member ID (``person``), so the card shows its member
their names, and the model their labels. A catalog edit's line shows an item's link, order link,
and notes as they are now only on the card (``held``): ``look_up_catalog_items`` never sends them,
and a draft's summary doesn't either.

**Confirm** runs under the write lock, uses the draft first, and refuses a draft whose item or
meal isn't as drafted any more: an item's every field (``item_snapshot``), and a meal's
``form_stamp``, which History's own forms send.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.meals import MealLogRepository
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.assistant import Pseudonyms
from dinnerbell.domain.assistant_catalog import (
    MealShown,
    active_lines,
    active_title,
    changed_fields,
    clean_snapshot,
    edit_lines,
    item_snapshot,
    log_lines,
    log_title,
    meal_delete_lines,
    meal_edit_lines,
    meal_title,
    past_day_words,
)
from dinnerbell.domain.assistant_coordinating import plain
from dinnerbell.domain.assistant_drafts import Draft, DraftKind
from dinnerbell.domain.catalog import (
    CatalogItem,
    CatalogRuleViolation,
    InvalidCatalogInput,
    ItemInput,
    ItemKind,
    Meal,
    Mode,
    clean_item,
    name_key,
)
from dinnerbell.domain.events import EventRuleViolation
from dinnerbell.domain.meals import CALL_PLACE_FIXED, InvalidMeal, MealInput, Slot, StaleMeal, can_change, check_meal
from dinnerbell.domain.members import Member
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services import meals as meal_service
from dinnerbell.services.assistant.acting import ToolRefusal
from dinnerbell.services.assistant.call_drafts import NOTHING_TO_DO, ActionConfirmed, DraftSpec
from dinnerbell.services.errors import Forbidden

Labeler = Callable[[int], str]
Use = Callable[[], Awaitable[None]]

MAX_MEALS = 25
NO_SUCH_ITEM = (
    "The catalog has no item by that name. Look it up with look_up_catalog_items, and use a name exactly as it "
    "returns it."
)
NO_ACTIVE_ITEM = "The catalog has no active item by that name. Look it up with look_up_catalog_items."
TWO_ITEMS = "A restaurant and a home meal share that name. Send kind too: restaurant or home."
LABEL_ONLY = "Name each person by a label such as Member 2."
NOT_SOMEONE = (
    "{label} can't be named there. Name people only by labels the conversation gives, such as the member's own "
    "or a Slack mention's, and only active members of the household."
)
"""The same for a label nobody has, a guest's, and a deactivated member's: it names nobody."""
NOT_A_COOK = "{label} isn't one of its cooks."
NOT_ON_THE_MEAL = "{label} isn't on that meal."
NO_COOKS = "Only a home meal has cooks."
HOME_MODES = "A home meal is always cooked, so leave modes empty."
NO_SERVINGS = "Only a home meal has servings."
NO_ORDER_LINK = "Only a restaurant has an order link."
NO_SUCH_TAG = "The household has no tag named {name}. New tags are added on the Catalog page."
NOT_TAGGED = "It has no tag named {name}."
BOTH = "Send a new {field} or clear it, not both."
ALREADY_ON = "It's on the catalog already, so there's nothing to draft. Say so."
ALREADY_OFF = "It's off the catalog already, so there's nothing to draft. Say so."
DELETE_ALONE = "To delete a meal, leave every change empty."
ACTIVE_ALONE = "To take an item off the catalog or put it back, leave every other change empty."
NEW_MEAL = "To log a new meal, send meal 0 with its day, slot, and name, and who ate in add_people; nothing else."
ITEM_CHANGED = "That item changed since the draft, so nothing changed. Ask the assistant for a new draft."
MEAL_CHANGED = "That meal changed since the draft, so nothing changed. Ask the assistant for a new draft."
_LABEL = re.compile(r"Member (\d{1,6})")
HELD = ("url", "order_url", "notes")


def _stale(message: str) -> EventRuleViolation:
    return EventRuleViolation(message, code="stale_draft")


def _refused(exc: Exception) -> EventRuleViolation:
    """A use case's refusal at Confirm, in its own words, as the card shows refusals."""
    return EventRuleViolation(str(exc), code="refused")


# People and items --------------------------------------------------------------------------------


def labeled(labels: Pseudonyms, label: str) -> int | None:
    """The member ``label`` names in this conversation, or None when its map has nobody by it."""
    match = _LABEL.fullmatch(" ".join(label.split()))
    if match is None:
        raise ToolRefusal(LABEL_ONLY)
    number = int(match.group(1))
    return next((member_id for member_id, n in labels.numbers.items() if n == number), None)


def adding(labels: Pseudonyms, wanted: Iterable[str], members: Mapping[int, Member]) -> set[int]:
    """The active members ``wanted`` names by label. Anyone else is refused the same way."""
    found: set[int] = set()
    for label in wanted:
        member_id = labeled(labels, label)
        member = members.get(member_id) if member_id is not None else None
        if member is None or not member.is_member:
            raise ToolRefusal(NOT_SOMEONE.format(label=plain(" ".join(label.split()))))
        found.add(member.id)
    return found


def removing(labels: Pseudonyms, wanted: Iterable[str], current: Iterable[int], refusal: str) -> set[int]:
    """Who of ``current`` ``wanted`` names by label, or ``refusal`` for a label that names nobody there."""
    there = set(current)
    found: set[int] = set()
    for label in wanted:
        member_id = labeled(labels, label)
        if member_id is None or member_id not in there:
            raise ToolRefusal(refusal.format(label=plain(" ".join(label.split()))))
        found.add(member_id)
    return found


async def _members(db: AsyncSession, actor: Member) -> dict[int, Member]:
    return {m.id: m for m in await MemberRepository(db).list(actor.household_id)}


async def find_item(
    db: AsyncSession, actor: Member, name: str, kind: ItemKind | None, *, active_only: bool = False
) -> CatalogItem:
    """The catalog item named ``name``, ignoring case, in either state unless ``active_only``."""
    key = name_key(name)
    found = [
        i
        for i in await CatalogRepository(db).items(actor.household_id)
        if i.key == key and (kind is None or i.kind is kind) and (i.active or not active_only)
    ]
    if not found:
        raise ToolRefusal(NO_ACTIVE_ITEM if active_only else NO_SUCH_ITEM)
    if len(found) > 1:
        raise ToolRefusal(TWO_ITEMS)
    return found[0]


# Catalog edits (FR-23) --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemEdit:
    """What the member asked to change: each part empty to keep it. ``clear`` names the link,
    order link, notes, or servings to remove."""

    name: str = ""
    modes: frozenset[Mode] = frozenset()
    meals: frozenset[Meal] = frozenset()
    add_tags: tuple[str, ...] = ()
    remove_tags: tuple[str, ...] = ()
    add_cooks: tuple[str, ...] = ()
    remove_cooks: tuple[str, ...] = ()
    url: str = ""
    order_url: str = ""
    notes: str = ""
    servings: int = 0
    clear: frozenset[str] = field(default_factory=frozenset)


def _text_field(item: CatalogItem, edit: ItemEdit, name: str) -> str | None:
    new: str = getattr(edit, name)
    if new.strip() and name in edit.clear:
        raise ToolRefusal(BOTH.format(field=name))
    if name in edit.clear:
        return None
    current: str | None = getattr(item, name)
    return new if new.strip() else current


async def edit_item(
    db: AsyncSession, actor: Member, name: str, kind: ItemKind | None, change: ItemEdit, *, labels: Pseudonyms
) -> DraftSpec:
    """An edit of the item ``name``, as ``catalog.update_item`` takes it: validated now with the
    rule it applies (``clean_item``), the cooks active members or ones the item has already, and
    its name not another item's. The draft holds the item as it is and what Confirm saves."""
    item = await find_item(db, actor, name, kind)
    home = item.kind is ItemKind.HOME
    if change.modes and home:
        raise ToolRefusal(HOME_MODES)
    if (change.add_cooks or change.remove_cooks) and not home:
        raise ToolRefusal(NO_COOKS)
    if (change.servings or "servings" in change.clear) and not home:
        raise ToolRefusal(NO_SERVINGS)
    if (change.order_url.strip() or "order_url" in change.clear) and home:
        raise ToolRefusal(NO_ORDER_LINK)
    repo = CatalogRepository(db)
    tags = await repo.tags(actor.household_id)
    by_key = {t.key: t for t in tags}
    tag_ids = set(item.tag_ids)
    for tag_name in change.remove_tags:
        tag = by_key.get(name_key(tag_name))
        if tag is None or tag.id not in tag_ids:
            raise ToolRefusal(NOT_TAGGED.format(name=plain(tag_name)))
        tag_ids.discard(tag.id)
    for tag_name in change.add_tags:
        tag = by_key.get(name_key(tag_name))
        if tag is None:
            raise ToolRefusal(NO_SUCH_TAG.format(name=plain(tag_name)))
        tag_ids.add(tag.id)
    members = await _members(db, actor)
    cooks = set(item.cook_ids) - removing(labels, change.remove_cooks, item.cook_ids, NOT_A_COOK)
    cooks |= adding(labels, change.add_cooks, members)
    if change.servings and "servings" in change.clear:
        raise ToolRefusal(BOTH.format(field="servings"))
    servings = None if "servings" in change.clear else (change.servings or item.default_servings)
    data = ItemInput(
        kind=item.kind,
        name=change.name or item.name,
        modes=change.modes or item.modes,
        meals=change.meals or item.meals,
        tag_ids=frozenset(tag_ids),
        cook_ids=frozenset(cooks),
        url=_text_field(item, change, "url"),
        order_url=_text_field(item, change, "order_url"),
        notes=_text_field(item, change, "notes"),
        default_servings=servings,
    )
    # As ``catalog._clean``: active members may cook it, and so may anyone it lists already.
    eligible = {m.id for m in members.values() if m.is_member} | item.cook_ids
    clean = clean_item(data, tags, eligible)
    same = await repo.item_by_name(actor.household_id, item.kind, clean.name)
    if same is not None and same.id != item.id:
        raise CatalogRuleViolation(f"A {item.kind.label.lower()} named {clean.name} already exists.")
    before, after = item_snapshot(item), clean_snapshot(clean)
    # A held field the member set gets a line even when it's the value it has, so a guess at
    # notes or a link the model never read can't learn whether it matched from a refusal.
    sent = {f for f in HELD if getattr(change, f).strip() or f in change.clear}
    if not changed_fields(before, after) and not sent:
        raise ToolRefusal(NOTHING_TO_DO)
    lines = edit_lines(before, after, {t.id: t.name for t in tags}, sent=sent)
    payload = {"item_id": item.id, "before": before, "after": after}
    return DraftSpec(DraftKind.ITEM_EDIT, payload, f"Edit {plain(item.name)}", lines, None)


async def item_active(db: AsyncSession, actor: Member, name: str, kind: ItemKind | None, *, active: bool) -> DraftSpec:
    """Taking the item ``name`` off the catalog, or putting it back (``catalog.set_item_active``)."""
    item = await find_item(db, actor, name, kind)
    if item.active == active:
        raise ToolRefusal(ALREADY_ON if active else ALREADY_OFF)
    payload = {"item_id": item.id, "active": active, "before": item_snapshot(item)}
    title = active_title(item.name, active=active)
    return DraftSpec(DraftKind.ITEM_ACTIVE, payload, title, active_lines(item.name, active=active), None)


def _item_input(kind: ItemKind, after: Mapping[str, Any]) -> ItemInput:
    return ItemInput(
        kind=kind,
        name=str(after["name"]),
        modes=frozenset(Mode(str(m)) for m in after["modes"]),
        meals=frozenset(Meal(str(m)) for m in after["meals"]),
        tag_ids=frozenset(int(i) for i in after["tag_ids"]),
        cook_ids=frozenset(int(i) for i in after["cook_ids"]),
        url=after.get("url"),
        order_url=after.get("order_url"),
        notes=after.get("notes"),
        default_servings=after.get("default_servings"),
    )


async def _as_drafted(db: AsyncSession, actor: Member, payload: Mapping[str, Any]) -> CatalogItem:
    item = await CatalogRepository(db).item(actor.household_id, int(payload["item_id"]))
    if item is None or item_snapshot(item) != dict(payload["before"]):
        raise _stale(ITEM_CHANGED)
    return item


async def _confirm_item(db: AsyncSession, actor: Member, draft: Draft, *, now: datetime, use: Use) -> ActionConfirmed:
    """``update_item`` or ``set_item_active`` under the write lock, once the item is as drafted.
    Each commits the draft's use with its change; a refusal rolls both back."""
    payload = draft.payload
    async with write_transaction(db):
        await use()
        item = await _as_drafted(db, actor, payload)
        try:
            if draft.kind is DraftKind.ITEM_EDIT:
                data = _item_input(item.kind, payload["after"])
                saved = await catalog_service.update_item(db, actor, item.id, data, now=now)
                text = f"Confirmed. {saved.name} is updated."
            else:
                active = bool(payload["active"])
                saved = await catalog_service.set_item_active(db, actor, item.id, active, now=now)
                where = "back on the catalog" if active else "off the catalog"
                text = f"Confirmed. {saved.name} is {where}."
        except (InvalidCatalogInput, CatalogRuleViolation) as exc:
            raise _refused(exc) from None
    return ActionConfirmed(None, text, link=(f"/catalog/items/{saved.id}", "Open it in the catalog"))


# The meal log (spec 5.6) ------------------------------------------------------------------------

HISTORY_LINK = ("/history", "Open History")


def _who(members: Mapping[int, Member], member_id: int, label: Labeler) -> str:
    member = members.get(member_id)
    if member is None:
        return "a former member"
    return "a guest" if member.is_guest else label(member_id)


PEOPLE_IN_PRIVATE = (
    "Who ate shows only in a direct message with Dinnerbell or on the Assistant page. To change who ate, the "
    "member names them by mentioning them."
)
"""What a lookup in a channel thread says instead of who ate: its reply posts for everyone, and a
label it gave would join the thread's shared pseudonyms."""


async def my_meals(
    db: AsyncSession, actor: Member, name: str, *, private: bool, label: Labeler, today: date
) -> dict[str, Any]:
    """The meals the member logged, which History lets them edit or delete, newest first, whose
    item's name contains ``name``: each meal's number, day, meal, item, whether its place is the
    call's (``meals.call_place``), and, in a private conversation, who ate, by label. In a channel
    thread, no one who ate gets a label, and the reply can't name them. Never anyone's feedback."""
    key = name_key(name)
    mine = [
        m
        for m in await MealLogRepository(db).logged_by(actor.household_id, actor.id)
        if can_change(actor, m) and key in name_key(m.item_name)
    ]
    members = await _members(db, actor) if private else {}
    listed: list[dict[str, Any]] = []
    for m in mine[:MAX_MEALS]:
        shown: dict[str, Any] = {
            "meal": m.id,
            "day": m.day.isoformat(),
            "slot": m.slot.value,
            "name": m.item_name,
            "from_a_call": await meal_service.call_place(db, actor, m) is not None,
        }
        if private:
            shown["people"] = [_who(members, p.id, label) for p in m.participants]
        listed.append(shown)
    found: dict[str, Any] = {"today": today.isoformat(), "meals": listed, "more": len(mine) > MAX_MEALS}
    return found if private else found | {"people": PEOPLE_IN_PRIVATE}


async def log_meal(
    db: AsyncSession,
    actor: Member,
    name: str,
    kind: ItemKind | None,
    *,
    slot: Slot,
    day: date,
    people: Iterable[str],
    labels: Pseudonyms,
    today: date,
) -> DraftSpec:
    """Logging an active item for ``slot`` on ``day`` (``meals.log_meal``), as History's form
    takes it: who ate by label, or the member alone, checked now with its rule (``check_meal``)."""
    item = await find_item(db, actor, name, kind, active_only=True)
    members = await _members(db, actor)
    who = adding(labels, people, members) or {actor.id}
    data = MealInput(day, slot, item.id, frozenset(who))
    check_meal(data, today=today, active_member_ids={m.id for m in members.values() if m.is_member}, item_exists=True)
    when = past_day_words(day, today)
    payload = {"day": day.isoformat(), "slot": slot.value, "item_id": item.id, "people": sorted(who)}
    return DraftSpec(
        DraftKind.MEAL_LOG,
        payload,
        log_title(item.name, slot, when),
        log_lines(item.name, slot=slot, when=when, people=who),
        None,
    )


@dataclass(frozen=True, slots=True)
class MealChange:
    """What the member asked to change on a meal they logged: each part empty to keep it."""

    day: date | None = None
    slot: Slot | None = None
    name: str = ""
    kind: ItemKind | None = None
    add_people: tuple[str, ...] = ()
    remove_people: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return self == MealChange()


async def change_meal(
    db: AsyncSession,
    actor: Member,
    meal_id: int,
    change: MealChange,
    *,
    delete: bool,
    labels: Pseudonyms,
    today: date,
) -> DraftSpec:
    """Editing (``meals.update_meal``) or deleting (``meals.delete_meal``) a meal the member
    logged, checked now as History checks: theirs only, a call's place fixed, and the meal's
    rules. The draft holds the meal's ``form_stamp``, as History's forms do."""
    meal = await meal_service.get_meal(db, actor, meal_id)
    if not can_change(actor, meal):
        raise Forbidden(meal_service.ONLY_YOURS)
    before = MealShown(meal.day, meal.slot, meal.item_id, meal.item_name, frozenset(p.id for p in meal.participants))
    if delete:
        if not change.empty:
            raise ToolRefusal(DELETE_ALONE)
        payload = {"meal_id": meal.id, "shown": meal.form_stamp}
        title = meal_title(before, today, delete=True)
        return DraftSpec(DraftKind.MEAL_DELETE, payload, title, meal_delete_lines(before, today), None)
    item_id, item_name = meal.item_id, meal.item_name
    if change.name:
        item = await find_item(db, actor, change.name, change.kind, active_only=True)
        if item.id != meal.item_id and await meal_service.call_place(db, actor, meal) is not None:
            raise InvalidMeal(CALL_PLACE_FIXED)
        item_id, item_name = item.id, item.name
    members = await _members(db, actor)
    people = (before.people - removing(labels, change.remove_people, before.people, NOT_ON_THE_MEAL)) | adding(
        labels, change.add_people, members
    )
    data = MealInput(change.day or meal.day, change.slot or meal.slot, item_id, frozenset(people))
    eligible = before.people | {m.id for m in members.values() if m.is_member}  # as ``meals._eligible``
    check_meal(data, today=today, active_member_ids=eligible, item_exists=True, original_day=meal.day)
    after = MealShown(data.day, data.slot, item_id, item_name, data.participant_ids)
    if (after.day, after.slot, after.item_id, after.people) == (before.day, before.slot, before.item_id, before.people):
        raise ToolRefusal(NOTHING_TO_DO)
    payload = {
        "meal_id": meal.id,
        "shown": meal.form_stamp,
        "day": data.day.isoformat(),
        "slot": data.slot.value,
        "item_id": item_id,
        "people": sorted(people),
    }
    return DraftSpec(
        DraftKind.MEAL_EDIT, payload, meal_title(before, today), meal_edit_lines(before, after, today), None
    )


def _meal_input(payload: Mapping[str, Any]) -> MealInput:
    return MealInput(
        date.fromisoformat(str(payload["day"])),
        Slot(str(payload["slot"])),
        int(payload["item_id"]),
        frozenset(int(i) for i in payload["people"]),
    )


async def _confirm_meal(
    db: AsyncSession, actor: Member, draft: Draft, *, today: date, now: datetime, use: Use
) -> ActionConfirmed:
    """The History use case under the write lock, which uses the draft first: ``log_meal`` in a
    transaction of its own, and ``update_meal`` and ``delete_meal`` in theirs, through ``expect``,
    with the ``form_stamp`` the draft holds, so a meal changed since is refused."""
    payload = draft.payload
    try:
        if draft.kind is DraftKind.MEAL_LOG:
            async with write_transaction(db):
                await use()
                logged = await meal_service.log_meal(db, actor, _meal_input(payload), today=today, now=now)
            when = past_day_words(logged.day, today)
            return ActionConfirmed(
                None, f"Confirmed. {logged.item_name} is logged for {logged.slot.value} {when}.", link=HISTORY_LINK
            )
        meal_id, shown = int(payload["meal_id"]), str(payload["shown"])
        if draft.kind is DraftKind.MEAL_EDIT:
            data = _meal_input(payload)
            await meal_service.update_meal(db, actor, meal_id, data, today=today, now=now, shown=shown, expect=use)
            return ActionConfirmed(None, "Confirmed. The meal is updated in History.", link=HISTORY_LINK)
        await meal_service.delete_meal(db, actor, meal_id, now=now, shown=shown, expect=use)
        return ActionConfirmed(None, "Confirmed. The meal is deleted from History.", link=HISTORY_LINK)
    except StaleMeal:
        raise _stale(MEAL_CHANGED) from None
    except InvalidMeal as exc:
        raise _refused(exc) from None


async def confirm(
    db: AsyncSession, actor: Member, draft: Draft, *, today: date, now: datetime, use: Use
) -> ActionConfirmed:
    """**Confirm** on a catalog edit or a meal-log draft, as ``actor``, who is ``tool_actor`` of
    its member: the use case the Catalog item form or History's forms run."""
    if draft.kind in {DraftKind.ITEM_EDIT, DraftKind.ITEM_ACTIVE}:
        return await _confirm_item(db, actor, draft, now=now, use=use)
    return await _confirm_meal(db, actor, draft, today=today, now=now, use=use)
