# SPDX-License-Identifier: AGPL-3.0-or-later
"""Block Kit for every notification (spec section 10, docs/channels.md).

Messages are short and friendly. They thank people for pitching in and never scold anyone for
not answering. Each one links to its event page on the web (FR-37). Rendering is pure: the
same notification and directory always give the same JSON, which the golden tests pin down.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.blocks import (
    MAX_FALLBACK,
    MAX_MESSAGE_BLOCKS,
    Block,
    Directory,
    actions,
    button,
    clip,
    clip_mrkdwn,
    clock,
    confirm,
    context,
    day_label,
    escape,
    fields_section,
    header,
    instance_banner,
    join_lines,
    link,
    local_clock,
    names,
    option,
    overflow,
    plural,
    safe_url,
    section,
)
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import AssignmentRole, EndReason, EventState, Response, RoleOffer, Status, Window
from dinnerbell.domain.meals import Slot

MAX_PROPOSALS = 10
"""Services allow 10 proposals per call. Each takes two blocks, well under the 50 a message
may have; any beyond 10 are listed on the web."""

OFFER_WORDS = {RoleOffer.COOK: "cook", RoleOffer.PICKUP: "pick up", RoleOffer.ORDER: "order"}
ROLE_TITLES = {
    AssignmentRole.COOK: "Cooking",
    AssignmentRole.PICKUP: "Picking up",
    AssignmentRole.ORDER: "Ordering",
    AssignmentRole.COORDINATOR: "Coordinating",
}
ROLE_PHRASES = {
    AssignmentRole.COOK: "cooking",
    AssignmentRole.PICKUP: "picking up",
    AssignmentRole.ORDER: "ordering",
    AssignmentRole.COORDINATOR: "coordinating",
}

ANSWERS = (Status.IN, Status.MAYBE, Status.OUT)
"""The participation buttons, in the order they're shown."""
ANSWER_WORDS = {Status.IN: "In", Status.MAYBE: "Maybe", Status.OUT: "Out"}
OFFER_LABELS = {RoleOffer.COOK: "I can cook", RoleOffer.PICKUP: "I can pick up", RoleOffer.ORDER: "I'll order"}
SELECTED = "✓ "
"""Marks a selected button's text as well as its color, for color-blind people and for Slack
themes where primary isn't green."""
MOVED_ON = "This call moved on."

CLOSE_LABELS = {Window.RESPOND: "Start the vote now", Window.VOTE: "Decide now"}
"""Close now, named for what it does to the open window (FR-56)."""
MENU_WINDOWS = {EventState.COLLECTING: Window.RESPOND, EventState.VOTING: Window.VOTE}
CALL_OFF = "Call it off"
MARKERS = str.maketrans("", "", "~*_`")
"""mrkdwn's emphasis and code markers, dropped from text that sits inside other formatting."""


@dataclass(frozen=True)
class Message:
    """A rendered message: ``text`` is the notification and screen-reader fallback."""

    text: str
    blocks: list[Block]

    def payload(self) -> dict[str, Any]:
        return {"text": self.text, "blocks": self.blocks}


@dataclass(frozen=True)
class RenderContext:
    base_url: str
    directory: Directory
    label: str = ""
    """The instance label (``DINNERBELL_INSTANCE_LABEL``). Set, it heads every message."""

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


def render(notification: note.Notification, ctx: RenderContext) -> Message:
    """Every notification the Slack adapter sends, posts and direct messages alike."""
    return labelled(_render(notification, ctx), ctx)


def labelled(message: Message, ctx: RenderContext) -> Message:
    """``message`` headed by the instance label, when the instance has one."""
    if not ctx.label:
        return message
    # The largest message, the picks, is 23 blocks, so the label never pushes the footer past
    # Slack's limit (test_more_proposals_than_the_cap_are_listed_on_the_web checks).
    blocks = [instance_banner(ctx.label), *message.blocks]
    return Message(message.text, blocks[:MAX_MESSAGE_BLOCKS])


