# SPDX-License-Identifier: AGPL-3.0-or-later
"""The version people see in the footer, and where it comes from."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dinnerbell import __version__


def test_package_version_matches_pyproject() -> None:
    """The release tag check in CI reads __version__; the wheel reads pyproject.toml."""
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__


def test_footer_shows_the_bare_version_on_a_release(client: TestClient) -> None:
    page = client.get("/").text
    assert f'<footer class="site-footer small muted">Dinnerbell {__version__}</footer>' in page
    assert f"/static/app.css?v={__version__}" in page


@pytest.mark.parametrize("settings_overrides", [{"build": "abc1234"}])
def test_footer_shows_the_build_stamp_on_a_main_build(client: TestClient) -> None:
    page = client.get("/").text
    assert f"Dinnerbell {__version__}+abc1234</footer>" in page
    # "+" in a query string means a space, so the cache buster escapes it.
    assert f"/static/app.js?v={__version__}%2Babc1234" in page
