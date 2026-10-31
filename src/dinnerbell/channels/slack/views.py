# SPDX-License-Identifier: AGPL-3.0-or-later
"""Modals and the App Home tab (spec section 10): the Details, Add proposal, Call it off, Close
now, and feedback modals, and each member's Home with today's calls, those coming up, and meals
awaiting feedback.

Modals carry their event and origin in ``private_metadata`` (see ``ids.ViewOrigin``). Input
blocks use fixed block and action IDs, which ``parse_details`` and ``parse_proposal`` read
back as untrusted input.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, time
from typing import Any
from zoneinfo import ZoneInfo

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.badge_text import ArtIds, art_element, icon_rows, image_block, tag
from dinnerbell.channels.slack.blocks import (
    FOOTER_BLOCKS,
    MAX_MODAL_TITLE,
    MAX_OPTION_TEXT,
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
    CALL_OFF,
    CANT_MAKE_IT,
    CLOSE_LABELS,
    OFFER_VERBS,
    POST_NOW,
    adjust_button,
    answer_actions,
    coordinator_line,
    join_late_button,
    mark_done_button,
    not_for_you_line,
)
from dinnerbell.domain import notifications as note
from dinnerbell.domain.badges import BADGES, BadgeKey, Tier, highest_tiers, next_up
from dinnerbell.domain.catalog import CatalogItem, ItemKind, Mode, Tag
from dinnerbell.domain.events import (
    CANCEL_NOTE_MAX,
    JOIN_LATE_PROMPT,
    MAX_GUESTS,
    MAX_NOTE_LENGTH,
    MAX_SUGGESTION_LENGTH,
    MODE_ROLE,
    Details,
    EventState,
    RoleOffer,
    Status,
    Window,
)
from dinnerbell.domain.feedback import COOK_NOTE_MAX, ISSUE_NOTE_MAX, FeedbackInput, PlaceIssue, Rating
from dinnerbell.domain.guests import GuestInput
from dinnerbell.domain.meals import CALL_ORDER, PICK_SLOT, MealInput, Slot
from dinnerbell.domain.members import MAX_DISPLAY_NAME_LENGTH, Member
from dinnerbell.domain.offers import MAX_CAPACITY, replaces_call
from dinnerbell.services.coordinator import Adjustment
from dinnerbell.services.event_views import EventView, TodayView
from dinnerbell.services.feedback import MAX_AWAITING, Awaiting, FeedbackForm
from dinnerbell.services.meals import LogForm
from dinnerbell.services.offers import OfferForm, OfferInput
from dinnerbell.services.stats import MemberStats

VALUE = "value"
"""Every input's action ID; blocks are told apart by block ID."""
STATUS_WORDS = {Status.IN: "You're in.", Status.MAYBE: "You said maybe.", Status.OUT: "You're out this time."}
OFFER_WORDS = {RoleOffer.COOK: "cook", RoleOffer.PICKUP: "pick up", RoleOffer.ORDER: "order"}
MAX_OLDER_CALLS = 3
"""Calls still open from earlier days that Home shows below today's (#51). They're rare, since
the scheduler closes old calls, so the rest are left to the web."""


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


PICKED = " (already a pick)"


def _no_hint(item: CatalogItem) -> str | None:
    return None


def _item_label(
    item: CatalogItem, picked: Collection[int] = (), hint: Callable[[CatalogItem], str | None] = _no_hint
) -> str:
    """The item's name, marked when the catalog says it's for other meals, "(usually dinner)"
    (#72), and when it's a pick on the call already (#77)."""
    note = hint(item)
    marks = (f" ({note})" if note else "") + (PICKED if item.id in picked else "")
    return clip(item.name, MAX_OPTION_TEXT - len(marks)) + marks if marks else item.name


