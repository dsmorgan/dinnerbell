# SPDX-License-Identifier: AGPL-3.0-or-later
"""Message encryption for Web Push (RFC 8291), in the aes128gcm content coding (RFC 8188).

One record of up to 4,096 bytes, so the payload is at most 3,993 bytes. Each push gets a fresh
key pair and salt.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from dinnerbell.domain.push import PushTooLarge

RECORD_SIZE = 4096
HEADER_SIZE = 86
"""Salt (16), record size (4), key ID length (1), and the sender's public key (65)."""
MAX_PLAINTEXT = RECORD_SIZE - HEADER_SIZE - 16 - 1
"""One record holds the header, the plaintext, its 0x02 delimiter, and the 16-byte tag."""


class BadKeys(ValueError):
    """A subscription's keys can't be decoded or used. Only this deletes the device."""


def b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(
    plaintext: bytes,
    p256dh: str,
    auth: str,
    *,
    _salt: bytes | None = None,
    _private_key: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """The request body for a push of ``plaintext`` to the browser whose public key is
    ``p256dh`` and authentication secret is ``auth`` (both base64url). Raises ``BadKeys`` for
    keys that can't be decoded or used, and ``PushTooLarge`` for a payload over one record.

    ``_salt`` and ``_private_key`` are for tests only, to check RFC 8291's example. Every real
    push must get a fresh salt and key pair, so never pass them outside a test."""
    if len(plaintext) > MAX_PLAINTEXT:
        raise PushTooLarge(f"a push holds at most {MAX_PLAINTEXT} bytes")
    try:
        ua_public = b64decode(p256dh)
        auth_secret = b64decode(auth)
        if len(auth_secret) != 16:
            raise ValueError("the authentication secret isn't 16 bytes")
        receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise BadKeys(type(exc).__name__) from None
    sender = _private_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _point(sender.public_key())
    salt = _salt if _salt is not None else os.urandom(16)
    # RFC 8291 section 3.4: combine the shared secret with the authentication secret.
    ecdh_secret = sender.exchange(ec.ECDH(), receiver)
    prk_key = _hmac(auth_secret, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    # RFC 8188 section 2.2: the content encryption key and nonce.
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + ciphertext
