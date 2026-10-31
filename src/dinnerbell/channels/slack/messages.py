# SPDX-License-Identifier: AGPL-3.0-or-later
"""Block Kit for every notification (spec section 10, docs/channels.md).

Messages are short and friendly. They thank people for pitching in and never scold anyone for
not answering. Each one links to its event page on the web (FR-37). Rendering is pure: the
same notification and directory always give the same JSON, which the golden tests pin down.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from typing import Any

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.badge_text import THUMBNAIL_CUTOFF, ArtIds, art_element, icon_rows, tag, wanted
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
from dinnerbell.domain import labels
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import (
    JOIN_LATE_PROMPT,
    AssignmentRole,
    EndReason,
    EventKind,
    EventState,
    Response,
    RoleOffer,
    Status,
    Window,
)
from dinnerbell.domain.labels import OFFER_LABELS, OFFER_WORDS, OFFERED_LABELS, ROLE_TITLES
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import SLACK_USER_ID_PATTERN
from dinnerbell.domain.schedule import SlotSchedule

MAX_PROPOSALS = 10
"""Services allow 10 proposals per call. Each takes two blocks, well under the 50 a message
may have; any beyond 10 are listed on the web."""

ROLE_PHRASES = {
    AssignmentRole.COOK: "cooking",
    AssignmentRole.PICKUP: "picking up",
    AssignmentRole.ORDER: "ordering",
    AssignmentRole.COORDINATOR: "coordinating",
}

ANSWERS = labels.ANSWER_ORDER
"""The participation buttons, in the order they're shown: In, Maybe, Out, as on the web."""
ANSWER_WORDS = labels.ANSWER_LABELS
SELECTED = "✓ "
"""Marks a selected button's text as well as its color, for color-blind people and for Slack
themes where primary isn't green."""
MOVED_ON = "This call moved on."
STARTED_AGAIN = "It started again, so answer on the newest call message."
"""Why a card or reminder for an earlier run can't act: answers belong to the new one."""

CLOSE_LABELS = labels.CLOSE_LABELS
MENU_WINDOWS = {EventState.COLLECTING: Window.RESPOND, EventState.VOTING: Window.VOTE}
CALL_OFF = labels.CALL_OFF
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
    art: ArtIds = field(default_factory=dict)
    """Uploaded badge art. A badge it has shows its art; any other shows as text."""

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
            return decision_message(
                notification.event, notification.decision, ctx, notification.locked_by, mention=True
            )
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
        case note.LateJoinChanged():
            return late_join_changed_message(notification, ctx)
        case note.RolesToChoose():
            return roles_to_choose_message(notification, ctx)
        case note.PicksShuffled():
            return picks_shuffled_message(notification)
        case note.BadgesEarned():
            return badges_earned_message(notification, ctx)
        case note.MonthlyRecap():
            return monthly_recap_message(notification, ctx)
    raise TypeError(f"no Slack rendering for {type(notification).__name__}")  # pragma: no cover


def _finish(text: str, blocks: list[Block]) -> Message:
    """``text`` is plain words built from names; Slack parses it as mrkdwn, so escape it."""
    return Message(clip_mrkdwn(escape(text), MAX_FALLBACK), blocks[:MAX_MESSAGE_BLOCKS])


def _slot(event: note.EventInfo) -> str:
    return event.slot.value


def _title(event: note.EventInfo) -> str:
    kind = "offer" if event.kind is EventKind.OFFER else "call"
    return f"{event.slot.value.capitalize()} {kind} · {day_label(event.day)}"


def _post_title(event: note.EventInfo, what: str) -> str:
    """The first line of the picks and decision posts, which stand alone in the channel (#92):
    the slot and the day tell posts from different calls apart."""
    return f"{event.slot.value.capitalize()} {what} · {day_label(event.day)}"


def _what(event: note.EventInfo) -> str:
    """What was called off: "dinner" for a call, "the dinner offer" for an offer."""
    return f"the {_slot(event)} offer" if event.kind is EventKind.OFFER else _slot(event)


def _web_button(ctx: RenderContext, event: note.EventInfo, text: str = labels.VIEW_ON_WEB) -> Block:
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


LATE_IN = labels.JOIN_LATE
LATE = "joined late"


def join_late_button(event: note.EventInfo) -> Block:
    """**I'm in (late)** (#76), for the run ``event`` shows: everyone who missed the answers can
    still join while the call is voting. The service says why not to anyone who can't."""
    return button(LATE_IN, ids.JOIN_LATE, ids.card_value(event.id, event.run))


def restart_button(event: note.EventInfo) -> Block:
    """**Start dinner again** where a call-off is told (#149): on the called-off call message and
    on the starter's message about an unposted one. It names the run it was drawn for. Any member
    may, until the usual meal time plus two hours (FR-56); the service says why not after that."""
    return button(f"Start {_slot(event)} again", ids.RESTART, ids.card_value(event.id, event.run), style="primary")


