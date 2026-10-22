# SPDX-License-Identifier: AGPL-3.0-or-later
"""The interface every channel adapter implements (spec section 13.4)."""

from __future__ import annotations

from typing import Protocol

from dinnerbell.domain.notifications import DeliveryResult, MessageRef, Notification


class Channel(Protocol):
    async def deliver(self, notification: Notification) -> DeliveryResult: ...

    async def update(self, ref: MessageRef, notification: Notification) -> None: ...
