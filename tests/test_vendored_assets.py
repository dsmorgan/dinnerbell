# SPDX-License-Identifier: AGPL-3.0-or-later
"""The vendored htmx and Figtree files are byte-for-byte what npm published, so a modified file
fails CI."""

import hashlib
import mimetypes
import re
from collections.abc import Iterator
from importlib.resources import files

import httpx
import pytest
from fastapi.testclient import TestClient

from dinnerbell.app_factory import create_app
from dinnerbell.config import Settings
from dinnerbell.web.middleware import CONTENT_SECURITY_POLICY, STATIC_CONTENT_SECURITY_POLICY
from dinnerbell.web.render import HTMX_FILE
from tests.conftest import BASE_URL
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider

# SHA-256 of `package/dist/htmx.min.js` in the npm package `htmx.org` at HTMX_VERSION (`npm pack
# htmx.org@2.0.11`, then `shasum -a 256 package/dist/htmx.min.js`). To update htmx, see
# CONTRIBUTING.md, "Update the vendored htmx".
HTMX_VERSION = "2.0.11"
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"


def test_htmx_file_name_carries_the_recorded_version() -> None:
    assert HTMX_VERSION in HTMX_FILE


def test_vendored_htmx_matches_the_npm_release() -> None:
    data = files("dinnerbell").joinpath("web", "static", HTMX_FILE).read_bytes()
    assert hashlib.sha256(data).hexdigest() == HTMX_SHA256, (
        f"{HTMX_FILE} differs from npm htmx.org@{HTMX_VERSION}. Restore it from the npm package, "
        "or follow CONTRIBUTING.md to update htmx on purpose."
    )


# SHA-256 of `package/files/figtree-<subset>-wght-normal.woff2` in the npm package
# `@fontsource-variable/figtree` at FIGTREE_VERSION (`npm pack @fontsource-variable/figtree@5.3.0`,
# then `shasum -a 256 package/files/*-wght-normal.woff2`). The license is the package's LICENSE. To
# update Figtree, see CONTRIBUTING.md, "Update the vendored Figtree".
FIGTREE_VERSION = "5.3.0"
FIGTREE_SHA256 = {
    "latin": "4ba7d3d096695818fe0686be4f1e82c6b05134e18a22260336130335027462dd",
    "latin-ext": "bf7828e2c258cffcfa50a048ee388a36b95bc16b452e8d36fa797635dbe15965",
}
FIGTREE_LICENSE_SHA256 = "ee23e6c84000126692e112ff067b456470493349f993ceaa8b4d3766dd6fad5d"
FONTS = files("dinnerbell").joinpath("web", "static", "fonts")


def figtree_file(subset: str) -> str:
    return f"figtree-{FIGTREE_VERSION}-{subset}-wght-normal.woff2"


def test_vendored_figtree_matches_the_npm_release() -> None:
    for subset, digest in FIGTREE_SHA256.items():
        data = FONTS.joinpath(figtree_file(subset)).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, (
            f"{figtree_file(subset)} differs from npm @fontsource-variable/figtree@{FIGTREE_VERSION}. Restore it "
            "from the npm package, or follow CONTRIBUTING.md to update Figtree on purpose."
        )
    license_text = FONTS.joinpath("Figtree-OFL-1.1.txt").read_bytes()
    assert hashlib.sha256(license_text).hexdigest() == FIGTREE_LICENSE_SHA256
    assert b"SIL OPEN FONT LICENSE Version 1.1" in license_text


def test_the_fonts_folder_holds_only_figtree_and_its_license() -> None:
    names = sorted(entry.name for entry in FONTS.iterdir())
    assert names == sorted(["Figtree-OFL-1.1.txt", *(figtree_file(s) for s in FIGTREE_SHA256)])


def test_the_stylesheet_loads_each_vendored_font_and_falls_back_to_the_device_font() -> None:
    css = files("dinnerbell").joinpath("web", "static", "app.css").read_text()
    faces = re.findall(r"@font-face \{(.*?)\}", css, flags=re.DOTALL)
    urls = [url for face in faces for url in re.findall(r'url\("fonts/([^"]+)"\)', face)]
    assert sorted(urls) == sorted(figtree_file(s) for s in FIGTREE_SHA256)
    assert all("font-display: swap;" in face and 'font-family: "Figtree";' in face for face in faces)
    assert '--font: "Figtree", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;' in css
    assert "font: 17px/1.5 var(--font);" in css


@pytest.fixture
def no_host_mime_types(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Guess types as the slim image does: from Python's own table, with no /etc/mime.types."""
    monkeypatch.setattr(mimetypes, "knownfiles", [])
    mimetypes.init()
    assert mimetypes.guess_type("x.woff2") == (None, None)  # what the image would serve without help
    yield
    monkeypatch.undo()
    mimetypes.init()


def test_fonts_are_served_from_self_as_static_files(
    no_host_mime_types: None, migrated: Settings, clock: FakeClock, provider: FakeProvider
) -> None:
    app = create_app(migrated, clock=clock, oidc_transport=httpx.ASGITransport(app=provider.app))
    with TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}) as client:
        for subset in FIGTREE_SHA256:
            response = client.get(f"/static/fonts/{figtree_file(subset)}")
            assert response.status_code == 200, subset
            assert response.headers["content-type"] == "font/woff2", subset
            assert response.headers["content-security-policy"] == STATIC_CONTENT_SECURITY_POLICY, subset
            assert response.headers.get("cache-control") != "no-store", subset
    # The page loads fonts from its own origin only: default-src covers fonts, and nothing widens it.
    policy = [part.strip() for part in CONTENT_SECURITY_POLICY.split(";")]
    assert "default-src 'self'" in policy
    assert [part for part in policy if part.startswith("font-src")] in ([], ["font-src 'self'"])


def test_build_group_matches_build_system_requires() -> None:
    import tomllib
    from pathlib import Path

    data = tomllib.loads(Path(__file__).parents[1].joinpath("pyproject.toml").read_text())
    assert sorted(data["build-system"]["requires"]) == sorted(data["dependency-groups"]["build"]), (
        "The Containerfile builds with the locked `build` group; keep it equal to [build-system].requires."
    )