def _entry_line(entry: note.RosterEntry, ctx: RenderContext) -> str:
    parts: list[str] = []
    if entry.late:
        parts.append(f"{LATE}, vote counts" if entry.vote_counts else LATE)
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
    by_status = {status: [e for e in roster if e.status is status and not e.late] for status in Status}
    by_status[Status.IN] = [e for e in roster if e.status is Status.IN]  # late joiners taking part too (#76)
    taken_off = [e for e in roster if e.late and e.status is Status.OUT]
    lines: list[str] = []
    if by_status[Status.IN]:
        lines.append(f"*In* · {plural(head_count, 'person', 'people')} eating")
        lines += [_entry_line(e, ctx) for e in by_status[Status.IN]]
    for status, label in ((Status.MAYBE, "Maybe"), (Status.OUT, "Out")):
        if by_status[status]:
            lines.append(f"*{label}:* {ctx.directory.mentions(e.member for e in by_status[status])}")
    if taken_off:
        lines.append(f"*Joined late, not taking part:* {ctx.directory.mentions(e.member for e in taken_off)}")
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


def _call_status(event: note.EventInfo, *, picks: str = "posted below") -> str:
    """Plain words with mrkdwn emphasis: escape before putting it in a block. ``picks`` says
    where the picks are, for a reader who isn't looking at the call message."""
    slot = _slot(event)
    match event.state:
        case EventState.COLLECTING if event.early:  # posted the day before, or earlier (#59)
            by = local_clock(event.respond_deadline, event.timezone)
            return f"Who's in for {slot} on {day_label(event.day)}? Answer by *{by}* that day."
        case EventState.COLLECTING:
            return f"Who's in for {slot}? Answer by *{local_clock(event.respond_deadline, event.timezone)}*."
        case EventState.VOTING:
            closes = f" Vote by *{local_clock(event.vote_deadline, event.timezone)}*." if event.vote_deadline else ""
            return f"Thanks for answering! Voting is open: the proposals are {picks}.{closes}"
        case EventState.DECIDED if event.plan is not None:
            plan = event.plan
            at = local_clock(plan.meal_time, event.timezone)
            return f"It's decided: {plan.item}, {plan.mode.label.lower()}, at {at}"
        case EventState.DECIDED:
            return f"It's decided! The plan is {picks}."
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
    if event.state is EventState.VOTING:
        status += f"\nMissed the answers? Tap *{LATE_IN}* to join."
    blocks: list[Block] = [
        header(_title(event)),
        section(status),
        section(_roster(event, notification.roster, notification.waiting_on, notification.head_count, ctx)),
    ]
    if event.state is EventState.VOTING:
        # Answering is over, but anyone who missed it can still join late (#76), and someone can
        # still take the vote in hand. Each names the run, so a message from before a restart
        # can't act on the newer one (#54).
        late = [join_late_button(event)]
        if event.coordinator is None:
            late.append(button(labels.COORDINATE, ids.CALL_COORDINATE, ids.card_value(event.id, event.run)))
        blocks.append(actions(*late, block_id="roles"))
    more: list[Block] = []
    if event.state is EventState.COLLECTING:
        # Everyone sees this message, so it offers no choices, only the way to your own card,
        # where In, Maybe, Out, the offers, I'll coordinate, and Details are (issue #52).
        more.append(answer_button(event))
    if event.state is EventState.CANCELLED:
        more.append(restart_button(event))
    more.append(_web_button(ctx, event))
    more += call_menu(event)
    blocks.append(actions(*more, block_id="more"))
    text = f"{_title(event)}: {plain_status.splitlines()[0]}"
    return _finish(text.replace("*", ""), blocks)


# Proposals ---------------------------------------------------------------------------------------


MAX_IN_SECTIONS = 3
"""Sections of "In:" names a post shows; the web has the rest."""
IN_CHUNK = 2800


def _in_blocks(people: tuple[note.PersonInfo, ...], ctx: RenderContext, *, mention: bool) -> list[Block]:
    """Who's in, as mentions when ``mention`` (a post's first rendering) and plain names
    otherwise: an edit must not add a mention, because Slack may notify it. Split across
    sections so none passes Slack's limit, and cut off after ``MAX_IN_SECTIONS``."""
    if not people:
        return []
    items = [ctx.directory.mention(p) if mention else escape(p.name) for p in people]
    chunks: list[list[str]] = [[]]
    size = len("*In:* ")
    for item in items:
        if chunks[-1] and size + len(item) + 2 > IN_CHUNK:
            chunks.append([])
            size = 0
        chunks[-1].append(item)
        size += len(item) + 2
    shown = chunks[:MAX_IN_SECTIONS]
    blocks = [section(", ".join(chunk)) for chunk in shown]
    blocks[0] = section("*In:* " + ", ".join(shown[0]))
    left = sum(len(c) for c in chunks[MAX_IN_SECTIONS:])
    if left:
        blocks.append(context(f"…and {plural(left, 'more person', 'more people')} on the web."))
    return blocks


def _item_name(item: note.ItemInfo) -> str:
    return link(item.url, item.name) if item.url else escape(item.name)


def _kind(proposal: note.ProposalInfo) -> str:
    cuisines = f" · {escape(', '.join(proposal.item.cuisines))}" if proposal.item.cuisines else ""
    return f"{proposal.mode.label}{cuisines}"


