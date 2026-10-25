# SPDX-License-Identifier: AGPL-3.0-or-later
"""Modals and the App Home tab (spec section 10): the Details, Add proposal, Call it off, Close
now, and feedback modals, and each member's Home with today's calls, those coming up, and meals
awaiting feedback.

Modals carry their event and origin in ``private_metadata`` (see ``ids.ViewOrigin``). Input
blocks use fixed block and action IDs, which ``parse_details`` and ``parse_proposal`` read
back as untrusted input.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, time
from typing import Any

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.blocks import (
    MAX_MODAL_TITLE,
    MAX_OPTIONS,
    MAX_VIEW_BLOCKS,
    Block,
    actions,
    button,
    clip,
    confirm,
    context,
    day_label,
    divider,
    escape,
    header,
    local_clock,
    option,
    plain,
    plural,
    section,
)
from dinnerbell.channels.slack.messages import (
    ANSWER_WORDS,
    ANSWERS,
    CALL_OFF,
    CANT_MAKE_IT,
    CLOSE_LABELS,
    OFFER_VERBS,
    POST_NOW,
    choice_button,
    mark_done_button,
)
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import CatalogItem, ItemKind, Mode, Tag
from dinnerbell.domain.events import (
    CANCEL_NOTE_MAX,
    MAX_GUESTS,
    MAX_NOTE_LENGTH,
    MAX_SUGGESTION_LENGTH,
    Details,
    EventState,
    RoleOffer,
    Status,
    Window,
)
from dinnerbell.domain.feedback import COOK_NOTE_MAX, FeedbackInput, PlaceIssue, Rating
from dinnerbell.domain.meals import MealInput, Slot
from dinnerbell.domain.offers import MAX_CAPACITY, replaces_call
from dinnerbell.services.event_views import EventView, TodayView
from dinnerbell.services.feedback import MAX_AWAITING, Awaiting, FeedbackForm
from dinnerbell.services.meals import LogForm
from dinnerbell.services.offers import OfferForm, OfferInput

VALUE = "value"
"""Every input's action ID; blocks are told apart by block ID."""
STATUS_WORDS = {Status.IN: "You're in.", Status.MAYBE: "You said maybe.", Status.OUT: "You're out this time."}
OFFER_WORDS = {RoleOffer.COOK: "cook", RoleOffer.PICKUP: "pick up", RoleOffer.ORDER: "order"}


def _modal(
    title: str,
    callback_id: str,
    origin: ids.ViewOrigin | ids.FormOrigin,
    blocks: list[Block],
    submit: str,
    close: str = "Cancel",
) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": callback_id,
        "private_metadata": origin.encode(),
        "title": plain(title, MAX_MODAL_TITLE),
        "submit": plain(submit, MAX_MODAL_TITLE),
        "close": plain(close, MAX_MODAL_TITLE),
        "blocks": blocks[:MAX_VIEW_BLOCKS],
    }


def _input(block_id: str, label: str, element: Block, *, optional: bool = True, hint: str | None = None) -> Block:
    block: Block = {
        "type": "input",
        "block_id": block_id,
        "label": plain(label, 2000),
        "element": {**element, "action_id": VALUE},
        "optional": optional,
    }
    if hint:
        block["hint"] = plain(hint, 2000)
    return block


def _hhmm(at: time | None) -> dict[str, str]:
    return {"initial_time": f"{at.hour:02d}:{at.minute:02d}"} if at else {}


def _item_label(item: CatalogItem) -> str:
    return item.name


def _item_groups(items: Iterable[CatalogItem], keep: Iterable[int] = ()) -> tuple[list[Block], list[dict[str, Any]]]:
    """Catalog items as option groups (home meals, restaurants), at most 100 each. Items in
    ``keep`` are always listed, so a saved choice survives a big catalog. Returns the groups
    and the options for ``keep``."""
    keep_ids = set(keep)
    groups: list[Block] = []
    chosen: list[dict[str, Any]] = []
    for kind, label in ((ItemKind.HOME, "Home meals"), (ItemKind.RESTAURANT, "Restaurants")):
        of_kind = [i for i in items if i.kind is kind]
        ordered = [i for i in of_kind if i.id in keep_ids] + [i for i in of_kind if i.id not in keep_ids]
        options = [option(_item_label(i), str(i.id)) for i in ordered[:MAX_OPTIONS]]
        chosen += [o for o, i in zip(options, ordered, strict=False) if i.id in keep_ids]
        if options:
            groups.append({"label": plain(label, 75), "options": options})
    return groups, chosen