def _render(notification: note.Notification, ctx: RenderContext) -> Message:
    match notification:
        case note.CallOpened():
            return call_message(notification, ctx, mentions=notification.mentions)
        case note.CallUpdated():
            return call_message(notification, ctx)
        case note.ProposalsReady() | note.ProposalsUpdated():
            return proposals_message(notification, ctx)
        case note.DecisionMade():
            return decision_message(notification.event, notification.decision, ctx, notification.locked_by)
        case note.DecisionUpdated():
            return decision_message(notification.event, notification.decision, ctx, notification.locked_by)
        case note.ResponseReminder():
            return reminder_message(notification, ctx)
        case note.MaybeResolved():
            return maybe_resolved_message(notification, ctx)
        case note.AssignmentMade():
            return assignment_message(notification, ctx)
        case note.CallAbandoned():
            return abandoned_message(notification, ctx)
        case note.EventCancelled():
            return cancelled_message(notification, ctx)
        case note.AssignmentCancelled():
            return assignment_cancelled_message(notification, ctx)
        case note.StartedCallCancelled():
            return started_call_cancelled_message(notification, ctx)
    raise TypeError(f"no Slack rendering for {type(notification).__name__}")  # pragma: no cover


def _finish(text: str, blocks: list[Block]) -> Message:
    """``text`` is plain words built from names; Slack parses it as mrkdwn, so escape it."""
    return Message(clip_mrkdwn(escape(text), MAX_FALLBACK), blocks[:MAX_MESSAGE_BLOCKS])


def _slot(event: note.EventInfo) -> str:
    return event.slot.value


def _title(event: note.EventInfo) -> str:
    return f"{event.slot.value.capitalize()} call · {day_label(event.day)}"


def _web_button(ctx: RenderContext, event: note.EventInfo, text: str = "View on web") -> Block:
    return button(text, f"{ids.LINK_PREFIX}event", url=ctx.url(event.path))


def choice_button(text: str, action_id: str, value: str, *, selected: bool) -> Block:
    """A button on a surface only one person sees, showing whether it's their current choice:
    primary and a ✓ when it is. Shared messages never use this; nothing there is selected."""
    if selected:
        return button(SELECTED + text, action_id, value, style="primary")
    return button(text, action_id, value)


def call_menu(event: note.EventInfo) -> list[Block]:
    """The ⋯ menu for a posted call that's still open: **Call it off**, and Close now while a
    window is open. Everyone sees the same message, so the menu lists what the call's state
    allows; the server checks who may when someone picks one. Each value names the run the
    message shows. Once decided, only **Call it off** is left; Slack has required two options
    in an overflow menu, so it's a plain button then, which opens the same modal."""
    if event.state not in {EventState.COLLECTING, EventState.VOTING, EventState.DECIDED}:
        return []
    call_off = ids.menu_value(ids.MenuChoice.CANCEL, event.id, event.run)
    window = MENU_WINDOWS.get(event.state)
    if window is None:
        return [button(CALL_OFF, ids.CANCEL_CALL, call_off)]
    close = ids.menu_value(ids.MenuChoice.CLOSE, event.id, event.run)
    return [overflow(ids.CALL_MENU, [option(CALL_OFF, call_off), option(CLOSE_LABELS[window], close)])]


# The call message --------------------------------------------------------------------------------


def _window(entry: note.RosterEntry) -> str | None:
    start, end = entry.eat_from, entry.eat_until
    if start and end:
        return f"eats {clock(start)} to {clock(end)}"
    if start:
        return f"eats after {clock(start)}"
    return f"eats by {clock(end)}" if end else None


def _entry_line(entry: note.RosterEntry, ctx: RenderContext) -> str:
    parts: list[str] = []
    if entry.guests:
        guests = f"+{plural(entry.guests, 'guest')}"
        if entry.guest_needs:
            guests += f" ({escape(', '.join(entry.guest_needs))})"
        parts.append(guests)
    if entry.offers:
        parts.append("can " + " or ".join(OFFER_WORDS[o] for o in entry.offers))
    window = _window(entry)
    if window:
        parts.append(window)
    if entry.suggestions:
        parts.append("suggests " + escape(", ".join(entry.suggestions)))
    line = f"• {ctx.directory.mention(entry.member)}"
    if parts:
        line += " · " + " · ".join(parts)
    if entry.note:
        line += f"\n    “{escape(entry.note)}”"
    return line


