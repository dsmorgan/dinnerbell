# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign in with a code (spec 6.15): the code's alphabet, length, and lifetime, and how typed
text becomes a code.

A code is 10 characters from an alphabet without look-alikes: no 0 or O, no 1, I, or L, and
no U, so it reads aloud and types on a phone without guessing. It's valid for 15 minutes and
used once. Dinnerbell stores only its SHA-256 hash (``services.sign_in_codes``).
"""

from __future__ import annotations

from datetime import timedelta

ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 10
CODE_LIFETIME = timedelta(minutes=15)
MAX_TYPED_LENGTH = 32
"""The most a form may post for a code: room for spaces and dashes, and no more."""
_SEPARATORS = str.maketrans("", "", " -\t")


def normalize_code(typed: str) -> str | None:
    """The code ``typed`` spells, ignoring case, spaces, and dashes, or None if it can't be a
    code at all. A wrong code that looks right still comes back; only the database says
    whether it works."""
    code = typed[:MAX_TYPED_LENGTH].translate(_SEPARATORS).upper()
    if len(code) != CODE_LENGTH or any(char not in ALPHABET for char in code):
        return None
    return code


def display_code(code: str) -> str:
    """The code as Dinnerbell shows it once: two groups of five, such as ``K7M2Q-9XRTD``."""
    half = len(code) // 2
    return f"{code[:half]}-{code[half:]}"