def _item_groups(
    items: Iterable[CatalogItem],
    keep: Iterable[int] = (),
    picked: Collection[int] = (),
    hint: Callable[[CatalogItem], str | None] = _no_hint,
) -> tuple[list[Block], list[dict[str, Any]]]:
    """Catalog items as option groups (home meals, restaurants), at most 100 each, in the order
    given. Items in ``keep`` are always listed, so a saved choice survives a big catalog; items
    in ``picked`` are marked, and so is any item ``hint`` has a note for. Returns the groups and
    the options for ``keep``."""
    keep_ids = set(keep)
    groups: list[Block] = []
    chosen: list[dict[str, Any]] = []
    for kind, label in ((ItemKind.HOME, "Home meals"), (ItemKind.RESTAURANT, "Restaurants")):
        of_kind = [i for i in items if i.kind is kind]
        ordered = [i for i in of_kind if i.id in keep_ids] + [i for i in of_kind if i.id not in keep_ids]
        options = [option(_item_label(i, picked, hint), str(i.id)) for i in ordered[:MAX_OPTIONS]]
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
    """FR-13: a catalog item, or something new with a cuisine, and how to get it. Whoever adds
    it does it (#74), so the modal says so; items that are picks already are marked (#77). For
    someone who isn't in, it asks first whether they'll join late (#76): adding it does that,
    and the modal names the run it asked about (``origin.run``)."""
    modes = [option(m.label, m.value) for m in Mode]
    late = origin.run is not None
    blocks: list[Block] = [
        _intro(v),
        *([section(f"*{JOIN_LATE_PROMPT}* {JOIN_LATE_TO_ADD}")] if late else []),
        context(ADD_PICK_NOTE),
        _input("mode", "How", {"type": "static_select", "options": modes}, optional=False),
    ]
    groups, _ = _item_groups(v.items, picked=v.picked_item_ids, hint=v.meal_hint)
    if groups:
        blocks.append(_input("item", "From the catalog", {"type": "static_select", "option_groups": groups}))
    blocks.append(
        _input("free_text", "Or something new", {"type": "plain_text_input", "max_length": MAX_SUGGESTION_LENGTH})
    )
    if v.cuisine_tags:
        cuisines = {"type": "static_select", "options": _tag_options(v.cuisine_tags)}
        hint = "Needed for something new, so variety and vetoes still work."
        blocks.append(_input("cuisine", "Cuisine", cuisines, hint=hint))
    return _modal("Add a pick", ids.PROPOSAL_VIEW, origin, blocks, "Join late and add" if late else "Add")


JOIN_LATE_TO_ADD = (
    "Adding a pick joins you late: you're in, and your vote shows, but counts only if the coordinator says so."
)
ADD_PICK_NOTE = (
    "You'll be the one to pick it up, cook it, or order it, whichever way you choose. "
    "A home meal with a cook list you're not on needs one of its cooks to have offered to cook."
)


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


# Assign roles (FR-61) -----------------------------------------------------------------------------

MAX_ASSIGN_PEOPLE = 100
"""Slack's most options in a select; a household has far fewer."""


@dataclass(frozen=True, slots=True)
class AssignInput:
    mode: Mode
    member_id: int


def assign_modal(v: EventView, origin: ids.ViewOrigin) -> dict[str, Any]:
    """**Assign roles** (#87): how to get the winning pick, for a place that allows more than one
    way, and who does it, from the people who are in. A volunteer is preselected. The modal names
    the run (``origin.run``), which submitting checks."""
    choice = v.roles
    if choice is None:
        raise ValueError("an Assign roles modal needs a decided call")
    item = v.decision.proposal.item.name if v.decision else "the pick"
    blocks: list[Block] = [
        _intro(v),
        section(
            f"*{escape(item)}*: pick how to get it and who does it. Anyone who's in can. "
            "Someone who didn't offer is told you assigned them."
        ),
    ]
    modes = [option(m.label, m.value) for m in choice.modes]
    current = next(o for o in modes if o["value"] == choice.mode.value)
    how: Block = {"type": "static_select", "options": modes, "initial_option": current}
    blocks.append(_input("mode", "How", how, optional=False))
    people = [option(p.name, str(p.id)) for p in choice.people[:MAX_ASSIGN_PEOPLE]]
    volunteers = choice.volunteers.get(choice.mode, ())
    preselected = next((o for o in people if volunteers and o["value"] == str(volunteers[0])), None)
    element: Block = {"type": "static_select", "options": people}
    if preselected is not None:
        element["initial_option"] = preselected
    blocks.append(_input("who", "Who", element, optional=False))
    return _modal("Assign roles", ids.ASSIGN_VIEW, origin, blocks, "Assign")