def _tag_options(tags: Iterable[Tag]) -> list[dict[str, Any]]:
    return [option(t.name, str(t.id)) for t in list(tags)[:MAX_OPTIONS]]


def _intro(v: EventView) -> Block:
    return context(f"*{v.event.slot.value.capitalize()} · {day_label(v.event.day)}*")


# Details -----------------------------------------------------------------------------------------


def details_modal(v: EventView, origin: ids.ViewOrigin) -> dict[str, Any]:
    """FR-8: guests, their dietary needs, an eat window, a suggestion, and a note, prefilled
    with the member's current response."""
    mine = v.mine
    blocks: list[Block] = [_intro(v)]
    guests: Block = {
        "type": "number_input",
        "is_decimal_allowed": False,
        "min_value": "0",
        "max_value": str(MAX_GUESTS),
    }
    if mine and mine.guests:
        guests["initial_value"] = str(mine.guests)
    blocks.append(_input("guests", "Guests you're bringing", guests))
    if v.dietary_tags:
        needs: Block = {"type": "multi_static_select", "options": _tag_options(v.dietary_tags)}
        chosen = [o for o in needs["options"] if mine and int(o["value"]) in mine.guest_needs]
        if chosen:
            needs["initial_options"] = chosen
        blocks.append(_input("guest_needs", "Guests' dietary needs", needs))
    blocks.append(
        _input("eat_from", "Earliest you can eat", {"type": "timepicker", **_hhmm(mine.eat_from if mine else None)})
    )
    blocks.append(
        _input("eat_until", "Latest you can eat", {"type": "timepicker", **_hhmm(mine.eat_until if mine else None)})
    )
    groups, chosen_items = _item_groups(v.items, v.my_suggestion_ids[:1])
    if groups:
        select: Block = {
            "type": "static_select",
            "option_groups": groups,
            "placeholder": plain("Pick from the catalog"),
        }
        if chosen_items:
            select["initial_option"] = chosen_items[0]
        blocks.append(_input("suggest_item", "Suggest something", select))
    text: Block = {"type": "plain_text_input", "max_length": MAX_SUGGESTION_LENGTH}
    if v.my_suggestion_texts:
        text["initial_value"] = v.my_suggestion_texts[0]
    blocks.append(_input("suggest_text", "Or suggest something new", text, hint="An idea that isn't in the catalog."))
    note: Block = {"type": "plain_text_input", "max_length": MAX_NOTE_LENGTH, "multiline": True}
    if mine and mine.note:
        note["initial_value"] = mine.note
    blocks.append(_input("note", "Note", note))
    return _modal("Your details", ids.DETAILS_VIEW, origin, blocks, "Save")


def _state(values: Any, block_id: str) -> dict[str, Any]:
    block = values.get(block_id) if isinstance(values, dict) else None
    entry = block.get(VALUE) if isinstance(block, dict) else None
    return entry if isinstance(entry, dict) else {}


def _text(values: Any, block_id: str) -> str:
    value = _state(values, block_id).get("value")
    return value if isinstance(value, str) else ""


def _picked(values: Any, block_id: str) -> int | None:
    selected = _state(values, block_id).get("selected_option")
    if not isinstance(selected, dict):
        return None
    return ids.parse_option_id(selected.get("value"))


def _picked_many(values: Any, block_id: str) -> frozenset[int]:
    selected = _state(values, block_id).get("selected_options") or []
    if not isinstance(selected, list):
        raise ids.BadValue("bad selection")
    return frozenset(ids.parse_option_id(o.get("value") if isinstance(o, dict) else None) for o in selected)


def _time(values: Any, block_id: str) -> time | None:
    raw = _state(values, block_id).get("selected_time")
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw) != 5:
        raise ids.BadValue("bad time")
    try:
        return time.fromisoformat(raw)
    except ValueError:
        raise ids.BadValue("bad time") from None


class FormError(ValueError):
    """Input to show next to a modal field. ``block_id`` names the field."""

    def __init__(self, block_id: str, message: str) -> None:
        super().__init__(message)
        self.block_id = block_id


