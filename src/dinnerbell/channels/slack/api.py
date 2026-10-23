# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Slack Web API calls Dinnerbell makes, with Slack's errors turned into the outbox's.

- Errors that retrying can't fix, such as ``not_in_channel``, raise ``PermanentDeliveryError``,
  and the drainer closes the row at once.
- Rate limits (HTTP 429, ``ratelimited``) raise ``SlackRateLimited``, whose ``retry_after``
  is Slack's ``Retry-After`` in seconds. The drainer waits at least that long.
- Anything else, including timeouts and Slack outages, raises ``SlackUnavailable``, which the
  drainer retries with backoff.

Every exception message is built from Slack's error code and the method name only. Slack's
own exception text and request details never reach logs or ``outbox.last_error``, so no
token can either.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import aiohttp
from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.web.async_client import AsyncWebClient

from dinnerbell.domain.notifications import MessageRef, PermanentDeliveryError

log = logging.getLogger(__name__)

DEFAULT_RETRY_AFTER = timedelta(seconds=30)
MAX_RETRY_AFTER = timedelta(hours=1)
PERMANENT = frozenset(
    {
        # The channel or person can't be reached by this bot.
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "user_not_found",
        "users_not_found",
        "user_disabled",
        "account_inactive",
        "cannot_dm_bot",
        "restricted_action",
        "restricted_action_read_only_channel",
        "restricted_action_thread_only_channel",
        "restricted_action_non_threadable_channel",
        "restricted_action_thread_locked",
        "cannot_reply_to_message",
        # The message itself is wrong or gone; sending it again changes nothing.
        "invalid_blocks",
        "invalid_blocks_format",
        "msg_blocks_too_long",
        "msg_too_long",
        "no_text",
        "invalid_arguments",
        "message_not_found",
        "cant_update_message",
        "edit_window_closed",
        "too_many_attachments",
    }
)
RATE_LIMITED = frozenset({"ratelimited", "rate_limited"})
AUTH = frozenset(
    {"invalid_auth", "not_authed", "token_revoked", "token_expired", "missing_scope", "not_allowed_token_type"}
)
"""Token problems. Deliveries retry them, because fixing the token and restarting lets the
outbox catch up; the startup check reports them as an error."""


class SlackUnavailable(Exception):
    """A Slack call failed in a way that may work later. Safe to log."""


class SlackAuthError(SlackUnavailable):
    """Slack refused the token. ``code`` is Slack's error code, such as ``invalid_auth``."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class SlackRateLimited(SlackUnavailable):
    """Slack asked the app to slow down. ``retry_after`` is how long it asked to wait, in
    seconds, which the outbox drainer honors (docs/channels.md)."""

    def __init__(self, message: str, *, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def error_code(exc: SlackApiError) -> str:
    """Slack's error code, such as ``not_in_channel``, or ``http_<status>``."""
    response = exc.response
    data = getattr(response, "data", None)
    code = data.get("error") if isinstance(data, dict) else None
    if isinstance(code, str) and code.isascii() and len(code) <= 64:
        return code
    return f"http_{getattr(response, 'status_code', 'unknown')}"


def _retry_after(exc: SlackApiError) -> timedelta:
    headers = getattr(exc.response, "headers", None) or {}
    for name, value in dict(headers).items():
        if str(name).lower() == "retry-after":
            try:
                return min(timedelta(seconds=max(int(str(value).strip()), 1)), MAX_RETRY_AFTER)
            except ValueError:
                break
    return DEFAULT_RETRY_AFTER


