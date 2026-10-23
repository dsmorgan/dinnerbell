# SPDX-License-Identifier: AGPL-3.0-or-later
"""``SlackChannel``: the ``Channel`` protocol (docs/channels.md) on top of the Slack Web API.

Channel posts go to the configured channel, threaded when the drainer passes a thread. Direct
messages go to the member's Slack user ID with ``chat.postMessage``, which opens the DM. Every
post returns a ``MessageRef`` of Slack's channel ID and message ``ts``.

While the setup is broken, deliveries are held (``HeldDelivery``) instead of reaching Slack,
where ``not_in_channel`` would close them for good. A held row doesn't use up an attempt; it
goes out once the admin fixes the setup. A bot that isn't in the channel, or an archived
channel, holds channel posts and updates only. A refused token or the wrong workspace holds
every delivery, direct messages too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.channels.slack.api import SlackApi
from dinnerbell.channels.slack.blocks import Directory
from dinnerbell.channels.slack.messages import RenderContext, render
from dinnerbell.domain.notifications import (
    DeliveryResult,
    HeldDelivery,
    MessageRef,
    Notification,
    PermanentDeliveryError,
)
from dinnerbell.services import slack_members

log = logging.getLogger(__name__)

HOLD_RETRY_SECONDS = 60.0
"""How soon a held delivery is tried again: when the supervisor checks the setup again."""


class HoldScope(StrEnum):
    NONE = "none"
    CHANNEL = "channel"
    """The bot can't post in the channel: hold channel posts and updates."""
    ALL = "all"
    """The token or the workspace is wrong: hold everything."""


@dataclass
class Hold:
    """What to hold back while the setup is broken. The connection sets it; the channel reads it."""

    scope: HoldScope = HoldScope.NONE

    def check(self, *, direct: bool) -> None:
        if self.scope is HoldScope.ALL or (self.scope is HoldScope.CHANNEL and not direct):
            raise HeldDelivery("Slack setup problem; see /healthz and the logs", retry_after=HOLD_RETRY_SECONDS)


class SlackChannel:
    def __init__(
        self,
        api: SlackApi,
        *,
        channel_id: str,
        base_url: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        hold: Hold,
    ) -> None:
        self.api = api
        self.hold = hold
        self.channel_id = channel_id
        self.base_url = base_url
        self.sessionmaker = sessionmaker

    async def _context(self, household_id: int) -> RenderContext:
        async with self.sessionmaker() as db:
            directory = Directory(await slack_members.slack_directory(db, household_id))
        return RenderContext(self.base_url, directory)

    async def deliver(self, notification: Notification, *, thread: MessageRef | None = None) -> DeliveryResult:
        self.hold.check(direct=notification.recipient_id is not None)
        ctx = await self._context(notification.event.household_id)
        message = render(notification, ctx).payload()
        recipient = notification.recipient_id
        if recipient is not None:
            slack_user = ctx.directory.slack_ids.get(recipient)
            if slack_user is None:
                raise PermanentDeliveryError(f"member {recipient} has no Slack user")
            ref = await self.api.post(slack_user, message)
        elif thread is not None:
            ref = await self.api.post(thread.channel_id, message, thread_ts=thread.message_id)
        else:
            ref = await self.api.post(self.channel_id, message)
        log.info(
            "slack message posted",
            extra={"kind": notification.kind, "event_id": notification.event.id, "recipient_id": recipient},
        )
        return DeliveryResult(ref)

    async def update(self, ref: MessageRef, notification: Notification) -> None:
        self.hold.check(direct=False)
        ctx = await self._context(notification.event.household_id)
        await self.api.update(ref, render(notification, ctx).payload())
