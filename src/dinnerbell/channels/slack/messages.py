# SPDX-License-Identifier: AGPL-3.0-or-later
"""Block Kit for every notification (spec section 10, docs/channels.md).

Messages are short and friendly. They thank people for pitching in and never scold anyone for
not answering. Each one links to its event page on the web (FR-37). Rendering is pure: the
same notification and directory always give the same JSON, which the golden tests pin down.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, time
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
from dinnerbell.domain.events import (
    AssignmentRole,
    EndReason,
    EventKind,
    EventState,
    Response,
    RoleOffer,
    Status,
    Window,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import SLACK_USER_ID_PATTERN
from dinnerbell.domain.schedule import SlotSchedule

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
STARTED_AGAIN = "It started again, so answer on the newest call message."
"""Why a card or reminder for an earlier run can't act: answers belong to the new one."""

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
        case note.ScheduleChanged():
            return schedule_changed_message(notification, ctx)
        case note.CallDiscarded():
            return discarded_message(notification)
        case note.FeedbackReminder():
            return feedback_reminder_message(notification, ctx)
        case note.OfferPosted() | note.OfferUpdated():
            return offer_message(notification, ctx)
        case note.OfferFull():
            return offer_full_message(notification, ctx)
        case note.GuestInvited():
            return guest_invited_message(notification, ctx)
        case note.GuestJoined():
            return guest_joined_message(notification, ctx)
        case note.RoleVacated():
            return role_vacated_message(notification, ctx)
        case note.LinkNeeded():
            return link_needed_message(notification, ctx)
    raise TypeError(f"no Slack rendering for {type(notification).__name__}")  # pragma: no cover


def _finish(text: str, blocks: list[Block]) -> Message:
    """``text`` is plain words built from names; Slack parses it as mrkdwn, so escape it."""
    return Message(clip_mrkdwn(escape(text), MAX_FALLBACK), blocks[:MAX_MESSAGE_BLOCKS])


def _slot(event: note.EventInfo) -> str:
    return event.slot.value


def _title(event: note.EventInfo) -> str:
    kind = "offer" if event.kind is EventKind.OFFER else "call"
    return f"{event.slot.value.capitalize()} {kind} · {day_label(event.day)}"


def _what(event: note.EventInfo) -> str:
    """What was called off: "dinner" for a call, "the dinner offer" for an offer."""
    return f"the {_slot(event)} offer" if event.kind is EventKind.OFFER else _slot(event)


def _web_button(ctx: RenderContext, event: note.EventInfo, text: str = "View on web") -> Block:
    return button(text, f"{ids.LINK_PREFIX}event", url=ctx.url(event.path))


def choice_button(text: str, action_id: str, value: str, *, selected: bool) -> Block:
    """A button on a surface only one person sees, showing whether it's their current choice:
    primary and a ✓ when it is. Shared messages never use this; nothing there is selected."""
    if selected:
        return button(SELECTED + text, action_id, value, style="primary")
    return button(text, action_id, value)


def answer_button(event: note.EventInfo) -> Block:
    """**Answer**, which posts the tapper's Your answer card. Primary: it's the one action the
    call asks of everyone, and not anyone's choice, so it can't look like one."""
    return button(
        "Answer",
        ids.ANSWER,
        ids.card_value(event.id, event.run),
        style="primary",
        accessibility_label=f"Answer the {_slot(event)} call: in, maybe, or out, and how you can help",
    )


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
    elif event.state is EventState.COLLECTING:
        lines.append("_No coordinator yet. Anyone can tap *Answer*, then *I'll coordinate*, to steer the vote._")
    elif event.state is EventState.VOTING:
        lines.append("_No coordinator yet. Anyone can tap *I'll coordinate* to steer the vote._")
    return join_lines(lines)


def _call_status(event: note.EventInfo) -> str:
    """Plain words with mrkdwn emphasis: escape before putting it in a block."""
    slot = _slot(event)
    match event.state:
        case EventState.COLLECTING if event.early:  # posted the day before, or earlier (#59)
            by = local_clock(event.respond_deadline, event.timezone)
            return f"Who's in for {slot} on {day_label(event.day)}? Answer by *{by}* that day."
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
    if event.coordinator is None and event.state is EventState.VOTING:
        # Answering is over, so no card is offered, but someone can still take the vote in hand.
        # It names the run, so a message from before a restart can't claim the newer one (#54).
        claim = button("I'll coordinate", ids.CALL_COORDINATE, ids.card_value(event.id, event.run))
        blocks.append(actions(claim, block_id="roles"))
    more: list[Block] = []
    if event.state is EventState.COLLECTING:
        # Everyone sees this message, so it offers no choices, only the way to your own card,
        # where In, Maybe, Out, the offers, I'll coordinate, and Details are (issue #52).
        more.append(answer_button(event))
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
        lines.append(f"*Dietary needs:* {escape(', '.join(decision.dietary_notes))}")
    blocks.append(section(join_lines(lines)))
    buttons: list[Block] = []
    if event.state is EventState.DECIDED:
        buttons.append(mark_done_button(event))
    elif event.state is EventState.DONE:
        buttons.append(feedback_button(event))
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


