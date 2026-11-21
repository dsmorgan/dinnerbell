# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web push adapter (ADR 0010, spec 6.15): Dinnerbell's own Web Push, on ``cryptography``,
``joserfc``, and ``httpx``. Nothing outside this package encrypts or signs a push.

- ``vapid``: the VAPID key pair, derived from ``DINNERBELL_SECRET_KEY``, and its JWT (RFC 8292).
- ``encryption``: the payload's aes128gcm encryption (RFC 8291, RFC 8188).
- ``sender``: ``WebPushSender``, the ``domain.push.PushSender`` protocol over HTTPS (RFC 8030).
"""