def parse_assign(values: Any) -> AssignInput:
    """The Assign roles modal's state. ``coordinator.choose_roles`` checks both choices against
    the call."""
    selected = _state(values, "mode").get("selected_option")
    raw = selected.get("value") if isinstance(selected, dict) else None
    if raw not in {m.value for m in Mode}:
        raise FormError("mode", "Pick how to get it.")
    mode = Mode(str(raw))
    member = _picked(values, "who")
    if member is None:
        raise FormError("who", "Pick who does it.")
    return AssignInput(mode, member)


# Adjust (spec 5.7) ----------------------------------------------------------------------------------

ROLE_WHO = {Mode.COOK: "Who cooks", Mode.PICKUP: "Who picks up", Mode.DELIVERY: "Who orders"}
ADJUST_BLOCKS = {"meal_time": "meal_time", "assignee": "who", "coordinator": "coordinator"}
"""The input block for each part of an ``Adjustment``, where its refusal shows."""


def adjust_opened(v: EventView) -> ids.Opened:
    """What an Adjust modal for ``v`` shows: the meal time, who does the role, and who
    coordinates, as they are now. It goes in the modal's metadata, so saving can tell what the
    person changed from what changed elsewhere since."""
    choice, decision = v.roles, v.decision
    if choice is None or decision is None:
        raise ValueError("an Adjust modal needs a decided call")
    at = decision.meal_time.astimezone(ZoneInfo(v.info.timezone))
    role = MODE_ROLE[choice.mode]
    doing = next((a.member.id for a in decision.assignments if a.role is role), None)
    return ids.Opened(time(at.hour, at.minute), doing, v.event.coordinator_id)


def adjust_modal(v: EventView, origin: ids.ViewOrigin) -> dict[str, Any]:
    """**Adjust** (spec 5.7): the coordinator's meal time, who does the decided pick's role, from
    the people who are in, and whom to hand coordinating to, each set to what ``origin.opened``
    says they were. Saving changes only what the person changed. The modal names the run
    (``origin.run``), which saving checks. With nobody in to pick, the role isn't asked: Slack
    refuses a select with no options."""
    choice, decision, opened = v.roles, v.decision, origin.opened
    if choice is None or decision is None or opened is None or opened.coordinator_id is None:
        raise ValueError("an Adjust modal needs a decided call, with what it opened with")
    item = decision.proposal.item.name
    blocks: list[Block] = [
        _intro(v),
        section(f"*{escape(item)}*: change the time, who does it, or who coordinates. Only what you change is saved."),
        _input("meal_time", "Meal time", {"type": "timepicker", **_hhmm(opened.meal_time)}, optional=False),
    ]
    people = [option(p.name, str(p.id)) for p in choice.people[:MAX_ASSIGN_PEOPLE]]
    if people:
        who: Block = {"type": "static_select", "options": people, "placeholder": plain("Pick someone who's in")}
        current = next((o for o in people if o["value"] == str(opened.assignee_id)), None)
        if current is not None:
            who["initial_option"] = current
        blocks.append(_input("who", ROLE_WHO[choice.mode], who, optional=current is None))
    coordinator = opened.coordinator_id
    them = next((p for p in v.members if p.id == coordinator), None)
    leads = [option(them.name, str(them.id))] if them is not None else []
    leads += [o for o in people if o["value"] != str(coordinator)]
    if leads:
        lead: Block = {"type": "static_select", "options": leads[:MAX_ASSIGN_PEOPLE]}
        if leads[0]["value"] == str(coordinator):
            lead["initial_option"] = leads[0]
        hint = "Hand it off to someone who's in. They're told, and the plan is theirs to change after."
        blocks.append(_input("coordinator", "Coordinator", lead, optional=False, hint=hint))
    return _modal("Adjust the plan", ids.ADJUST_VIEW, origin, blocks, "Save")


def parse_adjust(values: Any) -> Adjustment:
    """The Adjust modal's state, as everything it shows; ``coordinator.adjust`` keeps only what
    differs from what it opened with, and checks that against the call."""
    meal = _time(values, "meal_time")
    if meal is None:
        raise FormError("meal_time", "Pick a meal time.")
    lead = _picked(values, "coordinator")
    if lead is None:
        raise FormError("coordinator", "Pick who coordinates.")
    return Adjustment(meal_time=meal, assignee_id=_picked(values, "who"), coordinator_id=lead)