# Starting and posting a call from Slack (issue #59) ---------------------------------------------

POST_NOW = "Post it now"


def started_reply(
    event: note.EventInfo, opens_at: datetime, *, today: date, channel_id: str, ctx: RenderContext
) -> Message:
    """The private reply to starting a call from Slack. A call for today is posted at once. One
    for a later day is scheduled: the reply says when it posts, and has **Post it now**.
    ``opens_at`` is when it posts; ``channel_id`` is the household channel, which Slack shows
    as a link."""
    slot, channel = _slot(event), f"<#{channel_id}>"
    if event.state is not EventState.SCHEDULED:
        when = "today" if event.day == today else f"for {day_label(event.day)}"
        text = f"Started the {slot} call {when}. It's posted in {channel}."
        return labelled(Message(text, [section(text)]), ctx)
    posts = local_clock(opens_at, event.timezone)
    text = f"Scheduled the {slot} call for {day_label(event.day)}. It posts in {channel} at {posts} that day."
    post = button(
        POST_NOW,
        ids.POST_NOW,
        ids.card_value(event.id, event.run),
        style="primary",
        accessibility_label=f"Post the {slot} call for {day_label(event.day)} now",
    )
    blocks = [
        section(f"{text}\nTo ask everyone now instead, tap *{POST_NOW}*. Answers are still due that day."),
        actions(post, _web_button(ctx, event), block_id="more"),
    ]
    return labelled(Message(text, blocks), ctx)


def posted_reply(event: note.EventInfo, *, channel_id: str, ctx: RenderContext) -> Message:
    """The private reply to **Post it now**: the call is in the channel now, whoever posted it."""
    text = f"The {_slot(event)} call for {day_label(event.day)} is posted in <#{channel_id}>."
    if event.state is EventState.COLLECTING:
        that_day = " that day" if event.early else ""
        text += f" Answers are due by *{local_clock(event.respond_deadline, event.timezone)}*{that_day}."
    return labelled(Message(text.replace("*", ""), [section(text)]), ctx)


def feedback_button(event: note.EventInfo) -> Block:
    """**How was it?**, which opens the feedback modal for the meal (FR-47). It names the run,
    like a card button, and the service checks who ate and that feedback is still open."""
    return button(
        "How was it?",
        ids.FEEDBACK,
        ids.card_value(event.id, event.run),
        style="primary",
        accessibility_label=f"Say how {_slot(event)} was: the food, the place, or thanks to the cook",
    )


# Direct messages ---------------------------------------------------------------------------------


def reminder_message(
    notification: note.ResponseReminder | note.GuestInvited,
    ctx: RenderContext,
    *,
    mine: Response | None = None,
    run: int | None = None,
) -> Message:
    """The reminder (FR-11). Only its member sees it, so its buttons are card buttons: they name
    the run, and show the member's answer once there is one. After a tap on it, ``Interactions``
    edits it in place with ``mine``, their answer then, and ``run``, the run the tapped button
    named. If the call has moved on since, the buttons give way to what happened."""
    event = notification.event
    closes = local_clock(event.respond_deadline, event.timezone)
    when = f"{_slot(event)} on {day_label(event.day)}"
    if _moved_on(event, run):
        why = STARTED_AGAIN if run != event.run else escape(_call_status(event))
        text = f"*{MOVED_ON}* {why}"
        blocks = [section(text), actions(_web_button(ctx, event), block_id="more")]
        return _finish(f"The {_slot(event)} call for {day_label(event.day)} moved on.", blocks)
    guest = isinstance(notification, note.GuestInvited) or notification.guest
    if mine is None and isinstance(notification, note.GuestInvited):
        text = (
            f"Hi {escape(notification.member.name)}! {escape(notification.invited_by.name)} invited you to "
            f"{_slot(event)} on {day_label(event.day)}. Are you in? Answer by *{closes}*."
        )
        fallback = f"You're invited to {when}. Are you in? Answer by {closes}."
    elif mine is None:
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
            *_answer_buttons(value, mine, guest=guest),
            _web_button(ctx, event),
            block_id="participation",
        ),
    ]
    return _finish(fallback, blocks)


def guest_invited_message(notification: note.GuestInvited, ctx: RenderContext) -> Message:
    """A registered guest's invitation (#38): In and Out, as card buttons that name the run.
    A tap edits it in place, as on the reminder."""
    return reminder_message(notification, ctx)


