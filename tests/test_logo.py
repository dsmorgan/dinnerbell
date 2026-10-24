# SPDX-License-Identifier: AGPL-3.0-or-later
"""The logo: committed SVGs are inert, the PNG exports have the right shape, and every page
links the icons and shows the logo."""

from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from tests.conftest import sign_in
from tests.support.fake_oidc import FakeProvider

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "dinnerbell" / "web" / "static"
SKIP_DIRS = {".git", ".venv", "node_modules", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
ICON_LINKS = (
    '<link rel="icon" href="/static/favicon-32.png?v=',
    '<link rel="icon" href="/static/logo-mark.svg?v=',
    '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png?v=',
)


def committed_svgs() -> list[Path]:
    return sorted(path for path in ROOT.rglob("*.svg") if not SKIP_DIRS.intersection(path.relative_to(ROOT).parts))


def svg_problems(text: str) -> list[str]:
    """Anything that could run code or fetch something when the SVG is opened on its own."""
    # A DTD allows entity expansion and a stylesheet instruction loads CSS from elsewhere. Stop
    # before parsing a DTD; the files are ours, but the parser shouldn't expand anything.
    problems = [
        f"contains {marker}"
        for marker in ("<!doctype", "<!entity", "<?xml-stylesheet", "@import")
        if marker in text.lower()
    ]
    if problems:
        return problems
    for element in ElementTree.fromstring(text).iter():  # noqa: S314 - DTDs are refused above
        name = element.tag.rpartition("}")[2].lower()
        if name in {"script", "foreignobject", "iframe", "embed", "object"}:
            problems.append(f"<{name}> element")
        for attribute, value in element.attrib.items():
            local = attribute.rpartition("}")[2].lower()
            if local.startswith("on"):
                problems.append(f"{local}= on <{name}>")
            if local == "href" and not value.startswith("#"):
                problems.append(f"external href {value!r}")
            for url in re.findall(r"url\(\s*['\"]?([^)'\"]*)", value):
                if not url.startswith("#"):
                    problems.append(f"external url({url})")
    return problems


def test_there_are_svgs_to_check() -> None:
    names = {path.name for path in committed_svgs()}
    assert {"logo.svg", "logo-mark.svg"} <= names


@pytest.mark.parametrize("path", committed_svgs(), ids=lambda path: str(path.relative_to(ROOT)))
def test_committed_svg_is_inert(path: Path) -> None:
    assert svg_problems(path.read_text()) == []


@pytest.mark.parametrize(
    "svg",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><SCRIPT>alert(1)</SCRIPT></svg>',
        '<s:svg xmlns:s="http://www.w3.org/2000/svg"><s:script>alert(1)</s:script></s:svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="1" ONCLICK="alert(1)"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><p>hi</p></foreignObject></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xlink">'
        '<use x:href="https://example.com/a.svg#b"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"><circle r="1"/></a></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/png;base64,AAAA"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(https://example.com/x#p)"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(https://example.com/x.css);</style></svg>',
        '<?xml-stylesheet href="https://example.com/x.css"?><svg xmlns="http://www.w3.org/2000/svg"/>',
        '<!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><iframe src="https://example.com"/></svg>',
    ],
)
def test_svg_check_catches_active_content(svg: str) -> None:
    assert svg_problems(svg)


def png_rgba(path: Path) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGBA, non-interlaced PNG, which is what resvg writes."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks, offset = {}, 8
    idat = b""
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        if kind == b"IDAT":
            idat += body
        chunks[kind] = body
        offset += 12 + length
    width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (depth, color, interlace) == (8, 6, 0), path.name
    raw = zlib.decompress(idat)
    stride, pixels, previous = width * 4, bytearray(), bytearray(width * 4)
    for row in range(height):
        start = row * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1 : start + 1 + stride])
        for i in range(stride):
            left = line[i - 4] if i >= 4 else 0
            up = previous[i]
            corner = previous[i - 4] if i >= 4 else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif kind == 4:
                guess = left + up - corner
                pa, pb, pc = abs(guess - left), abs(guess - up), abs(guess - corner)
                nearest = left if pa <= pb and pa <= pc else up if pb <= pc else corner
                line[i] = (line[i] + nearest) & 0xFF
        pixels += line
        previous = line
    return width, height, bytes(pixels)


@pytest.mark.parametrize(
    ("path", "size", "opaque"),
    [
        (STATIC / "favicon-32.png", 32, False),
        (STATIC / "apple-touch-icon.png", 180, True),
        (ROOT / "deploy" / "slack-app-icon-1024.png", 1024, True),
    ],
    ids=lambda value: value.name if isinstance(value, Path) else str(value),
)
def test_png_exports(path: Path, size: int, opaque: bool) -> None:
    """iOS fills transparent pixels with black, so the Home Screen and Slack icons are opaque."""
    width, height, pixels = png_rgba(path)
    assert (width, height) == (size, size)
    alpha = pixels[3::4]
    if opaque:
        assert min(alpha) == 255
    else:
        assert alpha[0] == 0, "the favicon's corners should be transparent"


def test_logo_files_are_served_as_images(client: TestClient) -> None:
    for name, kind in [
        ("logo.svg", "image/svg+xml"),
        ("logo-mark.svg", "image/svg+xml"),
        ("favicon-32.png", "image/png"),
        ("apple-touch-icon.png", "image/png"),
    ]:
        response = client.get(f"/static/{name}")
        assert response.status_code == 200, name
        assert response.headers["content-type"] == kind
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'self'" in response.headers["content-security-policy"]


def test_csp_allows_same_origin_images(client: TestClient) -> None:
    directives = [part.strip() for part in client.get("/").headers["content-security-policy"].split(";")]
    assert "img-src 'self' data:" in directives


def assert_icons_and_logo(html: str, where: str) -> None:
    for link in ICON_LINKS:
        assert link in html, f"{link} missing on {where}"
    assert re.search(r'<img class="logo" src="/static/logo-mark\.svg\?v=[^"]+" [^>]*alt="[^"]+"', html), where


def test_every_kind_of_page_has_icons_and_logo(client: TestClient, provider: FakeProvider) -> None:
    assert_icons_and_logo(client.get("/").text, "sign-in page")
    not_found = client.get("/does-not-exist")
    assert not_found.status_code == 404
    assert_icons_and_logo(not_found.text, "404 page")

    async def boom() -> None:
        raise RuntimeError("kaboom")

    client.app.add_api_route("/boom", boom)  # type: ignore[attr-defined]
    server_error = client.get("/boom")
    assert server_error.status_code == 500
    assert_icons_and_logo(server_error.text, "500 page")

    sign_in(client, provider)
    for path in ("/", "/catalog", "/admin/members"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert_icons_and_logo(response.text, path)
