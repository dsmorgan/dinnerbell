# SPDX-License-Identifier: AGPL-3.0-or-later
"""The safe fetcher for menu links (ADR 0009, spec 6.14). No test touches the network."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from ipaddress import ip_address
from typing import Any

import httpcore
import httpx
import pytest

from dinnerbell.domain.fetch_policy import (
    FetchError,
    FetchFailure,
    check_resolved,
    is_public_address,
    parse_target,
    redirect_target,
)
from dinnerbell.services import safe_fetch
from dinnerbell.services.safe_fetch import MAX_BYTES, USER_AGENT, FetchedPage, decode, extract_html, fetch_page

pytestmark = pytest.mark.anyio

PUBLIC_V4 = "93.184.215.14"
PUBLIC_V6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"
HTML = "text/html; charset=utf-8"


# --- Addresses ---------------------------------------------------------------------------------

REFUSED_ADDRESSES = [
    "127.0.0.1",  # loopback
    "127.255.255.254",
    "10.1.2.3",  # private
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",  # link-local (cloud metadata)
    "100.64.0.1",  # CGNAT
    "100.127.255.255",
    "224.0.0.1",  # multicast, which is_global alone allows
    "239.255.255.250",
    "240.0.0.1",  # reserved
    "255.255.255.255",
    "0.0.0.0",  # unspecified  # noqa: S104
    "192.0.2.1",  # documentation
    "198.18.0.1",  # benchmarking
    "::1",  # IPv6 loopback
    "::",  # IPv6 unspecified
    "fe80::1",  # link-local
    "fe80::1%eth0",  # scoped
    "fc00::1",  # unique local
    "fd12:3456::1",
    "fec0::1",  # site-local, deprecated
    "ff02::1",  # multicast
    "ff0e::1",  # global-scope multicast, which is_global alone allows
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "::ffff:10.0.0.1",  # IPv4-mapped private
    "::ffff:100.64.0.1",  # IPv4-mapped CGNAT
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1, which is_global alone allows
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "64:ff9b:1::1",  # local-use NAT64
    "2002:7f00:1::1",  # 6to4
    "2002:5db8:d70e::1",  # 6to4 of a public address: still a relay Dinnerbell can't check
    "2001::1",  # Teredo
    "2001:db8::1",  # documentation
    "::7f00:1",  # IPv4-compatible, deprecated
]


@pytest.mark.parametrize("address", REFUSED_ADDRESSES)
def test_refuses_every_non_public_address(address: str) -> None:
    assert not is_public_address(ip_address(address))
    with pytest.raises(FetchError) as caught:
        check_resolved([address])
    assert caught.value.reason is FetchFailure.ADDRESS


@pytest.mark.parametrize("address", [PUBLIC_V4, PUBLIC_V6, f"::ffff:{PUBLIC_V4}", "64:ff9b::5db8:d70e"])
def test_allows_public_addresses_and_their_mapped_forms(address: str) -> None:
    assert is_public_address(ip_address(address))


def test_one_private_answer_refuses_the_whole_name() -> None:
    with pytest.raises(FetchError) as caught:
        check_resolved([PUBLIC_V4, PUBLIC_V6, "10.0.0.7"])
    assert caught.value.reason is FetchFailure.ADDRESS
    with pytest.raises(FetchError) as caught:
        check_resolved([PUBLIC_V6, "fd00::7"])
    assert caught.value.reason is FetchFailure.ADDRESS


def test_resolved_answers_keep_order_without_duplicates() -> None:
    assert check_resolved([PUBLIC_V6, PUBLIC_V4, PUBLIC_V6]) == (ip_address(PUBLIC_V6), ip_address(PUBLIC_V4))


@pytest.mark.parametrize(("answers", "reason"), [([], FetchFailure.DNS), (["not-an-ip"], FetchFailure.ADDRESS)])
def test_empty_or_garbled_answers_are_refused(answers: list[str], reason: FetchFailure) -> None:
    with pytest.raises(FetchError) as caught:
        check_resolved(answers)
    assert caught.value.reason is reason


# --- URLs --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://example.com/menu", FetchFailure.SCHEME),
        ("ftp://example.com/menu", FetchFailure.SCHEME),
        ("example.com/menu", FetchFailure.SCHEME),
        ("javascript:alert(1)", FetchFailure.SCHEME),
        ("https://user@example.com/", FetchFailure.USERINFO),
        ("https://user:secret@example.com/", FetchFailure.USERINFO),
        ("https://@example.com/", FetchFailure.USERINFO),
        ("https://example.com@10.0.0.1/", FetchFailure.USERINFO),
        ("https://example.com\\@10.0.0.1/", FetchFailure.USERINFO),
        ("https://example.com:8443/", FetchFailure.PORT),
        ("https://example.com:80/", FetchFailure.PORT),
        ("https://example.com:0/", FetchFailure.PORT),
        ("https://example.com:abc/", FetchFailure.INVALID_URL),
        ("https://example.com:99999/", FetchFailure.INVALID_URL),
        ("https://127.0.0.1/", FetchFailure.ADDRESS),
        ("https://169.254.169.254/latest/meta-data/", FetchFailure.ADDRESS),
        ("https://[::1]/", FetchFailure.ADDRESS),
        ("https://[::ffff:127.0.0.1]/", FetchFailure.ADDRESS),
        ("https://[64:ff9b::a00:1]/", FetchFailure.ADDRESS),
        ("https://[fe80::1%25eth0]/", FetchFailure.ADDRESS),
        ("https://localhost/", FetchFailure.HOST),
        ("https://admin.localhost/", FetchFailure.HOST),
        ("https://printer.local/", FetchFailure.HOST),
        ("https://nas.home.arpa/", FetchFailure.HOST),
        ("https://intranet/", FetchFailure.HOST),
        ("https://127.1/", FetchFailure.HOST),
        ("https://2130706433/", FetchFailure.HOST),
        ("https://0x7f.0.0.1/", FetchFailure.HOST),
        ("https://example.com\\.evil.example.org/", FetchFailure.HOST),
        ("https://exa_mple-.com/", FetchFailure.HOST),
        ("https://" + "a" * 64 + ".example.com/", FetchFailure.HOST),
        ("https:///menu", FetchFailure.INVALID_URL),
        ("https://exa mple.com/", FetchFailure.INVALID_URL),
        ("https://example.com/\x00", FetchFailure.INVALID_URL),
        ("", FetchFailure.INVALID_URL),
        ("https://example.com/" + "a" * 2048, FetchFailure.INVALID_URL),
    ],
)
def test_refuses_urls(url: str, reason: FetchFailure) -> None:
    with pytest.raises(FetchError) as caught:
        parse_target(url)
    assert caught.value.reason is reason
    assert caught.value.message
    assert "10.0.0.1" not in caught.value.message


def test_accepts_https_names_and_public_literals() -> None:
    target = parse_target("  HTTPS://Menu.Example.COM.:443/dinner%20menu?day=fri#top  ")
    assert (target.host, target.path, target.address) == ("menu.example.com", "/dinner%20menu?day=fri", None)
    assert target.url == "https://menu.example.com/dinner%20menu?day=fri"
    assert parse_target("https://example.com").path == "/"
    assert parse_target("https://café.example.com/").host == "xn--caf-dma.example.com"
    literal = parse_target(f"https://[{PUBLIC_V6}]/menu")
    assert literal.address == ip_address(PUBLIC_V6)
    assert literal.url == f"https://[{PUBLIC_V6}]/menu"
    assert parse_target(f"https://{PUBLIC_V4}/").netloc == PUBLIC_V4


def test_refuses_a_name_idna_cannot_encode() -> None:
    with pytest.raises(FetchError) as caught:
        parse_target("https://" + "é" * 70 + ".example.com/")
    assert caught.value.reason is FetchFailure.HOST


def test_redirects_resolve_against_the_current_page() -> None:
    current = parse_target("https://example.com/a/b")
    assert redirect_target(current, "c?x=1").url == "https://example.com/a/c?x=1"
    assert redirect_target(current, "//www.example.org/menu").url == "https://www.example.org/menu"
    for location, reason in [
        ("", FetchFailure.REDIRECT),
        ("http://example.com/", FetchFailure.SCHEME),
        ("https://10.0.0.1/", FetchFailure.ADDRESS),
    ]:
        with pytest.raises(FetchError) as caught:
            redirect_target(current, location)
        assert caught.value.reason is reason


# --- Fetching ----------------------------------------------------------------------------------

Handler = Callable[[httpx.Request], httpx.Response]


class Resolver:
    """Answers from a table and counts lookups."""

    def __init__(self, table: dict[str, Sequence[str]]) -> None:
        self.table = table
        self.lookups: list[str] = []

    async def __call__(self, host: str) -> Sequence[str]:
        self.lookups.append(host)
        if host not in self.table:
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return self.table[host]


class Recorder:
    """A MockTransport handler that records each request and answers from a function."""

    def __init__(self, answer: Handler) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


class Body(httpx.AsyncByteStream):
    """A body that streams, as a real transport's does; ``content=`` arrives already read."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data


