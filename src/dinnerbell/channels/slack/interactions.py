# SPDX-License-Identifier: AGPL-3.0-or-later
"""Slack interactions: buttons, modals, the App Home, and the slash command (spec section 10,
docs/channels.md "Interactions"). The command is ``/dinnerbell`` unless
``DINNERBELL_SLACK_COMMAND`` names another, such as ``/dinnerbell-test`` on a test instance.

Every handler acknowledges Slack first, inside its 3-second window, and does the work in a
tracked task. Before acting it checks the payload's workspace, where a button was pressed,
and that the Slack user is an active member (spec 13.5). Each action calls one service. The
services check every rule and scope every ID to the member's household, so a tampered value
can only name something the member could act on anyway.

Errors the services raise for people are shown back as ephemeral messages (or on the Home tab
when the button was there). Anything else is a bug: it's logged, and the member is told to try
again. Logs carry member and event IDs only (SEC-5), never a ``response_url``.

An answer or an offer tapped on the call message gets a Your answer card back: an ephemeral
showing the person's own choices (``messages.your_answer``). A tap on the card replaces it in
place through that tap's ``response_url``, and a tap on the reminder edits the reminder with
``chat.update``. A tap on the call message can't do either: its ``response_url`` would rewrite
the call for everyone, and Slack can't edit an ephemeral any other way, so it posts a new card.
Nothing about cards is stored; each is drawn from the person's response.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from slack_bolt.async_app import AsyncApp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell import __version__
from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.api import SlackApi, SlackUnavailable
from dinnerbell.channels.slack.blocks import (
    Directory,
    actions,
    button,
    day_label,
    escape,
    instance_banner,
    section,
    version_line,
    version_link,
    with_version_footer,
)
from dinnerbell.channels.slack.messages import (
    CardGuest,
    Message,
    RenderContext,
    labelled,
    posted_reply,
    reminder_message,
    schedule_reply,
    started_reply,
    your_answer,
    your_answer_text,
)
from dinnerbell.channels.slack.views import (
    FormError,
    HomeLinks,
    InviteInput,
    cancel_modal,
    close_modal,
    details_modal,
    feedback_modal,
    home_view,
    invite_modal,
    log_modal,
    offer_modal,
    parse_cancel,
    parse_details,
    parse_feedback,
    parse_invite,
    parse_log,
    parse_offer,
    parse_proposal,
    proposal_modal,
)
from dinnerbell.config import is_test_instance
from dinnerbell.domain.events import (
    GUESTS_DONT,
    STALE_CALL,
    EventRuleViolation,
    EventState,
    IllegalTransition,
    InvalidResponse,
    RoleOffer,
    Window,
    check_can_cancel,
    check_can_close_window,
    check_can_respond,
    check_expected_window,
    check_run,
    friendly,
)
from dinnerbell.domain.feedback import InvalidFeedback
from dinnerbell.domain.guests import REFUSED, GuestAction
from dinnerbell.domain.meals import InvalidMeal, Slot
from dinnerbell.domain.members import Member, Role
from dinnerbell.domain.notifications import (
    GuestInvited,
    MessageRef,
    PermanentDeliveryError,
    PersonInfo,
    ResponseReminder,
)
from dinnerbell.domain.offers import OfferPath
from dinnerbell.services import calls, discard, event_views, feedback, guests, meals, offers, slack_members
from dinnerbell.services import schedule as schedule_service
from dinnerbell.services.clock import Clock
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.event_views import EventView
from dinnerbell.services.lifecycle import CallExists

log = logging.getLogger(__name__)

DEFAULT_COMMAND = "/dinnerbell"
SHOWN_ERRORS = (EventRuleViolation, InvalidResponse, InvalidFeedback, InvalidMeal, NotFound, Forbidden)
SLACK_ERRORS = (SlackUnavailable, PermanentDeliveryError)
UNKNOWN_USER = (
    "I don't know your Slack account yet. Sign in to Dinnerbell on the web with Slack once, "
    "or ask the admin to link you on the Members page."
)
INACTIVE = "Your Dinnerbell account is turned off. Ask the admin if that's a mistake."
STALE = STALE_CALL
BROKEN = "That didn't work. Try again in a moment, or use the web."
OUT_OF_DATE = "That button is out of date. Try the latest message, or use the web."
WRONG_PLACE = "That button only works in the Dinnerbell channel."
WRONG_WORKSPACE = "This Dinnerbell belongs to another Slack workspace."
CONFIRM = {
    "respond_in": "You're in for {slot}. Thanks!",
    "respond_maybe": "Got it: you're a maybe for {slot}.",
    "respond_out": "Got it: you're out for {slot} this time.",
    ids.COORDINATE: "You're coordinating {slot}. Thanks for steering!",
}
OFFERED = {
    RoleOffer.COOK: "Thanks! You're offering to cook. Tap again to take it back.",
    RoleOffer.PICKUP: "Thanks! You're offering to pick up. Tap again to take it back.",
    RoleOffer.ORDER: "Thanks! You're offering to order. Tap again to take it back.",
}
WITHDRAWN = {
    RoleOffer.COOK: "Got it: you're not offering to cook.",
    RoleOffer.PICKUP: "Got it: you're not offering to pick up.",
    RoleOffer.ORDER: "Got it: you're not offering to order.",
}
CALLED_OFF = "You called off {slot} for {day}."
CLOSED = {Window.RESPOND: "Responses are closed for {slot}.", Window.VOTE: "Voting is closed for {slot}."}
VOTED = "Vote counted. Tap Vote again to take it back."
UNVOTED = "Vote taken back."
FEEDBACK_SAVED = "Thanks for the feedback! Only you see your ratings."
FEEDBACK_NONE = "Got it: no feedback from you on this meal."
SETTLE_TIMEOUT_SECONDS = 10.0
Ack = Callable[..., Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Source:
    """Who pressed something, and where: a channel or DM message, the Home tab, or a modal."""

    user_id: str
    channel_id: str | None = None
    home: bool = False


@dataclass(frozen=True, slots=True)
class Tap:
    """The message a button was on, from the payload's container: its ``ts``, whether it's an
    ephemeral, and the ``response_url`` Slack sent with the tap (checked by ``ids.response_url``).
    Never log the URL: it's a bearer capability for that message."""

    message_ts: str | None = None
    ephemeral: bool = False
    response_url: str | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    """A reply with a channel link or buttons, such as the one to starting a later day's call:
    ``message`` where the person acted, or ``notice``, plain words, on the Home tab, which
    shows replies as a line of text."""

    message: Message
    notice: str


Done = str | Message | Reply | None
"""What an action replies: a line of text, a message with a channel link or buttons (never
from Home), a ``Reply`` for both places, or nothing."""


@dataclass(slots=True)
class _CardLock:
    """One person's card and reminder work on one call, one tap at a time. ``users`` counts the
    taps holding or waiting for it; the last one out drops it."""

    lock: asyncio.Lock
    users: int = 0