def _slack_account(slack_user_id: str, email: str | None) -> str:
    """Who a Slack account really is, for an admin about to link it: the account itself, which
    Slack shows with its current profile, and the email Slack shares. A display name is whatever
    its owner typed, so it's never enough (#38)."""
    account = f"<@{slack_user_id}>" if SLACK_USER_ID_PATTERN.match(slack_user_id) else "an unknown Slack account"
    shared = f"Slack email {escape(email)}" if email else "Slack shares no email for it"
    return f"{account}, {shared}"


def link_needed_message(notification: note.LinkNeeded, ctx: RenderContext) -> Message:
    """To each admin: a Slack account Dinnerbell can't link without them (#38)."""
    member = escape(notification.member.name)
    slack = escape((notification.slack_name or "Someone").translate(MARKERS))
    account = _slack_account(notification.slack_user_id, notification.slack_email)
    if notification.reason == "join":
        text = (
            f"{slack} ({account}) tried to join Dinnerbell from the channel, and their Slack email is {member}'s. "
            f"If that's {member}, set their Slack member ID ({notification.slack_user_id}) on the Members page; "
            "otherwise add them as a guest there."
        )
    elif notification.member_is_admin:
        text = (
            f"{member}'s Slack account ({account}) is linked to someone else in Dinnerbell already, so it "
            f"can't be linked to {member}. {member} is an admin, so a guest can't be linked to them: remove "
            f"that guest, then set {member}'s Slack member ID on their row on the Members page."
        )
    else:
        text = (
            f"{member}'s Slack account ({account}) is linked to someone else in Dinnerbell already, so it "
            f"can't be linked to {member}. If that's a guest who is really {member}, select *Link to member* "
            "on their row on the Members page."
        )
    members = button("Members page", f"{ids.LINK_PREFIX}members", url=ctx.url("/admin/members"))
    blocks = [section(text), actions(members, block_id="more")]
    return _finish(f"A Slack account needs linking to {notification.member.name}.", blocks)


def role_vacated_message(notification: note.RoleVacated, ctx: RenderContext) -> Message:
    """To the coordinator, or each admin with none: a guest who had a role left the call."""
    event = notification.event
    role = " and ".join(ROLE_PHRASES[r] for r in notification.roles) or "helping"
    who = escape(notification.left.name)
    text = (
        f"Heads up, {escape(notification.member.name)}: {who} isn't coming to {_slot(event)} on "
        f"{day_label(event.day)} any more, so nobody is *{role}*. Pick someone who's in on the web."
    )
    blocks = [section(text), actions(_web_button(ctx, event, "Pick someone"), block_id="more")]
    return _finish(f"{notification.left.name} isn't coming, so nobody is {role} for {_slot(event)}.", blocks)


def guest_joined_message(notification: note.GuestJoined, ctx: RenderContext) -> Message:
    """To each admin when someone joins from the channel (#38 addendum): a guest until an admin
    says otherwise."""
    where = "Sign in with Slack" if notification.via == "web" else "the Dinnerbell channel"
    name = escape(notification.guest.name.translate(MARKERS))  # it sits inside bold
    account = _slack_account(notification.slack_user_id, notification.slack_email)
    kind = " They use a Slack guest account, not a full one." if notification.slack_guest_account else ""
    lines = [
        f"*{name}* ({account}) joined Dinnerbell from {where}, as a guest.{kind} Guests answer only the "
        "calls a member invites them to, and never vote.",
        "Make them a member, remove them, or leave them as a guest.",
    ]
    if notification.unlinked:
        one = len(notification.unlinked) == 1
        who = names(notification.unlinked) if one else f"one of {names(notification.unlinked)}"
        whose = "whose Slack account isn't" if one else "whose Slack accounts aren't"
        lines.append(
            f"_Is this {who}, {whose} linked yet? Anyone can take any name, so check the Slack email "
            "first, then link them to their membership on the Members page._"
        )
    guest = ids.event_value(notification.guest.id)
    remove = confirm(
        f"Remove {clip(notification.guest.name, 60)}?",
        "They can't use Dinnerbell or join from the channel again, unless you add them back.",
        "Remove",
    )
    members = button("Members page", f"{ids.LINK_PREFIX}members", url=ctx.url("/admin/members"))
    blocks = [
        section(join_lines(lines)),
        actions(
            button("Make member", ids.MAKE_MEMBER, guest, style="primary"),
            button("Remove", ids.REMOVE_GUEST, guest, style="danger", confirm=remove),
            members,
            block_id="more",
        ),
    ]
    return _finish(f"{notification.guest.name} joined Dinnerbell as a guest.", blocks)