def close_modal(v: EventView, origin: ids.ViewOrigin) -> dict[str, Any]:
    """Close now's confirmation from the ⋯ menu, which can't confirm one option and not the
    other. ``origin.window`` is the window it closes; the submission checks it's still open."""
    if origin.window is None:
        raise ValueError("a Close now modal needs the window it closes")
    window = origin.window
    blocks = [_intro(v), section(CLOSE_EFFECT[window])]
    return _modal(CLOSE_LABELS[window], ids.CLOSE_VIEW, origin, blocks, CLOSE_SUBMIT[window], close="Not yet")


# Feedback (FR-45 to FR-49) -------------------------------------------------------------------------

PRIVATE = (
    "Only you see your rating and what went wrong. The household sees a summary of everyone's, such as "
    '"Well liked" or "Recently late," never who said what. Your rating can also move your own score for it.'
)
THANKS = "thanks"
NO_ANSWER = option("No answer", "none")
"""Slack can't unselect a radio button, so this is how a rating is cleared, as on the web."""


def _rating_options() -> list[dict[str, Any]]:
    return [*(option(r.label, r.value) for r in Rating), NO_ANSWER]


def _radio(answer: Rating | None) -> Block:
    initial = option(answer.label, answer.value) if answer is not None else NO_ANSWER
    return {"type": "radio_buttons", "options": _rating_options(), "initial_option": initial}


def feedback_modal(form: FeedbackForm, origin: ids.ViewOrigin) -> dict[str, Any]:
    """How was it? One rating, what went wrong for a restaurant, and the cook for a home meal
    someone else cooked, each optional and prefilled with the member's answers (FR-46).
    ``origin.run`` is the run of the call, which saving checks again.

    A modal can't show a field only once "Other" is checked, so a restaurant's "What went
    wrong?" box is always there, and text in it counts as "Other", as on the web."""
    facts, mine = form.facts, form.mine or FeedbackInput()
    intro = f"*{facts.slot.value.capitalize()} · {day_label(facts.day)}* · {escape(facts.item_name)}"
    blocks: list[Block] = [context(intro), _input("rating", "Your rating", _radio(mine.rating))]
    if facts.has_place:
        chips: Block = {"type": "checkboxes", "options": [option(i.label, i.value) for i in PlaceIssue]}
        chosen = [option(i.label, i.value) for i in mine.issues]
        if chosen:
            chips["initial_options"] = chosen
        blocks.append(_input("place_issues", "Anything go wrong?", chips))
        other: Block = {"type": "plain_text_input", "max_length": ISSUE_NOTE_MAX}
        if mine.issue_note:
            other["initial_value"] = mine.issue_note
        blocks.append(
            _input(
                "issue_note",
                "What went wrong?",
                other,
                hint="For Other. Only you see it. Clear this box to uncheck Other.",
            )
        )
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


LEGACY_FEEDBACK = "This form changed. Close it and tap How was it? again."


def parse_feedback(values: Any) -> FeedbackInput:
    """The feedback modal's state as ``FeedbackInput``. Raises FormError or ids.BadValue.
    ``feedback.give`` checks each part against the meal."""
    # A modal opened before 0020 (one rating) asks about the food; saving it would drop the
    # member's rating. Remove after one release.
    if isinstance(values, dict) and "food" in values:
        raise FormError("food", LEGACY_FEEDBACK)
    issues = _picked_values(values, "place_issues")
    if not set(issues) <= {i.value for i in PlaceIssue}:
        raise ids.BadValue("not an issue")
    thanks = _picked_values(values, "cook_thanks")
    if not set(thanks) <= {THANKS}:
        raise ids.BadValue("not a thank-you")
    note = _text(values, "cook_note")
    if len(note) > COOK_NOTE_MAX:  # Slack enforces max_length; this is a tampered payload
        raise FormError("cook_note", f"Keep the note to {COOK_NOTE_MAX} characters.")
    issue_note = _text(values, "issue_note")
    if len(issue_note) > ISSUE_NOTE_MAX:  # likewise
        raise FormError("issue_note", f"Keep what went wrong to {ISSUE_NOTE_MAX} characters.")
    return FeedbackInput(
        rating=_picked_rating(values, "rating"),
        place_issues=frozenset(PlaceIssue(i) for i in issues),
        issue_note=issue_note or None,
        cook_thanks=bool(thanks),
        cook_note=note or None,
    )


