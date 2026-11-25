# SPDX-License-Identifier: AGPL-3.0-or-later
"""The member's own data, in private only (#253, spec 6.14, **Tools** and **Drafts**): their
levels on Preferences (vetoes and scores), their feedback on meals they ate, their away days,
and their dietary needs and **I can usually** (#278).

``tools`` offers these only in a private conversation, the web page or a DM, never a channel
thread (``private_only``). They're the only tools that send the member's own preferences,
ratings, feedback, away days, dietary needs, or **I can usually** to Anthropic, and only when
the member asks. Dietary needs can reveal health or religion (ADR 0009, amended for #278).

Everything here reads and writes through the ``my_`` use cases the Preferences page and the
feedback form call, ``guests.set_my_needs`` for dietary needs included, as the acting member,
never through the ``member_`` ones an admin uses on Admin > Members (#181). So nothing here
reads another member's rows: no one else's levels, ratings, notes, needs, or capabilities, not
who else ate a meal or who cooked it, and not the household's hints, which go out only through
meal history (#264). A level's ``from_your_ratings`` is what the member's own ratings did to
their own score, as Preferences shows only them.

Each draft records what it changes as it was. **Confirm** refuses when that changed since, under
the write lock, before it runs the use case, which checks everything again: a level set on
Preferences meanwhile, feedback given or changed meanwhile, or away days, dietary needs, or
**I can usually** changed meanwhile.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.contributions import ContributionRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.meals import MealLogRepository
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.assistant_drafts import Draft, DraftKind
from dinnerbell.domain.assistant_own import (
    and_words,
    away_lines,
    away_title,
    can_usually_flags,
    can_usually_lines,
    can_usually_title,
    can_usually_words,
    feedback_lines,
    feedback_title,
    level_label,
    needs_lines,
    needs_title,
    span_words,
    stance_lines,
    stance_title,
    when_words,
)
from dinnerbell.domain.away import Away, check_away
from dinnerbell.domain.catalog import ItemKind, Tag, TagKind, name_key
from dinnerbell.domain.events import EventRuleViolation
from dinnerbell.domain.feedback import (
    FEEDBACK_DAYS,
    Feedback,
    FeedbackInput,
    InvalidFeedback,
    MealFacts,
    PlaceIssue,
    Rating,
    clean,
)
from dinnerbell.domain.meals import newest_first
from dinnerbell.domain.members import Capabilities, InvalidMemberInput, Member
from dinnerbell.domain.preferences import InvalidPreference, Stance, TargetType
from dinnerbell.services import away as away_service
from dinnerbell.services import feedback as feedback_service
from dinnerbell.services import guests as guest_service
from dinnerbell.services import preferences as preference_service
from dinnerbell.services.assistant.acting import ToolRefusal, tool_actor
from dinnerbell.services.assistant.call_drafts import NOTHING_TO_DO, ActionConfirmed, DraftSpec
from dinnerbell.services.errors import NotFound
from dinnerbell.services.event_views import today_for

MAX_LISTED = 25
"""The most tags, items, or meals one lookup returns."""
KIND_WORDS = {ItemKind.RESTAURANT: "restaurant", ItemKind.HOME: "home meal"}
WHAT = ("", "tag", ItemKind.RESTAURANT.value, ItemKind.HOME.value)
"""What a level is on, for ``draft_preference``: any, a tag, a restaurant, or a home meal."""

NO_SUCH_TARGET = (
    "Nothing on the member's Preferences page has that name: no tag, restaurant, or home meal. Look it up with "
    "look_up_my_preferences or look_up_catalog_items, and use a name exactly as it returns it."
)
TWO_TARGETS = "More than one tag, restaurant, or home meal has that name. Ask which, then call again with what."
NO_AWAY = "The member has no away days set, so there's nothing to clear. Say so."
GONE = "That's gone from the catalog, so its level can't change. Ask the assistant again."
STANCE_CHANGED = "Your level for {name} changed since the draft: it's {now} now. Ask the assistant for a new draft."
FEEDBACK_CHANGED = (
    "Your feedback on that meal, or the meal, changed since the draft. Ask the assistant for a new draft."
)
AWAY_CHANGED = "Your away days changed since the draft. Ask the assistant for a new draft."
NO_SUCH_NEED = (
    "{name} isn't one of the household's dietary tags, the only needs Preferences lets the member pick: {known}. "
    "Use a name exactly as look_up_my_preferences returns it in dietary_tags."
)
NO_DIETARY_TAGS = "The household has no dietary tags, so Preferences has no dietary needs to pick. Say so."
ADD_AND_REMOVE = "Name each dietary need once, to add or to take off."
NEEDS_CHANGED = "Your dietary needs changed since the draft. Ask the assistant for a new draft."
CAN_USUALLY_CHANGED = 'Your "I can usually" changed since the draft. Ask the assistant for a new draft.'
PREFERENCES = ("/preferences", "Open Preferences")
AWAY_PAGE = ("/preferences#away", "Open Preferences")
NEEDS_PAGE = ("/preferences#dietary-needs", "Open Preferences")

Use = Callable[[], Awaitable[None]]


def _stale(message: str) -> EventRuleViolation:
    return EventRuleViolation(message, code="stale_draft")


# Levels on Preferences (FR-27, FR-28) ---------------------------------------------------------------


async def my_levels(db: AsyncSession, actor: Member, text: str, *, today: date) -> dict[str, Any]:
    """The member's own levels, from ``preferences.my_preferences``: with ``text``, every tag
    and item whose name contains it, at any level; without, those the member set, and items
    their own ratings move. Then their away days, their dietary needs with the household's
    dietary tags they pick from, as Preferences lists them, and **I can usually** (#278)."""
    mine = await preference_service.my_preferences(db, actor, today=today)
    key = name_key(text)
    tags = [
        {"name": tag.name, "kind": tag.kind.value, "level": mine.stance(TargetType.TAG, tag.id).level}
        for tag in sorted(mine.tags, key=lambda t: (t.key, t.id))
        if _listed(key, tag.key, set_by_member=not mine.stance(TargetType.TAG, tag.id).is_neutral)
    ]
    items = []
    for item in sorted(mine.items, key=lambda i: (i.key, i.id)):
        stance, note = mine.stance(TargetType.ITEM, item.id), mine.rating_notes.get(item.id)
        if _listed(key, item.key, set_by_member=not stance.is_neutral or note is not None):
            items.append(
                {
                    "name": item.name,
                    "kind": item.kind.value,
                    "in_catalog": item.active,
                    "level": stance.level,
                    "from_your_ratings": note,
                }
            )
    me = await fresh(db, actor)
    window = me.away_now(today)
    away = None if window is None else _away_json(window)
    dietary = _dietary(mine.tags)
    return {
        "today": today.isoformat(),
        "tags": tags[:MAX_LISTED],
        "items": items[:MAX_LISTED],
        "more": len(tags) > MAX_LISTED or len(items) > MAX_LISTED or len(dietary) > MAX_LISTED,
        "away": away,
        "dietary_needs": [t.name for t in dietary if t.id in me.dietary_needs],
        "dietary_tags": [t.name for t in dietary[:MAX_LISTED]],
        "can_usually": can_usually_flags(me.capabilities),
    }


def _dietary(tags: Sequence[Tag]) -> list[Tag]:
    """The household's dietary tags, by name: the needs Preferences lets a member pick."""
    return sorted((t for t in tags if t.kind is TagKind.DIETARY), key=lambda t: (t.key, t.id))


def _listed(key: str, name: str, *, set_by_member: bool) -> bool:
    """Whether a lookup lists a tag or item: any whose name contains ``key``, or with no ``key``,
    only those the member's own level or ratings set."""
    return key in name if key else set_by_member


def _away_json(window: Away) -> dict[str, str]:
    return {"first": window.first.isoformat(), "last": window.last.isoformat(), "back": window.back.isoformat()}


@dataclass(frozen=True, slots=True)
class _Target:
    target_type: TargetType
    target_id: int
    name: str
    words: str
    """How the card names it, such as "the tag Mushrooms"."""


async def stance_draft(
    db: AsyncSession, actor: Member, what: str, name: str, after: Stance, *, today: date
) -> DraftSpec:
    """A level on one of the tags and items the Preferences page lists, by its name
    (``preferences.set_my_preference``). The same level again is nothing to do, unless the
    member's earlier ratings move that item's score, which a level they set stops (FR-55)."""
    mine = await preference_service.my_preferences(db, actor, today=today)
    key = name_key(name)
    found: list[_Target] = []
    if what in ("", "tag"):
        found += [_Target(TargetType.TAG, t.id, t.name, f"the tag {t.name}") for t in mine.tags if t.key == key]
    if what != "tag":
        found += [
            _Target(TargetType.ITEM, i.id, i.name, f"the {KIND_WORDS[i.kind]} {i.name}")
            for i in mine.items
            if i.key == key and what in ("", i.kind.value)
        ]
    if not found:
        raise ToolRefusal(NO_SUCH_TARGET)
    if len(found) > 1:
        raise ToolRefusal(TWO_TARGETS)
    target = found[0]
    before = mine.stance(target.target_type, target.target_id)
    resets = target.target_type is TargetType.ITEM and target.target_id in mine.rating_notes and not after.veto
    if before == after and not resets:
        raise ToolRefusal(NOTHING_TO_DO)
    lines = stance_lines(
        target=target.words, tag=target.target_type is TargetType.TAG, before=before, after=after, resets_ratings=resets
    )
    payload = {
        "target_type": target.target_type.value,
        "target_id": target.target_id,
        "name": target.name,
        "level": after.level,
        "before": before.level,
        # A level on an item stops the member's earlier ratings moving it for good (FR-55), so
        # Undo couldn't put that back: such a level stays a draft (#374). Any rating counted since
        # their last level, whatever the Feedback setting, and however small its effect.
        "ratings_move": target.target_type is TargetType.ITEM
        and await preference_service.my_ratings_move(db, actor, target.target_id, today=today),
    }
    return DraftSpec(DraftKind.PREFERENCE, payload, stance_title(target.name, after), lines, None)


async def _confirm_stance(db: AsyncSession, actor: Member, draft: Draft, *, now: datetime, use: Use) -> ActionConfirmed:
    """``set_my_preference`` under the write lock, once the level is still the one the card
    said it changes."""
    payload = draft.payload
    target_type, target_id = TargetType(str(payload["target_type"])), int(payload["target_id"])
    after = Stance.parse(str(payload["level"]))
    async with write_transaction(db):
        await use()
        current = await preference_service.my_stance(db, actor, target_type, target_id)
        if current is None:
            raise NotFound(GONE)
        name, stance = current
        if stance.level != payload["before"]:
            raise _stale(STANCE_CHANGED.format(name=name, now=level_label(stance)))
        try:
            name = await preference_service.set_my_preference(db, actor, target_type, target_id, after, now=now)
        except InvalidPreference as exc:  # pragma: no cover - the draft stores a level Stance parsed
            raise EventRuleViolation(str(exc), code="invalid_preference") from None
        await db.commit()
    return ActionConfirmed(None, f"Confirmed. Your level for {name} is {level_label(after)} now.", link=PREFERENCES)


# Feedback (FR-45 to FR-47) -----------------------------------------------------------------------


def _answers_json(answers: FeedbackInput | None) -> dict[str, Any] | None:
    """The member's own answers, under the names ``draft_feedback`` takes."""
    if answers is None:
        return None
    return {
        "rating": answers.rating.value if answers.rating else "",
        "went_wrong": [i.value for i in answers.issues],
        "what_went_wrong": answers.issue_note or "",
        "thank_cook": answers.cook_thanks,
        "cook_note": answers.cook_note or "",
    }


def stored_answers(answers: FeedbackInput | None) -> dict[str, Any] | None:
    """The member's answers as a feedback draft's payload stores its own, or None."""
    if answers is None:
        return None
    return {
        "rating": answers.rating.value if answers.rating else None,
        "issues": sorted(i.value for i in answers.issues),
        "issue_note": answers.issue_note,
        "cook_thanks": answers.cook_thanks,
        "cook_note": answers.cook_note,
    }


async def my_meals(db: AsyncSession, actor: Member, text: str, *, now: datetime) -> dict[str, Any]:
    """Meals the member ate that still take their feedback, newest first: the ones the feedback
    form opens for them (``feedback.form``), with their own answers. Never who else ate, who
    cooked, anyone else's answers, or the hint."""
    today = await today_for(db, actor, now=now)
    key = name_key(text)
    meals = await MealLogRepository(db).between(actor.household_id, today - timedelta(days=FEEDBACK_DAYS), today)
    theirs = [m for m in meals if any(p.id == actor.id for p in m.participants) and key in name_key(m.item_name)]
    listed = []
    for meal in sorted(theirs, key=newest_first, reverse=True):
        # The window and the participants above are ``check_can_give``'s, so the form opens for
        # each. A meal edited or deleted meanwhile, such as on History, drops out of the list
        # rather than failing the whole lookup.
        try:
            form = await feedback_service.form(db, actor, meal.id, now=now)
        except (InvalidFeedback, NotFound):
            continue
        facts = form.facts
        listed.append(
            {
                "meal": facts.meal_id,
                "day": facts.day.isoformat(),
                "slot": facts.slot.value,
                "name": facts.item_name,
                "kind": facts.item_kind.value,
                "open_until": form.closes_on.isoformat(),
                "cook_to_thank": form.thanks_cook,
                "yours": _answers_json(form.mine),
            }
        )
    return {"today": today.isoformat(), "meals": listed[:MAX_LISTED], "more": len(listed) > MAX_LISTED}


def stamp(saved: Feedback | None) -> str | None:
    """When the member last saved feedback on the meal: what a feedback draft expects."""
    return saved.updated_at.isoformat() if saved is not None else None


async def feedback_draft(
    db: AsyncSession, actor: Member, meal_id: int, data: FeedbackInput, *, now: datetime
) -> DraftSpec:
    """Feedback on a meal the member ate, replacing any they gave (``feedback.give``), checked as
    the form checks it, with the form's words: the window, that they ate it, issues for a
    restaurant only, and a thank-you only to a cook who isn't them. Empty answers take theirs
    back. Answers that change nothing are nothing to do."""
    form = await feedback_service.form(db, actor, meal_id, now=now)
    facts = form.facts
    answers = clean(data, facts, actor.id)
    if answers == (form.mine or FeedbackInput()):
        raise ToolRefusal(NOTHING_TO_DO)
    saved = await FeedbackRepository(db).get(actor.household_id, meal_id, actor.id)
    when = when_words(facts.day, await today_for(db, actor, now=now))
    lines = feedback_lines(item=facts.item_name, slot=facts.slot, when=when, answers=answers, before=form.mine)
    payload = {
        "meal_id": facts.meal_id,
        "item_id": facts.item_id,
        "rating": answers.rating.value if answers.rating else None,
        "issues": [i.value for i in answers.issues],
        "issue_note": answers.issue_note,
        "cook_thanks": answers.cook_thanks,
        "cook_note": answers.cook_note,
        "before": stamp(saved),
        # What it replaces, and whether the member's feedback on the meal has counted toward
        # badges already, so saving again counts nothing new: for acting at once (#374).
        "replaced": stored_answers(form.mine),
        "counted": await ContributionRepository(db).gave_feedback(actor.household_id, actor.id, meal_id),
    }
    return DraftSpec(
        DraftKind.FEEDBACK, payload, feedback_title(facts.item_name, facts.slot, when, answers), lines, None
    )


async def _confirm_feedback(
    db: AsyncSession, actor: Member, draft: Draft, *, now: datetime, use: Use
) -> ActionConfirmed:
    """``feedback.give``, which takes the write lock itself. Under it, ``expect`` uses the draft,
    then checks the meal is the same item and the member's feedback on it is the one the draft
    replaces, so the draft's use commits with the feedback or neither does."""
    payload = draft.payload
    meal_id = int(payload["meal_id"])
    rating = payload.get("rating")
    data = FeedbackInput(
        rating=Rating(str(rating)) if rating else None,
        place_issues=frozenset(PlaceIssue(str(i)) for i in payload.get("issues", [])),
        issue_note=payload.get("issue_note"),
        cook_thanks=bool(payload.get("cook_thanks")),
        cook_note=payload.get("cook_note"),
    )
    named: list[str] = []

    async def expect(mine: Feedback | None, facts: MealFacts) -> None:
        await use()  # first, so a second Confirm hears the draft is used, not that it's stale
        if facts.item_id != int(payload["item_id"]) or stamp(mine) != payload.get("before"):
            raise _stale(FEEDBACK_CHANGED)
        named.append(facts.item_name)

    try:
        saved = await feedback_service.give(db, actor, meal_id, data, now=now, expect=expect)
    except InvalidFeedback as exc:  # such as feedback closed since: the card shows the form's words
        raise EventRuleViolation(str(exc), code="invalid_feedback") from None
    item = named[0]
    link = (f"/history/meals/{meal_id}/feedback", "Open your feedback")
    if saved is None:
        return ActionConfirmed(None, f"Confirmed. Your feedback on {item} is taken back.", link=link)
    return ActionConfirmed(None, f"Confirmed. Thanks for the feedback on {item}! Only you see it.", link=link)


# Away days (FR-63) -------------------------------------------------------------------------------


async def fresh(db: AsyncSession, actor: Member) -> Member:
    """``actor`` as stored now, still with a member's role: their away days as they are."""
    me = await MemberRepository(db).get(actor.household_id, actor.id)
    if me is None:  # pragma: no cover - a member who's gone has no conversation
        raise NotFound("No such member.")
    return tool_actor(me)


def _window(window: Away | None) -> list[str] | None:
    return None if window is None else [window.first.isoformat(), window.last.isoformat()]


async def away_draft(
    db: AsyncSession, actor: Member, first: date | None, last: date | None, *, clear: bool, today: date
) -> DraftSpec:
    """Setting the member's away days (``away.set_my_away``), checked by its rules and words
    (``check_away``), or ending the ones they have (``away.clear_my_away``)."""
    me = await fresh(db, actor)
    current = me.away_now(today)
    window: Away | None = None
    if clear:
        if current is None:
            raise ToolRefusal(NO_AWAY)
    else:
        if first is None or last is None:  # pragma: no cover - the tool sends both
            raise ToolRefusal("Send the first and last day away.")
        window = check_away(first, last, today)
        if window == current:
            raise ToolRefusal(NOTHING_TO_DO)
    payload = {
        "first": window.first.isoformat() if window else None,
        "last": window.last.isoformat() if window else None,
        "clear": clear,
        "before": _window(current),
    }
    return DraftSpec(
        DraftKind.AWAY, payload, away_title(window, today), away_lines(away=window, before=current, today=today), None
    )


async def _confirm_away(db: AsyncSession, actor: Member, draft: Draft, *, now: datetime, use: Use) -> ActionConfirmed:
    """``set_my_away`` or ``clear_my_away`` under the write lock, once the member's away days
    are still the ones the card said it changes."""
    payload = draft.payload
    async with write_transaction(db):
        await use()
        me = await fresh(db, actor)
        today = await today_for(db, me, now=now)
        if _window(me.away_now(today)) != payload.get("before"):
            raise _stale(AWAY_CHANGED)
        if payload.get("clear"):
            await away_service.clear_my_away(db, me, now=now)
            text = "Confirmed. Your away days are cleared."
        else:
            first, last = date.fromisoformat(str(payload["first"])), date.fromisoformat(str(payload["last"]))
            await away_service.set_my_away(db, me, first, last, now=now)
            window = Away(first, last)
            text = f"Confirmed. You're away {span_words(window, today)}, and back {when_words(window.back, today)}."
        await db.commit()  # the draft's use, whatever the use case committed
    return ActionConfirmed(None, text, link=AWAY_PAGE)


# Dietary needs and I can usually (FR-8, FR-29, #278) --------------------------------------------


async def needs_draft(db: AsyncSession, actor: Member, add: Sequence[str], remove: Sequence[str]) -> DraftSpec:
    """Adding or taking off dietary needs (``guests.set_my_needs``), by the names of the
    household's dietary tags: the only needs **Save needs** on Preferences takes, so a name that
    isn't one is refused, never made a tag. Checked as ``set_my_needs`` checks it, in its words:
    at most five, and no change while a call the member is in is voting."""
    me = await fresh(db, actor)
    dietary = await guest_service.dietary_tags(db, me)
    tags = _dietary(dietary)
    if not tags:
        raise ToolRefusal(NO_DIETARY_TAGS)
    by_key = {t.key: t for t in tags}

    def named(names: Sequence[str]) -> set[int]:
        found = set()
        for name in names:
            tag = by_key.get(name_key(name))
            if tag is None:
                raise ToolRefusal(NO_SUCH_NEED.format(name=name, known=", ".join(t.name for t in tags)))
            found.add(tag.id)
        return found

    adding, removing = named(add), named(remove)
    if adding & removing:
        raise ToolRefusal(ADD_AND_REMOVE)
    before = me.dietary_needs
    # A stored ID that's no longer a dietary tag, such as one a tag merge left behind, shows
    # nowhere and would fail ``set_my_needs``' check, so the draft drops it, as a Preferences
    # save does, whose checkboxes list only live tags. Confirm's stale check uses ``before`` as
    # stored.
    live = before & {t.id for t in tags}
    after = frozenset((live | adding) - removing)
    if after == live:
        raise ToolRefusal(NOTHING_TO_DO)
    await guest_service.check_my_needs(db, me, after)
    added = [t.name for t in tags if t.id in after - live]
    removed = [t.name for t in tags if t.id in live - after]
    lines = needs_lines(before=_names(tags, live), after=_names(tags, after))
    payload = {"needs": sorted(after), "before": sorted(before), "loosens": bool(live - after)}
    return DraftSpec(DraftKind.NEEDS, payload, needs_title(added, removed), lines, None)


def _names(tags: Sequence[Tag], ids: frozenset[int]) -> list[str]:
    return [t.name for t in tags if t.id in ids]


async def _confirm_needs(db: AsyncSession, actor: Member, draft: Draft, *, now: datetime, use: Use) -> ActionConfirmed:
    """``set_my_needs`` under the write lock, once the member's needs are still the ones the
    card said it changes."""
    payload = draft.payload
    needs = frozenset(int(n) for n in payload["needs"])
    async with write_transaction(db):
        await use()
        me = await fresh(db, actor)
        if sorted(me.dietary_needs) != payload["before"]:
            raise _stale(NEEDS_CHANGED)
        try:
            await guest_service.set_my_needs(db, me, needs, now=now)
        except InvalidMemberInput as exc:  # such as a dietary tag deleted since: the card shows it
            raise EventRuleViolation(str(exc), code="invalid_needs") from None
        names = _names(_dietary(await guest_service.dietary_tags(db, me)), needs)
        await db.commit()  # the draft's use, whatever the use case committed
    if not names:
        return ActionConfirmed(None, "Confirmed. You have no dietary needs now.", link=NEEDS_PAGE)
    return ActionConfirmed(None, f"Confirmed. Your dietary needs are {and_words(names, '')} now.", link=NEEDS_PAGE)


def capabilities(flags: Mapping[str, object]) -> Capabilities:
    return Capabilities(can_cook=bool(flags["cook"]), can_pickup=bool(flags["pick_up"]), can_order=bool(flags["order"]))


async def can_usually_draft(db: AsyncSession, actor: Member, changes: Mapping[str, bool | None]) -> DraftSpec:
    """Turning **I can usually**'s Cook, Pick up, or Order on or off (``update_my_settings``).
    ``changes`` maps ``cook``, ``pick_up``, and ``order`` to what each becomes, or None to keep
    it. The member's reminders, which the same form saves, stay as they are."""
    me = await fresh(db, actor)
    before = can_usually_flags(me.capabilities)
    after = {name: before[name] if changes.get(name) is None else bool(changes[name]) for name in before}
    if after == before:
        raise ToolRefusal(NOTHING_TO_DO)
    was, now = capabilities(before), capabilities(after)
    payload = {"after": after, "before": before}
    return DraftSpec(
        DraftKind.CAN_USUALLY, payload, can_usually_title(was, now), can_usually_lines(before=was, after=now), None
    )


async def _confirm_can_usually(
    db: AsyncSession, actor: Member, draft: Draft, *, now: datetime, use: Use
) -> ActionConfirmed:
    """``update_my_settings`` under the write lock, once **I can usually** is still what the card
    said it changes, with the reminders as they are then, so a reminder saved meanwhile stays."""
    payload = draft.payload
    after = capabilities(payload["after"])
    async with write_transaction(db):
        await use()
        me = await fresh(db, actor)
        if can_usually_flags(me.capabilities) != payload["before"]:
            raise _stale(CAN_USUALLY_CHANGED)
        settings = preference_service.MySettings(
            capabilities=after, mute_dm_reminders=me.mute_dm_reminders, feedback_reminder=me.feedback_reminder
        )
        await preference_service.update_my_settings(db, me, settings, now=now)
        await db.commit()  # the draft's use, whatever the use case committed
    text = f'Confirmed. On Preferences, "I can usually" is {can_usually_words(after)} now.'
    return ActionConfirmed(None, text, link=PREFERENCES)


# Confirm -----------------------------------------------------------------------------------------


async def confirm(db: AsyncSession, actor: Member, draft: Draft, *, now: datetime, use: Use) -> ActionConfirmed:
    """**Confirm** on a draft of the member's own data, as ``actor``, who is ``tool_actor`` of
    its member: the use case the Preferences page or the feedback form runs. ``use`` uses the
    draft in that use case's transaction, so a refusal leaves it unused."""
    if draft.kind is DraftKind.PREFERENCE:
        return await _confirm_stance(db, actor, draft, now=now, use=use)
    if draft.kind is DraftKind.FEEDBACK:
        return await _confirm_feedback(db, actor, draft, now=now, use=use)
    if draft.kind is DraftKind.NEEDS:
        return await _confirm_needs(db, actor, draft, now=now, use=use)
    if draft.kind is DraftKind.CAN_USUALLY:
        return await _confirm_can_usually(db, actor, draft, now=now, use=use)
    return await _confirm_away(db, actor, draft, now=now, use=use)