def feedback_reminder_message(notification: note.FeedbackReminder, ctx: RenderContext) -> Message:
    """The feedback reminder (FR-48), sent only to members who turned it on."""
    event = notification.event
    when = f"{_slot(event)} on {day_label(event.day)}"
    closes = day_label(notification.closes)
    text = (
        f"Hi {escape(notification.member.name)}! How was {when}: *{escape(notification.item)}*? "
        f"Only you see your ratings, and you can change them until *{closes}*."
    )
    blocks = [section(text), actions(feedback_button(event), _web_button(ctx, event), block_id="more")]
    return _finish(f"How was {when}? Tell us by {closes}.", blocks)


# Your answer -------------------------------------------------------------------------------------


def _moved_on(event: note.EventInfo, run: int | None) -> bool:
    """Whether a card drawn for ``run`` (None: no card was tapped) is out of date: the call has
    started again since, or responses have closed."""
    return run is not None and (run != event.run or event.state is not EventState.COLLECTING)


GUEST_ANSWERS = (Status.IN, Status.OUT)
"""A guest is in or out (#38)."""


def _answer_buttons(value: str, mine: Response | None, *, guest: bool = False) -> list[Block]:
    return [
        choice_button(
            ANSWER_WORDS[s], ids.CARD_ACTION_FOR_STATUS[s], value, selected=mine is not None and mine.status is s
        )
        for s in (GUEST_ANSWERS if guest else ANSWERS)
    ]


def _offers(mine: Response) -> list[RoleOffer]:
    return [role for role in RoleOffer if role in mine.offers]


def _mine_lines(event: note.EventInfo, mine: Response | None, member_id: int) -> list[str]:
    """The member's own answer, offers, and coordinating, for their eyes only. Mentions would
    look the same as names here, so names are plain."""
    lines: list[str] = []
    if mine is not None:
        line = f"Your answer: *{ANSWER_WORDS[mine.status]}*"
        if mine.guests and mine.status is not Status.OUT:
            line += f" · +{plural(mine.guests, 'guest')}"
        if mine.offers:
            line += " · can " + " or ".join(OFFER_WORDS[o] for o in _offers(mine))
        lines.append(line)
    elif event.state is EventState.COLLECTING:
        lines.append("You haven't answered yet.")
    coordinating = coordinator_line(event, member_id)
    if coordinating is not None:
        lines.append(coordinating)
    return lines


def coordinator_line(event: note.EventInfo, member_id: int | None) -> str | None:
    """Who's coordinating, as mrkdwn for ``member_id``'s eyes only: on their card and their Home
    (#84). None while nobody is."""
    coordinator = event.coordinator
    if coordinator is None:
        return None
    if coordinator.id == member_id:
        return "You're coordinating. Thanks for steering!"
    return f"*Coordinator:* {escape(coordinator.name)}"


def your_answer_text(event: note.EventInfo, mine: Response | None, *, run: int | None = None) -> str:
    """The card's words as plain text, unescaped: its notification and screen-reader fallback,
    and the line sent instead when a card or reminder can't be shown."""
    if run is not None and run != event.run:
        return f"{_title(event)}: {MOVED_ON}"
    if mine is None:
        return f"{_title(event)}: you haven't answered yet"
    summary = ANSWER_WORDS[mine.status]
    if mine.guests and mine.status is not Status.OUT:
        summary += f" with {plural(mine.guests, 'guest')}"
    if mine.offers:
        summary += ", can " + " or ".join(OFFER_WORDS[o] for o in _offers(mine))
    return f"{_title(event)}: your answer is {summary}"


MAX_CARD_GUESTS = 5
"""Guests without Slack a card shows buttons for; the web has the rest."""


@dataclass(frozen=True)
class CardGuest:
    """A guest without Slack the card's member answers for (#38): their name and answer."""

    id: int
    name: str
    answer: Response | None
    needs: tuple[str, ...] = ()
    """Their dietary needs, which the member answering for them should know."""


def _guest_rows(event: note.EventInfo, guests: tuple[CardGuest, ...]) -> list[Block]:
    """In, Out, and the role offers for each guest the member answers for, on their card."""
    blocks: list[Block] = []
    for guest in guests[:MAX_CARD_GUESTS]:
        mine = guest.answer
        value = ids.guest_value(event.id, event.run, guest.id)
        needs = f" ({escape(', '.join(guest.needs))})" if guest.needs else ""
        line = f"*{escape(guest.name)}*, your guest{needs}: " + (ANSWER_WORDS[mine.status] if mine else "no answer yet")
        if mine is not None and mine.offers:
            line += " · can " + " or ".join(OFFER_WORDS[o] for o in _offers(mine))
        buttons = [
            choice_button(
                ANSWER_WORDS[s], ids.GUEST_ACTION_FOR_STATUS[s], value, selected=mine is not None and mine.status is s
            )
            for s in GUEST_ANSWERS
        ]
        for role in RoleOffer:
            offered = mine is not None and role in mine.offers
            action = (ids.GUEST_ACTION_FOR_WITHDRAW if offered else ids.GUEST_ACTION_FOR_OFFER)[role]
            buttons.append(choice_button(OFFER_LABELS[role], action, value, selected=offered))
        blocks += [section(line, block_id=f"guest-{guest.id}"), actions(*buttons, block_id=f"guest-actions-{guest.id}")]
    if len(guests) > MAX_CARD_GUESTS:
        blocks.append(context("Answer for your other guests on the web."))
    return blocks