def _roster(
    event: note.EventInfo,
    roster: tuple[note.RosterEntry, ...],
    waiting_on: tuple[note.PersonInfo, ...],
    head_count: int,
    ctx: RenderContext,
) -> str:
    by_status = {status: [e for e in roster if e.status is status] for status in Status}
    lines: list[str] = []
    if by_status[Status.IN]:
        lines.append(f"*In* · {plural(head_count, 'person', 'people')} eating")
        lines += [_entry_line(e, ctx) for e in by_status[Status.IN]]
    for status, label in ((Status.MAYBE, "Maybe"), (Status.OUT, "Out")):
        if by_status[status]:
            lines.append(f"*{label}:* {ctx.directory.mentions(e.member for e in by_status[status])}")
    if not roster:
        lines.append("No answers yet. Be the first!")
    if waiting_on and event.state is EventState.COLLECTING:
        lines.append(f"_Still to answer: {names(waiting_on)}_")
    if event.coordinator is not None:
        lines.append(f"*Coordinator:* {ctx.directory.mention(event.coordinator)}")
    elif event.state in {EventState.COLLECTING, EventState.VOTING}:
        lines.append("_No coordinator yet. Anyone can tap *I'll coordinate* to steer the vote._")
    return join_lines(lines)


def _call_status(event: note.EventInfo) -> str:
    """Plain words with mrkdwn emphasis: escape before putting it in a block."""
    slot = _slot(event)
    match event.state:
        case EventState.COLLECTING:
            return f"Who's in for {slot}? Answer by *{local_clock(event.respond_deadline, event.timezone)}*."
        case EventState.VOTING:
            closes = f" by *{local_clock(event.vote_deadline, event.timezone)}*" if event.vote_deadline else ""
            return f"Thanks for answering! Vote on the picks in the thread{closes}."
        case EventState.DECIDED:
            return "It's decided! The plan is in the thread."
        case EventState.DONE:
            return f"{slot.capitalize()} is done. Thanks, everyone!"
        case EventState.ABANDONED if event.end_reason is EndReason.MISSED:
            return "This call ended without a decision."
        case EventState.ABANDONED:
            return f"No group {slot} this time. Everyone's on their own."
        case EventState.CANCELLED if event.ended_by is not None:
            reason = f" “{event.end_note}”" if event.end_note else ""
            return f"Called off by {event.ended_by.name}.{reason}"
        case EventState.CANCELLED:
            return "This call was cancelled."
        case _:
            return "This call opens soon."


def call_message(
    notification: note.CallOpened | note.CallUpdated,
    ctx: RenderContext,
    *,
    mentions: tuple[note.PersonInfo, ...] = (),
) -> Message:
    event = notification.event
    plain_status = _call_status(event)
    status = escape(plain_status)
    if mentions and event.state is EventState.COLLECTING:
        status += "\n" + " ".join(ctx.directory.mention(p) for p in mentions)
    blocks: list[Block] = [
        header(_title(event)),
        section(status),
        section(_roster(event, notification.roster, notification.waiting_on, notification.head_count, ctx)),
    ]
    value = ids.event_value(event.id)
    if event.state is EventState.COLLECTING:
        # Everyone sees the same buttons, so none is styled: a primary In would look chosen.
        blocks.append(
            actions(
                *(button(ANSWER_WORDS[s], ids.ACTION_FOR_STATUS[s], value) for s in ANSWERS),
                block_id="participation",
            )
        )
    roles: list[Block] = []
    if event.state is EventState.COLLECTING:
        roles += [button(OFFER_LABELS[role], ids.ACTION_FOR_OFFER[role], value) for role in RoleOffer]
    if event.coordinator is None and event.state in {EventState.COLLECTING, EventState.VOTING}:
        roles.append(button("I'll coordinate", ids.COORDINATE, value))
    if roles:
        blocks.append(actions(*roles, block_id="roles"))
    more: list[Block] = []
    if event.state is EventState.COLLECTING:
        more.append(button("Details", ids.DETAILS, value))
    more.append(_web_button(ctx, event))
    more += call_menu(event)
    blocks.append(actions(*more, block_id="more"))
    text = f"{_title(event)}: {plain_status.splitlines()[0]}"
    return _finish(text.replace("*", ""), blocks)


# Proposals ---------------------------------------------------------------------------------------


def _item_name(item: note.ItemInfo) -> str:
    return link(item.url, item.name) if item.url else escape(item.name)


def _kind(proposal: note.ProposalInfo) -> str:
    cuisines = f" · {escape(', '.join(proposal.item.cuisines))}" if proposal.item.cuisines else ""
    return f"{proposal.mode.label}{cuisines}"