def _proposal_text(proposal: note.ProposalInfo, number: int, ctx: RenderContext) -> str:
    lines = [f"*{number}. {_item_name(proposal.item)}* · {_kind(proposal)}"]
    if proposal.reasons:
        lines.append("_" + escape(" ".join(proposal.reasons)) + "_")
    votes = plural(proposal.votes, "vote")
    if proposal.counted:
        votes += f": {ctx.directory.mentions(proposal.counted)}"
    if proposal.not_counted:  # late joiners' votes show, marked, and don't count (#76)
        late = ", ".join(f"{ctx.directory.mention(p)} (late)" for p in proposal.not_counted)
        votes += f" · not counted: {late}"
    lines.append(votes if proposal.voters else "No votes yet")
    lines += [escape(w) for w in proposal.warnings]
    return join_lines(lines)


MAX_LATE_SHOWN = 8
"""Late joiners the proposals message lists with their switches; the web has the rest."""


def _late_line(late: note.LateJoinerInfo, ctx: RenderContext) -> str:
    part = "taking part" if late.taking_part else "not taking part"
    vote = "vote counts" if late.vote_counts else "vote doesn't count"
    return f"{ctx.directory.mention(late.member)} joined late · {part} · {vote}"


def late_menu(event: note.EventInfo, late: note.LateJoinerInfo) -> Block:
    """The coordinator's two switches for one late joiner (#76). Everyone sees the menu, so the
    server checks who picked it; each option names the run, and turns its switch the other way."""
    who, member = clip(late.member.name, 40), late.member.id
    part = ids.late_value(ids.LateSwitch.TAKING_PART, not late.taking_part, event.id, event.run, member)
    vote = ids.late_value(ids.LateSwitch.VOTE_COUNTS, not late.vote_counts, event.id, event.run, member)
    return overflow(
        ids.LATE_MENU,
        [
            option(f"Take {who} off this call" if late.taking_part else f"Put {who} back on", part),
            option(f"Don't count {who}'s vote" if late.vote_counts else f"Count {who}'s vote", vote),
        ],
    )


def _late_blocks(notification: note.ProposalsReady | note.ProposalsUpdated, ctx: RenderContext) -> list[Block]:
    """Who joined late, and, while voting, the coordinator's switches beside each one (#76)."""
    if not notification.late:
        return []
    event = notification.event
    if event.state is not EventState.VOTING:
        return [context("Joined late: " + ctx.directory.mentions(late.member for late in notification.late))]
    blocks: list[Block] = [
        context("Joined late: their votes show, but count only if the coordinator says so, with ⋯ beside each name.")
    ]
    shown = notification.late[:MAX_LATE_SHOWN]
    for late in shown:
        block = section(_late_line(late, ctx), block_id=f"late-{late.member.id}")
        blocks.append(block | {"accessory": late_menu(event, late)})
    if len(notification.late) > len(shown):
        blocks.append(context(f"…and {plural(len(notification.late) - len(shown), 'more')} on the web."))
    return blocks