# Offers and logging a meal (FR-4, spec 5.6) ---------------------------------------------------------

SLOT_OPTIONS = {slot: option(slot.value.capitalize(), slot.value) for slot in Slot}
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
    groups, _ = _item_groups(form.items, hint=form.meal_hint)
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
    slot = Slot(_choice_of(values, "slot", (s.value for s in Slot), PICK_SLOT))
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
    slot = Slot(_choice_of(values, "slot", (s.value for s in Slot), PICK_SLOT))
    who = _picked_many(values, "who")
    if not who:
        raise FormError("who", "Pick at least one person who ate.")
    return MealInput(day=_day(values, "day"), slot=slot, item_id=item, participant_ids=who)


# Inviting a guest (#38) ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InviteInput:
    guest_id: int | None
    new_guest: GuestInput | None


def invite_modal(v: EventView, saved: Sequence[Member], origin: ids.ViewOrigin) -> dict[str, Any]:
    """Invite a saved guest or a new one to the call. A guest with Slack answers themselves;
    for one without, you answer and offer roles on your card."""
    invited = {g.person.id for g in v.guests}
    choices = [option(m.display_name, str(m.id)) for m in saved if m.id not in invited][:MAX_OPTIONS]
    blocks: list[Block] = [
        _intro(v),
        context("Guests eat with you and can pitch in, but don't vote or suggest. Their dietary needs always apply."),
    ]
    if choices:
        blocks.append(_input("guest", "A saved guest", {"type": "static_select", "options": choices}))
    blocks.append(_input("name", "Or someone new", {"type": "plain_text_input", "max_length": MAX_DISPLAY_NAME_LENGTH}))
    if v.dietary_tags:
        needs: Block = {"type": "multi_static_select", "options": _tag_options(v.dietary_tags)}
        blocks.append(
            _input("needs", "Their dietary needs", needs, hint="For someone new. A saved guest keeps theirs.")
        )
    return _modal("Invite a guest", ids.INVITE_VIEW, origin, blocks, "Invite")


def parse_invite(values: Any) -> InviteInput:
    guest = _picked(values, "guest")
    name = _text(values, "name").strip()
    if (guest is None) == (not name):
        raise FormError("name", "Pick a saved guest, or type a new guest's name, but not both.")
    if len(name) > MAX_DISPLAY_NAME_LENGTH:  # Slack enforces max_length; this is a tampered payload
        raise FormError("name", f"Keep the name to {MAX_DISPLAY_NAME_LENGTH} characters.")
    if guest is not None:
        return InviteInput(guest, None)
    return InviteInput(None, GuestInput(name, _picked_many(values, "needs")))


# App Home ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HomeLinks:
    base_url: str

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


OPEN_STATES = frozenset({EventState.COLLECTING, EventState.VOTING, EventState.DECIDED})
"""States in which Home says who's coordinating (#84)."""