def _proposal_text(proposal: note.ProposalInfo, number: int, ctx: RenderContext) -> str:
    lines = [f"*{number}. {_item_name(proposal.item)}* · {_kind(proposal)}"]
    if proposal.reasons:
        lines.append("_" + escape(" ".join(proposal.reasons)) + "_")
    if proposal.voters:
        lines.append(f"{plural(proposal.votes, 'vote')}: {ctx.directory.mentions(proposal.voters)}")
    else:
        lines.append("No votes yet")
    return join_lines(lines)


def proposals_message(notification: note.ProposalsReady | note.ProposalsUpdated, ctx: RenderContext) -> Message:
    event = notification.event
    voting = event.state is EventState.VOTING
    people = plural(notification.head_count, "person", "people")
    if voting:
        closes = (
            f" Voting closes at *{local_clock(event.vote_deadline, event.timezone)}*." if event.vote_deadline else ""
        )
        intro = f"*Here are the picks for {people}.* Vote for any you'd enjoy.{closes}"
    else:
        intro = f"*The picks for {people}.* Voting is closed."
    blocks: list[Block] = [section(intro)]
    shown = notification.proposals[:MAX_PROPOSALS]
    for number, proposal in enumerate(shown, start=1):
        blocks.append(section(_proposal_text(proposal, number, ctx), block_id=f"proposal-{proposal.id}"))
        if voting:
            value = ids.proposal_value(event.id, proposal.id)
            name = clip(proposal.item.name, 60)
            lock = confirm(
                "Lock this in?",
                f"{name} becomes the plan right away, and voting closes.",
                "Lock it in",
            )
            blocks.append(
                actions(
                    button("Vote", ids.VOTE, value, style="primary"),
                    button("Lock this", ids.LOCK, value, confirm=lock),
                    block_id=f"proposal-actions-{proposal.id}",
                )
            )
    hidden = len(notification.proposals) - len(shown)
    if hidden:
        blocks.append(context(f"…and {plural(hidden, 'more pick')} on the web."))
    footer: list[Block] = []
    if voting:
        footer.append(button("Add proposal", ids.ADD_PROPOSAL, ids.event_value(event.id)))
    footer.append(_web_button(ctx, event))
    blocks.append(actions(*footer, block_id="more"))
    text = f"Picks for {_slot(event)} on {day_label(event.day)}: " + ", ".join(
        p.item.name for p in notification.proposals[:5]
    )
    return _finish(text, blocks)


# The decision ------------------------------------------------------------------------------------


def _head_count(decision: note.DecisionInfo) -> str:
    guests = sum(p.guests for p in decision.participants)
    count = plural(decision.head_count, "person", "people")
    return f"{count} ({plural(guests, 'guest')})" if guests else count


def _assignments(assignments: Iterable[note.AssignmentInfo], ctx: RenderContext) -> list[str]:
    return [f"*{ROLE_TITLES[a.role]}:* {ctx.directory.mention(a.member)}" for a in assignments]


def _item_buttons(item: note.ItemInfo, mode: Mode) -> list[Block]:
    out: list[Block] = []
    if safe_url(item.url):
        out.append(button("Website", f"{ids.LINK_PREFIX}item", url=safe_url(item.url)))
    if mode is Mode.DELIVERY and safe_url(item.order_url):
        out.append(button("Order online", f"{ids.LINK_PREFIX}order", url=safe_url(item.order_url)))
    return out


def decision_message(
    event: note.EventInfo, decision: note.DecisionInfo, ctx: RenderContext, locked_by: note.PersonInfo | None = None
) -> Message:
    proposal = decision.proposal
    item = _item_name(proposal.item)
    if event.state is EventState.DONE:
        top = f"*{_slot(event).capitalize()} is done: {item}.* Thanks to everyone who pitched in!"
    elif event.state is EventState.CANCELLED:
        # The reason sits beside a strike-through, so mrkdwn markers in it could break it.
        reason = event.end_note.translate(MARKERS) if event.end_note else event.end_note
        top = f"~{item}~ {escape(_call_status(replace(event, end_note=reason)))}"
    else:
        top = f"*{_slot(event).capitalize()} is decided: {item}* · {_kind(proposal)}"
    if locked_by is not None and event.state is not EventState.CANCELLED:
        top += f"\nLocked in by {ctx.directory.mention(locked_by)}."
    blocks: list[Block] = [
        section(top),
        fields_section(
            [
                f"*When*\n{local_clock(decision.meal_time, event.timezone)}",
                f"*Eating*\n{_head_count(decision)}",
            ]
        ),
    ]
    lines = _assignments(decision.assignments, ctx) or ["_Nobody is assigned yet._"]
    if decision.dietary_notes:
        lines.append(f"*Guests' dietary needs:* {escape(', '.join(decision.dietary_notes))}")
    blocks.append(section(join_lines(lines)))
    buttons: list[Block] = []
    if event.state is EventState.DECIDED:
        buttons.append(mark_done_button(event))
    buttons += _item_buttons(proposal.item, proposal.mode)
    buttons.append(_web_button(ctx, event))
    buttons += call_menu(event)
    blocks.append(actions(*buttons, block_id="more"))
    text = f"{_slot(event).capitalize()} on {day_label(event.day)}: {proposal.item.name} at " + local_clock(
        decision.meal_time, event.timezone
    )
    return _finish(text, blocks)