def parse_details(values: Any) -> Details:
    """The Details modal's state as ``Details``. Raises FormError or ids.BadValue."""
    raw_guests = _text(values, "guests").strip()
    if raw_guests and not (raw_guests.isascii() and raw_guests.isdigit() and len(raw_guests) <= 3):
        raise FormError("guests", f"Enter a whole number from 0 to {MAX_GUESTS}.")
    guests = int(raw_guests) if raw_guests else 0
    if guests > MAX_GUESTS:
        raise FormError("guests", f"Enter a whole number from 0 to {MAX_GUESTS}.")
    eat_from, eat_until = _time(values, "eat_from"), _time(values, "eat_until")
    if eat_from and eat_until and eat_until < eat_from:
        raise FormError("eat_until", "The latest time can't be before the earliest.")
    item = _picked(values, "suggest_item")
    text = _text(values, "suggest_text").strip()
    return Details(
        guests=guests,
        guest_needs=_picked_many(values, "guest_needs"),
        eat_from=eat_from,
        eat_until=eat_until,
        suggestion_item_ids=(item,) if item is not None else (),
        suggestion_texts=(text,) if text else (),
        note=_text(values, "note") or None,
    )


# Add proposal ------------------------------------------------------------------------------------


def proposal_modal(v: EventView, origin: ids.ViewOrigin) -> dict[str, Any]:
    """FR-13: a catalog item, or something new with a cuisine, and how to get it."""
    modes = [option(m.label, m.value) for m in Mode]
    blocks: list[Block] = [
        _intro(v),
        _input("mode", "How", {"type": "static_select", "options": modes}, optional=False),
    ]
    groups, _ = _item_groups(v.items)
    if groups:
        blocks.append(_input("item", "From the catalog", {"type": "static_select", "option_groups": groups}))
    blocks.append(
        _input("free_text", "Or something new", {"type": "plain_text_input", "max_length": MAX_SUGGESTION_LENGTH})
    )
    if v.cuisine_tags:
        cuisines = {"type": "static_select", "options": _tag_options(v.cuisine_tags)}
        hint = "Needed for something new, so variety and vetoes still work."
        blocks.append(_input("cuisine", "Cuisine", cuisines, hint=hint))
    return _modal("Add a pick", ids.PROPOSAL_VIEW, origin, blocks, "Add")


@dataclass(frozen=True, slots=True)
class ProposalInput:
    mode: Mode
    item_id: int | None
    free_text: str | None
    cuisine_id: int | None


def parse_proposal(values: Any) -> ProposalInput:
    selected = _state(values, "mode").get("selected_option")
    raw = selected.get("value") if isinstance(selected, dict) else None
    if raw not in {m.value for m in Mode}:
        raise FormError("mode", "Pick how you'd get it.")
    mode = Mode(str(raw))
    item = _picked(values, "item")
    text = _text(values, "free_text").strip() or None
    cuisine = _picked(values, "cuisine")
    if (item is None) == (text is None):
        raise FormError("free_text", "Pick something from the catalog, or type something new, but not both.")
    if text is not None and cuisine is None:
        raise FormError("cuisine", "Pick a cuisine for it, so variety and vetoes still work.")
    return ProposalInput(mode, item, text, cuisine if text is not None else None)


# Call it off and Close now (FR-56) ----------------------------------------------------------------

CLOSE_SUBMIT = {Window.RESPOND: "Start the vote", Window.VOTE: "Decide now"}
CLOSE_EFFECT = {
    Window.RESPOND: (
        "Responses close for everyone now, and the vote on the picks starts. "
        "If fewer than two people are in, everyone's on their own."
    ),
    Window.VOTE: "Voting closes for everyone now, and the votes so far decide the plan.",
}


