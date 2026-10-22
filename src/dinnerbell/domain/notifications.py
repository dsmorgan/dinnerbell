# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notification types that channels deliver.

M2 replaces ``Notification`` with a tagged union over the triggers in spec section 9.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NotificationBase:
    idempotency_key: str


Notification = NotificationBase


@dataclass(frozen=True, slots=True)
class MessageRef:
    channel_id: str
    message_id: str


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    ref: MessageRef | None