def page(body: str | bytes = "<title>Menu</title><p>Tacos</p>", content_type: str = HTML) -> httpx.Response:
    data = body.encode() if isinstance(body, str) else body
    return httpx.Response(200, headers={"content-type": content_type}, stream=Body(data))


def redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


class Outcome:
    """What a fetch returned or raised, with the requests and lookups it made."""

    def __init__(self, recorder: Recorder, resolver: Resolver) -> None:
        self.page: FetchedPage | None = None
        self.error: FetchError | None = None
        self.requests = recorder.requests
        self.lookups = resolver.lookups

    @property
    def reason(self) -> FetchFailure:
        assert self.error is not None, f"expected a refusal, got {self.page}"
        return self.error.reason


async def fetch(url: str, answer: Handler, table: dict[str, Sequence[str]], **kwargs: Any) -> Outcome:
    recorder = Recorder(answer)
    resolver = Resolver(table)
    outcome = Outcome(recorder, resolver)
    try:
        outcome.page = await fetch_page(url, resolver=resolver, transport=recorder.transport, **kwargs)
    except FetchError as exc:
        outcome.error = exc
    return outcome


async def test_connects_to_the_checked_address_with_the_name_in_host_and_tls() -> None:
    outcome = await fetch(
        "https://menu.example.com/dinner?day=fri", lambda _: page(), {"menu.example.com": [PUBLIC_V4]}
    )
    assert outcome.page is not None
    assert outcome.page.url == "https://menu.example.com/dinner?day=fri"
    (request,) = outcome.requests
    assert request.url.host == PUBLIC_V4
    assert request.url.port is None  # 443
    assert request.url.raw_path == b"/dinner?day=fri"
    assert request.headers["host"] == "menu.example.com"
    assert request.extensions["sni_hostname"] == "menu.example.com"
    assert request.headers["user-agent"] == USER_AGENT
    assert "@" not in USER_AGENT
    assert request.headers["accept-encoding"] == "identity"
    assert "cookie" not in request.headers
    assert "authorization" not in request.headers
    assert set(request.extensions["timeout"]) == {"connect", "read", "write", "pool"}