def cancel_modal(v: EventView, origin: ids.ViewOrigin) -> dict[str, Any]:
    """Call it off, with an optional reason. Submitting is the confirmation, so the modal says
    what happens. Slack can't color a modal's submit button, so it isn't red."""
    slot = v.event.slot.value
    lines = [f"This ends the {slot} call for everyone, so nobody eats together this time."]
    if v.offer is not None:
        lines = [f"This calls off your {slot} offer, *{escape(v.offer.item.name)}*, for everyone who joined."]
    if v.decision is not None:
        lines.append(
            f"The plan, *{escape(v.decision.proposal.item.name)}*, is off, and whoever was cooking, "
            "picking up, or ordering gets a message."
        )
    if v.event.state is EventState.SCHEDULED:
        lines.append("It hasn't been posted yet, so nothing goes to the channel.")
    else:
        lines.append("The call's thread says who called it off, with your reason if you give one.")
    reason: Block = {
        "type": "plain_text_input",
        "max_length": CANCEL_NOTE_MAX,
        "placeholder": plain("Pizza at Grandma's", 150),
    }
    blocks = [_intro(v), section(" ".join(lines)), _input("reason", "Reason", reason, hint="Everyone sees it.")]
    return _modal(CALL_OFF, ids.CANCEL_VIEW, origin, blocks, CALL_OFF, close="Keep it")


def parse_cancel(values: Any) -> str | None:
    """The Call it off modal's reason, or None. ``calls.cancel_call`` cleans it."""
    reason = _text(values, "reason")
    if len(reason) > CANCEL_NOTE_MAX:  # Slack enforces max_length; this is a tampered payload
        raise FormError("reason", f"Keep the reason to {CANCEL_NOTE_MAX} characters.")
    return reason or None


def close_modal(v: EventView, origin: ids.ViewOrigin) -> dict[str, Any]:
    """Close now's confirmation from the ⋯ menu, which can't confirm one option and not the
    other. ``origin.window`` is the window it closes; the submission checks it's still open."""
    if origin.window is None:
        raise ValueError("a Close now modal needs the window it closes")
    window = origin.window
    blocks = [_intro(v), section(CLOSE_EFFECT[window])]
    return _modal(CLOSE_LABELS[window], ids.CLOSE_VIEW, origin, blocks, CLOSE_SUBMIT[window], close="Not yet")


# Feedback (FR-45 to FR-49) -------------------------------------------------------------------------

PRIVATE = "Only you see your ratings. The catalog shows only how many loved an item, never who."
THANKS = "thanks"
NO_ANSWER = option("No answer", "none")
"""Slack can't unselect a radio button, so this is how a rating is cleared, as on the web."""


def _rating_options() -> list[dict[str, Any]]:
    return [*(option(r.label, r.value) for r in Rating), NO_ANSWER]


def _radio(answer: Rating | None) -> Block:
    initial = option(answer.label, answer.value) if answer is not None else NO_ANSWER
    return {"type": "radio_buttons", "options": _rating_options(), "initial_option": initial}


def feedback_modal(form: FeedbackForm, origin: ids.ViewOrigin) -> dict[str, Any]:
    """How was it? The food, the place for a restaurant, and the cook for a home meal someone
    else cooked, each optional and prefilled with the member's answers (FR-46). ``origin.run``
    is the run of the call, which saving checks again."""
    facts, mine = form.facts, form.mine or FeedbackInput()
    intro = f"*{facts.slot.value.capitalize()} · {day_label(facts.day)}* · {escape(facts.item_name)}"
    blocks: list[Block] = [context(intro), _input("food", "The food", _radio(mine.food))]
    if facts.has_place:
        blocks.append(_input("place", "The place", _radio(mine.place)))
        chips: Block = {"type": "checkboxes", "options": [option(i.label, i.value) for i in PlaceIssue]}
        chosen = [option(i.label, i.value) for i in PlaceIssue if i in mine.place_issues]
        if chosen:
            chips["initial_options"] = chosen
        blocks.append(_input("place_issues", "Anything go wrong?", chips))
    if form.thanks_cook and facts.cook is not None:
        cook = facts.cook.name
        thanks: Block = {"type": "checkboxes", "options": [option(f"Thanks, {cook}!", THANKS)]}
        if mine.cook_thanks:
            thanks["initial_options"] = thanks["options"]
        blocks.append(_input("cook_thanks", "The cook", thanks))
        note: Block = {"type": "plain_text_input", "max_length": COOK_NOTE_MAX, "multiline": True}
        if mine.cook_note:
            note["initial_value"] = mine.cook_note
        blocks.append(_input("cook_note", f"A note for {cook}", note, hint=f"Only {cook} sees it."))
    blocks.append(context(f"{PRIVATE} You can change it until {day_label(form.closes_on)}."))
    return _modal("How was it?", ids.FEEDBACK_VIEW, origin, blocks, "Save")


