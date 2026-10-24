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
from functools import partial
from typing import Any, NoReturn

import aiohttp
from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.webhook.async_client import AsyncWebhookClient

from dinnerbell.domain.notifications import ChannelUnavailable, MessageRef, PermanentDeliveryError

log = logging.getLogger(__name__)

DEFAULT_RETRY_AFTER = timedelta(seconds=30)
RESPONSE_URL_TIMEOUT_SECONDS = 10
WEBHOOK_LOG = logging.getLogger("dinnerbell.channels.slack.webhook")
WEBHOOK_LOG.setLevel(logging.INFO)
"""The SDK's webhook client logs each request's URL and body at DEBUG whenever its logger's own
level allows it. A ``response_url`` is a bearer capability, so this logger never does."""
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
        "user_not_visible",
        "messages_tab_disabled",
        "is_inactive",
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
        # The request itself is malformed; it would fail the same way again.
        "invalid_arg_name",
        "invalid_form_data",
        "invalid_json",
        "invalid_array_arg",
        "invalid_charset",
        "invalid_post_type",
        "missing_post_type",
    }
)
RATE_LIMITED = frozenset({"ratelimited", "rate_limited"})
AUTH = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "missing_scope",
        "not_allowed_token_type",
    }
)
"""Slack refused the call's token or its permissions. Deliveries retry them."""
TOKEN_REFUSED = frozenset({"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"})
"""The token itself is no good, whatever the method. These stop every delivery until it's fixed
(see ``SlackApi.on_auth_error``). ``missing_scope`` and ``not_allowed_token_type`` concern one
method only, so they don't."""


class SlackUnavailable(ChannelUnavailable):
    """A Slack call failed in a way that may work later. Safe to log. The drainer ends its pass
    on it, so an outage costs one timeout per drain, not one per row."""


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


def webhook_client(url: str) -> AsyncWebhookClient:
    """The client for one ``response_url``."""
    return AsyncWebhookClient(url, timeout=RESPONSE_URL_TIMEOUT_SECONDS, logger=WEBHOOK_LOG)


def _webhook_code(body: object, status: int) -> str:
    """Slack's error for a refused ``response_url`` post, such as ``expired_url``, or ``http_<status>``."""
    text = body.strip() if isinstance(body, str) else ""
    if text and len(text) <= 64 and text.isascii() and text.replace("_", "").isalnum():
        return text
    return f"http_{status}"


@dataclass(frozen=True, slots=True)
class Identity:
    """What ``auth.test`` says about the bot token."""

    team_id: str
    bot_user_id: str


class SlackApi:
    """The handful of Web API methods Dinnerbell uses. All failures raise the exceptions above.

    ``on_auth_error`` is called whenever Slack refuses the token itself (``TOKEN_REFUSED``), from
    any call: a delivery, an interaction, or a check. The connection uses it to hold deliveries
    and report the error at once, instead of at its next hourly check.
    """

    def __init__(
        self, client: AsyncWebClient, *, webhook: Callable[[str], AsyncWebhookClient] = webhook_client
    ) -> None:
        self.client = client
        self.webhook = webhook
        """Makes the client for a ``response_url``; tests pass a fake."""
        self.on_auth_error: Callable[[SlackAuthError], None] | None = None

    def _raise(self, method: str, exc: BaseException) -> NoReturn:
        error = translate(method, exc)
        if isinstance(error, SlackAuthError) and error.code in TOKEN_REFUSED and self.on_auth_error is not None:
            self.on_auth_error(error)
        raise error from None

    async def _call(self, method: str, call: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await call()
        except TRANSPORT_ERRORS as exc:
            self._raise(method, exc)

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

    async def ephemeral(self, channel: str, user: str, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        await self._call(
            "chat.postEphemeral",
            lambda: self.client.chat_postEphemeral(channel=channel, user=user, text=text, blocks=blocks),
        )

    async def replace_original(self, response_url: str, message: dict[str, Any]) -> None:
        """Replace the message a person tapped, through the tap's ``response_url``. Only ever
        for a message that person alone sees: on a channel message, this would rewrite it for
        everyone. Slack allows 5 posts within 30 minutes of the tap; after that it refuses."""
        body = {**message, "replace_original": True, "response_type": "ephemeral"}
        response = await self._call("response_url", lambda: self.webhook(response_url).send_dict(body))
        if response.status_code == 429:
            raise SlackRateLimited("Slack rate-limited response_url", retry_after=DEFAULT_RETRY_AFTER.total_seconds())
        if response.status_code != 200:
            code = _webhook_code(response.body, response.status_code)
            if response.status_code >= 500:
                raise SlackUnavailable(f"Slack response_url failed: {code}")
            raise PermanentDeliveryError(f"Slack response_url failed: {code}")

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

    async def channel_members(self, channel: str, *, page_size: int, max_pages: int) -> tuple[frozenset[str], bool]:
        """The user IDs in ``channel`` (``conversations.members``), and whether every page was
        read. Stops after ``max_pages`` pages, so a huge channel can't keep a check busy."""
        members: set[str] = set()
        cursor: str | None = None
        for _ in range(max_pages):
            response = await self._call(
                "conversations.members",
                partial(self.client.conversations_members, channel=channel, limit=page_size, cursor=cursor),
            )
            members.update(m for m in response.get("members") or () if isinstance(m, str))
            metadata = response.get("response_metadata")
            cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
            if not cursor:
                return frozenset(members), True
        return frozenset(members), False

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
            self._raise("users.lookupByEmail", exc)
        except TRANSPORT_ERRORS as exc:
            self._raise("users.lookupByEmail", exc)
        user: dict[str, Any] = response["user"]
        return user
