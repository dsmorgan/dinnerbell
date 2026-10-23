# SPDX-License-Identifier: AGPL-3.0-or-later
"""``SlackChannel``: the ``Channel`` protocol (docs/channels.md) on top of the Slack Web API.

Channel posts go to the configured channel, threaded when the drainer passes a thread. Direct
messages go to the member's Slack user ID with ``chat.postMessage``, which opens the DM. Every
post returns a ``MessageRef`` of Slack's channel ID and message ``ts``.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.channels.slack.api import SlackApi
from dinnerbell.channels.slack.blocks import Directory
from dinnerbell.channels.slack.messages import RenderContext, render
from dinnerbell.domain.notifications import DeliveryResult, MessageRef, Notification, PermanentDeliveryError
from dinnerbell.services import slack_members

log = logging.getLogger(__name__)


class SlackChannel:
    def __init__(
        self, api: SlackApi, *, channel_id: str, base_url: str, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        self.api = api
        self.channel_id = channel_id
        self.base_url = base_url
        self.sessionmaker = sessionmaker

    async def _context(self, household_id: int) -> RenderContext:
        async with self.sessionmaker() as db:
            directory = Directory(await slack_members.slack_directory(db, household_id))
        return RenderContext(self.base_url, directory)

    async def deliver(self, notification: Notification, *, thread: MessageRef | None = None) -> DeliveryResult:
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
        ctx = await self._context(notification.event.household_id)
        await self.api.update(ref, render(notification, ctx).payload())
