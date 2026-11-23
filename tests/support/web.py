# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers for driving M1 pages the way a browser does: with the CSRF token from the page."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.testclient import TestClient

from tests.conftest import csrf_from


def submit(
    client: TestClient,
    path: str,
    data: dict[str, Any] | None = None,
    *,
    files: dict[str, Any] | None = None,
    **headers: str,
) -> httpx.Response:
    """POST a form with the signed-in session's CSRF token, as the page would."""
    return client.post(path, data=(data or {}) | {"csrf_token": csrf_from(client)}, files=files, headers=headers)


def add_item(
    client: TestClient,
    name: str,
    *,
    kind: str = "restaurant",
    cuisines: str = "Thai",
    **fields: Any,
) -> httpx.Response:
    # The form starts with Lunch and Dinner ticked (#72), so a browser sends them.
    data: dict[str, Any] = {"kind": kind, "name": name, "new_cuisines": cuisines, "meals": ["lunch", "dinner"]}
    if kind == "restaurant":
        data["modes"] = ["pickup"]
    data.update(fields)
    return submit(client, "/catalog/items", data)


def htmx(client: TestClient, path: str, data: dict[str, Any]) -> httpx.Response:
    return client.post(path, data=data, headers={"HX-Request": "true", "X-CSRF-Token": csrf_from(client)})


def tampered(value: str) -> str:
    """``value`` with its first character changed, as a forger would change a signed value or its
    MAC. It always differs from ``value`` (#338). Overwriting the end with fixed characters, such
    as ``AA``, sometimes left a value unchanged, and a base64 string's last character can carry
    padding bits that some decoders ignore."""
    return ("B" if value[0] == "A" else "A") + value[1:]
