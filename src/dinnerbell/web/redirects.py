# SPDX-License-Identifier: AGPL-3.0-or-later
"""Redirects that stay on this site.

Build every redirect whose target holds a value from the request with ``local_redirect``: a
constant path such as ``"/events/{}"`` and the row IDs or enum members that fill it. Nothing
else from the request can reach the ``Location`` header.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit

from fastapi.responses import RedirectResponse

_SAFE_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _is_local_path(template: str) -> bool:
    """A path on this site: one leading "/", so a browser can't read it as another host
    (``//host``, ``/\\host``) or a scheme (``https:``), and printable ASCII only."""
    if not template.startswith("/") or template.startswith(("//", "/\\")):
        return False
    if "\\" in template or not (template.isascii() and template.isprintable()):
        return False
    parts = urlsplit(template)
    return not parts.scheme and not parts.netloc


def _text(value: int | StrEnum) -> str:
    """A row ID or an enum member as URL text; anything else is a bug in the caller."""
    if isinstance(value, StrEnum):
        text = value.value
    elif isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    else:
        raise TypeError(f"redirect values are row IDs or enum members, not {type(value).__name__}")
    if not text or not set(text) <= _SAFE_CHARACTERS:
        raise ValueError("a redirect value must be letters, digits, '-', or '_'")
    return text


def local_path(template: str, *values: int | StrEnum) -> str:
    """Fill each ``{}`` in ``template``, a constant path on this site, with a row ID or an enum
    member, such as ``local_path("/catalog/items/{}?notice=updated", item_id)``.

    Each value goes on the right of a ``+``. Only the start of a URL decides where it goes, and
    CodeQL's py/url-redirection query treats the right of a ``+`` as safe for that reason.
    """
    pieces = template.split("{}")
    if not _is_local_path(template) or any("{" in piece or "}" in piece for piece in pieces):
        raise ValueError(f"not a path on this site: {template!r}")
    if len(pieces) != len(values) + 1:
        raise ValueError(f"{template!r} has {len(pieces) - 1} placeholders but got {len(values)} values")
    path = pieces[0]
    for value, piece in zip(values, pieces[1:], strict=True):
        path = path + _text(value)
        path = path + piece
    return path


def local_redirect(template: str, *values: int | StrEnum) -> RedirectResponse:
    """A 303 See Other to ``local_path(template, *values)``, as every form post returns."""
    return RedirectResponse(local_path(template, *values), status_code=303)