async def test_ipv6_answers_and_literals_connect_to_the_address() -> None:
    outcome = await fetch("https://menu.example.com/", lambda _: page(), {"menu.example.com": [PUBLIC_V6]})
    assert outcome.requests[0].url.host == PUBLIC_V6
    assert outcome.requests[0].headers["host"] == "menu.example.com"
    literal = await fetch(f"https://[{PUBLIC_V6}]/", lambda _: page(), {})
    assert literal.lookups == []
    assert literal.requests[0].headers["host"] == f"[{PUBLIC_V6}]"
    assert literal.requests[0].extensions["sni_hostname"] == PUBLIC_V6


@pytest.mark.parametrize("answers", [[PUBLIC_V4, "10.0.0.7"], [PUBLIC_V6, "::1"], ["::ffff:192.168.0.1"]])
async def test_dns_with_any_private_answer_is_refused_before_connecting(answers: list[str]) -> None:
    outcome = await fetch("https://menu.example.com/", lambda _: page(), {"menu.example.com": answers})
    assert outcome.reason is FetchFailure.ADDRESS
    assert outcome.requests == []


async def test_a_name_that_does_not_resolve_is_refused() -> None:
    outcome = await fetch("https://menu.example.com/", lambda _: page(), {})
    assert outcome.reason is FetchFailure.DNS


async def test_scheme_and_userinfo_are_refused_before_any_lookup() -> None:
    for url, reason in [
        ("http://menu.example.com/", FetchFailure.SCHEME),
        ("https://chef:secret@menu.example.com/", FetchFailure.USERINFO),
    ]:
        outcome = await fetch(url, lambda _: page(), {"menu.example.com": [PUBLIC_V4]})
        assert outcome.reason is reason
        assert outcome.lookups == []
        assert outcome.requests == []