def _picked_rating(values: Any, block_id: str) -> Rating | None:
    selected = _state(values, block_id).get("selected_option")
    if selected is None:
        return None
    raw = selected.get("value") if isinstance(selected, dict) else None
    if raw == NO_ANSWER["value"]:
        return None
    if raw not in {r.value for r in Rating}:
        raise ids.BadValue("not a rating")
    return Rating(str(raw))


def _picked_values(values: Any, block_id: str) -> list[str]:
    selected = _state(values, block_id).get("selected_options") or []
    if not isinstance(selected, list) or not all(isinstance(o, dict) for o in selected):
        raise ids.BadValue("bad selection")
    return [str(o.get("value")) for o in selected]


def parse_feedback(values: Any) -> FeedbackInput:
    """The feedback modal's state as ``FeedbackInput``. Raises FormError or ids.BadValue.
    ``feedback.give`` checks the subjects against the meal."""
    issues = _picked_values(values, "place_issues")
    if not set(issues) <= {i.value for i in PlaceIssue}:
        raise ids.BadValue("not an issue")
    thanks = _picked_values(values, "cook_thanks")
    if not set(thanks) <= {THANKS}:
        raise ids.BadValue("not a thank-you")
    note = _text(values, "cook_note")
    if len(note) > COOK_NOTE_MAX:  # Slack enforces max_length; this is a tampered payload
        raise FormError("cook_note", f"Keep the note to {COOK_NOTE_MAX} characters.")
    return FeedbackInput(
        food=_picked_rating(values, "food"),
        place=_picked_rating(values, "place"),
        place_issues=frozenset(PlaceIssue(i) for i in issues),
        cook_thanks=bool(thanks),
        cook_note=note or None,
    )


# Offers and logging a meal (FR-4, spec 5.6) ---------------------------------------------------------

SLOT_OPTIONS = {slot: option(slot.value.capitalize(), slot.value) for slot in (Slot.LUNCH, Slot.DINNER)}
MODE_OPTIONS = {mode: option(mode.label, mode.value) for mode in Mode}


def _select(options: Iterable[dict[str, Any]], initial: dict[str, Any] | None = None) -> Block:
    element: Block = {"type": "static_select", "options": list(options)}
    if initial is not None:
        element["initial_option"] = initial
    return element


def offer_modal(form: OfferForm, origin: ids.FormOrigin) -> dict[str, Any]:
    """Make an offer (spec 5.2): what, how, when, and how many servings are spare. It starts on
    the slot most likely meant, with that slot's usual mode and meal time (FR-1), so a lunch
    offer starts as pickup at its usual lunch time."""
    defaults = form.defaults[form.slot]
    blocks: list[Block] = [
        context("Posts in the Dinnerbell channel. If that meal's call is open already, it's added there as a pick."),
        _input("slot", "Meal", _select(SLOT_OPTIONS.values(), SLOT_OPTIONS[form.slot]), optional=False),
        _input(
            "day",
            "Day",
            {"type": "datepicker", "initial_date": form.today.isoformat()},
            optional=False,
        ),
        _input("mode", "How", _select(MODE_OPTIONS.values(), MODE_OPTIONS[defaults.mode]), optional=False),
    ]
    groups, _ = _item_groups(form.items)
    if groups:
        blocks.append(_input("item", "From the catalog", {"type": "static_select", "option_groups": groups}))
    blocks.append(
        _input("free_text", "Or something new", {"type": "plain_text_input", "max_length": MAX_SUGGESTION_LENGTH})
    )
    if form.cuisine_tags:
        cuisines = {"type": "static_select", "options": _tag_options(form.cuisine_tags)}
        blocks.append(_input("cuisine", "Cuisine", cuisines, hint="Needed for something new."))
    blocks.append(_input("at", "Time", {"type": "timepicker", **_hhmm(defaults.at)}, optional=False))
    spare: Block = {
        "type": "number_input",
        "is_decimal_allowed": False,
        "min_value": "1",
        "max_value": str(MAX_CAPACITY),
        "initial_value": str(form.capacity),
    }
    blocks.append(
        _input("capacity", "Spare servings", spare, optional=False, hint="How many others can join, guests included.")
    )
    return _modal("Make an offer", ids.OFFER_VIEW, origin, blocks, "Post it")


