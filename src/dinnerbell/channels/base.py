# SPDX-License-Identifier: AGPL-3.0-or-later
"""The interface every channel adapter implements (spec section 13.4), and ``NullChannel``.

``Channel`` is defined in ``domain.notifications`` so the outbox drainer in ``services`` can
name it without importing this package (spec 13.2: channels depend on services, not the other
way round). docs/channels.md is the full contract.
"""

from __future__ import annotations

import logging
import uuid

from dinnerbell.domain.notifications import Channel, DeliveryResult, MessageRef, Notification

__all__ = ["Channel", "DeliveryResult", "MessageRef", "Notification", "NullChannel"]

log = logging.getLogger(__name__)
NULL_CHANNEL_ID = "null"


class NullChannel:
    """Delivers nowhere. It logs each notification and reports it delivered, with a made-up
    message reference so updates and threads follow the same path a real channel takes.

    It is the channel until a Slack adapter is configured. Logs carry IDs only (SEC-5).
    """

    async def deliver(self, notification: Notification, *, thread: MessageRef | None = None) -> DeliveryResult:
        log.info(
            "notification not sent: no channel configured",
            extra={
                "kind": notification.kind,
                "event_id": notification.event.id,
                "recipient_id": notification.recipient_id,
                "threaded": thread is not None,
            },
        )
        return DeliveryResult(MessageRef(NULL_CHANNEL_ID, uuid.uuid4().hex))

    async def update(self, ref: MessageRef, notification: Notification) -> None:
        log.info(
            "notification update not sent: no channel configured",
            extra={"kind": notification.kind, "event_id": notification.event.id, "message_id": ref.message_id},
        )