def _event_card(
    v: EventView, links: HomeLinks, today: date, *, guest: bool = False, member_id: int | None = None
) -> list[Block]:
    """A call on Home: two blocks, or four while answers are open, when the member's Your answer
    card buttons take two rows of their own above the call's other buttons (#84)."""
    state = v.event.state
    day = "" if v.event.day == today else f" · {day_label(v.event.day)}"  # posted early (#59)
    lines = [f"*{v.event.slot.value.capitalize()}*{day} · {state.label}"]
    if v.mine is not None and v.mine.late:  # #76
        mine = "You joined late." if v.mine.is_in else "You joined late, and the coordinator has you out."
        if v.mine.offers and v.mine.is_in:
            mine += " You offered to " + " or ".join(OFFER_WORDS[o] for o in sorted(v.mine.offers)) + "."
        lines.append(mine)
        if v.not_for_me:  # Home is theirs alone (FR-29)
            lines.append(not_for_you_line(v.not_for_me))
    elif v.mine is not None:
        mine = STATUS_WORDS[v.mine.status]
        if v.mine.guests and v.mine.status is not Status.OUT:
            mine += f" You're bringing {plural(v.mine.guests, 'guest')}."
        if v.mine.offers:
            mine += " You offered to " + " or ".join(OFFER_WORDS[o] for o in sorted(v.mine.offers)) + "."
        lines.append(mine)
    elif state is EventState.COLLECTING:
        lines.append("Are you in? Tap below.")
    coordinating = coordinator_line(v.info, member_id) if state in OPEN_STATES else None
    if coordinating is not None:
        lines.append(coordinating)
    if v.decision is not None:
        lines.append(f"The plan: *{escape(v.decision.proposal.item.name)}*")
    elif state is EventState.VOTING:
        lines.append("The picks are posted in the channel.")
    if state is EventState.CANCELLED and v.info.ended_by is not None:
        reason = f" “{escape(v.info.end_note)}”" if v.info.end_note else ""
        lines.append(f"Called off by {escape(v.info.ended_by.name)}.{reason}")
    rows: list[Block] = []
    buttons: list[Block] = []
    if v.can.respond:
        # Home is per member, so it offers what their Your answer card does, with their choices
        # selected, from the same builder and in the same rows (#84), each short enough that
        # Slack doesn't fold it into "+ N more". A guest is in or out, with no details (#38).
        # Rows for a member's guests without Slack stay on the card: Home's budget (#51) has
        # no room for two blocks per guest.
        answer = answer_actions(v.info, v.mine, guest=guest, can_invite=v.can.invite)
        rows = [
            actions(*answer.participation, block_id=f"answer-{v.id}"),
            actions(*answer.roles, block_id=f"roles-{v.id}"),
        ]
        buttons += answer.more
    elif v.can.late_roles:  # a late joiner pitches in while voting (#76)
        roles = answer_actions(v.info, v.mine, guest=guest).roles
        rows = [actions(*roles, block_id=f"roles-{v.id}")]
    if v.can.join_late:
        buttons.append(join_late_button(v.info))
    if v.can.mark_done:
        buttons.append(mark_done_button(v.info))
    if v.can.adjust:
        buttons.append(adjust_button(v.info))
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
    return [
        section("\n".join(lines), block_id=f"event-{v.id}"),
        *rows,
        actions(*buttons, block_id=f"event-actions-{v.id}"),
    ]


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
    blocks: list[Block] = [section("*How was it?* Meals you ate in the last week. Only you see your feedback.")]
    for meal in awaiting[:MAX_AWAITING]:
        what = f"*{meal.slot.value.capitalize()} · {day_label(meal.day)}*: {escape(meal.item_name)}"
        row = section(f"{what}\nOpen until {day_label(meal.closes_on)}", block_id=f"feedback-{meal.meal_id}")
        row["accessory"] = button("How was it?", ids.FEEDBACK, ids.card_value(meal.event_id, meal.run))
        blocks.append(row)
    more = len(awaiting) - MAX_AWAITING
    if more > 0:
        blocks.append(context(f"…and {more} more on <{links.url('/history')}|History>."))
    return blocks


def _fit(cards: Sequence[list[Block]], room: int, heading: str = "") -> tuple[list[Block], int]:
    """As many of ``cards`` as fit in ``room`` blocks, whole and in order, under ``heading`` if
    any fit: their blocks, and how many cards that is. A card never loses its buttons."""
    blocks: list[Block] = []
    for shown, card in enumerate(cards):
        title = [section(heading)] if heading and not blocks else []
        if len(title) + len(card) > room:
            return blocks, shown
        blocks += [*title, *card]
        room -= len(title) + len(card)
    return blocks, len(cards)


def _older(cards: Sequence[list[Block]], room: int, web: str) -> list[Block]:
    """Calls still open from earlier days (#51): at most ``MAX_OLDER_CALLS`` of them, as many as
    fit in ``room`` blocks, and a line linking ``web`` for the rest. ``room`` leaves out that line."""
    blocks, shown = _fit(cards[:MAX_OLDER_CALLS], room, "*Still open from earlier days*")
    more = len(cards) - shown
    if more:  # calls and offers alike
        what = f"…and {more} more from earlier days" if shown else f"{more} still open from earlier days"
        blocks.append(context(f"{what} on {web}."))
    return blocks


