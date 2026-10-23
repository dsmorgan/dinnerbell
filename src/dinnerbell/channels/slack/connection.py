# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Slack connection: startup checks, the Socket Mode socket, and linking members by email.

``SlackConnection.start`` runs a supervisor in the background, so a slow or broken Slack never
holds up the web app. Each pass:

1. Checks the bot token with ``auth.test``, that its workspace matches
   ``DINNERBELL_SLACK_TEAM_ID`` when that is set, the app-level token, and that the bot is a
   member of ``DINNERBELL_SLACK_CHANNEL`` (``conversations.info``'s ``is_member``). On a
   labeled instance it also reads the channel's members (``conversations.members``), the only
   people the bot may then DM or mention (``channel.Audience``, ADR 0006).
2. Connects Socket Mode once the tokens and workspace check out. The SDK's client pings Slack
   and reconnects by itself after a drop. If it stays down for two minutes, the app-level token
   is checked again, so a revoked ``xapp-`` token shows as an error.
3. Links active members who have no Slack user yet by email (``users.lookupByEmail``, FR-42).

A failed check is logged once with what to do, shown in /healthz as ``error``, and holds
deliveries (see ``channel.Hold``); the next pass checks again, so inviting the bot fixes it
without a restart. A token Slack refuses during a delivery or an interaction does the same at
once and wakes the supervisor. Slack being unreachable shows as ``disconnected`` and keeps any
problem already known.

On a labeled instance, the channel's members are read again on every pass, even when only the
app-level token is wrong, so inviting someone to the channel takes effect within the hour. Until
the first read, and after Slack refuses the read (``missing_scope`` gets a hint in the log) or a
check finds a problem that holds deliveries, the members are unknown: direct messages wait
(``HeldDelivery``) and mentions show as plain names until a later pass, within a minute, reads
them. Channel posts still go out. A read that times out, or Slack being unreachable, keeps the
members already read, like any problem already known.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.channels.slack.api import SlackApi, SlackAuthError, SlackUnavailable
from dinnerbell.channels.slack.channel import HOLD_RETRY_SECONDS, Audience, Hold, HoldScope, SlackChannel
from dinnerbell.channels.slack.interactions import DEFAULT_COMMAND, Interactions
from dinnerbell.domain.members import SLACK_USER_ID_PATTERN
from dinnerbell.domain.notifications import PermanentDeliveryError
from dinnerbell.services import slack_members
from dinnerbell.services.clock import Clock
from dinnerbell.services.health import SlackState

log = logging.getLogger(__name__)

WEB_TIMEOUT_SECONDS = 15
PING_INTERVAL_SECONDS = 10.0
CHECK_AGAIN_AFTER_FAILURE = HOLD_RETRY_SECONDS
CHECK_AGAIN_AFTER_SUCCESS = 3600.0
"""Hourly: re-check the setup and link newly invited members by email."""
MIN_CHECK_GAP = 5.0
"""The least time between two supervisor passes, however often it's woken."""
RECHECK_APP_TOKEN_AFTER = 120.0
"""Seconds the socket may stay down before the app-level token is checked again."""
SOCKET_CLOSE_SECONDS = 4.0
SETTLE_SECONDS = 3.0
"""Shutdown waits at most this long for each, and never past the deadline ``stop`` is given."""
MEMBERS_PAGE_SIZE = 200
"""Slack's recommended page size for ``conversations.members``."""
MAX_MEMBER_PAGES = 10
"""Read at most this many pages (2,000 people) per check. A household channel is far smaller;
past the cap, the people not read are treated as outside the channel."""
MEMBERS_SCOPE_HINT = (
    "Slack refused conversations.members (missing_scope), so this labeled instance holds direct "
    "messages and mentions nobody. Add the channels:read scope (groups:read for a private channel) "
    "to the Slack app, then reinstall it."
)
REFUSED_FLAGS = ("deleted", "is_bot", "is_app_user", "is_restricted", "is_ultra_restricted", "is_stranger")
"""User flags that stop an email match from linking. Slack may leave a flag out when it's false
(docs.slack.dev/reference/objects/user-object), so any value but a missing or false one refuses."""


_SOCKET_URL = re.compile(r"\bwss?://[^\s'\"]+", re.IGNORECASE)
_QUERY_URL = re.compile(r"\bhttps?://[^\s'\"?]*\?[^\s'\"]*", re.IGNORECASE)


def quiet(record: logging.LogRecord) -> logging.LogRecord:
    """The SDK's Socket Mode client logs a full traceback when a connect fails, and its errors
    can include the one-time WebSocket URL, which carries a connection ticket. Keep the record
    at WARNING or below, drop tracebacks and stacks, and redact WebSocket URLs and any URL with
    a query string."""
    message = _QUERY_URL.sub("https://(redacted)", _SOCKET_URL.sub("wss://(redacted)", record.getMessage()))
    record.msg, record.args = message, None
    record.exc_info, record.exc_text, record.stack_info = None, None, None
    if record.levelno > logging.WARNING:
        record.levelno, record.levelname = logging.WARNING, "WARNING"
    return record


class _QuietForwarder(logging.Handler):
    """Passes the socket logger's records, and its children's, on to the parent logger after
    ``quiet``. A filter on a logger misses records from its children; this handler doesn't."""

    def emit(self, record: logging.LogRecord) -> None:
        logging.getLogger("dinnerbell.channels.slack").handle(quiet(record))


SOCKET_LOG = logging.getLogger("dinnerbell.channels.slack.socket")
BOLT_APP_LOGGER = "dinnerbell.channels.slack.bolt"
"""The Bolt app's name, which Bolt also uses as a logger name. Keep it a leaf logger."""
SOCKET_LOG.setLevel(logging.INFO)  # its DEBUG records include every payload
SOCKET_LOG.propagate = False  # everything goes through the forwarder instead
SOCKET_LOG.addHandler(_QuietForwarder())


class Socket(Protocol):
    """The Socket Mode connection, so tests can swap in a fake. A socket calls ``on_drop``,
    given to its factory, when a connection it had closes."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def is_connected(self) -> bool: ...


class BoltSocket:
    """Bolt's asyncio Socket Mode handler over aiohttp, which the async Web API client already
    uses, so Socket Mode adds no dependency. Its client pings every ``PING_INTERVAL_SECONDS``
    and reconnects on its own after a drop or a stale connection."""

    def __init__(self, app: AsyncApp, app_token: str, on_drop: Callable[[], None]) -> None:
        self.handler = AsyncSocketModeHandler(app, app_token, logger=SOCKET_LOG, ping_interval=PING_INTERVAL_SECONDS)

        async def dropped(message: object) -> None:
            on_drop()

        self.handler.client.on_close_listeners.append(dropped)

    async def connect(self) -> None:
        await self.handler.connect_async()  # type: ignore[no-untyped-call]

    async def close(self) -> None:
        await self.handler.close_async()  # type: ignore[no-untyped-call]

    async def is_connected(self) -> bool:
        return bool(await self.handler.client.is_connected())


SocketFactory = Callable[[AsyncApp, str, Callable[[], None]], Socket]


class SetupProblem(Exception):
    """A startup check failed. The message says what to fix and never includes a secret.
    ``hold`` is what to hold back until it's fixed."""

    def __init__(self, message: str, *, hold: HoldScope) -> None:
        super().__init__(message)
        self.hold = hold


BOT_REFUSED = (
    "Slack refused DINNERBELL_SLACK_BOT_TOKEN ({error}). Copy the Bot User OAuth Token (xoxb-) "
    "again from the app's Install App page, then restart Dinnerbell."
)


class SlackConnection:
    def __init__(
        self,
        *,
        bot_token: str,
        app_token: str,
        channel_id: str,
        team_id: str | None,
        base_url: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        clock: Clock,
        state: SlackState,
        web_client: AsyncWebClient | None = None,
        socket_factory: SocketFactory = BoltSocket,
        command: str = DEFAULT_COMMAND,
        instance_label: str = "",
    ) -> None:
        """A non-empty ``instance_label`` (``DINNERBELL_INSTANCE_LABEL``, such as test's) also
        limits DMs and mentions to the channel's members."""
        client = web_client or AsyncWebClient(token=bot_token, timeout=WEB_TIMEOUT_SECONDS)
        self.api = SlackApi(client)
        self.api.on_auth_error = self._token_refused
        self.app_token = app_token
        self.channel_id = channel_id
        self.required_team = team_id
        self.sessionmaker = sessionmaker
        self.clock = clock
        self.state = state
        self.hold = Hold()
        self.audience = Audience(limited=bool(instance_label))
        self._audience_error: str | None = None
        if self.audience.limited:
            state.dms_dropped = 0
        self.socket_factory = socket_factory
        self.socket: Socket | None = None
        self.team_id: str | None = None
        self.monotonic: Callable[[], float] = time.monotonic
        self._down_since: float | None = None
        self._checking = False
        self._wake = asyncio.Event()
        self._connect_task: asyncio.Task[None] | None = None
        self.channel = SlackChannel(
            self.api,
            channel_id=channel_id,
            base_url=base_url,
            sessionmaker=sessionmaker,
            hold=self.hold,
            instance_label=instance_label,
            audience=self.audience,
            state=state,
        )
        self.interactions = Interactions(
            self.api,
            sessionmaker=sessionmaker,
            clock=clock,
            base_url=base_url,
            channel_id=channel_id,
            command=command,
            instance_label=instance_label,
        )

        async def authorize(team_id: str | None) -> AuthorizeResult:
            """Whose request this is, for Bolt. The handlers check the workspace themselves."""
            return AuthorizeResult(enterprise_id=None, team_id=team_id, bot_token=bot_token)

        # Bolt gets an authorize function instead of the token: with a token it would call
        # auth.test itself on the first request. Every Web API call goes through ``self.api``;
        # the tokenless client here only serves the socket's apps.connections.open.
        # Bolt names a logger after the app and copies the root logger's level onto it on every
        # request. Named "dinnerbell", that reset the level of every Dinnerbell logger.
        self.app = AsyncApp(
            client=AsyncWebClient(timeout=WEB_TIMEOUT_SECONDS),
            authorize=authorize,
            name=BOLT_APP_LOGGER,
            raise_error_for_unhandled_request=False,
        )
        self.interactions.register(self.app)
        self._task: asyncio.Task[None] | None = None
        state.configured = True
        state.connected = self.is_connected

    async def is_connected(self) -> bool:
        return self.socket is not None and await self.socket.is_connected()

    # Lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._supervise())

    async def stop(self, deadline: float | None = None) -> None:
        """Shut down by ``deadline`` (the event loop's clock), or within the default waits."""
        loop = asyncio.get_running_loop()
        end = deadline if deadline is not None else loop.time() + SOCKET_CLOSE_SECONDS + SETTLE_SECONDS

        def left(most: float) -> float:
            return max(0.0, min(most, end - loop.time()))

        for task in (self._task, self._connect_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self.socket is not None:
            try:
                await asyncio.wait_for(self.socket.close(), left(SOCKET_CLOSE_SECONDS))
            except Exception:
                log.warning("slack socket did not close cleanly", exc_info=True)
        await self.interactions.settle(left(SETTLE_SECONDS))
        await self.interactions.cancel_pending()

    async def _supervise(self) -> None:
        while True:
            self._wake.clear()  # before the pass, so a wake during it isn't lost
            started = self.monotonic()
            healthy = await self.run_once()
            settled = healthy and self._down_since is None and self.audience.known
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(), CHECK_AGAIN_AFTER_SUCCESS if settled else CHECK_AGAIN_AFTER_FAILURE
                )
            # However often something wakes it, never check Slack more than every few seconds.
            await asyncio.sleep(max(0.0, MIN_CHECK_GAP - (self.monotonic() - started)))

    async def run_once(self) -> bool:
        """One supervisor pass. True when every check passed."""
        self._checking = True
        try:
            await self.check()
        except SetupProblem as exc:
            self._report(str(exc), exc.hold)
            if exc.hold is not HoldScope.NONE:
                self.audience.members = None  # the channel or token is wrong: trust no old list
            return False
        except SlackUnavailable as exc:
            # Unreachable says nothing about the setup: keep any problem, and its hold, as is.
            log.warning("slack unreachable; will retry", extra={"error": str(exc)})
            return False
        except Exception:
            log.exception("slack startup check failed")
            return False
        finally:
            self._checking = False
        self._report(None, HoldScope.NONE)
        try:
            await self.link_members()
        except Exception:
            log.exception("linking slack users failed")
        return True

    def _report(self, problem: str | None, hold: HoldScope) -> None:
        if problem != self.state.problem:
            if problem is None:
                log.info("slack checks passed")
            else:
                log.error("slack setup problem: %s", problem)
        self.state.problem = problem
        self.hold.scope = hold

    def _socket_dropped(self) -> None:
        """The socket lost a connection it had. The SDK reconnects by itself; the supervisor
        starts timing the outage now, so the app-token check comes two minutes after a drop."""
        if self._down_since is None:
            self._down_since = self.monotonic()
        self._wake.set()

    def _token_refused(self, error: SlackAuthError) -> None:
        """Slack refused the bot token during a delivery or an interaction: hold everything,
        show the error now, and have the supervisor check again right away."""
        if self._checking or self.hold.scope is HoldScope.ALL:
            return  # the check reports it itself; or it's already known
        self._report(BOT_REFUSED.format(error=error), HoldScope.ALL)
        self._wake.set()

    # Checks ------------------------------------------------------------------------------------

    async def check(self) -> None:
        """Raise SetupProblem with what to fix, or SlackUnavailable if Slack can't be reached.
        Connects the socket once the tokens and the workspace are right."""
        try:
            identity = await self.api.identity()
        except (SlackAuthError, PermanentDeliveryError) as exc:
            raise SetupProblem(BOT_REFUSED.format(error=exc), hold=HoldScope.ALL) from None
        if self.required_team and identity.team_id != self.required_team:
            raise SetupProblem(
                f"The bot token belongs to Slack workspace {identity.team_id}, but DINNERBELL_SLACK_TEAM_ID "
                f"is {self.required_team}. Install the app in the household workspace, or fix the team ID.",
                hold=HoldScope.ALL,
            )
        self.team_id = identity.team_id
        self.interactions.team_id = identity.team_id
        if self.socket is None:
            try:
                await self._check_app_token()
            except SetupProblem:
                if self.audience.limited:
                    # Deliveries use the bot token and still go, so keep their audience current.
                    with contextlib.suppress(SetupProblem, SlackUnavailable):
                        await self._check_channel()
                raise
            self._connect()
        await self._check_channel()
        await self._check_socket()

    async def _check_app_token(self) -> None:
        try:
            await self.api.check_app_token(self.app_token)
        except (SlackAuthError, PermanentDeliveryError) as exc:
            raise SetupProblem(
                f"Slack refused DINNERBELL_SLACK_APP_TOKEN ({exc}). Create an app-level token (xapp-) "
                "with the connections:write scope under Basic Information, and check that Socket Mode is on.",
                hold=HoldScope.NONE,  # deliveries use the bot token and still work
            ) from None

    def _connect(self) -> None:
        self.socket = self.socket_factory(self.app, self.app_token, self._socket_dropped)
        # The SDK retries a failed connect forever, so never wait for it here. Keep the task, so
        # it isn't garbage-collected mid-connect, and so stop() can cancel it.
        self._connect_task = asyncio.get_running_loop().create_task(self.socket.connect())
        self._connect_task.add_done_callback(_log_connect_failure)

    async def _check_socket(self) -> None:
        """If the socket has been down a while, check that the app-level token still works."""
        if await self.is_connected():
            self._down_since = None
            return
        now = self.monotonic()
        if self._down_since is None:
            self._down_since = now
        elif now - self._down_since > RECHECK_APP_TOKEN_AFTER:
            await self._check_app_token()

    async def _check_channel(self) -> None:
        channel = self.channel_id
        try:
            info = await self.api.channel_info(channel)
        except PermanentDeliveryError:
            raise SetupProblem(
                f"Slack can't find the channel {channel}. Set DINNERBELL_SLACK_CHANNEL to the channel's ID "
                "(it starts with C), and use a public channel.",
                hold=HoldScope.CHANNEL,
            ) from None
        if info.get("is_archived"):
            raise SetupProblem(
                f"The channel {channel} is archived. Unarchive it or pick another channel.", hold=HoldScope.CHANNEL
            )
        if not info.get("is_member"):
            raise SetupProblem(
                f"The bot isn't a member of the channel {channel}. In that channel, send /invite @Dinnerbell.",
                hold=HoldScope.CHANNEL,
            )
        if self.audience.limited:
            await self._read_audience()

    async def _read_audience(self) -> None:
        """Refresh who a labeled instance may DM or mention. Never raises. When Slack refuses
        the read, the members become unknown; when it's only unreachable, the members already
        read stay. Either way channel posts still go out."""
        try:
            members, complete = await self.api.channel_members(
                self.channel_id, page_size=MEMBERS_PAGE_SIZE, max_pages=MAX_MEMBER_PAGES
            )
        except Exception as exc:
            self._audience_failed(exc)
            return
        if self._audience_error is not None:
            log.info("read the channel's members again; DMs and mentions resume")
            self._audience_error = None
        if not complete:
            log.warning("the channel has more members than Dinnerbell reads; the rest get no DMs or mentions")
        self.audience.members = members

    def _audience_failed(self, exc: Exception) -> None:
        """Slack refusing the read (auth, scope, a missing channel), or a bug, makes the members
        unknown. A timeout, rate limit, or outage keeps the last list. Each failure logs once,
        not on every retry a minute apart."""
        unreachable = isinstance(exc, SlackUnavailable) and not isinstance(exc, SlackAuthError)
        if not unreachable:
            self.audience.members = None
        error = str(exc) if isinstance(exc, SlackUnavailable | PermanentDeliveryError) else type(exc).__name__
        if error == self._audience_error:
            return
        self._audience_error = error
        if isinstance(exc, SlackAuthError) and exc.code == "missing_scope":
            log.error(MEMBERS_SCOPE_HINT)
        elif unreachable:
            log.warning("reading the channel's members failed; keeping the last list", extra={"error": error})
        else:
            log.warning(
                "reading the channel's members failed; members unknown, so DMs wait and nobody is mentioned",
                extra={"error": error},
            )

    # Linking members (FR-42) -------------------------------------------------------------------

    async def link_members(self) -> list[int]:
        """Find each unlinked active member's Slack user by email. Emails are never logged."""
        async with self.sessionmaker() as db:
            household_id, unlinked = await slack_members.unlinked_members(db)
        found: dict[int, str] = {}
        for member in unlinked:
            try:
                user = await self.api.user_by_email(member.email)
            except (SlackUnavailable, PermanentDeliveryError) as exc:
                log.warning("slack user lookup failed; will retry", extra={"error": str(exc)})
                break
            slack_id = self._usable(user)
            if slack_id is not None:
                found[member.member_id] = slack_id
        if not found:
            return []
        async with self.sessionmaker() as db:
            linked = await slack_members.link_slack_users(db, household_id, found, now=self.clock())
        if linked:
            log.info("slack users linked by email", extra={"member_ids": linked})
        return linked

    def _usable(self, user: dict[str, Any] | None) -> str | None:
        """The user's ID if they're a full, active person in this workspace. Fails closed: a
        missing team, a guest, a bot, an external (Slack Connect) user, or a deactivated
        account never links."""
        if not user or self.team_id is None or user.get("team_id") != self.team_id:
            return None
        if any(user.get(flag, False) is not False for flag in REFUSED_FLAGS):
            return None
        slack_id = user.get("id")
        return slack_id if isinstance(slack_id, str) and SLACK_USER_ID_PATTERN.match(slack_id) else None


def _log_connect_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("slack socket connect failed", extra={"error": type(task.exception()).__name__})
