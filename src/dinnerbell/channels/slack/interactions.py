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
again. Logs carry member and event IDs only (SEC-5).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from slack_bolt.async_app import AsyncApp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell import __version__
from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.api import SlackApi, SlackUnavailable
from dinnerbell.channels.slack.blocks import (
    day_label,
    escape,
    instance_banner,
    version_line,
    version_link,
    with_version_footer,
)
from dinnerbell.channels.slack.views import (
    FormError,
    HomeLinks,
    cancel_modal,
    close_modal,
    details_modal,
    home_view,
    parse_cancel,
    parse_details,
    parse_proposal,
    proposal_modal,
)
from dinnerbell.domain.events import (
    STALE_CALL,
    EventRuleViolation,
    IllegalTransition,
    InvalidResponse,
    RoleOffer,
    Window,
    check_can_cancel,
    check_can_close_window,
    check_run,
    friendly,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.notifications import PermanentDeliveryError
from dinnerbell.services import calls, event_views, slack_members
from dinnerbell.services.clock import Clock
from dinnerbell.services.errors import Forbidden, NotFound

log = logging.getLogger(__name__)

DEFAULT_COMMAND = "/dinnerbell"
SHOWN_ERRORS = (EventRuleViolation, InvalidResponse, NotFound, Forbidden)
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
SETTLE_TIMEOUT_SECONDS = 10.0
Ack = Callable[..., Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Source:
    """Who pressed something, and where: a channel or DM message, the Home tab, or a modal."""

    user_id: str
    channel_id: str | None = None
    home: bool = False


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
    ) -> None:
        self.api = api
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
        """The active member behind ``source``, or None after telling them why not."""
        async with self.sessionmaker() as db:
            member = await slack_members.member_for_slack_user(db, source.user_id)
        if member is None or not member.active:
            log.info("slack user refused", extra={"member_id": member.id if member else None})
            await self.tell(source, UNKNOWN_USER if member is None else INACTIVE, member=None)
            return None
        return member

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
                today = await event_views.today_view(db, member, now=self.clock())
            view = home_view(today, self.links, notice, label=self.instance_label)
        await self.api.publish_home(user_id, with_version_footer(view, self.version, self.instance_label))

    async def _run(self, source: Source, member: Member, work: Callable[[], Awaitable[str | None]]) -> None:
        """Run one action and report how it went. ``work`` returns a confirmation or None."""
        notice: str | None = None
        try:
            notice = await work()
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
        member = await self._member(source)
        if member is None:
            return
        action_id, value = acts[0].get("action_id"), acts[0].get("value")
        if action_id == ids.CALL_MENU:  # an overflow menu sends the option it picked, not a value
            value = _get(acts[0], "selected_option", "value")
        trigger = body.get("trigger_id")
        log.info("slack action", extra={"member_id": member.id, "action": str(action_id)[:40]})

        async def work() -> str | None:
            return await self._dispatch(member, source, str(action_id), value, trigger)

        await self._run(source, member, work)

    async def _dispatch(
        self, member: Member, source: Source, action_id: str, value: object, trigger: object
    ) -> str | None:
        if action_id == ids.CALL_MENU:
            choice, event_id, run = ids.parse_menu(value)
            return await self._open_end(member, source, choice, event_id, run, trigger)
        if action_id == ids.CANCEL_CALL:
            choice, event_id, run = ids.parse_menu(value)
            if choice is not ids.MenuChoice.CANCEL:
                raise ids.BadValue("not a call-off value")
            return await self._open_end(member, source, choice, event_id, run, trigger)
        if action_id == ids.CLOSE_WINDOW:  # Slack asked them to confirm already
            window, event_id, run = ids.parse_window(value)
            return await self._close(member, event_id, window, run)
        now = self.clock()
        async with self.sessionmaker() as db:
            if action_id in ids.RESPOND:
                event_id = ids.parse_event(value)
                await calls.respond(db, member, event_id, ids.RESPOND[action_id], now=now)
                return await self._confirm(db, member, event_id, action_id)
            if action_id in ids.OFFER:
                role = ids.OFFER[action_id]
                offered = await calls.toggle_role(db, member, ids.parse_event(value), role, now=now)
                return (OFFERED if offered else WITHDRAWN)[role]
            if action_id == ids.COORDINATE:
                event_id = ids.parse_event(value)
                await calls.claim_coordinator(db, member, event_id, now=now)
                return await self._confirm(db, member, event_id, action_id)
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
                await calls.start_call(db, member, slot, day, now=now)
                return f"Started the {slot.value} call. It's posted in the Dinnerbell channel."
            if action_id in {ids.DETAILS, ids.ADD_PROPOSAL}:
                return await self._open_modal(db, member, source, action_id, ids.parse_event(value), trigger)
        raise ids.BadValue("unknown action")

    async def _confirm(self, db: AsyncSession, member: Member, event_id: int, action_id: str) -> str:
        view = await event_views.event_view(db, member, event_id, now=self.clock())
        return CONFIRM[action_id].format(slot=view.event.slot.value)

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
            await self.api.open_view(trigger, details_modal(view, origin))
        else:
            if not view.can.propose:
                return "You can add proposals only while voting is open."
            await self.api.open_view(trigger, proposal_modal(view, origin))
        return None

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
            if (origin.run is not None) != (kind in {"cancel", "close"}):
                raise ids.BadValue("bad metadata")  # and only it and Call it off name a run
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
            async with self.sessionmaker() as db:
                if kind == "details":
                    await calls.save_details(db, member, event_id, form, now=self.clock())
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
            if member is None or not member.active:
                await self.publish_home(user, None, UNKNOWN_USER if member is None else INACTIVE)
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
        if sub not in {"call", "cancel"}:
            await ack(response_type="ephemeral", text=self.command_reply(sub))
            return
        user, channel = body.get("user_id"), body.get("channel_id")
        await ack()
        if isinstance(user, str):
            source = Source(user, channel_id=channel if ids.is_channel_id(channel) else None)
            if sub == "call":
                self._spawn(self._call(source, [word.lower() for word in args]))
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
            case "log":
                return f"Logging a meal from Slack is coming soon. For now, log it on <{url('/history')}|History>."
            case "offer":
                return f"Offers are coming soon. Until then, start a call with `{self.command} call`."
            case "schedule":
                return (
                    "Changing the schedule from Slack is coming soon. For now, the admin can change it "
                    f"on the web: <{url('/admin/schedule')}|Schedule>."
                )
            case "version":
                return self.version_reply()
            case _:
                help_text = HELP.format(command=self.command, today=url("/"))
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

        async def work() -> str | None:
            async with self.sessionmaker() as db:
                today = await slack_members.today(db, member, now=self.clock())
                slot, day = parse_call_args(args, today, command=self.command)
                await calls.start_call(db, member, slot, day, now=self.clock())
            when = "today" if day == today else f"for {day_label(day)}"
            return f"Started the {slot.value} call {when}. It's posted in the Dinnerbell channel."

        await self._run(source, member, work)

    async def _cancel_command(self, source: Source, args: list[str]) -> None:
        """``cancel [lunch|dinner] [day] [reason]``: call off that call, or today's only open one."""
        member = await self._member(source)
        if member is None:
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


HELP = (
    "*Dinnerbell* helps the household decide what to eat.\n"
    "• `{command} call` starts a dinner call for today. Add `lunch`, `tomorrow`, or a date "
    "like `2026-10-24`.\n"
    "• `{command} cancel` calls off today's call. Add `lunch` or `dinner`, a day, and a reason, "
    "like `{command} cancel dinner Pizza at Grandma's`.\n"
    "• `{command} prefs` links to your preferences.\n"
    "• `{command} catalog` and `{command} history` link to the catalog and meal history.\n"
    "• `{command} version` says which version of Dinnerbell is answering.\n"
    "• `offer`, `log`, and `schedule` are coming soon.\n"
    "Open the Dinnerbell app's Home tab for today's calls, or <{today}|the web app>."
)
CALL_USAGE = "Try `{command} call`, `{command} call lunch tomorrow`, or `{command} call dinner 2026-10-24`."
CANCEL_USAGE = (
    "Try `{command} cancel`, `{command} cancel lunch`, or `{command} cancel dinner tomorrow Pizza at Grandma's`."
)
NO_OPEN_CALL = "There's no open call to call off {when}."
WHICH_CALL = (
    "Lunch and dinner are both open {when}. Which one? Try `{command} cancel lunch{day}` or "
    "`{command} cancel dinner{day}`, and add a reason after it if you like."
)
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
FORMS: dict[str, Callable[[Any], Any]] = {"details": parse_details, "proposal": parse_proposal, "cancel": parse_cancel}
"""Each modal's form parser, by kind. Close now's confirmation has no fields."""


def parse_call_args(args: list[str], today: date, *, command: str = DEFAULT_COMMAND) -> tuple[Slot, date]:
    """``call [lunch|dinner] [today|tomorrow|YYYY-MM-DD]``, in any order. Dinner today by default."""
    slot, day = Slot.DINNER, today
    for word in args:
        if word in {s.value for s in Slot}:
            slot = Slot(word)
        elif word == "today":
            day = today
        elif word == "tomorrow":
            day = today + timedelta(days=1)
        else:
            try:
                day = date.fromisoformat(word)
            except ValueError:
                raise EventRuleViolation(CALL_USAGE.format(command=command)) from None
    return slot, day


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