def proposals_message(notification: note.ProposalsReady | note.ProposalsUpdated, ctx: RenderContext) -> Message:
    event = notification.event
    voting = event.state is EventState.VOTING
    people = plural(notification.head_count, "person", "people")
    if voting:
        closes = (
            f" Voting closes at *{local_clock(event.vote_deadline, event.timezone)}*." if event.vote_deadline else ""
        )
        intro = f"Vote for any you'd enjoy.{closes}"
    else:
        intro = "Voting is closed."
    blocks: list[Block] = [section(f"*{_post_title(event, 'proposals')}* for {people}\n{intro}")]
    if voting and event.shuffle_note:  # plain words: an edit pings nobody (#92)
        blocks.append(context(escape(event.shuffle_note)))
    if voting and any(p.tied for p in notification.proposals):
        blocks.append(context("Tied for the most votes. The coordinator can break the tie."))
    # Only the first post mentions anyone (an edit that added a mention might ping it), and the
    # list comes after the instructions, so a big household can't push them out.
    blocks += _in_blocks(notification.participants, ctx, mention=isinstance(notification, note.ProposalsReady))
    shown = notification.proposals[:MAX_PROPOSALS]
    for number, proposal in enumerate(shown, start=1):
        blocks.append(section(_proposal_text(proposal, number, ctx), block_id=f"proposal-{proposal.id}"))
        if voting:
            value = ids.proposal_value(event.id, proposal.id)
            name = clip(proposal.item.name, 60)
            lock = confirm(labels.LOCK_TITLE, labels.decides_now(name), labels.LOCK_SUBMIT)
            row = [button("Vote", ids.VOTE, value, style="primary"), button(labels.LOCK, ids.LOCK, value, confirm=lock)]
            if proposal.tied:
                choose = confirm(labels.BREAK_TIE_TITLE, labels.decides_now(name), labels.BREAK_TIE_SUBMIT)
                row.append(button(labels.BREAK_TIE, ids.BREAK_TIE, value, confirm=choose))
            blocks.append(actions(*row, block_id=f"proposal-actions-{proposal.id}"))
    hidden = len(notification.proposals) - len(shown)
    if hidden:
        blocks.append(context(f"…and {plural(hidden, 'more proposal')} on the web."))
    blocks += _late_blocks(notification, ctx)
    footer: list[Block] = []
    if voting:
        footer.append(button("Add proposal", ids.ADD_PROPOSAL, ids.event_value(event.id)))
        if notification.shuffles_left:
            shuffle = confirm(labels.SHUFFLE_TITLE, labels.SHUFFLE_EFFECT, labels.SHUFFLE_SUBMIT)
            footer.append(button(labels.SHUFFLE, ids.SHUFFLE, ids.card_value(event.id, event.run), confirm=shuffle))
    footer.append(_web_button(ctx, event))
    blocks.append(actions(*footer, block_id="more"))
    text = f"Proposals for {_slot(event)} on {day_label(event.day)}: " + ", ".join(
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
    event: note.EventInfo,
    decision: note.DecisionInfo,
    ctx: RenderContext,
    locked_by: note.PersonInfo | None = None,
    *,
    mention: bool = False,
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
    top = f"*{_post_title(event, 'decision')}*\n{top}"
    if locked_by is not None and event.state is not EventState.CANCELLED:
        top += f"\nLocked in by {ctx.directory.mention(locked_by)}."
    if decision.tie_note and event.state is not EventState.CANCELLED:
        top += f"\n_{escape(decision.tie_note)}._"
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
    if decision.roles_due_at and event.state is EventState.DECIDED:
        lines.append(_roles_pending(event, decision))
    late = [p.member for p in decision.participants if p.late]
    if late:
        lines.append(f"*Joined late:* {ctx.directory.mentions(late)}")
    lines += [escape(w) for w in proposal.warnings]  # the needs of a late joiner's guests (#76)
    if decision.dietary_notes:
        lines.append(f"*Dietary needs:* {escape(', '.join(decision.dietary_notes))}")
    blocks.append(section(join_lines(lines)))
    # Everyone in, not only those with a role (#92), after the lines that matter more.
    blocks += _in_blocks(tuple(p.member for p in decision.participants), ctx, mention=mention)
    buttons: list[Block] = []
    if event.state is EventState.DECIDED:
        buttons.append(mark_done_button(event))
        if decision.assign_roles:
            buttons.append(assign_roles_button(event))
        if event.kind is EventKind.CALL and event.coordinator is not None:  # nobody else may
            buttons.append(adjust_button(event))
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


def assign_roles_button(event: note.EventInfo) -> Block:
    """**Assign roles** (#87): opens the modal for the coordinator, or an admin standing in. Everyone
    sees the button; the service checks who tapped it."""
    return button("Assign roles", ids.ASSIGN_ROLES, ids.card_value(event.id, event.run))


ADJUST = labels.ADJUST


def adjust_button(event: note.EventInfo) -> Block:
    """**Adjust** (spec 5.7): the coordinator's modal for the meal time, who does the role, and
    handing off. Everyone sees the button on the decision; the service checks who tapped it."""
    return button(ADJUST, ids.ADJUST, ids.card_value(event.id, event.run))


def _roles_pending(event: note.EventInfo, decision: note.DecisionInfo) -> str:
    """While the coordinator chooses: who does the role is theirs to say until the time shown."""
    verb = {Mode.COOK: "cooks", Mode.PICKUP: "picks up", Mode.DELIVERY: "orders"}[decision.proposal.mode]
    until = local_clock(decision.roles_due_at, event.timezone) if decision.roles_due_at else ""
    who = escape(event.coordinator.name) if event.coordinator else "The coordinator"  # plain: an edit pings nobody
    return f"_{who} is choosing who {verb}, until {until}. If they don't, the first person who offered does._"


def mark_done_button(event: note.EventInfo) -> Block:
    """Mark done ends the meal and logs it, so it asks first (docs/channels.md)."""
    sure = confirm(labels.mark_done_title(_slot(event)), labels.MARK_DONE_EFFECT, labels.MARK_DONE)
    return button(labels.MARK_DONE, ids.MARK_DONE, ids.event_value(event.id), style="primary", confirm=sure)


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
        accessibility_label=f"Say how {_slot(event)} was: a rating, what went wrong, or thanks to the cook",
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
        why = STARTED_AGAIN if run != event.run else escape(_call_status(event, picks="posted in the channel"))
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
    answer = answer_actions(event, mine, guest=guest)
    blocks = [
        section(text),
        # In, Maybe, Out on a row of their own: one choice, apart from the role offers, which are
        # the card's (#149, Q6). A tap on either edits this message, as on the card.
        actions(*answer.participation, block_id="participation"),
        actions(*answer.roles, block_id="roles"),
        actions(_web_button(ctx, event), block_id="more"),
    ]
    if not guest:
        blocks.append(mute_hint(ctx))
    return _finish(fallback, blocks)


def mute_hint(ctx: RenderContext) -> Block:
    """How to stop reminders, on each one (#149): both are set in Preferences."""
    return context(f"Don't want reminders? Turn them off in {link(ctx.url('/preferences'), 'your preferences')}.")


def guest_invited_message(notification: note.GuestInvited, ctx: RenderContext) -> Message:
    """A registered guest's invitation (#38): In and Out, and the role offers, as on their card,
    as card buttons that name the run. A tap edits it in place, as on the reminder."""
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
        f"{day_label(event.day)} any more, so nobody is *{role}*. "
    )
    coordinating = event.coordinator is not None and event.coordinator.id == notification.member.id
    if coordinating:  # the coordinator picks in Slack; an admin with nobody coordinating, on the web
        text += f"Tap *{ADJUST}* to pick someone who's in."
        buttons = [adjust_button(event), _web_button(ctx, event)]
    else:
        text += "Pick someone who's in on the web."
        buttons = [_web_button(ctx, event)]
    blocks = [section(text), actions(*buttons, block_id="more")]
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
        f"Only you see your feedback, and you can change it until *{closes}*."
    )
    blocks = [
        section(text),
        actions(feedback_button(event), _web_button(ctx, event), block_id="more"),
        mute_hint(ctx),
    ]
    return _finish(f"How was {when}? Tell us by {closes}.", blocks)