def _day(values: Any, block_id: str) -> date:
    raw = _state(values, block_id).get("selected_date")
    if not isinstance(raw, str) or len(raw) != 10:
        raise FormError(block_id, "Pick a day.")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ids.BadValue("bad date") from None


def _choice_of(values: Any, block_id: str, kinds: Iterable[str], message: str) -> str:
    selected = _state(values, block_id).get("selected_option")
    raw = selected.get("value") if isinstance(selected, dict) else None
    if raw not in set(kinds):
        raise FormError(block_id, message)
    return str(raw)


def parse_offer(values: Any) -> OfferInput:
    """The offer modal's state as ``OfferInput``. Raises FormError or ids.BadValue; the service
    checks the rest."""
    slot = Slot(_choice_of(values, "slot", (s.value for s in Slot), "Pick lunch or dinner."))
    mode = Mode(_choice_of(values, "mode", (m.value for m in Mode), "Pick how it arrives."))
    at = _time(values, "at")
    if at is None:
        raise FormError("at", "Pick a time.")
    raw = _text(values, "capacity").strip()
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 3) or not 1 <= int(raw) <= MAX_CAPACITY:
        raise FormError("capacity", f"Enter a whole number from 1 to {MAX_CAPACITY}.")
    item = _picked(values, "item")
    text = _text(values, "free_text").strip() or None
    cuisine = _picked(values, "cuisine")
    if (item is None) == (text is None):
        raise FormError("free_text", "Pick something from the catalog, or type something new, but not both.")
    if text is not None and cuisine is None:
        raise FormError("cuisine", "Pick a cuisine for it, so variety and vetoes still work.")
    return OfferInput(
        slot=slot,
        day=_day(values, "day"),
        mode=mode,
        at=at,
        capacity=int(raw),
        item_id=item,
        free_text=text,
        cuisine_id=cuisine if text is not None else None,
    )


def log_modal(form: LogForm, origin: ids.FormOrigin) -> dict[str, Any]:
    """Log a meal eaten outside Dinnerbell (spec 5.6): what, which meal and day, and who ate.
    It starts with today's dinner and the member who opened it."""
    groups, _ = _item_groups(form.items)
    people = [option(p.name, str(p.id)) for p in form.people[:MAX_OPTIONS]]
    who: Block = {"type": "multi_static_select", "options": people}
    mine = [o for o in people if o["value"] == str(form.member_id)]
    if mine:
        who["initial_options"] = mine
    blocks: list[Block] = [
        context("Logged meals count for variety and history. Feedback on them is on the History page."),
        _input("item", "What", {"type": "static_select", "option_groups": groups}, optional=False),
        _input("slot", "Meal", _select(SLOT_OPTIONS.values(), SLOT_OPTIONS[Slot.DINNER]), optional=False),
        _input("day", "Day", {"type": "datepicker", "initial_date": form.today.isoformat()}, optional=False),
        _input("who", "Who ate", who, optional=False),
    ]
    return _modal("Log a meal", ids.LOG_VIEW, origin, blocks, "Log it")


def parse_log(values: Any) -> MealInput:
    """The log modal's state as ``MealInput``. ``meals.log_meal`` checks the rest."""
    item = _picked(values, "item")
    if item is None:
        raise FormError("item", "Pick something from the catalog.")
    slot = Slot(_choice_of(values, "slot", (s.value for s in Slot), "Pick lunch or dinner."))
    who = _picked_many(values, "who")
    if not who:
        raise FormError("who", "Pick at least one person who ate.")
    return MealInput(day=_day(values, "day"), slot=slot, item_id=item, participant_ids=who)


# App Home ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HomeLinks:
    base_url: str

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