def your_answer(
    event: note.EventInfo,
    mine: Response | None,
    member_id: int,
    ctx: RenderContext,
    *,
    run: int | None = None,
    notice: str | None = None,
    guest: bool = False,
    can_invite: bool = False,
    guests: tuple[CardGuest, ...] = (),
) -> Message:
    """The Your answer card: an ephemeral only ``member_id`` sees, with their answer and offers
    marked on the same buttons the call has (spec section 10). ``event`` is the call as it is now
    and ``mine`` their response to it. A tap in the channel posts a card; a tap on the card
    replaces it, so ``run`` is the run the tapped card named, or None after a channel tap.
    ``notice`` is why a tap changed nothing, such as someone else coordinating already."""
    title = f"*{_title(event)}*"
    if run is not None and run != event.run:
        # Another run's answers aren't what this card was about; don't mix them in.
        gone = [
            section(f"{title}\n*{MOVED_ON}* {STARTED_AGAIN}"),
            actions(_web_button(ctx, event), block_id="more"),
        ]
        return labelled(_finish(your_answer_text(event, mine, run=run), gone), ctx)
    lines = [title]
    live = not _moved_on(event, run) and event.state is EventState.COLLECTING
    if not live:
        status = escape(_call_status(event))
        lines.append(f"*{MOVED_ON}* {status}" if _moved_on(event, run) else status)
    lines += _mine_lines(event, mine, member_id)
    blocks: list[Block] = [section(join_lines(lines))]
    if notice:
        blocks.append(context(escape(notice)))
    text = your_answer_text(event, mine, run=run)
    if not live:
        blocks.append(actions(_web_button(ctx, event), block_id="more"))
        return labelled(_finish(text, blocks), ctx)
    closes = local_clock(event.respond_deadline, event.timezone)
    that_day = " that day" if event.early else ""
    blocks.append(context(f"Only you can see this. Tap to change your answer until *{closes}*{that_day}."))
    answer = answer_actions(event, mine, guest=guest, can_invite=can_invite)
    blocks.append(actions(*answer.participation, block_id="participation"))
    blocks.append(actions(*answer.roles, block_id="roles"))
    blocks += _guest_rows(event, guests)
    blocks.append(actions(*answer.more, _web_button(ctx, event), block_id="more"))
    return labelled(_finish(text, blocks), ctx)


@dataclass(frozen=True)
class AnswerActions:
    """A call's answer buttons for one person, with their own choices selected, in the rows the
    Your answer card and App Home's call card both show (#84). Slack folds a row past about
    five buttons into "+ N more", so no row here has more than four."""

    participation: list[Block]
    """**In**, **Maybe**, and **Out**; a guest's are **In** and **Out** (#38)."""
    roles: list[Block]
    """The role offers, each saying which way it goes, and **I'll coordinate** while nobody is."""
    more: list[Block]
    """**Details**, for members, and **Invite a guest** for whoever may: the start of a last
    row that each surface fills with its own buttons."""

    @property
    def all(self) -> list[Block]:
        """Every button, in order."""
        return [*self.participation, *self.roles, *self.more]


def answer_actions(
    event: note.EventInfo, mine: Response | None, *, guest: bool = False, can_invite: bool = False
) -> AnswerActions:
    """The answer buttons for ``event`` as it is now, drawn for its run (``ids.card_value``), with
    ``mine`` selected. The Your answer card and App Home both draw them here, so the two offer
    the same actions with the same IDs and can't drift apart (#84)."""
    value = ids.card_value(event.id, event.run)
    roles: list[Block] = []
    for role in RoleOffer:
        offered = mine is not None and role in mine.offers
        action = (ids.CARD_ACTION_FOR_WITHDRAW if offered else ids.CARD_ACTION_FOR_OFFER)[role]
        roles.append(choice_button(OFFER_LABELS[role], action, value, selected=offered))
    if event.coordinator is None:
        roles.append(button("I'll coordinate", ids.CARD_COORDINATE, value))
    more: list[Block] = []
    if not guest:  # a guest has only In, Out, and the roles (#38)
        more.append(button("Details", ids.CARD_DETAILS, value))
    if can_invite:
        more.append(button("Invite a guest", ids.INVITE_GUEST, value))
    return AnswerActions(_answer_buttons(value, mine, guest=guest), roles, more)


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
    who = (
        f"Hi {escape(notification.via.name)}! Your guest {escape(notification.member.name)} is"
        if notification.via is not None
        else f"Thanks for pitching in, {escape(notification.member.name)}! You're"
    )
    lines = [
        f"{who} *{role}* for {_slot(event)} on {day_label(event.day)}: *{_item_name(proposal.item)}* · "
        f"{_kind(proposal)}.",
        f"*When:* {when} · *Eating:* {_head_count(decision)}",
    ]
    if decision.dietary_notes:
        lines.append(f"*Dietary needs:* {escape(', '.join(decision.dietary_notes))}")
    others = [a for a in decision.assignments if a.member.id != notification.member.id]
    if others:
        lines.append(
            "*Also helping:* " + ", ".join(f"{escape(a.member.name)} ({ROLE_PHRASES[a.role]})" for a in others)
        )
    buttons = [*_item_buttons(proposal.item, proposal.mode), _web_button(ctx, event)]
    blocks = [section(join_lines(lines)), actions(*buttons, block_id="more")]
    subject = f"{notification.member.name} is" if notification.via is not None else "You're"
    return _finish(f"{subject} {role} for {_slot(event)} at {when}: {proposal.item.name}.", blocks)