class Interactions:
    def __init__(
        self,
        api: SlackApi,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        clock: Clock,
        base_url: str,
        channel_id: str,
        command: str = DEFAULT_COMMAND,
        instance_label: str = "",
        version: str = __version__,
        schema_revision: str,
        people: guests.SlackPeople | None = None,
    ) -> None:
        self.api = api
        self.people = people
        """Who may join from the channel (#38); None leaves joining to the web and the admin."""
        self.join_limiter = guests.JoinLimiter()
        self.sessionmaker = sessionmaker
        self.clock = clock
        self.links = HomeLinks(base_url)
        self.channel_id = channel_id
        self.command = command
        """The slash command, as registered with Slack and named in replies."""
        self.instance_label = instance_label
        self.version = version
        """``Settings.version_label``, for ``version``, the help reply, and the Home footer."""
        self.schema_revision = schema_revision
        """The database's schema revision, read once at startup, for ``version``."""
        self.team_id: str | None = None
        """The workspace whose payloads are accepted. Until it's known, nothing is."""
        self.tasks: set[asyncio.Task[None]] = set()
        self.dm_channels: dict[str, str] = {}
        """Slack user ID to the bot's DM with them, as conversations.open reported it."""
        self.card_locks: dict[tuple[str, int], _CardLock] = {}
        """Per (Slack user, event), while a tap holds or waits for it; see ``_card_lock``."""

    @property
    def any_time(self) -> bool:
        """On a test instance, a called-off call can start again at any time (#60)."""
        return is_test_instance(self.instance_label)

    # Wiring ------------------------------------------------------------------------------------

    def register(self, app: AsyncApp) -> None:
        """Route Slack's requests here. Bolt passes arguments by parameter name and can't call
        bound methods, so each route is a plain function."""

        async def link(ack: Ack) -> None:
            await ack()  # a link button opened the browser; there's nothing to do

        async def action(ack: Ack, body: dict[str, Any]) -> None:
            await ack()
            self._spawn(self._action(body))

        async def details(ack: Ack, body: dict[str, Any]) -> None:
            await self._submission(ack, body, "details")

        async def proposal(ack: Ack, body: dict[str, Any]) -> None:
            await self._submission(ack, body, "proposal")

        async def cancel(ack: Ack, body: dict[str, Any]) -> None:
            await self._submission(ack, body, "cancel")

        async def close(ack: Ack, body: dict[str, Any]) -> None:
            await self._submission(ack, body, "close")

        async def feedback_view(ack: Ack, body: dict[str, Any]) -> None:
            await self._submission(ack, body, "feedback")

        async def invite_view(ack: Ack, body: dict[str, Any]) -> None:
            await self._submission(ack, body, "invite")

        async def offer_view(ack: Ack, body: dict[str, Any]) -> None:
            await self._form_submission(ack, body, "offer")

        async def log_view(ack: Ack, body: dict[str, Any]) -> None:
            await self._form_submission(ack, body, "log")

        async def command(ack: Ack, body: dict[str, Any]) -> None:
            await self._command(ack, body)

        async def home_opened(body: dict[str, Any], event: dict[str, Any]) -> None:
            if event.get("tab") == "home" and self._team_ok(body.get("team_id")) and isinstance(event.get("user"), str):
                self._spawn(self._home(event["user"]))

        app.action(ids.LINK_PATTERN)(link)
        app.action(re.compile(r"^[a-z_]{1,40}$"))(action)
        app.view(ids.DETAILS_VIEW)(details)
        app.view(ids.PROPOSAL_VIEW)(proposal)
        app.view(ids.CANCEL_VIEW)(cancel)
        app.view(ids.CLOSE_VIEW)(close)
        app.view(ids.FEEDBACK_VIEW)(feedback_view)
        app.view(ids.INVITE_VIEW)(invite_view)
        app.view(ids.OFFER_VIEW)(offer_view)
        app.view(ids.LOG_VIEW)(log_view)
        app.command(self.command)(command)
        app.event("app_home_opened")(home_opened)

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def settle(self, limit: float = SETTLE_TIMEOUT_SECONDS) -> None:
        """Wait for work already acknowledged (and work it starts), for shutdown and tests.
        Gives up after ``limit`` seconds."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit
        while self.tasks and loop.time() < deadline:
            await asyncio.wait(set(self.tasks), timeout=deadline - loop.time())

    async def cancel_pending(self) -> None:
        """Cancel work still running after ``settle`` gave up, at shutdown."""
        pending = set(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=1.0)

    # Checks ------------------------------------------------------------------------------------

    def _team_ok(self, team_id: object) -> bool:
        ok = self.team_id is not None and team_id == self.team_id
        if not ok:
            log.warning("slack payload from another workspace refused")
        return ok

    async def _member(self, source: Source) -> Member | None:
        """The active member or guest behind ``source``, or None after telling them why not.
        Someone in the channel whom Dinnerbell doesn't know is offered **Join Dinnerbell**
        instead, while joining from the channel is on (#38)."""
        async with self.sessionmaker() as db:
            member = await slack_members.member_for_slack_user(db, source.user_id)
        if member is None:
            log.info("slack user refused", extra={"member_id": None})
            await self._unknown(source)
            return None
        if not member.active:
            log.info("slack user refused", extra={"member_id": member.id})
            await self.tell(source, INACTIVE, member=None)
            return None
        return member

    # Joining from the channel (#38, option A) ---------------------------------------------------

    async def _joinable(self) -> bool:
        if self.people is None:
            return False
        async with self.sessionmaker() as db:
            return await guests.joining_open(db)

    async def _unknown(self, source: Source) -> None:
        """Someone Dinnerbell doesn't know tapped, opened Home, or ran the command: offer to join,
        or say to ask the admin while joining is off."""
        if not await self._joinable():
            await self.tell(source, UNKNOWN_USER, member=None)
            return
        message = join_offer_message()
        try:
            if source.home:
                blocks = list(message.blocks)
                if self.instance_label:
                    blocks.insert(0, instance_banner(self.instance_label))
                view = with_version_footer({"type": "home", "blocks": blocks}, self.version, self.instance_label)
                await self.api.publish_home(source.user_id, view)
                return
            if source.channel_id is not None:
                try:
                    await self.api.ephemeral(source.channel_id, source.user_id, message.text, message.blocks)
                    return
                except SLACK_ERRORS:
                    pass  # not in that channel: a direct message instead
            await self.api.post(source.user_id, message.payload())
        except SLACK_ERRORS as exc:
            log.warning("slack reply failed", extra={"error": str(exc)})

    async def _join(self, source: Source) -> None:
        """**Join Dinnerbell**: check with Slack that this is a real person of the workspace in
        the channel, and join them as a guest (``guests.join``). Rate-limited per Slack user."""
        user, now = source.user_id, self.clock()
        refused: guests.NotJoinable | None = None
        person: guests.SlackPerson | guests.NotJoinable | None = None
        if not self.join_limiter.allow(user, now):
            if not self.join_limiter.first_refusal(user):
                return  # told once this window already; stay quiet
            refused = guests.NotJoinable.TOO_MANY
        elif self.people is None or not await self._joinable():
            refused = guests.NotJoinable.OFF
        else:
            person = await self.people.person(user)
            if isinstance(person, guests.NotJoinable):
                refused = person
        if refused is not None or not isinstance(person, guests.SlackPerson):
            reason = refused or guests.NotJoinable.UNAVAILABLE
            async with self.sessionmaker() as db:
                await guests.record_refusal(db, user, reason, via="slack", now=now)
            await self.tell(source, guests.JOIN_REFUSALS[reason], member=None)
            return
        try:
            async with self.sessionmaker() as db:
                joined = await guests.join(db, person, via="slack", now=now)
        except guests.JoinRefused as exc:
            await self.tell(source, str(exc), member=None)
            return
        await self.tell(
            Source(user, home=source.home) if source.home else source, JOINED[joined.joined_as], member=joined.member
        )
        if not source.home:
            try:
                await self.publish_home(user, joined.member)
            except SLACK_ERRORS as exc:
                log.warning("slack home publish failed", extra={"error": str(exc)})

    async def _is_their_dm(self, source: Source) -> bool:
        """True when ``source.channel_id`` is the bot's direct message with ``source.user_id``.
        Asks Slack once per user; any trouble asking counts as no."""
        user, channel = source.user_id, source.channel_id
        if user not in self.dm_channels:
            try:
                found = await self.api.dm_channel(user)
            except SLACK_ERRORS as exc:
                log.warning("slack dm check failed", extra={"error": str(exc)})
                return False
            if found is None:
                return False
            self.dm_channels[user] = found
        return self.dm_channels[user] == channel

    def _message_source(self, body: dict[str, Any]) -> Source | None:
        """Where a block action came from, or None if from somewhere it shouldn't be. A
        message's channel must be the household channel or look like a direct message; the
        caller then checks that a direct message is the bot's own with this user."""
        user = _get(body, "user", "id")
        if not isinstance(user, str):
            return None
        container = body.get("container") or {}
        if container.get("type") == "view":
            if _get(body, "view", "type") == "home":
                return Source(user, home=True)
            return None
        channel = container.get("channel_id") or _get(body, "channel", "id")
        if channel == self.channel_id or (ids.is_channel_id(channel) and str(channel).startswith("D")):
            return Source(user, channel_id=str(channel))
        return Source(user, channel_id=None)

    # Replies -----------------------------------------------------------------------------------

    async def tell(self, source: Source, text: str, *, member: Member | None) -> None:
        """Say something only ``source.user_id`` sees: on their Home tab if they were there,
        else an ephemeral message where they acted, else a direct message."""
        try:
            if source.home:
                if member is None:  # nobody to draw a Home for: just the words
                    view = {
                        "type": "home",
                        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": escape(text)}}],
                    }
                    await self.api.publish_home(source.user_id, view)
                    return
                await self.publish_home(source.user_id, member, notice=text)
                return
            safe = escape(text)  # service messages can quote what people typed
            if source.channel_id is not None:
                try:
                    await self.api.ephemeral(source.channel_id, source.user_id, safe)
                    return
                except SLACK_ERRORS:
                    pass  # not in that channel, for example; fall back to a direct message
            # Unlabelled, like ephemerals: it answers something the person just did in this app.
            await self.api.post(source.user_id, {"text": safe})
        except SLACK_ERRORS as exc:
            log.warning("slack reply failed", extra={"error": str(exc)})

    async def _reply(self, source: Source, message: Message, member: Member) -> None:
        """A private reply with blocks: an ephemeral in the household channel, where its
        buttons work, or else a direct message, where they work too."""
        try:
            if source.channel_id == self.channel_id:
                try:
                    await self.api.ephemeral(source.channel_id, source.user_id, message.text, message.blocks)
                    return
                except SLACK_ERRORS:
                    pass  # fall back to a direct message
            await self.api.post(source.user_id, message.payload())
        except SLACK_ERRORS as exc:
            log.warning("slack reply failed", extra={"error": str(exc), "member_id": member.id})

    async def publish_home(self, user_id: str, member: Member | None, notice: str | None = None) -> None:
        if member is None:
            view: dict[str, Any] = {
                "type": "home",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": escape(notice or UNKNOWN_USER)}}],
            }
            if self.instance_label:
                view["blocks"].insert(0, instance_banner(self.instance_label))
        else:
            async with self.sessionmaker() as db:
                today = await event_views.today_view(db, member, now=self.clock(), any_time=self.any_time)
                awaiting = await feedback.awaiting(db, member, now=self.clock())
            view = home_view(
                today, self.links, notice, label=self.instance_label, awaiting=awaiting, guest=member.is_guest
            )
        await self.api.publish_home(user_id, with_version_footer(view, self.version, self.instance_label))

    async def _run(self, source: Source, member: Member, work: Callable[[], Awaitable[Done]]) -> None:
        """Run one action and report how it went. ``work`` returns what to reply (``Done``)."""
        notice: str | None = None
        try:
            done = await work()
            if isinstance(done, Reply):
                done = done.notice if source.home else done.message
            if isinstance(done, Message):
                await self._reply(source, done, member)
            else:
                notice = done
        except SHOWN_ERRORS as exc:
            notice = str(exc)
        except IllegalTransition as exc:
            notice = str(friendly(exc))
        except ids.BadValue:
            log.warning("slack action with a value this app didn't write", extra={"member_id": member.id})
            notice = OUT_OF_DATE
        except Exception:
            log.exception("slack action failed", extra={"member_id": member.id})
            notice = BROKEN
        if source.home:
            try:
                await self.publish_home(source.user_id, member, notice)
            except Exception:
                log.exception("slack home publish failed", extra={"member_id": member.id})
        elif notice:
            await self.tell(source, notice, member=member)

    # Buttons -----------------------------------------------------------------------------------

    async def _action(self, body: dict[str, Any]) -> None:
        if not self._team_ok(_get(body, "team", "id")):
            return
        source = self._message_source(body)
        acts = body.get("actions")
        if source is None or not isinstance(acts, list) or len(acts) != 1 or not isinstance(acts[0], dict):
            log.warning("slack action payload refused")
            return
        in_dm = source.channel_id is not None and source.channel_id != self.channel_id
        if (source.channel_id is None and not source.home) or (in_dm and not await self._is_their_dm(source)):
            log.warning("slack action from an unexpected channel refused")
            await self.tell(Source(source.user_id), WRONG_PLACE, member=None)
            return
        if acts[0].get("action_id") == ids.JOIN_DINNERBELL:  # before _member: they aren't one yet
            if acts[0].get("value") == ids.JOIN_DINNERBELL:
                await self._join(source)
            return
        member = await self._member(source)
        if member is None:
            return
        action_id, value = acts[0].get("action_id"), acts[0].get("value")
        if action_id == ids.CALL_MENU:  # an overflow menu sends the option it picked, not a value
            value = _get(acts[0], "selected_option", "value")
        trigger = body.get("trigger_id")
        container = body.get("container") or {}
        tap = Tap(
            message_ts=ids.message_ts(container.get("message_ts")),
            ephemeral=container.get("is_ephemeral") is True,
            response_url=ids.response_url(body.get("response_url")),
        )
        log.info("slack action", extra={"member_id": member.id, "action": str(action_id)[:40]})

        async def work() -> Done:
            return await self._dispatch(member, source, str(action_id), value, trigger, tap)

        await self._run(source, member, work)

    async def _dispatch(
        self, member: Member, source: Source, action_id: str, value: object, trigger: object, tap: Tap
    ) -> Done:
        if member.is_guest and action_id not in GUEST_TAPS:
            # Guests answer, offer roles, and coordinate logistics; the services check again (#38).
            raise EventRuleViolation(GUESTS_DONT)
        if action_id in ids.GUEST_ACTIONS:
            return await self._guest_tap(member, source, action_id, value, tap)
        if action_id == ids.INVITE_GUEST:
            return await self._open_invite(member, source, value, trigger)
        if action_id in {ids.MAKE_MEMBER, ids.REMOVE_GUEST}:
            return await self._admin_guest(member, action_id, value)
        if action_id in ids.CARD_ACTIONS:
            return await self._card_tap(member, source, action_id, value, trigger, tap)
        if action_id == ids.ANSWER:
            await self._answer(member, source, value, tap)
            return None
        if action_id == ids.CALL_MENU:
            choice, event_id, run = ids.parse_menu(value)
            return await self._open_end(member, source, choice, event_id, run, trigger)
        if action_id == ids.CANCEL_CALL:
            choice, event_id, run = ids.parse_menu(value)
            if choice is not ids.MenuChoice.CANCEL:
                raise ids.BadValue("not a call-off value")
            return await self._open_end(member, source, choice, event_id, run, trigger)
        if action_id == ids.CALL_COORDINATE:
            return await self._coordinate(member, source, value, tap)
        if action_id == ids.POST_NOW:
            event_id, run = ids.parse_card(value)
            return await self._post_now(member, event_id, run)
        if action_id == ids.CLOSE_WINDOW:  # Slack asked them to confirm already
            window, event_id, run = ids.parse_window(value)
            return await self._close(member, event_id, window, run)
        if action_id == ids.FEEDBACK:
            return await self._open_feedback(member, source, value, trigger)
        if action_id in ids.OFFER_ACTIONS:
            return await self._offer_tap(member, action_id, value)
        if action_id == ids.MAKE_OFFER:
            if value != ids.MAKE_OFFER:
                raise ids.BadValue("not a make-offer value")
            return await self._open_offer(member, source, trigger)
        now = self.clock()
        async with self.sessionmaker() as db:
            if action_id in ids.RESPOND:
                event_id = ids.parse_event(value)
                await calls.respond(db, member, event_id, ids.RESPOND[action_id], now=now)
                return await self._confirm(db, member, source, event_id, action_id)
            if action_id in ids.OFFER:
                role = ids.OFFER[action_id]
                event_id = ids.parse_event(value)
                offered = await calls.toggle_role(db, member, event_id, role, now=now)
                if self._on_call_message(source):
                    await self._post_card(db, member, source, event_id)
                    return None
                return (OFFERED if offered else WITHDRAWN)[role]
            if action_id == ids.COORDINATE:
                event_id = ids.parse_event(value)
                await calls.claim_coordinator(db, member, event_id, now=now)
                return await self._confirm(db, member, source, event_id, action_id)
            if action_id == ids.VOTE:
                event_id, proposal_id = ids.parse_proposal(value)
                voted = await calls.toggle_vote(db, member, event_id, proposal_id, now=now)
                return VOTED if voted else UNVOTED
            if action_id == ids.LOCK:
                event_id, proposal_id = ids.parse_proposal(value)
                await calls.lock(db, member, event_id, proposal_id, now=now)
                return None
            if action_id == ids.MARK_DONE:
                await calls.mark_done(db, member, ids.parse_event(value), now=now)
                return "Marked done. Thanks!"
            if action_id in ids.START_CALL:
                slot = ids.parse_slot(value)
                day = await slack_members.today(db, member, now=now)
                await calls.start_call(db, member, slot, day, now=now, instance_label=self.instance_label)
                return f"Started the {slot.value} call. It's posted in the Dinnerbell channel."
            if action_id in {ids.DETAILS, ids.ADD_PROPOSAL}:
                return await self._open_modal(db, member, source, action_id, ids.parse_event(value), trigger)
        raise ids.BadValue("unknown action")

    async def _confirm(
        self, db: AsyncSession, member: Member, source: Source, event_id: int, action_id: str
    ) -> str | None:
        """The answer to a tap that worked: a Your answer card on the call message, or a line of
        text on the Home tab and an older reminder, whose buttons don't name a run."""
        if self._on_call_message(source):
            await self._post_card(db, member, source, event_id)
            return None
        view = await event_views.event_view(db, member, event_id, now=self.clock())
        return CONFIRM[action_id].format(slot=view.event.slot.value)

    # Your answer cards and the reminder (spec section 10) --------------------------------------

    def _on_call_message(self, source: Source) -> bool:
        """Whether a tap was in the household channel, where the call message and cards are."""
        return not source.home and source.channel_id == self.channel_id

    def _context(self) -> RenderContext:
        """Cards and reminders mention nobody, so the directory is empty."""
        return RenderContext(self.links.base_url, Directory(), self.instance_label)

    def _card(self, view: EventView, member: Member, *, run: int | None = None, notice: str | None = None) -> Message:
        mine = tuple(CardGuest(g.person.id, g.person.name, g.answer) for g in view.guests if g.answerable)
        return your_answer(
            view.info,
            view.mine,
            member.id,
            self._context(),
            run=run,
            notice=notice,
            guest=member.is_guest,
            can_invite=view.can.invite,
            guests=mine,
        )

    @contextlib.asynccontextmanager
    async def _card_lock(self, user: str, event_id: int) -> AsyncIterator[None]:
        """Run one tap's card or reminder work at a time per person and call, from reading their
        response through replacing or posting the card. Otherwise two quick taps could each read
        their own state and land in the other order, leaving the older one on screen. Locks
        exist only while a tap holds or waits for one, so there are at most as many as taps in
        flight."""
        key = (user, event_id)
        entry = self.card_locks.get(key)
        if entry is None:
            entry = self.card_locks[key] = _CardLock(asyncio.Lock())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self.card_locks[key]

    async def _post_card(
        self, db: AsyncSession, member: Member, source: Source, event_id: int, *, run: int | None = None
    ) -> None:
        """After a tap on the call message: a new card. Never through the tap's response_url,
        which belongs to the call message everyone sees. ``run`` is the run the tapped button
        named, if it named one; a card for an earlier run says the call moved on."""
        async with self._card_lock(source.user_id, event_id):
            view = await event_views.event_view(db, member, event_id, now=self.clock())
            await self._send_card(source, self._card(view, member, run=run), member)

    async def _answer(self, member: Member, source: Source, value: object, tap: Tap) -> None:
        """**Answer** on the call message: post the tapper's card for the run the message showed.
        It changes nothing itself; the card is where they choose."""
        event_id, run = ids.parse_card(value)
        if not self._on_call_message(source) or tap.ephemeral:
            raise ids.BadValue("Answer is drawn only on the call message")
        async with self.sessionmaker() as db:
            await self._post_card(db, member, source, event_id, run=run)

    async def _coordinate(self, member: Member, source: Source, value: object, tap: Tap) -> str | None:
        """**I'll coordinate** on the call message while voting: claim it for the run the
        message showed, so a message from before a restart can't claim the newer run (#54)."""
        event_id, run = ids.parse_card(value)
        if not self._on_call_message(source) or tap.ephemeral:
            raise ids.BadValue("I'll coordinate with a run is drawn only on the call message")
        async with self.sessionmaker() as db:
            await calls.claim_coordinator(db, member, event_id, run=run, now=self.clock())
            return await self._confirm(db, member, source, event_id, ids.COORDINATE)

    async def _send_card(self, source: Source, card: Message, member: Member, tap: Tap | None = None) -> None:
        """Replace the card ``tap`` was on, or post a new one when there's no card to replace,
        or Slack won't (its response_url lasts 30 minutes and 5 uses). Only an ephemeral in the
        household channel is ever replaced."""
        user, channel = source.user_id, source.channel_id
        if tap is not None and tap.ephemeral and tap.response_url is not None and self._on_call_message(source):
            try:
                await self.api.replace_original(tap.response_url, card.payload())
                return
            except SLACK_ERRORS as exc:
                log.info("slack card not replaced; posting a new one", extra={"error": str(exc)})
        try:
            if channel is None:
                raise PermanentDeliveryError("no channel to post the card in")  # pragma: no cover
            await self.api.ephemeral(channel, user, card.text, card.blocks)
        except SLACK_ERRORS as exc:
            log.warning("slack card not posted", extra={"error": str(exc), "member_id": member.id})
            try:
                await self.api.post(user, {"text": card.text})  # escaped already, and names nobody
            except SLACK_ERRORS as again:
                log.warning("slack reply failed", extra={"error": str(again)})

    async def _card_tap(
        self, member: Member, source: Source, action_id: str, value: object, trigger: object, tap: Tap
    ) -> str | None:
        """A button on a Your answer card or a reminder: act on the run it names, then show the
        person where they stand on that same message. A card or reminder for a call that has
        moved on says so instead of acting on what came after."""
        event_id, run = ids.parse_card(value)
        in_dm = source.channel_id is not None and not self._on_call_message(source)
        if source.home or (not in_dm and not tap.ephemeral):
            raise ids.BadValue("card buttons are drawn only on cards and reminders")
        async with self._card_lock(source.user_id, event_id):
            return await self._card_tap_locked(member, source, action_id, event_id, run, trigger, tap, in_dm=in_dm)

    async def _card_tap_locked(
        self,
        member: Member,
        source: Source,
        action_id: str,
        event_id: int,
        run: int,
        trigger: object,
        tap: Tap,
        *,
        in_dm: bool,
    ) -> str | None:
        notice: str | None = None
        async with self.sessionmaker() as db:
            try:
                if action_id == ids.CARD_DETAILS:
                    return await self._card_details(db, member, source, event_id, run, trigger)
                await self._card_change(db, member, action_id, event_id, run)
            except SHOWN_ERRORS as exc:
                notice = str(exc)
            except IllegalTransition as exc:
                notice = str(friendly(exc))
            view = await event_views.event_view(db, member, event_id, now=self.clock())
        moved_on = view.event.run != run or view.event.state is not EventState.COLLECTING
        if moved_on:
            notice = None  # the card or reminder says so itself
        if in_dm:
            return await self._update_reminder(view, member, source, tap, run, notice)
        await self._send_card(source, self._card(view, member, run=run, notice=notice), member, tap)
        return None

    async def _card_change(self, db: AsyncSession, member: Member, action_id: str, event_id: int, run: int) -> None:
        now = self.clock()
        if action_id in ids.CARD_RESPOND:
            await calls.respond(db, member, event_id, ids.CARD_RESPOND[action_id], run=run, now=now)
        elif action_id in ids.CARD_OFFER:
            await calls.offer_role(db, member, event_id, ids.CARD_OFFER[action_id], True, run=run, now=now)
        elif action_id in ids.CARD_WITHDRAW:
            await calls.offer_role(db, member, event_id, ids.CARD_WITHDRAW[action_id], False, run=run, now=now)
        else:
            await calls.claim_coordinator(db, member, event_id, run=run, now=now)

    async def _card_details(
        self, db: AsyncSession, member: Member, source: Source, event_id: int, run: int, trigger: object
    ) -> str | None:
        """Details from a card: the modal names the card's run, which saving checks again."""
        if not isinstance(trigger, str) or not trigger:
            raise ids.BadValue("no trigger")
        view = await event_views.event_view(db, member, event_id, now=self.clock())
        check_run(view.event, run)
        check_can_respond(view.event, member)
        await self.api.open_view(trigger, details_modal(view, ids.ViewOrigin(event_id, source.channel_id, run=run)))
        return None

    async def _update_reminder(
        self, view: EventView, member: Member, source: Source, tap: Tap, run: int, notice: str | None
    ) -> str | None:
        """Edit the reminder that was tapped to show the answer, with ``chat.update``: it's an
        ordinary message, and the bot's own. ``_action`` checked that the DM is this member's.
        If it can't be edited, a line of text says what happened instead."""
        me = PersonInfo(member.id, member.display_name)
        reminder: ResponseReminder | GuestInvited = ResponseReminder(view.info, me, member.is_guest)
        if member.is_guest:  # their invitation, which has In and Out only (#38)
            inviter = next((g.invited_by for g in view.guests if g.person.id == member.id), None)
            reminder = GuestInvited(view.info, me, inviter or PersonInfo(0, "A member"))
        ctx = self._context()
        message = labelled(reminder_message(reminder, ctx, mine=view.mine, run=run), ctx)
        if source.channel_id is not None and tap.message_ts is not None:
            try:
                await self.api.update(MessageRef(source.channel_id, tap.message_ts), message.payload())
                return notice
            except SLACK_ERRORS as exc:
                log.warning("slack reminder not updated", extra={"error": str(exc), "member_id": member.id})
        return notice or your_answer_text(view.info, view.mine, run=run)  # tell() escapes it

    async def _open_modal(
        self, db: AsyncSession, member: Member, source: Source, action_id: str, event_id: int, trigger: object
    ) -> str | None:
        if not isinstance(trigger, str) or not trigger:
            raise ids.BadValue("no trigger")
        view = await event_views.event_view(db, member, event_id, now=self.clock())
        origin = ids.ViewOrigin(event_id, source.channel_id, home=source.home)
        if action_id == ids.DETAILS:
            if not view.can.respond:
                return "Responses are closed for this call."
            # Named for the run it was drawn for, so a save after a restart changes nothing.
            await self.api.open_view(trigger, details_modal(view, replace(origin, run=view.event.run)))
        else:
            if not view.can.propose:
                return "You can add proposals only while voting is open."
            await self.api.open_view(trigger, proposal_modal(view, origin))
        return None

    # Starting a call and posting it now (issue #59) --------------------------------------------

    async def _post_now(self, member: Member, event_id: int, run: int | None) -> Reply:
        async with self.sessionmaker() as db:
            await calls.post_now(db, member, event_id, run=run, now=self.clock())
            view = await event_views.event_view(db, member, event_id, now=self.clock())
        event = view.event
        notice = f"The {event.slot.value} call for {day_label(event.day)} is posted in the Dinnerbell channel."
        return Reply(posted_reply(view.info, channel_id=self.channel_id, ctx=self._context()), notice)

    def _started(self, view: EventView, today: date) -> Message:
        """The reply to starting a call from the slash command: posted, or scheduled with
        **Post it now**. Home's start buttons start today's call, and reply with a line."""
        event = view.event
        return started_reply(view.info, event.opens_at, today=today, channel_id=self.channel_id, ctx=self._context())

    # Call it off and Close now (FR-56) ---------------------------------------------------------

    async def _open_end(
        self, member: Member, source: Source, choice: ids.MenuChoice, event_id: int, run: int, trigger: object
    ) -> str | None:
        """Open Call it off's modal, or Close now's confirmation, for someone allowed to, on the
        run of the call they tapped. Anyone else is told why not instead, with the rule's own
        words. The modal keeps the run, and the service checks it again on submit."""
        if not isinstance(trigger, str) or not trigger:
            raise ids.BadValue("no trigger")
        async with self.sessionmaker() as db:
            view = await event_views.event_view(db, member, event_id, now=self.clock())
        check_run(view.event, run)
        origin = ids.ViewOrigin(event_id, source.channel_id, home=source.home, run=run)
        if choice is ids.MenuChoice.CANCEL:
            check_can_cancel(view.event, member)
            await self.api.open_view(trigger, cancel_modal(view, origin))
        else:
            window = check_can_close_window(view.event, member)
            # Past the window's deadline the call is advancing, so there's nothing to confirm.
            check_expected_window(view.event, window, window, now=self.clock())
            await self.api.open_view(trigger, close_modal(view, replace(origin, window=window)))
        return None

    async def _close(self, member: Member, event_id: int, window: Window, run: int | None) -> str:
        """Close ``window`` on ``run``: the window and run the member confirmed. The service
        checks both under the event's lock, so a confirmation that arrives after the call moved
        on, or a second one racing the first, closes nothing."""
        async with self.sessionmaker() as db:
            await calls.close_window(db, member, event_id, run=run, expected_window=window, now=self.clock())
            view = await event_views.event_view(db, member, event_id, now=self.clock())
        return CLOSED[window].format(slot=view.event.slot.value)

    async def _cancel(self, member: Member, event_id: int, reason: str | None, run: int | None) -> str:
        """Call off ``event_id``; ``run`` is the run the member saw, or None from the slash
        command, which names the call as it is now."""
        async with self.sessionmaker() as db:
            await calls.cancel_call(db, member, event_id, note=reason, run=run, now=self.clock())
            view = await event_views.event_view(db, member, event_id, now=self.clock())
        return CALLED_OFF.format(slot=view.event.slot.value, day=day_label(view.event.day))

    # Feedback (FR-45 to FR-49) -----------------------------------------------------------------

    async def _open_feedback(self, member: Member, source: Source, value: object, trigger: object) -> str | None:
        """**How was it?** from the decision message, the reminder, or Home: open the feedback
        modal for the run the button names. The service says why not to anyone who didn't eat
        the meal, or after feedback has closed."""
        event_id, run = ids.parse_card(value)
        if not isinstance(trigger, str) or not trigger:
            raise ids.BadValue("no trigger")
        async with self.sessionmaker() as db:
            form = await feedback.form_for_event(db, member, event_id, run=run, now=self.clock())
        origin = ids.ViewOrigin(event_id, source.channel_id, home=source.home, run=run)
        await self.api.open_view(trigger, feedback_modal(form, origin))
        return None

    async def _give_feedback(self, member: Member, event_id: int, form: Any, run: int | None) -> str:
        if run is None:  # pragma: no cover - _submission refuses a feedback modal without a run
            raise ids.BadValue("no run")
        async with self.sessionmaker() as db:
            saved = await feedback.give_for_event(db, member, event_id, form, run=run, now=self.clock())
        return FEEDBACK_SAVED if saved is not None else FEEDBACK_NONE

    # Offers and logging a meal (FR-4, spec 5.2 and 5.6) ----------------------------------------

    async def _offer_tap(self, member: Member, action_id: str, value: object) -> str:
        """**Count me in**, **+1 guest**, or **Can't make it** on an offer, on the shared message or
        Home, for the run it names. The reply says where the member stands now."""
        event_id, run = ids.parse_card(value)
        use_case = {ids.JOIN_OFFER: offers.toggle_join, ids.ADD_GUEST: offers.add_guest, ids.LEAVE_OFFER: offers.leave}
        async with self.sessionmaker() as db:
            seat = await use_case[action_id](db, member, event_id, run=run, now=self.clock())
        return seat_reply(seat)

    async def _open_offer(self, member: Member, source: Source, trigger: object) -> str | None:
        if not isinstance(trigger, str) or not trigger:
            raise ids.BadValue("no trigger")
        async with self.sessionmaker() as db:
            form = await offers.offer_form(db, member, now=self.clock())
        await self.api.open_view(trigger, offer_modal(form, ids.FormOrigin(source.channel_id, home=source.home)))
        return None

    async def _open_log(self, member: Member, source: Source, trigger: object) -> str | None:
        if not isinstance(trigger, str) or not trigger:
            raise ids.BadValue("no trigger")
        async with self.sessionmaker() as db:
            today = await slack_members.today(db, member, now=self.clock())
            form = await meals.log_form(db, member, today=today)
        if not form.items:
            return f"The catalog is empty. Add something on <{self.links.url('/catalog')}|the catalog> first."
        await self.api.open_view(trigger, log_modal(form, ids.FormOrigin(source.channel_id, home=source.home)))
        return None

    async def _form_submission(self, ack: Callable[..., Awaitable[None]], body: dict[str, Any], kind: str) -> None:
        """The offer and log modals: check the fields before closing, then save."""
        user = _get(body, "user", "id")
        if not self._team_ok(_get(body, "team", "id")) or not isinstance(user, str):
            await ack()
            return
        view = body.get("view") or {}
        values = _get(view, "state", "values")
        try:
            origin = ids.FormOrigin.decode(view.get("private_metadata"))
            form: Any = parse_offer(values) if kind == "offer" else parse_log(values)
        except FormError as exc:
            await ack(response_action="errors", errors={exc.block_id: str(exc)})
            return
        except ids.BadValue:
            log.warning("slack modal with values this app didn't write")
            await ack()
            return
        await ack()
        source = Source(user, channel_id=origin.channel_id, home=origin.home)
        self._spawn(self._save_form(source, kind, form))

    async def _save_form(self, source: Source, kind: str, form: Any) -> None:
        member = await self._member(source)
        if member is None:
            return

        async def work() -> str:
            async with self.sessionmaker() as db:
                if kind == "offer":
                    return posted_offer_reply(await offers.post_offer(db, member, form, now=self.clock()))
                today = await slack_members.today(db, member, now=self.clock())
                meal = await meals.log_meal(db, member, form, today=today, now=self.clock())
            return f"Logged {meal.item_name} for {meal.slot.value} on {day_label(meal.day)}. Thanks!"

        await self._run(source, member, work)

    # Guests (#38) ------------------------------------------------------------------------------

    async def _open_invite(self, member: Member, source: Source, value: object, trigger: object) -> str | None:
        """**Invite a guest** on a member's card: the modal names the run, which inviting checks."""
        event_id, run = ids.parse_card(value)
        if not isinstance(trigger, str) or not trigger:
            raise ids.BadValue("no trigger")
        async with self.sessionmaker() as db:
            view = await event_views.event_view(db, member, event_id, now=self.clock())
            check_run(view.event, run)
            if not view.can.invite:
                raise EventRuleViolation("Invite guests while answers are open.")
            saved = await guests.saved_guests(db, member)
        origin = ids.ViewOrigin(event_id, source.channel_id, home=source.home, run=run)
        await self.api.open_view(trigger, invite_modal(view, saved, origin))
        return None

    async def _invite(self, member: Member, event_id: int, form: InviteInput, run: int | None) -> str:
        async with self.sessionmaker() as db:
            invited = await guests.invite(
                db, member, event_id, guest_id=form.guest_id, new_guest=form.new_guest, run=run, now=self.clock()
            )
        name = invited.guest.display_name
        if invited.guest.is_registered_guest:
            return f"{name} is invited, and gets a message to say whether they're in."
        return f"{name} is invited and counted in. Tap Answer on the call to answer or offer a role for them."

    async def _guest_tap(self, member: Member, source: Source, action_id: str, value: object, tap: Tap) -> str | None:
        """In, Out, or a role for a guest without Slack, on the card of the member answering
        for them. Like any card tap, it acts on the run the card names and redraws the card."""
        event_id, run, guest_id = ids.parse_guest(value)
        if not (self._on_call_message(source) and tap.ephemeral):
            raise ids.BadValue("guest buttons are drawn only on cards")
        async with self._card_lock(source.user_id, event_id):
            notice: str | None = None
            async with self.sessionmaker() as db:
                try:
                    if action_id in ids.GUEST_RESPOND:
                        status = ids.GUEST_RESPOND[action_id]
                        await guests.answer_for(
                            db, member, event_id, guest_id, status=status, run=run, now=self.clock()
                        )
                    else:
                        offered = action_id in ids.GUEST_OFFER
                        role = (ids.GUEST_OFFER if offered else ids.GUEST_WITHDRAW)[action_id]
                        await guests.answer_for(
                            db, member, event_id, guest_id, role=role, offered=offered, run=run, now=self.clock()
                        )
                except SHOWN_ERRORS as exc:
                    notice = str(exc)
                except IllegalTransition as exc:
                    notice = str(friendly(exc))
                view = await event_views.event_view(db, member, event_id, now=self.clock())
            if view.event.run != run or view.event.state is not EventState.COLLECTING:
                notice = None
            await self._send_card(source, self._card(view, member, run=run, notice=notice), member, tap)
        return None

    async def _email_of(self, db: AsyncSession, admin: Member, guest_id: int) -> str | None:
        """The email Slack shares for a guest who's a full member of the workspace, to make
        them a member with; None when Slack doesn't share one, or for a Slack guest account."""
        guest = next((g for g in await guests.guests_for_admin(db, admin) if g.id == guest_id), None)
        if guest is None or guest.slack_user_id is None or self.people is None:
            return None
        person = await self.people.person(guest.slack_user_id)
        if not isinstance(person, guests.SlackPerson) or not person.full_member:
            return None
        return person.email

    async def _admin_guest(self, member: Member, action_id: str, value: object) -> str:
        """**Make member** and **Remove** on an admin's message about a guest who joined."""
        guest_id = ids.parse_event(value)
        async with self.sessionmaker() as db:
            if action_id == ids.MAKE_MEMBER:
                email = await self._email_of(db, member, guest_id)
                if email is None:
                    return NO_EMAIL.format(url=self.links.url("/admin/members#guests"))
                promoted = await guests.promote(db, member, guest_id, Role.MEMBER, email=email, now=self.clock())
                return f"{promoted.display_name} is a member now."
            removed = await guests.remove(db, member, guest_id, now=self.clock())
        return f"Removed {removed.display_name}. They can't join from the channel again unless you add them back."

    # Modals ------------------------------------------------------------------------------------

    async def _submission(self, ack: Callable[..., Awaitable[None]], body: dict[str, Any], kind: str) -> None:
        """Check the form's own fields before closing the modal, so mistakes show next to the
        field. Rules that need the database run after; their errors come as a message."""
        user = _get(body, "user", "id")
        if not self._team_ok(_get(body, "team", "id")) or not isinstance(user, str):
            await ack()
            return
        view = body.get("view") or {}
        values = _get(view, "state", "values")
        try:
            origin = ids.ViewOrigin.decode(view.get("private_metadata"))
            if (origin.window is not None) != (kind == "close"):
                raise ids.BadValue("bad metadata")  # only Close now's confirmation names a window
            if (origin.run is None and kind in {"cancel", "close", "feedback"}) or (
                origin.run is not None and kind == "proposal"
            ):
                raise ids.BadValue("bad metadata")  # these name a run, Details from a card may
            form: Any = origin.window if kind == "close" else FORMS[kind](values)
        except FormError as exc:
            await ack(response_action="errors", errors={exc.block_id: str(exc)})
            return
        except ids.BadValue:
            log.warning("slack modal with values this app didn't write")
            await ack()
            return
        await ack()
        source = Source(user, channel_id=origin.channel_id, home=origin.home)
        self._spawn(self._save(source, origin, kind, form))

    async def _save(self, source: Source, origin: ids.ViewOrigin, kind: str, form: Any) -> None:
        member = await self._member(source)
        if member is None:
            return
        event_id = origin.event_id

        async def work() -> str | None:
            if kind == "close":
                return await self._close(member, event_id, form, origin.run)
            if kind == "cancel":
                return await self._cancel(member, event_id, form, origin.run)
            if kind == "feedback":
                return await self._give_feedback(member, event_id, form, origin.run)
            if kind == "invite":
                return await self._invite(member, event_id, form, origin.run)
            async with self.sessionmaker() as db:
                if kind == "details":
                    await calls.save_details(db, member, event_id, form, run=origin.run, now=self.clock())
                    return None
                await calls.add_proposal(
                    db,
                    member,
                    event_id,
                    form.mode,
                    item_id=form.item_id,
                    free_text=form.free_text,
                    cuisine_id=form.cuisine_id,
                    now=self.clock(),
                )
                return None

        await self._run(source, member, work)

    # App Home ----------------------------------------------------------------------------------

    async def _home(self, user: str) -> None:
        async with self.sessionmaker() as db:
            member = await slack_members.member_for_slack_user(db, user)
        try:
            if member is None:
                await self._unknown(Source(user, home=True))
            elif not member.active:
                await self.publish_home(user, None, INACTIVE)
            else:
                await self.publish_home(user, member)
        except SLACK_ERRORS as exc:
            log.warning("slack home publish failed", extra={"error": str(exc)})

    # The slash command -------------------------------------------------------------------------

    async def _command(self, ack: Callable[..., Awaitable[None]], body: dict[str, Any]) -> None:
        if not self._team_ok(body.get("team_id")):
            await ack(response_type="ephemeral", text=WRONG_WORKSPACE)
            return
        words = str(body.get("text") or "").split()
        sub, args = (words[0].lower(), words[1:]) if words else ("help", [])
        discards = sub == "discard" and is_test_instance(self.instance_label)  # #60: test only
        if sub not in {"call", "cancel", "schedule", "offer", "log"} and not discards:
            await ack(response_type="ephemeral", text=self.command_reply(sub))
            return
        user, channel = body.get("user_id"), body.get("channel_id")
        await ack()
        if isinstance(user, str):
            source = Source(user, channel_id=channel if ids.is_channel_id(channel) else None)
            if sub == "call":
                self._spawn(self._call(source, [word.lower() for word in args]))
            elif sub in {"offer", "log"}:
                self._spawn(self._form_command(source, sub, body.get("trigger_id")))
            elif sub == "schedule":
                self._spawn(self._schedule_command(source))
            elif sub == "discard":
                self._spawn(self._discard_command(source, [word.lower() for word in args]))
            else:
                self._spawn(self._cancel_command(source, args))

    def command_reply(self, sub: str) -> str:
        url = self.links.url
        match sub:
            case "prefs" | "preferences":
                return f"Set what you like and what you'd rather skip: <{url('/preferences')}|your preferences>."
            case "catalog":
                return f"Browse and add meals and restaurants: <{url('/catalog')}|the catalog>."
            case "history":
                return f"See what the household ate lately: <{url('/history')}|history>."
            case "version":
                return self.version_reply()
            case "discard":  # reached only on an unlabeled instance; see _command
                return DISCARD_REFUSED
            case _:
                help_text = HELP.format(command=self.command, today=url("/"))
                if is_test_instance(self.instance_label):
                    help_text += "\n" + DISCARD_HELP.format(command=self.command)
                return f"{help_text}\n{version_line(self.version, self.instance_label)}"

    def version_reply(self) -> str:
        """Which version, build, instance, and schema revision is answering. None of it is
        secret: /healthz shows the version and label, and the revision follows from the version.
        So anyone in the workspace may ask, linked or not."""
        line = version_line(self.version, self.instance_label)
        return f"{line} · database {escape(self.schema_revision)} · {version_link(self.version)}"

    async def _call(self, source: Source, args: list[str]) -> None:
        member = await self._member(source)
        if member is None:
            return

        async def work() -> Done:
            async with self.sessionmaker() as db:
                today = await slack_members.today(db, member, now=self.clock())
                slot, day, early = parse_call_args(args, today, command=self.command)
                try:
                    event = await calls.start_call(
                        db, member, slot, day, now=self.clock(), instance_label=self.instance_label
                    )
                except CallExists:
                    scheduled = await event_views.scheduled_call(db, member, slot, day) if early else None
                    if scheduled is None:
                        raise
                    event = scheduled  # posting a call that's scheduled already
                if not early:
                    return self._started(await event_views.event_view(db, member, event.id, now=self.clock()), today)
            return await self._post_now(member, event.id, None)

        await self._run(source, member, work)

    async def _form_command(self, source: Source, sub: str, trigger: object) -> None:
        """``offer`` and ``log``: open the modal, as **Make an offer** on Home does."""
        member = await self._member(source)
        if member is None:
            return

        async def work() -> str | None:
            if sub == "offer":
                return await self._open_offer(member, source, trigger)
            return await self._open_log(member, source, trigger)

        await self._run(source, member, work)

    async def _schedule_command(self, source: Source) -> None:
        """``schedule``: each slot's standing schedule, with a link to change it on the web
        (FR-44). Changing it happens on the web, where the whole form fits."""
        member = await self._member(source)
        if member is None:
            return

        async def work() -> str | None:
            async with self.sessionmaker() as db:
                schedules = await schedule_service.slot_schedules_for(db, member)
            reply = schedule_reply(schedules, self._context())
            try:
                if source.channel_id is None:
                    raise PermanentDeliveryError("no channel to reply in")
                await self.api.ephemeral(source.channel_id, source.user_id, reply.text, reply.blocks)
            except SLACK_ERRORS:  # not in that channel, for example: a direct message instead
                await self.api.post(source.user_id, reply.payload())
            return None

        await self._run(source, member, work)

    async def _cancel_command(self, source: Source, args: list[str]) -> None:
        """``cancel [lunch|dinner] [day] [reason]``: call off that call, or today's only open one."""
        member = await self._member(source)
        if member is None:
            return
        if member.is_guest:  # before looking anything up, so the reply says nothing about calls (#38)
            await self.tell(source, REFUSED[GuestAction.CALL_OFF], member=member)
            return

        async def work() -> str | None:
            async with self.sessionmaker() as db:
                today = await slack_members.today(db, member, now=self.clock())
                slot, day, reason = parse_cancel_args(args, today, command=self.command)
                on = day or today
                found = await event_views.calls_on(db, member, on)
            when = "today" if on == today else f"on {day_label(on)}"
            if slot is not None:
                found = [event for event in found if event.slot is slot]
                if not found:
                    raise EventRuleViolation(f"There's no {slot.value} call {when}.")
            else:
                found = [event for event in found if event.state.is_open]
                if not found:
                    raise EventRuleViolation(NO_OPEN_CALL.format(when=when))
                if len(found) > 1:
                    named = "" if on == today else f" {on.isoformat()}"
                    return WHICH_CALL.format(when=when, command=self.command, day=named)
            return await self._cancel(member, found[0].id, reason, None)

        await self._run(source, member, work)

    async def _discard_command(self, source: Source, args: list[str]) -> None:
        """``discard [lunch|dinner] [today|tomorrow|YYYY-MM-DD]``, on a test instance only (#60):
        discard that call, dinner today by default. The service checks for an admin."""
        member = await self._member(source)
        if member is None:
            return
        if member.is_guest:  # before looking anything up (#38)
            await self.tell(source, GUESTS_DONT, member=member)
            return

        async def work() -> str | None:
            async with self.sessionmaker() as db:
                today = await slack_members.today(db, member, now=self.clock())
                try:
                    slot, day, early = parse_call_args(args, today, command=self.command)
                    if early:  # `now` means nothing to a discard
                        raise EventRuleViolation(DISCARD_USAGE)
                except EventRuleViolation:
                    raise EventRuleViolation(DISCARD_USAGE.format(command=self.command)) from None
                when = "today" if day == today else f"for {day_label(day)}"
                found = [event for event in await event_views.calls_on(db, member, day) if event.slot is slot]
                if not found:
                    raise EventRuleViolation(f"There's no {slot.value} call {when}.")
                await discard.discard_call(
                    db,
                    member.household_id,
                    found[0].id,
                    actor=member,
                    instance_label=self.instance_label,
                    now=self.clock(),
                )
            return f"Discarded the {slot.value} call {when}. Start a fresh one with `{self.command} call`."

        await self._run(source, member, work)


DISCARD_HELP = (
    "• Test instance: `{command} discard` deletes today's dinner call and everything in it, so a fresh "
    "one can start. Add `lunch` or a day. Admins only."
)
DISCARD_REFUSED = "Discarding a call works only on a test instance."
DISCARD_USAGE = "Try `{command} discard`, `{command} discard lunch`, or `{command} discard dinner 2026-10-24`."
HELP = (
    "*Dinnerbell* helps the household decide what to eat.\n"
    "• `{command} call` starts a dinner call for today. Add `lunch`, `tomorrow`, or a date "
    "like `2026-10-24`. A later day's call posts at its usual time that day; add `now` to post it now.\n"
    "• `{command} cancel` calls off today's call. Add `lunch` or `dinner`, a day, and a reason, "
    "like `{command} cancel dinner Pizza at Grandma's`.\n"
    "• `{command} prefs` links to your preferences.\n"
    "• `{command} catalog` and `{command} history` link to the catalog and meal history.\n"
    "• `{command} version` says which version of Dinnerbell is answering.\n"
    "• `{command} schedule` shows when calls go out, with a link to change it.\n"
    "• `{command} offer` offers a meal you're making or getting, with seats to spare. Lunch offers start as "
    "pickup at the usual lunch time.\n"
    "• `{command} log` records a meal the household ate outside Dinnerbell.\n"
    "Open the Dinnerbell app's Home tab for today's calls, or <{today}|the web app>."
)
CALL_USAGE = (
    "Try `{command} call`, `{command} call lunch tomorrow`, `{command} call lunch tomorrow now`, "
    "or `{command} call dinner 2026-10-24`."
)
POST_WORDS = frozenset({"now", "early"})
"""Words in ``call`` that post a later day's call now instead of at its usual time (#59)."""
CANCEL_USAGE = (
    "Try `{command} cancel`, `{command} cancel lunch`, or `{command} cancel dinner tomorrow Pizza at Grandma's`."
)
NO_OPEN_CALL = "There's no open call to call off {when}."
WHICH_CALL = (
    "Lunch and dinner are both open {when}. Which one? Try `{command} cancel lunch{day}` or "
    "`{command} cancel dinner{day}`, and add a reason after it if you like."
)
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
FORMS: dict[str, Callable[[Any], Any]] = {
    "details": parse_details,
    "proposal": parse_proposal,
    "cancel": parse_cancel,
    "feedback": parse_feedback,
    "invite": parse_invite,
}
"""Each modal's form parser, by kind. Close now's confirmation has no fields."""


GUEST_TAPS = frozenset(
    {
        *ids.CARD_RESPOND,
        *ids.CARD_OFFER,
        *ids.CARD_WITHDRAW,
        ids.CARD_COORDINATE,
        ids.ANSWER,
        ids.CALL_COORDINATE,
        ids.COORDINATE,
        *ids.RESPOND,
        *ids.OFFER,
        ids.MARK_DONE,
        ids.CLOSE_WINDOW,
        ids.CALL_MENU,
    }
)
"""Buttons a guest may tap (#38): answering, the roles, and coordinating's logistics. Everything
else is refused before any service runs; the services refuse it again."""
JOINED = {
    guests.JoinedAs.GUEST: (
        "Welcome to Dinnerbell! You're in as a guest. When a member invites you to a meal, you'll get a "
        "message here, and you can say whether you're in."
    ),
    guests.JoinedAs.MEMBER: "Welcome! Your Slack account is now linked to your Dinnerbell membership.",
    guests.JoinedAs.ALREADY: "You're in Dinnerbell already.",
}


NO_EMAIL = (
    "Slack doesn't share their email, or they're a Slack guest, so make them a member on "
    "<{url}|the Members page>, with their email."
)
JOIN_OFFER = (
    "You're not in Dinnerbell yet. Join as a guest: household members can then invite you to meals, "
    "and the admin can make you a member."
)


def join_offer_message() -> Message:
    text = JOIN_OFFER
    join = button("Join Dinnerbell", ids.JOIN_DINNERBELL, ids.JOIN_DINNERBELL, style="primary")
    return Message(text, [section(text), actions(join, block_id="join")])


def seat_reply(seat: offers.Seat) -> str:
    """Where a member stands on an offer after a tap on it."""
    left = "It's full now." if seat.seats_left == 0 else f"{seat.seats_left} spare."
    if seat.offerer:
        guests = f", with {seat.guests} guest{'' if seat.guests == 1 else 's'}" if seat.guests else ""
        return f"It's your offer, so you're in{guests}. {left}"
    if not seat.joined:
        return f"Got it: you're not joining {seat.item_name}. {left}"
    guests = f" with {seat.guests} guest{'' if seat.guests == 1 else 's'}" if seat.guests else ""
    return f"You're in for {seat.item_name}{guests}. {left} Tap Count me in again to drop out."


def posted_offer_reply(posted: offers.Posted) -> str:
    slot, day = posted.slot.value, day_label(posted.day)
    if posted.path is OfferPath.ATTACH:
        return (
            f"The {slot} call for {day} is open, so {posted.item_name} is a pick on it now, and you're in to "
            "make it happen. Thanks!"
        )
    if posted.path is OfferPath.REPLACE:
        return f"Posted your {slot} offer for {day}. It replaces the {slot} call scheduled for that day. Thanks!"
    return f"Posted your {slot} offer for {day} in the Dinnerbell channel. Thanks for sharing!"


def parse_call_args(args: list[str], today: date, *, command: str = DEFAULT_COMMAND) -> tuple[Slot, date, bool]:
    """``call [lunch|dinner] [today|tomorrow|YYYY-MM-DD] [now|early]``, in any order. Dinner
    today by default. The flag is whether to post a later day's call now (#59)."""
    slot, day, early = Slot.DINNER, today, False
    for word in args:
        if word in {s.value for s in Slot}:
            slot = Slot(word)
        elif word in POST_WORDS:
            early = True
        elif word == "today":
            day = today
        elif word == "tomorrow":
            day = today + timedelta(days=1)
        else:
            try:
                day = date.fromisoformat(word)
            except ValueError:
                raise EventRuleViolation(CALL_USAGE.format(command=command)) from None
    return slot, day, early


def parse_cancel_args(
    args: list[str], today: date, *, command: str = DEFAULT_COMMAND
) -> tuple[Slot | None, date | None, str | None]:
    """``cancel [lunch|dinner] [today|tomorrow|YYYY-MM-DD] [reason]``: the slot and the day, in
    either order, then the reason, which keeps its case. Only the first slot and the first day
    count, so after ``dinner`` a reason can start with "lunch". Slack escapes ``&``, ``<``, and ``>`` in a
    command's text; the reason gets them back, and is escaped again wherever it's shown."""
    slot: Slot | None = None
    day: date | None = None
    rest = list(args)
    while rest:
        word = rest[0].lower()
        if slot is None and word in {s.value for s in Slot}:
            slot = Slot(word)
        elif day is None and word in {"today", "tomorrow"}:
            day = today if word == "today" else today + timedelta(days=1)
        elif day is None and _ISO_DAY.fullmatch(word):
            try:
                day = date.fromisoformat(word)
            except ValueError:
                raise EventRuleViolation(CANCEL_USAGE.format(command=command)) from None
        else:
            break
        rest.pop(0)
    reason = " ".join(rest).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return slot, day, reason or None


def _get(data: object, *path: str) -> object:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data