def mark_done_button(event: note.EventInfo) -> Block:
    """Mark done ends the meal and logs it, so it asks first (docs/channels.md)."""
    sure = confirm(f"Mark {_slot(event)} done?", "This ends the meal and adds it to the history.", "Mark done")
    return button("Mark done", ids.MARK_DONE, ids.event_value(event.id), style="primary", confirm=sure)


# Direct messages ---------------------------------------------------------------------------------


def reminder_message(
    notification: note.ResponseReminder, ctx: RenderContext, *, mine: Response | None = None, run: int | None = None
) -> Message:
    """The reminder (FR-11). Only its member sees it, so its buttons are card buttons: they name
    the run, and show the member's answer once there is one. After a tap on it, ``Interactions``
    edits it in place with ``mine``, their answer then, and ``run``, the run the tapped button
    named. If the call has moved on since, the buttons give way to what happened."""
    event = notification.event
    closes = local_clock(event.respond_deadline, event.timezone)
    when = f"{_slot(event)} on {day_label(event.day)}"
    if _moved_on(event, run):
        text = f"*{MOVED_ON}* {escape(_call_status(event))}"
        blocks = [section(text), actions(_web_button(ctx, event), block_id="more")]
        return _finish(f"The {_slot(event)} call for {day_label(event.day)} moved on.", blocks)
    if mine is None:
        text = (
            f"Hi {escape(notification.member.name)}! The {_slot(event)} call for {day_label(event.day)} "
            f"closes at *{closes}*. Are you in? A quick tap helps whoever's cooking plan ahead."
        )
        fallback = f"Are you in for {when}? Answer by {closes}."
    else:
        text = (
            f"Thanks, {escape(notification.member.name)}! Your answer for {when} is "
            f"*{ANSWER_WORDS[mine.status]}*. You can change it here until *{closes}*."
        )
        fallback = f"Your answer for {when}: {ANSWER_WORDS[mine.status]}."
    value = ids.card_value(event.id, event.run)
    blocks = [
        section(text),
        actions(
            *_answer_buttons(value, mine),
            _web_button(ctx, event),
            block_id="participation",
        ),
    ]
    return _finish(fallback, blocks)


# Your answer -------------------------------------------------------------------------------------


def _moved_on(event: note.EventInfo, run: int | None) -> bool:
    """Whether a card drawn for ``run`` (None: no card was tapped) is out of date: the call has
    started again since, or responses have closed."""
    return run is not None and (run != event.run or event.state is not EventState.COLLECTING)


def _answer_buttons(value: str, mine: Response | None) -> list[Block]:
    return [
        choice_button(
            ANSWER_WORDS[s], ids.CARD_ACTION_FOR_STATUS[s], value, selected=mine is not None and mine.status is s
        )
        for s in ANSWERS
    ]


def _offers(mine: Response) -> list[RoleOffer]:
    return [role for role in RoleOffer if role in mine.offers]


def _mine_lines(event: note.EventInfo, mine: Response | None, member_id: int) -> list[str]:
    """The member's own answer, offers, and coordinating, for their eyes only. Mentions would
    look the same as names here, so names are plain."""
    lines: list[str] = []
    if mine is not None:
        line = f"Your answer: *{ANSWER_WORDS[mine.status]}*"
        if mine.offers:
            line += " · can " + " or ".join(OFFER_WORDS[o] for o in _offers(mine))
        lines.append(line)
    elif event.state is EventState.COLLECTING:
        lines.append("You haven't answered yet.")
    coordinator = event.coordinator
    if coordinator is not None and coordinator.id == member_id:
        lines.append("You're coordinating. Thanks for steering!")
    elif coordinator is not None:
        lines.append(f"*Coordinator:* {escape(coordinator.name)}")
    return lines


