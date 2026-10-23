# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Slack connection: startup checks, the Socket Mode socket, and linking members by email.

``SlackConnection.start`` runs a supervisor in the background, so a slow or broken Slack never
holds up the web app. Each pass:

1. Checks the bot token with ``auth.test``, that its workspace matches
   ``DINNERBELL_SLACK_TEAM_ID`` when that is set, the app-level token, and that the bot is a
   member of ``DINNERBELL_SLACK_CHANNEL`` (``conversations.info``'s ``is_member``).
2. Connects Socket Mode once the tokens and workspace check out. The SDK's client pings Slack
   and reconnects by itself after a drop.
3. Links active members who have no Slack user yet by email (``users.lookupByEmail``, FR-42).

A failed check is logged once with what to do and shown in /healthz as ``error``; the next pass
checks again, so inviting the bot fixes it without a restart. Slack being unreachable shows as
``disconnected``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any, Protocol

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.channels.slack.api import SlackApi, SlackAuthError, SlackUnavailable
from dinnerbell.channels.slack.channel import SlackChannel
from dinnerbell.channels.slack.interactions import Interactions
from dinnerbell.domain.members import SLACK_USER_ID_PATTERN
from dinnerbell.domain.notifications import PermanentDeliveryError
from dinnerbell.services import slack_members
from dinnerbell.services.clock import Clock
from dinnerbell.services.health import SlackState

log = logging.getLogger(__name__)

WEB_TIMEOUT_SECONDS = 15
PING_INTERVAL_SECONDS = 10.0
CHECK_AGAIN_AFTER_FAILURE = 60.0
CHECK_AGAIN_AFTER_SUCCESS = 3600.0
"""Hourly: re-check the setup and link newly invited members by email."""
STOP_TIMEOUT_SECONDS = 10.0
REFUSED_FLAGS = ("deleted", "is_bot", "is_app_user", "is_restricted", "is_ultra_restricted", "is_stranger")
"""User flags that stop an email match from linking. Slack may leave a flag out when it's false
(docs.slack.dev/reference/objects/user-object), so any value but a missing or false one refuses."""


class Socket(Protocol):
    """The Socket Mode connection, so tests can swap in a fake."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def is_connected(self) -> bool: ...


class BoltSocket:
    """Bolt's asyncio Socket Mode handler over aiohttp, which the async Web API client already
    uses, so Socket Mode adds no dependency. Its client pings every ``PING_INTERVAL_SECONDS``
    and reconnects on its own after a drop or a stale connection."""

    def __init__(self, app: AsyncApp, app_token: str) -> None:
        self.handler = AsyncSocketModeHandler(app, app_token, ping_interval=PING_INTERVAL_SECONDS)

    async def connect(self) -> None:
        await self.handler.connect_async()  # type: ignore[no-untyped-call]

    async def close(self) -> None:
        await self.handler.close_async()  # type: ignore[no-untyped-call]

    async def is_connected(self) -> bool:
        return bool(await self.handler.client.is_connected())


SocketFactory = Callable[[AsyncApp, str], Socket]


class SetupProblem(Exception):
    """A startup check failed. The message says what to fix and never includes a secret."""


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
    ) -> None:
        client = web_client or AsyncWebClient(token=bot_token, timeout=WEB_TIMEOUT_SECONDS)
        self.api = SlackApi(client)
        self.app_token = app_token
        self.channel_id = channel_id
        self.required_team = team_id
        self.sessionmaker = sessionmaker
        self.clock = clock
        self.state = state
        self.socket_factory = socket_factory
        self.socket: Socket | None = None
        self.team_id: str | None = None
        self.channel = SlackChannel(
            self.api, channel_id=channel_id, base_url=base_url, sessionmaker=sessionmaker, state=state
        )
        self.interactions = Interactions(
            self.api, sessionmaker=sessionmaker, clock=clock, base_url=base_url, channel_id=channel_id
        )

        async def authorize(team_id: str | None) -> AuthorizeResult:
            """Whose request this is, for Bolt. The handlers check the workspace themselves."""
            return AuthorizeResult(enterprise_id=None, team_id=team_id, bot_token=bot_token)

        # Bolt gets an authorize function instead of the token: with a token it would call
        # auth.test itself on the first request. Every Web API call goes through ``self.api``;
        # the tokenless client here only serves the socket's apps.connections.open.
        self.app = AsyncApp(
            client=AsyncWebClient(timeout=WEB_TIMEOUT_SECONDS),
            authorize=authorize,
            name="dinnerbell",
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

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.socket is not None:
            try:
                await asyncio.wait_for(self.socket.close(), STOP_TIMEOUT_SECONDS)
            except Exception:
                log.warning("slack socket did not close cleanly", exc_info=True)
        await self.interactions.settle(STOP_TIMEOUT_SECONDS)

    async def _supervise(self) -> None:
        while True:
            healthy = await self.run_once()
            await asyncio.sleep(CHECK_AGAIN_AFTER_SUCCESS if healthy else CHECK_AGAIN_AFTER_FAILURE)

    async def run_once(self) -> bool:
        """One supervisor pass. True when every check passed."""
        try:
            await self.check()
        except SetupProblem as exc:
            self._report(str(exc))
            return False
        except SlackUnavailable as exc:
            self._report(None)
            log.warning("slack unreachable; will retry", extra={"error": str(exc)})
            return False
        except Exception:
            log.exception("slack startup check failed")
            return False
        self._report(None)
        try:
            await self.link_members()
        except Exception:
            log.exception("linking slack users failed")
        return True

    def _report(self, problem: str | None) -> None:
        if problem != self.state.problem:
            if problem is None:
                log.info("slack checks passed")
            else:
                log.error("slack setup problem: %s", problem)
        self.state.problem = problem

    # Checks ------------------------------------------------------------------------------------

    async def check(self) -> None:
        """Raise SetupProblem with what to fix, or SlackUnavailable if Slack can't be reached.
        Connects the socket once the tokens and the workspace are right."""
        try:
            identity = await self.api.identity()
        except (SlackAuthError, PermanentDeliveryError) as exc:
            raise SetupProblem(
                f"Slack refused DINNERBELL_SLACK_BOT_TOKEN ({exc}). Copy the Bot User OAuth Token "
                "(xoxb-) again from the app's Install App page, then restart Dinnerbell."
            ) from None
        if self.required_team and identity.team_id != self.required_team:
            raise SetupProblem(
                f"The bot token belongs to Slack workspace {identity.team_id}, but DINNERBELL_SLACK_TEAM_ID "
                f"is {self.required_team}. Install the app in the household workspace, or fix the team ID."
            )
        self.team_id = identity.team_id
        self.interactions.team_id = identity.team_id
        if self.socket is None:
            await self._connect()
        await self._check_channel()

    async def _connect(self) -> None:
        try:
            await self.api.check_app_token(self.app_token)
        except (SlackAuthError, PermanentDeliveryError) as exc:
            raise SetupProblem(
                f"Slack refused DINNERBELL_SLACK_APP_TOKEN ({exc}). Create an app-level token (xapp-) "
                "with the connections:write scope under Basic Information, and check that Socket Mode is on."
            ) from None
        self.socket = self.socket_factory(self.app, self.app_token)
        # The SDK retries a failed connect forever, so never wait for it here.
        connect = asyncio.get_running_loop().create_task(self.socket.connect())
        connect.add_done_callback(_log_connect_failure)

    async def _check_channel(self) -> None:
        channel = self.channel_id
        try:
            info = await self.api.channel_info(channel)
        except PermanentDeliveryError:
            raise SetupProblem(
                f"Slack can't find the channel {channel}. Set DINNERBELL_SLACK_CHANNEL to the channel's ID "
                "(it starts with C), and use a public channel."
            ) from None
        if info.get("is_archived"):
            raise SetupProblem(f"The channel {channel} is archived. Unarchive it or pick another channel.")
        if not info.get("is_member"):
            raise SetupProblem(
                f"The bot isn't a member of the channel {channel}. In that channel, send /invite @Dinnerbell."
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
        log.error("slack socket connect failed", extra={"error": type(task.exception()).__name__})
