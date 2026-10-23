# SPDX-License-Identifier: AGPL-3.0-or-later
"""Block Kit for every notification (spec section 10, docs/channels.md).

Messages are short and friendly. They thank people for pitching in and never scold anyone for
not answering. Each one links to its event page on the web (FR-37). Rendering is pure: the
same notification and directory always give the same JSON, which the golden tests pin down.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
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
    join_lines,
    link,
    local_clock,
    names,
    plural,
    safe_url,
    section,
)
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import AssignmentRole, EndReason, EventState, RoleOffer, Status

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

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


def render(notification: note.Notification, ctx: RenderContext) -> Message:
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
            return decision_message(notification.event, notification.decision, ctx)
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
            return "All done. Thanks, everyone!"
        case EventState.ABANDONED:
            return f"No group {slot} this time. Everyone's on their own."
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
    status = _call_status(event)
    if mentions and event.state is EventState.COLLECTING:
        status += "\n" + " ".join(ctx.directory.mention(p) for p in mentions)
    blocks: list[Block] = [
        header(_title(event)),
        section(status),
        section(_roster(event, notification.roster, notification.waiting_on, notification.head_count, ctx)),
    ]
    value = ids.event_value(event.id)
    if event.state is EventState.COLLECTING:
        blocks.append(
            actions(
                button("In", ids.ACTION_FOR_STATUS[Status.IN], value, style="primary"),
                button("Maybe", ids.ACTION_FOR_STATUS[Status.MAYBE], value),
                button("Out", ids.ACTION_FOR_STATUS[Status.OUT], value),
                block_id="participation",
            )
        )
    roles: list[Block] = []
    if event.state is EventState.COLLECTING:
        roles += [
            button("I can cook", ids.ACTION_FOR_OFFER[RoleOffer.COOK], value),
            button("I can pick up", ids.ACTION_FOR_OFFER[RoleOffer.PICKUP], value),
            button("I'll order", ids.ACTION_FOR_OFFER[RoleOffer.ORDER], value),
        ]
    if event.coordinator is None and event.state in {EventState.COLLECTING, EventState.VOTING}:
        roles.append(button("I'll coordinate", ids.COORDINATE, value))
    if roles:
        blocks.append(actions(*roles, block_id="roles"))
    more: list[Block] = []
    if event.state is EventState.COLLECTING:
        more.append(button("Details", ids.DETAILS, value))
    more.append(_web_button(ctx, event))
    blocks.append(actions(*more, block_id="more"))
    text = f"{_title(event)}: {status.splitlines()[0]}"
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
        top = f"*{item}* is done. Thanks to everyone who pitched in!"
    elif event.state is EventState.CANCELLED:
        top = f"~{item}~ This {_slot(event)} was cancelled."
    else:
        top = f"*{_slot(event).capitalize()} is decided: {item}* · {_kind(proposal)}"
        if locked_by is not None:
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
        buttons.append(button("Mark done", ids.MARK_DONE, ids.event_value(event.id), style="primary"))
    buttons += _item_buttons(proposal.item, proposal.mode)
    buttons.append(_web_button(ctx, event))
    blocks.append(actions(*buttons, block_id="more"))
    text = f"{_slot(event).capitalize()} on {day_label(event.day)}: {proposal.item.name} at " + local_clock(
        decision.meal_time, event.timezone
    )
    return _finish(text, blocks)


# Direct messages ---------------------------------------------------------------------------------


def reminder_message(notification: note.ResponseReminder, ctx: RenderContext) -> Message:
    event = notification.event
    closes = local_clock(event.respond_deadline, event.timezone)
    text = (
        f"Hi {escape(notification.member.name)}! The {_slot(event)} call for {day_label(event.day)} "
        f"closes at *{closes}*. Are you in? A quick tap helps whoever's cooking plan."
    )
    value = ids.event_value(event.id)
    blocks = [
        section(text),
        actions(
            button("In", ids.ACTION_FOR_STATUS[Status.IN], value, style="primary"),
            button("Maybe", ids.ACTION_FOR_STATUS[Status.MAYBE], value),
            button("Out", ids.ACTION_FOR_STATUS[Status.OUT], value),
            _web_button(ctx, event),
            block_id="participation",
        ),
    ]
    return _finish(f"Are you in for {_slot(event)} on {day_label(event.day)}? Answer by {closes}.", blocks)


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
    role = ROLE_PHRASES[notification.role]
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


def cancelled_message(notification: note.EventCancelled, ctx: RenderContext) -> Message:
    event = notification.event
    who = escape(notification.cancelled_by.name) if notification.cancelled_by else "An admin"
    text = f"{who} cancelled the {_slot(event)} call for {day_label(event.day)}."
    lines = [text]
    if notification.participants:
        lines.append(
            f"Heads up {ctx.directory.mentions(notification.participants)}: no group {_slot(event)} this time."
        )
    blocks = [section(join_lines(lines)), context(link(ctx.url(event.path), "Open the call on the web"))]
    return _finish(text, blocks)