def _guest_home(
    head: list[Block], current: list[list[Block]], older: list[list[Block]], links: HomeLinks
) -> dict[str, Any]:
    web = f"<{links.url('/')}|Your invitations on the web>"
    tail = [divider(), context(f"<{links.url('/me')}|Your dietary needs> · {web}")]
    empty = not current and not older
    # Room for the version footer, the empty line, and a line for each "…and N more".
    room = MAX_VIEW_BLOCKS - FOOTER_BLOCKS - len(head) - len(tail) - (1 if empty else 0) - 1 - (1 if older else 0)
    now, shown = _fit(current, room)
    blocks = [*head, *now]
    if empty:
        blocks.append(section("No invitations right now. When a member invites you to a meal, it shows here."))
    hidden = len(current) - shown
    if hidden:
        blocks.append(context(f"…and {hidden} more on {web}."))
    blocks += _older(older, 0 if hidden else room - len(now), web)
    return {"type": "home", "blocks": [*blocks, *tail]}


def held_badges(stats: MemberStats) -> dict[BadgeKey, Tier]:
    """The highest tier the member holds of each badge they have."""
    return highest_tiers(stats.earned)


def art_wanted(stats: MemberStats) -> set[tuple[str, str]]:
    """The art Home shows for ``stats``: each held badge at its highest tier, and the next tier of
    the badge in reach (the small icon in the "Next" line)."""
    wanted = {(key.value, tier.value) for key, tier in held_badges(stats).items()}
    step = next_up(stats.progress)
    if step is not None and step.next_tier is not None:
        wanted.add((step.badge.key.value, step.next_tier.value))
    return wanted


def _badges(stats: MemberStats, art: ArtIds, links: HomeLinks, composite: str | None = None) -> list[Block]:
    """The member's own badges and the next one in reach. With no art (``art``, from ``BadgeFiles``)
    it is a heading with the badges as text tags, then the "Next" line: two blocks. With art, the
    heading, the held badges (art, at their highest tier) as compact rows of small icons, ``ICONS_PER_BLOCK``
    to a block, a line of text tags for held badges without art, a link to the
    Stats page for the names (when there is a web address), and the "Next" line with its badge's icon: at
    most six blocks, which the budget in ``home_view`` counts as part of its tail. With a ``composite``
    (the file of the one picture of every held badge, once Slack has had it a while), the
    picture stands in for the icon rows: five blocks at most. The "Next" line leaves the tier emoji off
    when it shows its badge's icon (#114). Only their own: it never mentions anyone else's."""
    held = held_badges(stats)
    if composite:
        shown = [b for b in BADGES if b.key in held]
    else:
        shown = [b for b in BADGES if b.key in held and (b.key.value, held[b.key].value) in art]
    tags = [tag(escape(b.name), held[b.key].label) for b in BADGES if b.key in held and b not in shown]
    line = "   ".join(tags) if tags else "None yet. Pitch in on a meal and your first one is on its way."
    step = next_up(stats.progress)
    icon = None
    if step is None or step.next_tier is None or step.next_at is None:
        hint = "You hold every badge at the top tier. Thank you!"
    else:
        icon_id = art.get((step.badge.key.value, step.next_tier.value))
        mark = tag(f"*{escape(step.badge.name)}*", step.next_tier.label, mark=icon_id is None)
        hint = f"Next: {mark}, {step.remaining} to go ({step.count} of {step.next_at} {step.badge.earned_by})"
        if icon_id:
            icon = art_element(icon_id, f"{step.badge.name}, {step.next_tier.label}")
    next_line = context(hint)
    if icon is not None:
        next_line["elements"].insert(0, icon)
    if not shown:
        return [section(f"*Your badges*\n{line}"), next_line]
    blocks = [section("*Your badges*")]
    if composite:
        names = ", ".join(f"{b.name} ({held[b.key].label})" for b in shown)
        blocks.append(image_block(composite, f"Your badges: {names}"))
    else:
        icons = [art_element(art[(b.key.value, held[b.key].value)], f"{b.name}, {held[b.key].label}") for b in shown]
        blocks += icon_rows(icons)
    if tags:
        blocks.append(context(f"Also: {line}"))
    if links.base_url:
        blocks.append(context(f"<{links.url('/stats')}|All badges on the web>"))
    return [*blocks, next_line]