# Your answer -------------------------------------------------------------------------------------


def _moved_on(event: note.EventInfo, run: int | None, mine: Response | None = None) -> bool:
    """Whether a card drawn for ``run`` (None: no card was tapped) is out of date: the call has
    started again since, or responses have closed, unless ``mine`` is a late joiner's answer
    while voting, which they can still add roles to (#76)."""
    return run is not None and (run != event.run or not card_live(event, mine))


def card_live(event: note.EventInfo, mine: Response | None) -> bool:
    """Whether a Your answer card for ``event`` as it is now still takes taps: while answers are
    open, and for a late joiner taking part, while voting (#76)."""
    return event.state is EventState.COLLECTING or (late_answer(event, mine) and mine is not None and mine.is_in)


def late_answer(event: note.EventInfo, mine: Response | None) -> bool:
    return event.state is EventState.VOTING and mine is not None and mine.late


def can_join_late(event: note.EventInfo, mine: Response | None, *, guest: bool = False) -> bool:
    """Whether to offer **I'm in (late)** on someone's own card: while voting, to a member who
    isn't in and wasn't switched off (``domain.events.check_can_join_late``)."""
    return event.state is EventState.VOTING and not guest and (mine is None or (not mine.is_in and not mine.late))


GUEST_ANSWERS = labels.GUEST_ANSWER_ORDER


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
    if mine is not None and mine.late:
        lines.append(_late_answer_line(mine))
    elif mine is not None:
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


def not_for_you_line(names: tuple[str, ...]) -> str:
    """For a late joiner's eyes only (#76): picks that miss their own stances or needs."""
    return f"⚠ Not a fit for your preferences: {escape(', '.join(names))}"


def _late_answer_line(mine: Response) -> str:
    """A late joiner's own answer, for their eyes only (#76)."""
    if not mine.is_in:
        return "You joined late, and the coordinator has you out for this one."
    line = "Your answer: *In (joined late)*"
    if mine.offers:
        line += " · can " + " or ".join(OFFER_WORDS[o] for o in _offers(mine))
    vote = "Your vote counts." if mine.vote_counts else "Your vote shows, but counts only if the coordinator says so."
    return f"{line}\n{vote}"


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
    summary = ANSWER_WORDS[mine.status] + (" (joined late)" if mine.late else "")
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
            buttons.append(choice_button(OFFERED_LABELS[role], action, value, selected=offered))
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
    not_for_you: tuple[str, ...] = (),
) -> Message:
    """The Your answer card: an ephemeral only ``member_id`` sees, with their answer and offers
    marked on the same buttons the call has (spec section 10). ``event`` is the call as it is now
    and ``mine`` their response to it. A tap in the channel posts a card; a tap on the card
    replaces it, so ``run`` is the run the tapped card named, or None after a channel tap.
    ``notice`` is why a tap changed nothing, such as someone else coordinating already.
    ``not_for_you`` names the picks that miss a late joiner's own stances or needs (#76): only
    they see it (FR-29)."""
    title = f"*{_title(event)}*"
    if run is not None and run != event.run:
        # Another run's answers aren't what this card was about; don't mix them in.
        gone = [
            section(f"{title}\n*{MOVED_ON}* {STARTED_AGAIN}"),
            actions(_web_button(ctx, event), block_id="more"),
        ]
        return labelled(_finish(your_answer_text(event, mine, run=run), gone), ctx)
    lines = [title]
    moved_on = _moved_on(event, run, mine)
    live = not moved_on and card_live(event, mine)
    if not live:
        status = escape(_call_status(event, picks="posted in the channel"))
        lines.append(f"*{MOVED_ON}* {status}" if moved_on else status)
    lines += _mine_lines(event, mine, member_id)
    if not_for_you and late_answer(event, mine):
        lines.append(not_for_you_line(not_for_you))
    blocks: list[Block] = [section(join_lines(lines))]
    if notice:
        blocks.append(context(escape(notice)))
    text = your_answer_text(event, mine, run=run)
    if not live:
        more = [join_late_button(event)] if can_join_late(event, mine, guest=guest) else []
        blocks.append(actions(*more, _web_button(ctx, event), block_id="more"))
        return labelled(_finish(text, blocks), ctx)
    if event.state is EventState.VOTING:  # a late joiner adds roles while voting (#76)
        closes = local_clock(event.vote_deadline, event.timezone) if event.vote_deadline else None
        until = f" until voting closes at *{closes}*" if closes else ""
        blocks.append(context(f"Only you can see this. Offer to pitch in{until}."))
        blocks.append(actions(*answer_actions(event, mine, guest=guest).roles, block_id="roles"))
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
        roles.append(button(labels.COORDINATE, ids.CARD_COORDINATE, value))
    more: list[Block] = []
    if not guest:  # a guest has only In, Out, and the roles (#38)
        more.append(button(labels.DETAILS, ids.CARD_DETAILS, value))
    if can_invite:
        more.append(button("Invite a guest", ids.INVITE_GUEST, value))
    return AnswerActions(_answer_buttons(value, mine, guest=guest), roles, more)