# Endings -----------------------------------------------------------------------------------------


ABANDONED = {
    EndReason.TOO_FEW: "Not enough people for a group {slot} on {day}, so everyone's on their own this time.",
    EndReason.NO_COVERAGE: (
        "Nobody was free to cook or fetch any of the picks for {slot} on {day}, so everyone's on their own this time."
    ),
}
NEEDS_UNMET = (
    "Nothing in the catalog fits everyone's dietary needs for {slot} on {day}, so everyone's on their own this "
    "time. Adding a dish or a place that fits them helps next time."
)
ABANDONED_OTHERWISE = "The {slot} call for {day} has ended, so everyone's on their own this time."


def abandoned_message(notification: note.CallAbandoned, ctx: RenderContext) -> Message:
    event = notification.event
    template = NEEDS_UNMET if notification.needs_unmet else ABANDONED.get(notification.reason, ABANDONED_OTHERWISE)
    text = template.format(slot=_slot(event), day=day_label(event.day))
    blocks = [
        section(text + " Thanks for answering, and see you at the next one!"),
        context(link(ctx.url(event.path), "Open the call on the web")),
    ]
    return _finish(text, blocks)


ENJOY = {Slot.BREAKFAST: "Enjoy your morning!", Slot.LUNCH: "Enjoy your day!", Slot.DINNER: "Enjoy your evening!"}


def _stop(reason: str) -> str:
    """A full stop after a reason, unless it ends a sentence already."""
    return "" if reason.endswith((".", "!", "?", "…")) else "."


def _called_off(event: note.EventInfo, by: note.PersonInfo | None, reason: str | None) -> str:
    """Plain words for a call-off, such as "Sam called off dinner for Friday, Oct 23: pizza at
    Grandma's." Escape before putting it in a block."""
    who = by.name if by is not None else "An admin"
    said = f": {reason}{_stop(reason)}" if reason else "."
    return f"{who} called off {_what(event)} for {day_label(event.day)}{said}"


def _reason(event: note.EventInfo, by: note.PersonInfo | None, reason: str | None) -> str:
    """``_called_off`` as mrkdwn, with the reason in bold."""
    who = escape(by.name) if by is not None else "An admin"
    said = f": *{escape(reason)}*{_stop(reason)}" if reason else "."
    return f"{who} called off {_what(event)} for {day_label(event.day)}{said}"


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
    via = notification.via
    reader, subject = (
        (via.name, f"Your guest {notification.member.name} is") if via else (notification.member.name, "You're")
    )
    lines = [
        f"Change of plans, {escape(reader)}. " + _reason(event, notification.cancelled_by, notification.note),
        f"{escape(subject)} no longer {role}: ~{_item_name(proposal.item)}~. Thanks for being ready to pitch in!",
    ]
    blocks = [section(join_lines(lines)), actions(_web_button(ctx, event), block_id="more")]
    text = f"{_called_off(event, notification.cancelled_by, notification.note)} {subject} no longer {role}."
    return _finish(text, blocks)


def started_call_cancelled_message(notification: note.StartedCallCancelled, ctx: RenderContext) -> Message:
    event = notification.event
    lines = [
        _reason(event, notification.cancelled_by, notification.note),
        f"That's the {_slot(event)} call you started. It hadn't been posted yet, so nobody else was told.",
    ]
    blocks = [section(join_lines(lines)), actions(_web_button(ctx, event), block_id="more")]
    return _finish(_called_off(event, notification.cancelled_by, notification.note), blocks)


# The schedule (FR-44) ----------------------------------------------------------------------------

MODE_WORDS = {Mode.COOK: "cook", Mode.PICKUP: "pickup", Mode.DELIVERY: "delivery"}