def _event_card(v: EventView, links: HomeLinks, today: date) -> list[Block]:
    state = v.event.state
    day = "" if v.event.day == today else f" · {day_label(v.event.day)}"  # posted early (#59)
    lines = [f"*{v.event.slot.value.capitalize()}*{day} · {state.label}"]
    if v.mine is not None:
        mine = STATUS_WORDS[v.mine.status]
        if v.mine.guests and v.mine.status is not Status.OUT:
            mine += f" You're bringing {plural(v.mine.guests, 'guest')}."
        if v.mine.offers:
            mine += " You offered to " + " or ".join(OFFER_WORDS[o] for o in sorted(v.mine.offers)) + "."
        lines.append(mine)
    elif state is EventState.COLLECTING:
        lines.append("Are you in? Tap below.")
    if v.decision is not None:
        lines.append(f"The plan: *{escape(v.decision.proposal.item.name)}*")
    elif state is EventState.VOTING:
        lines.append("Voting is open in the channel thread.")
    if state is EventState.CANCELLED and v.info.ended_by is not None:
        reason = f" “{escape(v.info.end_note)}”" if v.info.end_note else ""
        lines.append(f"Called off by {escape(v.info.ended_by.name)}.{reason}")
    value = ids.event_value(v.id)
    buttons: list[Block] = []
    if v.can.respond:
        # Home is per member, so their answer shows as selected (spec section 10).
        answer = v.mine.status if v.mine is not None else None
        buttons += [
            *(choice_button(ANSWER_WORDS[s], ids.ACTION_FOR_STATUS[s], value, selected=s is answer) for s in ANSWERS),
            button("Details", ids.DETAILS, value),
        ]
    if v.can.mark_done:
        buttons.append(mark_done_button(v.info))
    # Home is per member, so these show only to people allowed to use them (FR-56).
    window = v.can.close_window
    if window is not None:
        sure = confirm(f"{CLOSE_LABELS[window]}?", CLOSE_EFFECT[window], CLOSE_SUBMIT[window])
        buttons.append(
            button(CLOSE_LABELS[window], ids.CLOSE_WINDOW, ids.window_value(window, v.id, v.event.run), confirm=sure)
        )
    if v.can.cancel:
        cancel = ids.menu_value(ids.MenuChoice.CANCEL, v.id, v.event.run)
        buttons.append(button(CALL_OFF, ids.CANCEL_CALL, cancel, style="danger"))
    buttons.append(button("Open", f"{ids.LINK_PREFIX}event", url=links.url(v.event.path)))
    return [section("\n".join(lines), block_id=f"event-{v.id}"), actions(*buttons, block_id=f"event-actions-{v.id}")]


def _offer_card(v: EventView, offer: note.OfferInfo, links: HomeLinks, today: date) -> list[Block]:
    """An offer on Home (FR-4): what, when, seats left, and the member's own place in it, with
    the buttons they may use."""
    event = v.event
    day = "" if event.day == today else f" · {day_label(event.day)}"
    at = local_clock(offer.at, v.info.timezone)
    what = f"{escape(offer.offerer.name)} {OFFER_VERBS[offer.mode]} *{escape(offer.item.name)}* at {at}"
    lines = [f"*{event.slot.value.capitalize()} offer*{day} · {what}"]
    if event.state is EventState.COLLECTING:
        lines.append("It's full." if offer.full else f"{plural(offer.seats_left, 'serving')} spare.")
    elif event.state is EventState.DECIDED:
        lines.append("It's on! No more seats.")
    else:
        lines.append(event.state.label + ".")
    mine = v.mine
    if mine is not None and mine.is_in:
        guests = f" with {plural(mine.guests, 'guest')}" if mine.guests else ""
        lines.append(f"You're in{guests}.")
    value = ids.card_value(v.id, event.run)
    buttons: list[Block] = []
    if v.can.join:
        buttons.append(button("Count me in", ids.JOIN_OFFER, value, style="primary"))
    if v.can.add_guest:
        buttons.append(button("+1 guest", ids.ADD_GUEST, value))
    if v.can.leave:
        buttons.append(button(CANT_MAKE_IT, ids.LEAVE_OFFER, value))
    if v.can.mark_done:
        buttons.append(mark_done_button(v.info))
    if v.can.cancel:
        cancel = ids.menu_value(ids.MenuChoice.CANCEL, v.id, event.run)
        buttons.append(button(CALL_OFF, ids.CANCEL_CALL, cancel, style="danger"))
    buttons.append(button("Open", f"{ids.LINK_PREFIX}event", url=links.url(event.path)))
    return [section("\n".join(lines), block_id=f"event-{v.id}"), actions(*buttons, block_id=f"event-actions-{v.id}")]