def your_answer(
    event: note.EventInfo,
    mine: Response | None,
    member_id: int,
    ctx: RenderContext,
    *,
    run: int | None = None,
    notice: str | None = None,
) -> Message:
    """The Your answer card: an ephemeral only ``member_id`` sees, with their answer and offers
    marked on the same buttons the call has (spec section 10). ``event`` is the call as it is now
    and ``mine`` their response to it. A tap in the channel posts a card; a tap on the card
    replaces it, so ``run`` is the run the tapped card named, or None after a channel tap.
    ``notice`` is why a tap changed nothing, such as someone else coordinating already."""
    title = f"*{_title(event)}*"
    plain_title = _title(event)
    if run is not None and run != event.run:
        # Another run's answers aren't what this card was about; don't mix them in.
        gone = [
            section(f"{title}\n*{MOVED_ON}* It started again, so answer on the newest call message."),
            actions(_web_button(ctx, event), block_id="more"),
        ]
        return labelled(_finish(f"{plain_title}: {MOVED_ON}", gone), ctx)
    lines = [title]
    live = not _moved_on(event, run) and event.state is EventState.COLLECTING
    if not live:
        status = escape(_call_status(event))
        lines.append(f"*{MOVED_ON}* {status}" if _moved_on(event, run) else status)
    lines += _mine_lines(event, mine, member_id)
    blocks: list[Block] = [section(join_lines(lines))]
    if notice:
        blocks.append(context(escape(notice)))
    summary = f"{ANSWER_WORDS[mine.status]}" if mine is not None else "no answer yet"
    if mine is not None and mine.offers:
        summary += ", can " + " or ".join(OFFER_WORDS[o] for o in _offers(mine))
    text = f"{plain_title}: your answer is {summary}"
    if not live:
        blocks.append(actions(_web_button(ctx, event), block_id="more"))
        return labelled(_finish(text, blocks), ctx)
    closes = local_clock(event.respond_deadline, event.timezone)
    blocks.append(context(f"Only you can see this. Tap to change your answer until *{closes}*."))
    value = ids.card_value(event.id, event.run)
    blocks.append(actions(*_answer_buttons(value, mine), block_id="participation"))
    roles: list[Block] = []
    for role in RoleOffer:
        offered = mine is not None and role in mine.offers
        action = (ids.CARD_ACTION_FOR_WITHDRAW if offered else ids.CARD_ACTION_FOR_OFFER)[role]
        roles.append(choice_button(OFFER_LABELS[role], action, value, selected=offered))
    if event.coordinator is None:
        roles.append(button("I'll coordinate", ids.CARD_COORDINATE, value))
    blocks.append(actions(*roles, block_id="roles"))
    blocks.append(actions(button("Details", ids.CARD_DETAILS, value), _web_button(ctx, event), block_id="more"))
    return labelled(_finish(text, blocks), ctx)


def maybe_resolved_message(notification: note.MaybeResolved, ctx: RenderContext) -> Message:
    event = notification.event
    text = (
        f"The {_slot(event)} call for {day_label(event.day)} closed while you were a maybe, so you're "
        "counted out this time. If your plans change, let the coordinator know in the thread."
    )
    blocks = [section(text), actions(_web_button(ctx, event), block_id="more")]
    return _finish(f"You're counted out for {_slot(event)} on {day_label(event.day)}.", blocks)


def assignment_message(notification: note.AssignmentMade, ctx: RenderContext) -> Message:
    event, decision = notification.event, notification.decision
    proposal = decision.proposal
    role = " and ".join(ROLE_PHRASES[r] for r in notification.roles) or "helping"
    when = local_clock(decision.meal_time, event.timezone)
    lines = [
        f"Thanks for pitching in, {escape(notification.member.name)}! You're *{role}* for {_slot(event)} "
        f"on {day_label(event.day)}: *{_item_name(proposal.item)}* · {_kind(proposal)}.",
        f"*When:* {when} · *Eating:* {_head_count(decision)}",
    ]
    if decision.dietary_notes:
        lines.append(f"*Guests' dietary needs:* {escape(', '.join(decision.dietary_notes))}")
    others = [a for a in decision.assignments if a.member.id != notification.member.id]
    if others:
        lines.append(
            "*Also helping:* " + ", ".join(f"{escape(a.member.name)} ({ROLE_PHRASES[a.role]})" for a in others)
        )
    buttons = [*_item_buttons(proposal.item, proposal.mode), _web_button(ctx, event)]
    blocks = [section(join_lines(lines)), actions(*buttons, block_id="more")]
    return _finish(f"You're {role} for {_slot(event)} at {when}: {proposal.item.name}.", blocks)