async def test_redirect_to_a_private_name_is_refused_without_connecting() -> None:
    table = {"menu.example.com": [PUBLIC_V4], "internal.example.org": ["10.0.0.5"]}
    outcome = await fetch("https://menu.example.com/", lambda _: redirect("https://internal.example.org/admin"), table)
    assert outcome.reason is FetchFailure.ADDRESS
    assert outcome.lookups == ["menu.example.com", "internal.example.org"]
    assert len(outcome.requests) == 1


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("https://169.254.169.254/latest/meta-data/", FetchFailure.ADDRESS),
        ("https://[::1]/", FetchFailure.ADDRESS),
        ("http://menu.example.com/", FetchFailure.SCHEME),
        ("https://menu.example.com:8443/", FetchFailure.PORT),
        ("https://localhost/", FetchFailure.HOST),
    ],
)
async def test_every_redirect_is_checked_like_the_first_url(location: str, reason: FetchFailure) -> None:
    outcome = await fetch("https://menu.example.com/", lambda _: redirect(location), {"menu.example.com": [PUBLIC_V4]})
    assert outcome.reason is reason
    assert len(outcome.requests) == 1


async def test_redirects_are_followed_and_each_host_is_resolved_and_pinned() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "menu.example.com":
            return redirect("https://www.example.org/menu", 301)
        if request.url.path == "/menu":
            return redirect("/menu/today", 307)
        return page()

    table = {"menu.example.com": [PUBLIC_V4], "www.example.org": [PUBLIC_V6]}
    outcome = await fetch("https://menu.example.com/", answer, table)
    assert outcome.page is not None
    assert outcome.page.url == "https://www.example.org/menu/today"
    assert outcome.lookups == ["menu.example.com", "www.example.org", "www.example.org"]
    assert [(r.url.host, r.headers["host"]) for r in outcome.requests] == [
        (PUBLIC_V4, "menu.example.com"),
        (PUBLIC_V6, "www.example.org"),
        (PUBLIC_V6, "www.example.org"),
    ]


async def test_redirect_loop_is_refused() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return redirect("/b" if request.url.path == "/a" else "/a")

    outcome = await fetch("https://menu.example.com/a", answer, {"menu.example.com": [PUBLIC_V4]})
    assert outcome.reason is FetchFailure.REDIRECT_LOOP
    assert len(outcome.requests) == 2


async def test_five_redirects_are_followed_and_a_sixth_is_refused() -> None:
    def chain(length: int) -> Handler:
        def answer(request: httpx.Request) -> httpx.Response:
            hop = int(request.url.path.strip("/") or 0)
            return redirect(f"/{hop + 1}") if hop < length else page()

        return answer

    table = {"menu.example.com": [PUBLIC_V4]}
    followed = await fetch("https://menu.example.com/0", chain(5), table)
    assert followed.page is not None
    assert len(followed.requests) == 6
    refused = await fetch("https://menu.example.com/0", chain(6), table)
    assert refused.reason is FetchFailure.TOO_MANY_REDIRECTS
    assert len(refused.requests) == 6


async def test_redirect_without_a_location_is_refused() -> None:
    outcome = await fetch("https://menu.example.com/", lambda _: httpx.Response(302), {"menu.example.com": [PUBLIC_V4]})
    assert outcome.reason is FetchFailure.REDIRECT


@pytest.mark.parametrize("status", [204, 304, 404, 500])
async def test_anything_but_ok_is_refused(status: int) -> None:
    outcome = await fetch(
        "https://menu.example.com/", lambda _: httpx.Response(status), {"menu.example.com": [PUBLIC_V4]}
    )
    assert outcome.reason is FetchFailure.STATUS


@pytest.mark.parametrize("content_type", ["application/json", "image/png", "application/pdf", "text/htmlx", ""])
async def test_content_types_other_than_text_are_refused(content_type: str) -> None:
    response = httpx.Response(200, headers={"content-type": content_type} if content_type else {}, content=b"{}")
    outcome = await fetch("https://menu.example.com/", lambda _: response, {"menu.example.com": [PUBLIC_V4]})
    assert outcome.reason is FetchFailure.CONTENT_TYPE