def translate(method: str, exc: BaseException) -> Exception:
    """The outbox-facing exception for a failed Slack call."""
    if isinstance(exc, SlackApiError):
        code = error_code(exc)
        status = getattr(exc.response, "status_code", None)
        if code in RATE_LIMITED or status == 429:
            wait = _retry_after(exc)
            seconds = int(wait.total_seconds())
            return SlackRateLimited(f"Slack rate-limited {method}; retry after {seconds}s", retry_after=seconds)
        if code in PERMANENT:
            return PermanentDeliveryError(f"Slack {method} failed: {code}")
        if code in AUTH:
            return SlackAuthError(f"Slack {method} failed: {code}", code=code)
        return SlackUnavailable(f"Slack {method} failed: {code}")
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return SlackUnavailable(f"Slack {method} timed out")
    return SlackUnavailable(f"Slack {method} failed: {type(exc).__name__}")


TRANSPORT_ERRORS = (SlackClientError, aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError)


@dataclass(frozen=True, slots=True)
class Identity:
    """What ``auth.test`` says about the bot token."""

    team_id: str
    bot_user_id: str


class SlackApi:
    """The handful of Web API methods Dinnerbell uses. All failures raise the exceptions above."""

    def __init__(self, client: AsyncWebClient) -> None:
        self.client = client

    async def _call(self, method: str, call: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await call()
        except TRANSPORT_ERRORS as exc:
            raise translate(method, exc) from None

    async def post(self, channel: str, message: dict[str, Any], *, thread_ts: str | None = None) -> MessageRef:
        response = await self._call(
            "chat.postMessage",
            lambda: self.client.chat_postMessage(
                channel=channel,
                text=message.get("text"),
                blocks=message.get("blocks"),
                thread_ts=thread_ts,
                unfurl_links=False,
            ),
        )
        return MessageRef(str(response["channel"]), str(response["ts"]))

    async def update(self, ref: MessageRef, message: dict[str, Any]) -> None:
        await self._call(
            "chat.update",
            lambda: self.client.chat_update(
                channel=ref.channel_id, ts=ref.message_id, text=message.get("text"), blocks=message.get("blocks")
            ),
        )

    async def ephemeral(self, channel: str, user: str, text: str) -> None:
        await self._call(
            "chat.postEphemeral", lambda: self.client.chat_postEphemeral(channel=channel, user=user, text=text)
        )

    async def open_view(self, trigger_id: str, view: dict[str, Any]) -> None:
        await self._call("views.open", lambda: self.client.views_open(trigger_id=trigger_id, view=view))

    async def publish_home(self, user: str, view: dict[str, Any]) -> None:
        await self._call("views.publish", lambda: self.client.views_publish(user_id=user, view=view))

    async def identity(self) -> Identity:
        response = await self._call("auth.test", self.client.auth_test)
        return Identity(team_id=str(response["team_id"]), bot_user_id=str(response["user_id"]))

    async def channel_info(self, channel: str) -> dict[str, Any]:
        response = await self._call("conversations.info", lambda: self.client.conversations_info(channel=channel))
        info: dict[str, Any] = response["channel"]
        return info

    async def dm_channel(self, user: str) -> str | None:
        """The ID of the bot's existing direct message with ``user``, or None. Never opens one."""
        response = await self._call(
            "conversations.open",
            lambda: self.client.conversations_open(users=user, prevent_creation=True),
        )
        channel = response.get("channel")
        found = channel.get("id") if isinstance(channel, dict) else None
        return found if isinstance(found, str) else None

    async def check_app_token(self, app_token: str) -> None:
        """Ask for a Socket Mode URL with the app-level token, to check it before connecting."""
        await self._call("apps.connections.open", lambda: self.client.apps_connections_open(app_token=app_token))

    async def user_by_email(self, email: str) -> dict[str, Any] | None:
        """The workspace user with this email, or None when there isn't one."""
        try:
            response = await self.client.users_lookupByEmail(email=email)
        except SlackApiError as exc:
            if error_code(exc) == "users_not_found":
                return None
            raise translate("users.lookupByEmail", exc) from None
        except TRANSPORT_ERRORS as exc:
            raise translate("users.lookupByEmail", exc) from None
        user: dict[str, Any] = response["user"]
        return user