def _upcoming_card(v: EventView, links: HomeLinks) -> list[Block]:
    """A call scheduled and not posted yet (issue #59): when it posts, with **Post it now** and
    **Call it off** for whoever may."""
    event = v.event
    posts = local_clock(event.opens_at, v.info.timezone)
    line = f"*{event.slot.value.capitalize()}* · {day_label(event.day)} · posts at {posts}"
    buttons: list[Block] = []
    if v.can.post_now:
        buttons.append(button(POST_NOW, ids.POST_NOW, ids.card_value(v.id, event.run), style="primary"))
    if v.can.cancel:
        cancel = ids.menu_value(ids.MenuChoice.CANCEL, v.id, event.run)
        buttons.append(button(CALL_OFF, ids.CANCEL_CALL, cancel, style="danger"))
    buttons.append(button("Open", f"{ids.LINK_PREFIX}event", url=links.url(event.path)))
    return [section(line, block_id=f"event-{v.id}"), actions(*buttons, block_id=f"event-actions-{v.id}")]


def _awaiting(awaiting: Sequence[Awaiting], links: HomeLinks) -> list[Block]:
    """Meals from done calls the member ate and hasn't said how they were (spec 5.9). At most
    ``MAX_AWAITING``, one block each, so Home stays far under Slack's 100 blocks."""
    if not awaiting:
        return []
    blocks: list[Block] = [section("*How was it?* Meals you ate in the last week. Only you see your ratings.")]
    for meal in awaiting[:MAX_AWAITING]:
        what = f"*{meal.slot.value.capitalize()} · {day_label(meal.day)}*: {escape(meal.item_name)}"
        row = section(f"{what}\nOpen until {day_label(meal.closes_on)}", block_id=f"feedback-{meal.meal_id}")
        row["accessory"] = button("How was it?", ids.FEEDBACK, ids.card_value(meal.event_id, meal.run))
        blocks.append(row)
    more = len(awaiting) - MAX_AWAITING
    if more > 0:
        blocks.append(context(f"…and {more} more on <{links.url('/history')}|History>."))
    return blocks


def home_view(
    today: TodayView,
    links: HomeLinks,
    notice: str | None = None,
    *,
    label: str = "",
    awaiting: Sequence[Awaiting] = (),
) -> dict[str, Any]:
    """Each member's Home tab. ``label``, the instance label, prefixes the header when set.
    ``awaiting`` lists meals the member hasn't given feedback on yet."""
    title = f"Today · {day_label(today.today)}"
    blocks: list[Block] = [header(f"{label} · {title}" if label else title)]
    if notice:
        blocks.append(section(f"*{escape(clip(notice, 500))}*"))
    for v in today.current:
        blocks += (
            _offer_card(v, v.offer, links, today.today) if v.offer is not None else _event_card(v, links, today.today)
        )
    if not today.current:
        blocks.append(section("No calls today yet."))
    todays = [v.event for v in today.events if v.event.day == today.today]
    # A cooking offer stands in for that slot's call (FR-5), so there's no call to start.
    started = {e.slot for e in todays if not e.is_offer or replaces_call(e)}
    called_off = {v.event.slot for v in today.events if v.event.day == today.today and v.can.restart}
    starts: list[Block] = [button("Make an offer", ids.MAKE_OFFER, ids.MAKE_OFFER)]
    for slot in (Slot.DINNER, Slot.LUNCH):
        if slot in called_off:  # starting it again takes the start button's path (FR-56)
            starts.append(button(f"Start {slot.value} again", ids.ACTION_FOR_START[slot], slot.value))
        elif slot not in started:
            starts.append(button(f"Start a {slot.value} call", ids.ACTION_FOR_START[slot], slot.value))
    if starts:
        blocks.append(actions(*starts, block_id="start"))
    if today.upcoming:
        blocks.append(section("*Coming up*"))
        for v in today.upcoming:
            blocks += _upcoming_card(v, links)
    blocks += _awaiting(awaiting, links)
    blocks += [
        divider(),
        context(
            f"<{links.url('/preferences')}|Your preferences> · <{links.url('/catalog')}|Catalog> · "
            f"<{links.url('/history')}|History> · <{links.url('/')}|Today on the web>"
        ),
    ]
    return {"type": "home", "blocks": blocks[:MAX_VIEW_BLOCKS]}