@pytest.mark.parametrize("content_type", ["text/html", "TEXT/HTML; charset=UTF-8", "application/xhtml+xml"])
async def test_html_and_xhtml_are_read(content_type: str) -> None:
    outcome = await fetch(
        "https://menu.example.com/", lambda _: page(content_type=content_type), {"menu.example.com": [PUBLIC_V4]}
    )
    assert outcome.page is not None
    assert outcome.page.title == "Menu"


async def test_a_compressed_body_is_refused() -> None:
    response = httpx.Response(200, headers={"content-type": HTML, "content-encoding": "gzip"}, content=b"\x1f\x8b")
    outcome = await fetch("https://menu.example.com/", lambda _: response, {"menu.example.com": [PUBLIC_V4]})
    assert outcome.reason is FetchFailure.ENCODING


class Endless(httpx.AsyncByteStream):
    """A body that never ends, counting what it yields."""

    def __init__(self, chunk: bytes = b"a" * 65536) -> None:
        self.chunk = chunk
        self.yielded = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.yielded += 1
            yield self.chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_body_stops_at_two_megabytes_while_the_server_keeps_sending() -> None:
    body = Endless()
    response = httpx.Response(200, headers={"content-type": "text/plain"}, stream=body)
    outcome = await fetch("https://menu.example.com/", lambda _: response, {"menu.example.com": [PUBLIC_V4]})
    assert outcome.page is not None
    assert outcome.page.truncated
    assert len(outcome.page.text) == MAX_BYTES
    assert body.yielded == MAX_BYTES // len(body.chunk) + 1
    assert body.closed


async def test_a_body_of_exactly_two_megabytes_is_not_truncated() -> None:
    response = page(b"a" * MAX_BYTES, "text/plain")
    outcome = await fetch("https://menu.example.com/", lambda _: response, {"menu.example.com": [PUBLIC_V4]})
    assert outcome.page is not None
    assert not outcome.page.truncated