def _minutes(count: int) -> str:
    return plural(count, "minute")


def _schedule_fields(schedule: note.SlotScheduleInfo) -> list[tuple[str, str]]:
    """Each field of a slot's schedule, labeled, in the order the web form shows them."""
    return [
        ("Daily call", "on" if schedule.enabled else "off"),
        ("Call time", clock(schedule.call_time)),
        ("Usual meal time", clock(schedule.meal_time)),
        ("Time to answer", _minutes(schedule.response_minutes)),
        ("Time to vote", _minutes(schedule.vote_minutes)),
        ("Reminder", f"{_minutes(schedule.reminder_minutes)} before answers close"),
        ("Usual ways to get it", ", ".join(MODE_WORDS[m] for m in schedule.modes)),
    ]


def schedule_changed_message(notification: note.ScheduleChanged, ctx: RenderContext) -> Message:
    """FR-44: who changed a slot's standing schedule, what changed, and from which day. Calls
    already out or scheduled keep their times, so it says so instead of naming them, and says
    when today's call keeps the old time because the new one has passed."""
    slot = notification.slot.value
    who = notification.changed_by.name
    when = day_label(notification.starts_on)
    before = dict(_schedule_fields(notification.before))
    changes = [(label, before[label], value) for label, value in _schedule_fields(notification.after)]
    changes = [(label, old, new) for label, old, new in changes if old != new]
    lines = [f"{escape(who)} changed the {slot} schedule, starting *{when}*."]
    lines += [f"• {label}: {old} → *{new}*" for label, old, new in changes]
    kept = notification.kept_today_at
    if kept is not None:
        lines.append(f"Today's {slot} call still goes out at {clock(kept)}, as planned.")
    blocks = [
        section(join_lines(lines)),
        context(
            "Calls already out or scheduled keep their times; a call started again uses the new ones.",
            link(ctx.url(notification.path), "See the schedule"),
        ),
    ]
    summary = "; ".join(f"{label.lower()} {old} → {new}" for label, old, new in changes)
    text = f"{who} changed the {slot} schedule, starting {when}" + (f": {summary}." if summary else ".")
    return _finish(text, blocks)


def schedule_reply(schedules: dict[Slot, SlotSchedule], ctx: RenderContext) -> Message:
    """``/dinnerbell schedule``: each slot's standing schedule, and a link to change it on the
    web. Only the person who asked sees it."""
    lines: list[str] = []
    for slot, schedule in schedules.items():
        name = slot.value.capitalize()
        if not schedule.enabled:
            lines.append(f"*{name}:* no daily call.")
            continue
        answer = clock(_add(schedule.call_time, schedule.response_minutes))
        vote = clock(_add(schedule.call_time, schedule.response_minutes + schedule.vote_minutes))
        lines.append(
            f"*{name}:* the call goes out at *{clock(schedule.call_time)}*. Answers close at {answer}, "
            f"voting at {vote}, and the usual meal time is {clock(schedule.meal_time)}."
        )
    blocks = [
        section(join_lines(lines)),
        actions(button("Change the schedule", f"{ids.LINK_PREFIX}schedule", url=ctx.url("/schedule")), block_id="more"),
        context("Anyone in the household can change it. Each change is posted in the Dinnerbell channel."),
    ]
    text = "When calls go out: " + " ".join(lines).replace("*", "")
    return labelled(_finish(text, blocks), ctx)