def maybe_resolved_message(notification: note.MaybeResolved, ctx: RenderContext) -> Message:
    event = notification.event
    text = (
        f"The {_slot(event)} call for {day_label(event.day)} closed while you were a maybe, so you're "
        f"counted out this time. If your plans change, tap *{LATE_IN}* on the call while the vote is open."
    )
    blocks = [section(text), actions(join_late_button(event), _web_button(ctx, event), block_id="more")]
    return _finish(f"You're counted out for {_slot(event)} on {day_label(event.day)}.", blocks)


LATE_VOTE = "Join late and vote"


def join_late_vote_prompt(event_id: int, proposal_id: int) -> Message:
    """The private reply to **Vote** from someone who isn't in (#76): it asks first, and its
    button joins them late and casts that vote."""
    text = (
        f"*{JOIN_LATE_PROMPT}* Voting joins you late: you're in, and your vote shows, but counts only "
        "if the coordinator says so."
    )
    join = button(LATE_VOTE, ids.JOIN_LATE_VOTE, ids.proposal_value(event_id, proposal_id), style="primary")
    return _finish(JOIN_LATE_PROMPT, [section(text), actions(join, block_id="join-late")])


def late_join_changed_message(notification: note.LateJoinChanged, ctx: RenderContext) -> Message:
    """To a late joiner (#76): the coordinator took them off the call, kindly, or put them back."""
    event, by = notification.event, escape(notification.by.name)
    slot, day = _slot(event), day_label(event.day)
    if notification.taking_part:
        text = f"Good news: {by} has you back in for {slot} on {day}. Thanks for joining!"
        plain = f"You're back in for {slot} on {day}."
    else:
        text = f"Thanks for wanting to join {slot} on {day}! {by} has you out for this one. See you next time."
        plain = f"You're out for {slot} on {day} this time."
    blocks = [section(text), actions(_web_button(ctx, event), block_id="more")]
    return _finish(plain, blocks)


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
    by = notification.assigned_by
    if by is not None:  # they hadn't offered: say who asked, and whom to tell
        tell = f"tell {escape(by.name)}"
        if event.coordinator is not None and event.coordinator.id == by.id:
            tell += f", who can pick someone else with *{ADJUST}* on the decision"
        lines.insert(1, f"{escape(by.name)} assigned you this. If it doesn't work for you, {tell}.")
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


def roles_to_choose_message(notification: note.RolesToChoose, ctx: RenderContext) -> Message:
    """To the coordinator (#87): the decision left the role without a clear owner, so they pick
    the mode and who does it, until ``due_at``; after that the first volunteer does."""
    event, decision = notification.event, notification.decision
    proposal = decision.proposal
    until = local_clock(notification.due_at, event.timezone)
    name = _item_name(proposal.item)
    reader = notification.via.name if notification.via else notification.member.name
    who = f"Your guest {escape(notification.member.name)} is" if notification.via else "You're"
    text = (
        f"Hi {escape(reader)}! {who} coordinating {_slot(event)} on {day_label(event.day)}: *{name}* · "
        f"{_kind(proposal)}. Nobody has a clear claim on the {proposal.mode.verb}, so it's yours to choose. "
        f"Tap *Assign roles* by *{until}*. After that, the first person who offered does it."
    )
    blocks = [section(text), actions(assign_roles_button(event), _web_button(ctx, event), block_id="more")]
    return _finish(f"Choose who will {proposal.mode.verb} {proposal.item.name} by {until}.", blocks)


def picks_shuffled_message(notification: note.PicksShuffled) -> Message:
    """The note in the call's thread after a shuffle (#87): plain words, and no mention."""
    event = notification.event
    text = (
        f"{notification.by.name} shuffled the proposals for {_slot(event)} ({notification.replaced} replaced). "
        "See the new set in the proposals message."
    )
    return _finish(text, [section(escape(text))])


# Endings -----------------------------------------------------------------------------------------