class SteppingClock:
    """A monotonic clock that moves one second each time it's read."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


async def test_the_deadline_covers_the_whole_body_not_each_read() -> None:
    body = Endless(b"a" * 10)  # every read is quick; only the total runs long
    response = httpx.Response(200, headers={"content-type": "text/plain"}, stream=body)
    outcome = await fetch(
        "https://menu.example.com/", lambda _: response, {"menu.example.com": [PUBLIC_V4]}, clock=SteppingClock()
    )
    assert outcome.reason is FetchFailure.TIMEOUT
    assert body.yielded <= 10  # one read per second of the 10-second deadline
    assert body.closed


async def test_the_deadline_stops_a_fetch_that_stalls() -> None:
    stalled = asyncio.Event()

    async def resolver(host: str) -> Sequence[str]:
        await stalled.wait()  # never set
        return [PUBLIC_V4]

    with pytest.raises(FetchError) as caught:
        await fetch_page("https://menu.example.com/", resolver=resolver, transport=Recorder(page).transport, deadline=0)
    assert caught.value.reason is FetchFailure.TIMEOUT


def raising(exc: Exception) -> Handler:
    def answer(request: httpx.Request) -> httpx.Response:
        raise exc

    return answer


async def test_a_refused_connection_moves_to_the_next_checked_address() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == PUBLIC_V6:
            raise httpx.ConnectError("unreachable", request=request)
        return page()

    outcome = await fetch("https://menu.example.com/", answer, {"menu.example.com": [PUBLIC_V6, PUBLIC_V4]})
    assert outcome.page is not None
    assert [r.url.host for r in outcome.requests] == [PUBLIC_V6, PUBLIC_V4]


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (httpx.ConnectError("refused"), FetchFailure.NETWORK),
        (httpx.ConnectTimeout("slow"), FetchFailure.TIMEOUT),
        (httpx.RemoteProtocolError("garbled"), FetchFailure.NETWORK),
        (httpx.InvalidURL("bad"), FetchFailure.INVALID_URL),
    ],
)
async def test_connection_failures_have_reasons(exc: Exception, reason: FetchFailure) -> None:
    outcome = await fetch("https://menu.example.com/", raising(exc), {"menu.example.com": [PUBLIC_V4, PUBLIC_V6]})
    assert outcome.reason is reason


class Failing(httpx.AsyncByteStream):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"<p>Ta"
        raise self.exc


@pytest.mark.parametrize(
    ("exc", "reason"),
    [(httpx.ReadTimeout("slow"), FetchFailure.TIMEOUT), (httpx.ReadError("reset"), FetchFailure.NETWORK)],
)
async def test_failures_while_reading_have_reasons(exc: Exception, reason: FetchFailure) -> None:
    response = httpx.Response(200, headers={"content-type": HTML}, stream=Failing(exc))
    outcome = await fetch("https://menu.example.com/", lambda _: response, {"menu.example.com": [PUBLIC_V4]})
    assert outcome.reason is reason


async def test_logs_name_the_host_reason_and_duration_but_never_the_page(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dinnerbell.services.safe_fetch")
    secret = "<p>Secret family recipe</p>"
    table = {"menu.example.com": [PUBLIC_V4], "internal.example.org": ["10.0.0.5"]}
    await fetch("https://menu.example.com/private-token?key=abc", lambda _: page(secret), table, clock=lambda: 7.0)
    await fetch(
        "https://menu.example.com/r", lambda _: redirect("https://internal.example.org/"), table, clock=lambda: 7.0
    )
    await fetch("not a url", lambda _: page(), table)
    fetched, refused, invalid = caplog.records
    assert (fetched.message, fetched.host, fetched.duration_ms) == ("page fetched", "menu.example.com", 0)  # type: ignore[attr-defined]
    assert (refused.message, refused.host, refused.reason) == ("page fetch refused", "menu.example.com", "address")  # type: ignore[attr-defined]
    assert invalid.host is None  # type: ignore[attr-defined]
    for record in caplog.records:
        logged = f"{record.getMessage()} {vars(record)}"
        assert "Secret" not in logged
        assert "private-token" not in logged
        assert "10.0.0.5" not in logged
        assert "internal.example.org" not in logged


async def test_without_a_transport_the_fetcher_makes_and_closes_its_own() -> None:
    with pytest.raises(FetchError) as caught:
        await fetch_page("https://menu.example.com/", resolver=Resolver({"menu.example.com": ["10.0.0.1"]}))
    assert caught.value.reason is FetchFailure.ADDRESS


async def test_system_resolver_returns_every_address(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        assert (host, port) == ("menu.example.com", 443)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_V4, 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (PUBLIC_V6, 443, 0, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    assert await safe_fetch.system_resolver("menu.example.com") == [PUBLIC_V4, PUBLIC_V6]


# --- The real transport: the TCP connection goes to the checked IP, TLS to the name -------------


class FakeStream(httpcore.AsyncNetworkStream):
    def __init__(self, backend: FakeNetwork, response: bytes) -> None:
        self.backend = backend
        self.response = response
        self.sent = b""
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        data, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self.sent += buffer

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        self.backend.tls.append((server_hostname, ssl_context.check_hostname, ssl_context.verify_mode))
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class FakeNetwork(httpcore.AsyncNetworkBackend):
    """Records every TCP connection and TLS handshake, and answers each with the next response."""

    def __init__(self, responses: Iterable[bytes]) -> None:
        self.responses = iter(responses)
        self.connects: list[tuple[str, int]] = []
        self.tls: list[tuple[str | None, bool, ssl.VerifyMode]] = []
        self.streams: list[FakeStream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        **kwargs: Any,
    ) -> httpcore.AsyncNetworkStream:
        self.connects.append((host, port))
        stream = FakeStream(self, next(self.responses))
        self.streams.append(stream)
        return stream

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - retries are off
        raise AssertionError("the fetcher never retries")


def http_response(status: str, headers: dict[str, str], body: bytes = b"") -> bytes:
    lines = [f"HTTP/1.1 {status}", f"Content-Length: {len(body)}", *(f"{k}: {v}" for k, v in headers.items())]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


async def test_the_real_transport_pins_each_hop_and_never_reuses_a_connection() -> None:
    network = FakeNetwork(
        [
            http_response("301 Moved Permanently", {"Location": "https://www.example.org/menu"}),
            http_response("200 OK", {"Content-Type": HTML}, b"<title>Menu</title><p>Tacos</p>"),
        ]
    )
    transport = safe_fetch.new_transport()
    transport._pool._network_backend = network  # type: ignore[attr-defined]
    table = {"menu.example.com": [PUBLIC_V4], "www.example.org": [PUBLIC_V4]}  # one IP, two names
    try:
        result = await fetch_page("https://menu.example.com/", resolver=Resolver(table), transport=transport)
    finally:
        await transport.aclose()
    assert result.text == "Tacos"
    assert network.connects == [(PUBLIC_V4, 443), (PUBLIC_V4, 443)]
    assert network.tls == [
        ("menu.example.com", True, ssl.CERT_REQUIRED),
        ("www.example.org", True, ssl.CERT_REQUIRED),
    ]
    first, second = network.streams
    assert b"\r\nHost: menu.example.com\r\n" in first.sent
    assert b"\r\nHost: www.example.org\r\n" in second.sent
    assert first.closed
    assert second.closed


# --- Decoding and text -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "mime", "charset", "expected"),
    [
        ("Café".encode("latin-1"), "text/plain", "iso-8859-1", "Café"),
        ("Crème brûlée".encode("cp1252"), "text/html", "windows-1252", "Crème brûlée"),
        ("Jalapeño".encode("shift_jis", errors="ignore"), "text/plain", "shift_jis", "Jalapeo"),
        (
            '<meta charset="windows-1252"><p>Café'.encode("cp1252"),
            "text/html",
            None,
            '<meta charset="windows-1252"><p>Café',
        ),
        (
            b'<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">\xe9',
            "text/html",
            None,
            '<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">é',
        ),
        ("Café".encode(), "text/plain", None, "Café"),
        (b"\xef\xbb\xbfMenu", "text/plain", "utf-8", "Menu"),
        (b"Caf\xe9", "text/plain", None, "Caf�"),
        (b"Caf\xe9", "text/plain", "no-such-charset", "Caf�"),
        (b"Caf\xe9", "text/plain", "rot13", "Caf�"),  # a codec, but not a text encoding
        (b'<meta charset="hex">Caf\xe9', "text/html", None, '<meta charset="hex">Caf�'),
    ],
)
def test_decodes_by_the_declared_charset_or_utf8(body: bytes, mime: str, charset: str | None, expected: str) -> None:
    assert decode(body, mime, charset) == expected


async def test_charset_from_the_header_reaches_the_text() -> None:
    response = page("<p>Crème brûlée</p>".encode("cp1252"), 'text/html; charset="windows-1252"')
    outcome = await fetch("https://menu.example.com/", lambda _: response, {"menu.example.com": [PUBLIC_V4]})
    assert outcome.page is not None
    assert outcome.page.text == "Crème brûlée"


def test_extracts_readable_text_and_the_title() -> None:
    html = """<!doctype html>
    <html><head><title>  Taco   Tuesday
      Menu </title><style>p { color: red }</style><script>var menu = "fake";</script></head>
    <body><noscript>Enable JavaScript <b>now</b></noscript>
      <h1>Tacos</h1>
      <ul><li>Al pastor &amp; pineapple — $4</li><li>Carnitas&nbsp;&nbsp; $4</li></ul>
      <p>Hello <b>world</b>,   and   <a href="/x">more</a>.<br>Next line</p>
      <script type="application/ld+json">{"name": "hidden"}</script>
      <svg><title>An icon</title></svg>
      <div>Open <span>daily</span></div><script/>After
    </body></html>"""
    title, text = extract_html(html)
    assert title == "Taco Tuesday Menu"
    assert text.splitlines() == [
        "Tacos",
        "Al pastor & pineapple — $4",
        "Carnitas $4",
        "Hello world, and more.",
        "Next line",
        "An icon",
        "Open daily",
        "After",
    ]
    assert "fake" not in text
    assert "color" not in text
    assert "hidden" not in text
    assert "JavaScript" not in text


def test_a_page_without_a_title_has_none() -> None:
    assert extract_html("<p>Just text</p></script>") == (None, "Just text")


async def test_plain_text_keeps_its_lines_and_has_no_title() -> None:
    response = page("Tacos  $4\r\n\r\n\r\n  Burritos\t$8\n", "text/plain")
    outcome = await fetch("https://menu.example.com/", lambda _: response, {"menu.example.com": [PUBLIC_V4]})
    assert outcome.page is not None
    assert (outcome.page.title, outcome.page.text, outcome.page.truncated) == (None, "Tacos $4\nBurritos $8", False)