# Endings -----------------------------------------------------------------------------------------


ABANDONED = {
    EndReason.TOO_FEW: "Not enough people for a group {slot} on {day}, so everyone's on their own this time.",
    EndReason.NO_COVERAGE: (
        "Nobody was free to cook or fetch any of the picks for {slot} on {day}, so everyone's on their own this time."
    ),
}
ABANDONED_OTHERWISE = "The {slot} call for {day} has ended, so everyone's on their own this time."


def abandoned_message(notification: note.CallAbandoned, ctx: RenderContext) -> Message:
    event = notification.event
    text = ABANDONED.get(notification.reason, ABANDONED_OTHERWISE).format(slot=_slot(event), day=day_label(event.day))
    blocks = [
        section(text + " Thanks for answering, and see you at the next one!"),
        context(link(ctx.url(event.path), "Open the call on the web")),
    ]
    return _finish(text, blocks)


ENJOY = {Slot.LUNCH: "Enjoy your day!", Slot.DINNER: "Enjoy your evening!"}


def _stop(reason: str) -> str:
    """A full stop after a reason, unless it ends a sentence already."""
    return "" if reason.endswith((".", "!", "?", "…")) else "."


def _called_off(event: note.EventInfo, by: note.PersonInfo | None, reason: str | None) -> str:
    """Plain words for a call-off, such as "Sam called off dinner for Friday, Oct 23: pizza at
    Grandma's." Escape before putting it in a block."""
    who = by.name if by is not None else "An admin"
    said = f": {reason}{_stop(reason)}" if reason else "."
    return f"{who} called off {_slot(event)} for {day_label(event.day)}{said}"


def _reason(event: note.EventInfo, by: note.PersonInfo | None, reason: str | None) -> str:
    """``_called_off`` as mrkdwn, with the reason in bold."""
    who = escape(by.name) if by is not None else "An admin"
    said = f": *{escape(reason)}*{_stop(reason)}" if reason else "."
    return f"{who} called off {_slot(event)} for {day_label(event.day)}{said}"


def cancelled_message(notification: note.EventCancelled, ctx: RenderContext) -> Message:
    event = notification.event
    lines = [
        f"{_reason(event, notification.cancelled_by, notification.note)} No group {_slot(event)} this time. "
        f"{ENJOY[event.slot]}"
    ]
    people = notification.participants if notification.audience is None else notification.audience
    if people:
        lines.append(f"Heads up {ctx.directory.mentions(people)}.")
    blocks = [section(join_lines(lines)), context(link(ctx.url(event.path), "Open the call on the web"))]
    return _finish(_called_off(event, notification.cancelled_by, notification.note), blocks)


def assignment_cancelled_message(notification: note.AssignmentCancelled, ctx: RenderContext) -> Message:
    event, proposal = notification.event, notification.decision.proposal
    role = " and ".join(ROLE_PHRASES[r] for r in notification.roles) or "helping"
    lines = [
        f"Change of plans, {escape(notification.member.name)}. "
        + _reason(event, notification.cancelled_by, notification.note),
        f"You're no longer {role}: ~{_item_name(proposal.item)}~. Thanks for being ready to pitch in!",
    ]
    blocks = [section(join_lines(lines)), actions(_web_button(ctx, event), block_id="more")]
    text = f"{_called_off(event, notification.cancelled_by, notification.note)} You're no longer {role}."
    return _finish(text, blocks)


def started_call_cancelled_message(notification: note.StartedCallCancelled, ctx: RenderContext) -> Message:
    event = notification.event
    lines = [
        _reason(event, notification.cancelled_by, notification.note),
        f"That's the {_slot(event)} call you started. It hadn't been posted yet, so nobody else was told.",
    ]
    blocks = [section(join_lines(lines)), actions(_web_button(ctx, event), block_id="more")]
    return _finish(_called_off(event, notification.cancelled_by, notification.note), blocks)