ABANDONED = {
    EndReason.TOO_FEW: "Not enough people for a group {slot} on {day}, so everyone's on their own this time.",
    EndReason.NO_COVERAGE: (
        "Nobody was free to cook or fetch any of the proposals for {slot} on {day}, "
        "so everyone's on their own this time."
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
        context(link(ctx.url(event.path), labels.VIEW_ON_WEB)),
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
    blocks = [section(join_lines(lines)), context(link(ctx.url(event.path), labels.VIEW_ON_WEB))]
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
    blocks = [section(join_lines(lines)), actions(restart_button(event), _web_button(ctx, event), block_id="more")]
    return _finish(_called_off(event, notification.cancelled_by, notification.note), blocks)


# The schedule (FR-44) ----------------------------------------------------------------------------

MODE_WORDS = {Mode.COOK: "cook", Mode.PICKUP: "pickup", Mode.DELIVERY: "delivery"}
SCHEDULE_LINK = "Schedule on the web"
STATS_LINK = "Stats on the web"
"""One name per page a message links to (#149), as every event link is **View on web**."""


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
            link(ctx.url(notification.path), SCHEDULE_LINK),
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
        actions(button(SCHEDULE_LINK, f"{ids.LINK_PREFIX}schedule", url=ctx.url("/schedule")), block_id="more"),
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
CANT_MAKE_IT = labels.CANT_MAKE_IT


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


ADD_GUEST = "+1 guest"
DROP_GUEST = "\u22121 guest"
"""Read "minus 1 guest": a minus sign, as wide as the plus in **+1 guest**."""


def offer_buttons(event: note.EventInfo, offer: note.OfferInfo) -> list[Block]:
    """The offer message's buttons. Everyone sees the same message, so they name no one's own
    state: while seats are left, **Count me in** joins and **+1 guest** adds one; while it's
    open, **-1 guest** takes one back and **Can't make it** drops out, so each does one thing
    however often it's tapped (#149, spec section 10). The service refuses what doesn't apply."""
    value = ids.card_value(event.id, event.run)
    if event.state is EventState.COLLECTING:
        joining: list[Block] = []
        if not offer.full:
            joining = [
                button(
                    labels.COUNT_ME_IN,
                    ids.JOIN_OFFER,
                    value,
                    style="primary",
                    accessibility_label=f"Join {offer.offerer.name}'s {_slot(event)} offer",
                ),
                button(ADD_GUEST, ids.ADD_GUEST, value),
            ]
        return [
            *joining,
            button(DROP_GUEST, ids.DROP_GUEST, value, accessibility_label="Bring one guest fewer"),
            button(CANT_MAKE_IT, ids.LEAVE_OFFER, value),
        ]
    if event.state is EventState.DECIDED:
        return [mark_done_button(event)]
    if event.state is EventState.DONE:
        return [feedback_button(event)]
    return []


def offer_message(notification: note.OfferPosted | note.OfferUpdated, ctx: RenderContext) -> Message:
    """The offer message (spec section 10): offerer, item, mode, time, spare servings, and who's
    eating, with **Count me in** and **+1 guest** until it's full, and **-1 guest** and **Can't
    make it** until its time."""
    event, offer = notification.event, notification.offer
    status = _offer_status(event, offer)
    top = _offer_line(event, offer, rich=True) + "\n" + escape(status)
    if event.state is EventState.CANCELLED:
        top = f"~{_offer_line(event, offer, rich=True)}~\n{escape(status)}"
    blocks: list[Block] = [header(_title(event)), section(top), section(_offer_roster(notification.roster, offer, ctx))]
    joining = offer_buttons(event, offer)
    if event.state is EventState.COLLECTING:  # a row of its own, so the next stays within five
        blocks.append(actions(*joining, block_id="join"))
        joining = []
    buttons = [*joining, *_item_buttons(offer.item, offer.mode), _web_button(ctx, event)]
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


# Badges and the monthly recap (M4, FR-39, FR-40) ---------------------------------------------------


MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _and(items: list[str]) -> str:
    """``a``, ``a and b``, or ``a, b, and c``."""
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _tags(news: note.BadgeNews) -> list[str]:
    tags = [tag(f"*{escape(b.name)}*", b.tier) for b in news.badges]
    if news.more:
        tags.append(f"{plural(news.more, 'more badge')}")
    return tags


MAX_ART = 12
"""Badges shown as art in one post: each is its own block, and a post holds 50."""


def badges_earned_message(notification: note.BadgesEarned, ctx: RenderContext) -> Message:
    """FR-39: one post for the badges a done call earned. It mentions each member by name order
    (never by what they earned) and only celebrates: no counts against anyone else's. A badge with
    uploaded art (``ctx.art``) gets a line with its art beside it; the rest stay text. With no art,
    the post is one text block. A member with more than ``THUMBNAIL_CUTOFF`` badges gets one line naming
    them and a compact row of small icons instead of a thumbnail row each (#112)."""
    who = ", ".join(n.person.name for n in notification.news)
    drawn = _drawn(notification.news, ctx.art)
    tail = []
    if notification.more_people:
        tail.append(f"…and {plural(notification.more_people, 'more member')} earned badges too.")
    if not any(drawn):
        lines = [f"{ctx.directory.mention(n.person)} earned {_and(_tags(n))}." for n in notification.news]
        blocks = [section(join_lines(["*New badges!* 🎉", *lines, *tail]))]
    else:
        blocks = [section("*New badges!* 🎉")]
        for news, art in zip(notification.news, drawn, strict=True):
            if _is_many(news):
                blocks.append(section(_many_line(news, ctx, art)))
                blocks += icon_rows([art_element(f, f"{b.name}, {b.tier}") for b, f in art.items()])
                continue
            text = [tag(f"*{escape(b.name)}*", b.tier) for b in news.badges if b not in art]
            if news.more:
                text.append(plural(news.more, "more badge"))
            lead = f"{ctx.directory.mention(news.person)} earned"
            for i, (badge, file_id) in enumerate(art.items()):
                last = i == len(art) - 1 and not text
                line = section(
                    f"{lead if i == 0 else 'and'} *{escape(badge.name)}* · {badge.tier}{'.' if last else ''}"
                )
                line["accessory"] = art_element(file_id, f"{badge.name}, {badge.tier}")
                blocks.append(line)
            if text:
                blocks.append(section(f"{'Also earned' if art else lead} {_and(text)}."))
        blocks += [section(t) for t in tail]
    blocks.append(
        context("Thank you for all you do to get a meal on the table.", link(ctx.url("/stats/household"), STATS_LINK))
    )
    return _finish(f"New badges for {who}.", blocks)


def _is_many(news: note.BadgeNews) -> bool:
    return len(news.badges) + news.more > THUMBNAIL_CUTOFF


def _many_line(news: note.BadgeNews, ctx: RenderContext, art: Mapping[note.BadgeTier, str]) -> str:
    """``@Kim earned 8 badges: Chef, Scout, ... and 2 more badges.`` With art for some of the badges,
    each of those is named plain, since its icon shows the tier in the row below (#114); the rest keep
    their tag. Every badge is named when the post has them (``news.rest``). Without art for any, it is
    the text-only post's wording, with the count first."""
    who = f"{ctx.directory.mention(news.person)} earned {len(news.badges) + news.more} badges:"
    if not art:
        return f"{who} {_and(_tags(news))}."
    names = [
        f"*{escape(b.name)}*" if b in art else tag(f"*{escape(b.name)}*", b.tier) for b in (*news.badges, *news.rest)
    ]
    if left := news.more - len(news.rest):  # a post queued before ``rest`` existed
        names.append(plural(left, "more badge"))
    return f"{who} {_and(names)}."


def _drawn(news: tuple[note.BadgeNews, ...], art: ArtIds) -> list[dict[note.BadgeTier, str]]:
    """For each member in ``news``, the badges to show as art with their file IDs, in order: those
    ``art`` has, until ``MAX_ART`` are used up in the post. A member shown as a compact row of icons
    (``_is_many``) uses none of that room, and gets every badge they earned, not only those named in the
    text (#114).

    The post stays within Slack's 50 blocks (``MAX_MESSAGE_BLOCKS``). Three are fixed: the heading, the
    line about members left off, and the closing line. At most ``MAX_PEOPLE`` (10) members follow, and
    each takes at most 3 blocks. A member shown as a row of icons has their line and at most 13 badges
    in two blocks of 10. Any other member has at most three badges (counting those left off): a block
    for each with art, and one line for the rest, 3 together. So 3 + 10 x 3 = 33."""
    room = MAX_ART
    drawn: list[dict[note.BadgeTier, str]] = []
    for n in news:
        mine: dict[note.BadgeTier, str] = {}
        many = _is_many(n)
        for badge in (*n.badges, *n.rest) if many else n.badges:
            file_id = art.get(wanted(badge.name, badge.tier) or ("", ""))
            if file_id and (many or room > 0):
                mine[badge] = file_id
                room -= 0 if many else 1
        drawn.append(mine)
    return drawn


THANKED_AS = {"cooked": "Cooked", "picked_up": "Picked up", "ordered": "Ordered", "coordinated": "Coordinated"}


def monthly_recap_message(notification: note.MonthlyRecap, ctx: RenderContext) -> Message:
    """FR-40: the month in review. Household totals first, then a thank-you for each member who
    pitched in, in name order, then badges. It names no one for doing less and ranks no one."""
    month = MONTHS[notification.month.month - 1]
    lines = [f"*{month} in review* 🍽️", f"Together you put *{plural(notification.meals, 'meal')}* on the table."]
    if notification.cuisines:
        first = [escape(c) for c in notification.first_cuisines]
        new = f" (new to you: {_and(first)})" if first else ""
        lines.append(f"*Cuisines:* {', '.join(escape(c) for c in notification.cuisines)}{new}")
    if notification.new_items:
        added = [escape(n) for n in notification.new_item_names]
        rest = notification.new_items - len(added)
        names_ = _and([*added, f"{rest} more"] if rest else added)
        lines.append(f"*New in the catalog:* {notification.new_items} ({names_})")
    blocks = [section(join_lines(lines))]
    if notification.thanks:
        groups = []
        for group in notification.thanks:
            who = [escape(p.name) for p in group.people]
            if group.more:
                who.append(plural(group.more, "more person", "more people"))
            groups.append(f"• *{THANKED_AS[group.kind]}:* {_and(who)}")
        thanks = [
            "*Thank you*",
            "Every meal takes a team, and everyone here is part of it.",
            *groups,
            "And thank you to everyone who answered a call or pulled up a chair.",
        ]
        blocks.append(section(join_lines(thanks)))
    if notification.badges:
        earned = [f"• {escape(n.person.name)}: {', '.join(_tags(n))}" for n in notification.badges]
        if notification.more_badges:
            earned.append(f"…and {plural(notification.more_badges, 'more member')} earned badges too.")
        blocks.append(section(join_lines(["*Badges earned*", *earned])))
    following = MONTHS[notification.month.month % 12]
    blocks.append(context(f"Here's to a tasty {following}!", link(ctx.url("/stats/household"), STATS_LINK)))
    return _finish(f"{month} in review: {plural(notification.meals, 'meal')} together.", blocks)