def home_view(
    today: TodayView,
    links: HomeLinks,
    notice: str | None = None,
    *,
    label: str = "",
    awaiting: Sequence[Awaiting] = (),
    guest: bool = False,
    member_id: int | None = None,
    badges: MemberStats | None = None,
    badge_art: ArtIds | None = None,
    badge_composite: str | None = None,
) -> dict[str, Any]:
    """Each member's Home tab. ``label``, the instance label, prefixes the header when set.
    ``member_id`` is whose it is, so a call they coordinate says so.
    ``awaiting`` lists meals the member hasn't given feedback on yet. ``badges``, the member's own
    stats, adds a "Your badges" section above the links: two blocks, or up to six with art
    (``badge_art``, from ``BadgeFiles``) or the picture of them all (``badge_composite``); see ``_badges``.
    A guest's Home (``guest``) shows only the calls they're invited to, with In, Out, and what they may do, and none of
    the household's buttons or links (#38).

    Today's calls and offers come first, then the start buttons, the calls coming up, and at
    most ``MAX_OLDER_CALLS`` calls still open from earlier days, newest first (#51). It leaves
    room for the version footer (``blocks.with_version_footer``) within Slack's 100 blocks, and
    keeps the start buttons and the links: when cards don't all fit, the least important ones
    are left to the web, whole, and a line says how many."""
    title = f"Today · {day_label(today.today)}"
    head: list[Block] = [header(f"{label} · {title}" if label else title)]
    if notice:
        head.append(section(f"*{escape(clip(notice, 500))}*"))

    def card(v: EventView) -> list[Block]:
        if v.offer is not None:
            return _offer_card(v, v.offer, links, today.today)
        return _event_card(v, links, today.today, guest=guest, member_id=member_id)

    # Today's, and any posted early for a later day (#59), in the order they came. Anything
    # open from an earlier day, even a call that never posted, is left over.
    current = [card(v) for v in today.current if v.event.day >= today.today]
    left_over = [v for v in today.events if v.event.day < today.today]
    older = [card(v) for v in sorted(left_over, key=lambda v: v.event.day, reverse=True)]
    if guest:
        return _guest_home(head, current, older, links)
    upcoming = [_upcoming_card(v, links) for v in today.upcoming if v.event.day >= today.today]
    todays = [v.event for v in today.events if v.event.day == today.today]
    # A cooking offer stands in for that slot's call (FR-5), so there's no call to start.
    started = {e.slot for e in todays if not e.is_offer or replaces_call(e)}
    called_off = {v.event.slot for v in today.events if v.event.day == today.today and v.can.restart}
    starts: list[Block] = [button("Make an offer", ids.MAKE_OFFER, ids.MAKE_OFFER)]
    for slot in CALL_ORDER:
        if slot in called_off:  # starting it again takes the start button's path (FR-56)
            starts.append(button(f"Start {slot.value} again", ids.ACTION_FOR_START[slot], slot.value))
        elif slot not in started:
            starts.append(button(f"Start a {slot.value} call", ids.ACTION_FOR_START[slot], slot.value))
    web = f"<{links.url('/')}|Today on the web>"
    tail: list[Block] = [
        *_awaiting(awaiting, links),
        *(_badges(badges, badge_art or {}, links, badge_composite) if badges is not None else []),
        divider(),
        context(
            f"<{links.url('/preferences')}|Your preferences> · <{links.url('/catalog')}|Catalog> · "
            f"<{links.url('/history')}|History> · {web}"
        ),
    ]
    # Room for the version footer, the start buttons, "No calls today yet.", and a line for
    # each "…and N more".
    fixed = FOOTER_BLOCKS + len(head) + 1 + len(tail) + (0 if current else 1) + 1 + (1 if older else 0)
    room = MAX_VIEW_BLOCKS - fixed
    now, shown = _fit(current, room)
    room -= len(now)
    blocks = [*head, *now]
    if not current:
        blocks.append(section("No calls today yet."))
    blocks.append(actions(*starts, block_id="start"))
    # Nothing less important shows while something more important is left out.
    later, shown_later = _fit(upcoming, room, "*Coming up*") if shown == len(current) else ([], 0)
    room -= len(later)
    blocks += later
    hidden = len(current) + len(upcoming) - shown - shown_later
    if hidden:
        blocks.append(context(f"…and {hidden} more on {web}."))
    blocks += _older(older, 0 if hidden else room, web)
    blocks += tail
    return {"type": "home", "blocks": blocks}
