# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request bodies are capped before anything reads them, signed in or not."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dinnerbell.app_factory import create_app
from dinnerbell.config import Settings
from dinnerbell.web.app import DEFAULT_BODY_LIMIT, IMPORT_BODY_LIMIT
from tests.conftest import BASE_URL, csrf_from, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider

MB = 1024 * 1024


@dataclass
class Result:
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    consumed: int = 0


def drive(app: FastAPI, path: str, chunks: Iterable[bytes], *, content_length: int | None) -> Result:
    """Send a POST through the whole ASGI stack and count how many body bytes the app pulled."""
    result = Result()
    parts = list(chunks)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            pending = iter(enumerate(parts))

            async def receive() -> dict[str, object]:
                try:
                    index, body = next(pending)
                except StopIteration:
                    return {"type": "http.disconnect"}
                result.consumed += len(body)
                return {"type": "http.request", "body": body, "more_body": index < len(parts) - 1}

            async def send(message: dict[str, object]) -> None:
                if message["type"] == "http.response.start":
                    result.status = int(message["status"])  # type: ignore[call-overload]
                    raw = message.get("headers") or []
                    result.headers = {k.decode().lower(): v.decode() for k, v in raw}  # type: ignore[union-attr]

            headers = [
                (b"host", b"dinnerbell.test"),
                (b"origin", BASE_URL.encode()),
                (b"content-type", b"application/x-www-form-urlencoded"),
            ]
            if content_length is not None:
                headers.append((b"content-length", str(content_length).encode()))
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": headers,
                "client": ("203.0.113.9", 1234),
                "server": ("dinnerbell.test", 443),
            }
            await app(scope, receive, send)  # type: ignore[arg-type]

    asyncio.run(run())
    return result


def app_for(migrated: Settings, clock: FakeClock, provider: FakeProvider) -> FastAPI:
    return create_app(migrated, clock=clock, oidc_transport=httpx.ASGITransport(app=provider.app))


def test_limits() -> None:
    assert DEFAULT_BODY_LIMIT == 64 * 1024
    assert 2 * 1_000_000 < IMPORT_BODY_LIMIT <= 2_300_000


def test_oversize_content_length_is_refused_unread(
    migrated: Settings, clock: FakeClock, provider: FakeProvider
) -> None:
    app = app_for(migrated, clock, provider)
    total = 50 * MB
    result = drive(app, "/catalog/import/preview", [b"A" * MB] * 50, content_length=total)
    assert result.status == 413
    assert result.consumed == 0
    assert "frame-ancestors 'none'" in result.headers["content-security-policy"]
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.headers["cache-control"] == "no-store"


def test_oversize_chunked_body_is_cut_off(migrated: Settings, clock: FakeClock, provider: FakeProvider) -> None:
    app = app_for(migrated, clock, provider)
    anonymous = drive(app, "/catalog/import/preview", [b"A" * MB] * 50, content_length=None)
    assert anonymous.status == 303  # the import form is read only for a signed-in member
    assert anonymous.consumed == 0
    small = drive(app, "/admin/members", [b"a=" + b"x" * 10_000] * 10, content_length=None)
    assert small.status == 413
    assert small.consumed <= DEFAULT_BODY_LIMIT + 10_002


def test_bodies_at_the_limit_pass(migrated: Settings, clock: FakeClock, provider: FakeProvider) -> None:
    app = app_for(migrated, clock, provider)
    body = b"a=" + b"x" * (DEFAULT_BODY_LIMIT - 2)
    at_limit = drive(app, "/admin/members", [body], content_length=len(body))
    assert at_limit.status == 303  # not signed in: the normal redirect, not 413
    over = drive(app, "/admin/members", [body + b"x"], content_length=len(body) + 1)
    assert over.status == 413
    chunked = drive(app, "/admin/members", [body[:30_000], body[30_000:]], content_length=None)
    assert chunked.status == 303


def test_import_limit_applies_only_to_import_paths(
    migrated: Settings, clock: FakeClock, provider: FakeProvider
) -> None:
    app = app_for(migrated, clock, provider)
    body = b"a=" + b"x" * (200 * 1024)
    for path in ("/catalog/items", "/preferences/stance", "/history/meals", "/auth/logout", "/catalog/tags"):
        assert drive(app, path, [body], content_length=len(body)).status == 413, path
    for path in ("/catalog/import/preview", "/catalog/import/apply"):
        assert drive(app, path, [body], content_length=len(body)).status == 303, path


def test_bad_content_length_is_refused(migrated: Settings, clock: FakeClock, provider: FakeProvider) -> None:
    app = app_for(migrated, clock, provider)
    assert drive(app, "/admin/members", [b"a=1"], content_length=-1).status == 400
    from dinnerbell.web.middleware import BodyLimitMiddleware

    assert BodyLimitMiddleware.declared_length([(b"content-length", b"12x")]) == "invalid"
    assert BodyLimitMiddleware.declared_length([(b"content-length", b"\xb2")]) == "invalid"
    assert BodyLimitMiddleware.declared_length([]) is None
    assert BodyLimitMiddleware.declared_length([(b"content-length", b"42")]) == 42


def test_signed_in_chunked_import_is_cut_off(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    sent = 0

    def body() -> Iterator[bytes]:
        nonlocal sent
        yield b'--x\r\nContent-Disposition: form-data; name="content"\r\n\r\n'
        for _ in range(50):
            sent += MB
            yield b"#" * MB

    response = client.post(
        "/catalog/import/preview",
        content=body(),
        headers={"Content-Type": "multipart/form-data; boundary=x", "X-CSRF-Token": csrf_from(client)},
    )
    assert response.status_code == 413
    assert "too large" in response.text


def test_pasted_file_that_grows_with_crlf_gets_the_friendly_message(client: TestClient, provider: FakeProvider) -> None:
    """A browser sends textarea line breaks as CRLF, so a 1 MB file can arrive larger."""
    sign_in(client, provider)
    pasted = ("version: 1\n" + "# x\n" * 300_000).replace("\n", "\r\n")  # 1.5 MB on the wire
    response = client.post(
        "/catalog/import/preview",
        files={"content": (None, pasted.encode())},
        headers={"X-CSRF-Token": csrf_from(client)},
    )
    assert response.status_code == 400
    assert "larger than 1 MB" in page(response)


def test_too_large_message_fits_the_route(migrated: Settings, clock: FakeClock, provider: FakeProvider) -> None:
    from dinnerbell.web.middleware import too_large

    assert "Catalog files can be up to 1 MB" in too_large("/catalog/import/preview").body.decode()
    assert "Catalog" not in too_large("/admin/members").body.decode()
