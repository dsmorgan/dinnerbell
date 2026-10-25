# SPDX-License-Identifier: AGPL-3.0-or-later
"""Redirects stay on this site, whatever reaches them (CodeQL py/url-redirection)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import pytest

from dinnerbell.domain.meals import Slot
from dinnerbell.web.redirects import local_path, local_redirect


class Sneaky(StrEnum):
    HOST = "/evil.example.com"
    EMPTY = ""


def test_fills_ids_and_enum_members() -> None:
    assert local_path("/") == "/"
    assert local_path("/events/{}", 7) == "/events/7"
    assert local_path("/catalog/items/{}?notice=updated", 2**63 - 1) == f"/catalog/items/{2**63 - 1}?notice=updated"
    assert local_path("/schedule?saved={}#{}", Slot.DINNER, Slot.DINNER) == "/schedule?saved=dinner#dinner"
    response = local_redirect("/events/{}#feedback", 3)
    assert response.status_code == 303
    assert response.headers["location"] == "/events/3#feedback"


@pytest.mark.parametrize(
    "template",
    [
        "",
        "events/{}",
        "//evil.example.com/{}",
        "/\\evil.example.com/{}",
        "/\\/evil.example.com",
        "https://evil.example.com/{}",
        "https:/evil.example.com",
        "javascript:alert(1)",
        " /events/{}",
        "/events/\n{}",
        "/events/é{}",
        "/events/{0}",
        "/events/{id}",
    ],
)
def test_refuses_templates_that_leave_the_site(template: str) -> None:
    with pytest.raises(ValueError, match="not a path on this site"):
        local_path(template, 1)


@pytest.mark.parametrize(
    "value",
    ["/evil.example.com", "//evil.example.com", "https://evil.example.com", "1", b"1", 1.0, True, None],
)
def test_refuses_anything_but_ids_and_enum_members(value: Any) -> None:
    """Request text never reaches a redirect, even when a caller passes it by mistake: with an
    empty prefix it would otherwise become ``//host`` or ``https:``."""
    with pytest.raises(TypeError):
        local_path("/{}", value)


@pytest.mark.parametrize("value", [Sneaky.HOST, Sneaky.EMPTY])
def test_refuses_enum_values_that_arent_plain_words(value: StrEnum) -> None:
    with pytest.raises(ValueError, match="letters, digits"):
        local_path("/{}", value)


def test_counts_values() -> None:
    with pytest.raises(ValueError, match="1 placeholders but got 2 values"):
        local_path("/events/{}", 1, 2)
    with pytest.raises(ValueError, match="1 placeholders but got 0 values"):
        local_path("/events/{}")