def _add(at: time, minutes: int) -> time:
    """``at`` plus ``minutes`` on the clock. Schedules end by midnight (``check_schedule``)."""
    total = (at.hour * 60 + at.minute + minutes) % (24 * 60)
    return time(total // 60, total % 60)


# Offers (FR-4) -----------------------------------------------------------------------------------

OFFER_VERBS = {Mode.COOK: "is making", Mode.PICKUP: "is picking up", Mode.DELIVERY: "is ordering"}
CANT_MAKE_IT = "Can't make it"


def _offer_line(event: note.EventInfo, offer: note.OfferInfo, *, rich: bool) -> str:
    """ "Sam is picking up Siam Garden for lunch at 12:30 PM." With ``rich``, mrkdwn with the
    item linked and the time in bold; otherwise plain words, unescaped."""
    at = local_clock(offer.at, event.timezone)
    if not rich:
        return f"{offer.offerer.name} {OFFER_VERBS[offer.mode]} {offer.item.name} for {_slot(event)} at {at}."
    who = escape(offer.offerer.name)
    return f"{who} {OFFER_VERBS[offer.mode]} *{_item_name(offer.item)}* for {_slot(event)} at *{at}*."


def _offer_status(event: note.EventInfo, offer: note.OfferInfo) -> str:
    """Plain words: escape before putting it in a block."""
    match event.state:
        case EventState.COLLECTING if offer.full:
            return "It's full. Thanks, everyone!"
        case EventState.COLLECTING:
            return f"{plural(offer.seats_left, 'serving')} spare. Tap Count me in to join."
        case EventState.DECIDED:
            return "It's on! No more seats."
        case EventState.DONE:
            return f"Done. Thanks, {offer.offerer.name}!"
        case EventState.CANCELLED if event.ended_by is not None:
            reason = f" “{event.end_note}”" if event.end_note else ""
            return f"Called off by {event.ended_by.name}.{reason}"
        case _:
            return "This offer ended."


def _offer_roster(roster: tuple[note.RosterEntry, ...], offer: note.OfferInfo, ctx: RenderContext) -> str:
    eating = [e for e in roster if e.status is Status.IN]
    people = sum(1 + e.guests for e in eating)
    lines = [f"*Eating* · {plural(people, 'person', 'people')}"]
    for entry in eating:
        line = f"• {ctx.directory.mention(entry.member)}"
        if entry.guests:
            line += f" · +{plural(entry.guests, 'guest')}"
        lines.append(line)
    if offer.dietary_notes:
        lines.append(f"*Dietary needs:* {escape(', '.join(offer.dietary_notes))}")
    return join_lines(lines)


def offer_buttons(event: note.EventInfo, offer: note.OfferInfo) -> list[Block]:
    """The offer message's buttons. Everyone sees the same message, so they name no one's own
    state: **Count me in** joins or drops out, and **+1 guest** adds one, while seats are left;
    once it's full only **Can't make it** stays, to free a seat (spec section 10)."""
    value = ids.card_value(event.id, event.run)
    if event.state is EventState.COLLECTING and not offer.full:
        return [
            button(
                "Count me in",
                ids.JOIN_OFFER,
                value,
                style="primary",
                accessibility_label=f"Join {offer.offerer.name}'s {_slot(event)} offer, or tap again to drop out",
            ),
            button("+1 guest", ids.ADD_GUEST, value),
        ]
    if event.state is EventState.COLLECTING:
        return [button(CANT_MAKE_IT, ids.LEAVE_OFFER, value)]
    if event.state is EventState.DECIDED:
        return [mark_done_button(event)]
    if event.state is EventState.DONE:
        return [feedback_button(event)]
    return []


def offer_message(notification: note.OfferPosted | note.OfferUpdated, ctx: RenderContext) -> Message:
    """The offer message (spec section 10): offerer, item, mode, time, spare servings, and who's
    eating, with **Count me in** and **+1 guest** until it's full or its time."""
    event, offer = notification.event, notification.offer
    status = _offer_status(event, offer)
    top = _offer_line(event, offer, rich=True) + "\n" + escape(status)
    if event.state is EventState.CANCELLED:
        top = f"~{_offer_line(event, offer, rich=True)}~\n{escape(status)}"
    blocks: list[Block] = [header(_title(event)), section(top), section(_offer_roster(notification.roster, offer, ctx))]
    buttons = offer_buttons(event, offer)
    buttons += _item_buttons(offer.item, offer.mode)
    buttons.append(_web_button(ctx, event))
    if event.state in {EventState.COLLECTING, EventState.DECIDED}:
        buttons.append(button(CALL_OFF, ids.CANCEL_CALL, ids.menu_value(ids.MenuChoice.CANCEL, event.id, event.run)))
    blocks.append(actions(*buttons, block_id="more"))
    return _finish(f"{_offer_line(event, offer, rich=False)} {status}", blocks)


def offer_full_message(notification: note.OfferFull, ctx: RenderContext) -> Message:
    """To whoever made the offer, once, when the last seat goes (spec section 9)."""
    event, offer = notification.event, notification.offer
    eating = plural(offer.taken, "person", "people")
    lines = [
        f"Your {_slot(event)} offer is full, {escape(notification.member.name)}: *{eating}* joining you for "
        f"*{_item_name(offer.item)}* at {local_clock(offer.at, event.timezone)}. Thanks for sharing!"
    ]
    if offer.dietary_notes:
        lines.append(f"*Dietary needs:* {escape(', '.join(offer.dietary_notes))}")
    blocks = [section(join_lines(lines)), actions(_web_button(ctx, event), block_id="more")]
    return _finish(f"Your {_slot(event)} offer is full: {eating} joining you.", blocks)


DISCARDED = "Discarded (test)"


def discarded_message(notification: note.CallDiscarded) -> Message:
    """What every message of a call discarded on a test instance becomes (#60): no buttons and
    no link, since the call and its page are gone."""
    title = _title(notification.event)
    blocks = [
        section(f"~{escape(title)}~ · *{DISCARDED}*"),
        context("An admin discarded this call on the test instance, with everything in it."),
    ]
    return _finish(f"{title}: {DISCARDED}", blocks)
