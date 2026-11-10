# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch a public HTTPS page as text, for the assistant's menu links (ADR 0009, spec 6.14).

Every hop resolves its host, refuses the fetch unless every address is public, and connects to
an address it checked: the request goes to the IP itself, with the original name in ``Host``
and in TLS (SNI and certificate verification, through httpcore's ``sni_hostname`` extension).
Nothing re-resolves the name between the check and the connection. Connections are never
reused, so each hop's certificate is verified for that hop's own name.

No cookies, no credentials, no proxies from the environment, and a fixed User-Agent. Logs carry
the host, the reason code, and the duration, never the URL's path or the page.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

from dinnerbell.domain.fetch_policy import (
    HTTPS_PORT,
    FetchError,
    FetchFailure,
    IPAddress,
    Target,
    check_resolved,
    parse_target,
    redirect_target,
)

__all__ = [
    "DEADLINE_SECONDS",
    "MAX_BYTES",
    "MAX_REDIRECTS",
    "USER_AGENT",
    "FetchError",
    "FetchFailure",
    "FetchedPage",
    "fetch_page",
    "new_transport",
    "system_resolver",
]

log = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
DEADLINE_SECONDS = 10.0
MAX_REDIRECTS = 5
USER_AGENT = "Dinnerbell (+https://github.com/dsmorgan/dinnerbell)"
ALLOWED_TYPES = frozenset({"text/html", "text/plain", "application/xhtml+xml"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html, application/xhtml+xml, text/plain;q=0.9",
    "Accept-Encoding": "identity",
}
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)

_WHITESPACE = re.compile(r"\s+")

Resolver = Callable[[str], Awaitable[Sequence[str]]]
"""Resolves a host name to address strings, as ``getaddrinfo`` does."""

Monotonic = Callable[[], float]


@dataclass(frozen=True)
class FetchedPage:
    url: str
    """The URL after redirects."""
    title: str | None
    text: str
    truncated: bool
    """True when the body went past ``MAX_BYTES`` and only its start was read."""


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, HTTPS_PORT, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def new_transport() -> httpx.AsyncHTTPTransport:
    """HTTP/1.1, TLS verified, no retries, and no idle connections kept for reuse."""
    return httpx.AsyncHTTPTransport(limits=httpx.Limits(max_keepalive_connections=0), retries=0)


async def fetch_page(
    url: str,
    *,
    resolver: Resolver = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Monotonic = time.monotonic,
    deadline: float = DEADLINE_SECONDS,
) -> FetchedPage:
    """Fetch ``url`` as text, or raise ``FetchError``.

    ``deadline`` bounds the whole fetch, every hop and the body together, not each read. A body
    past ``MAX_BYTES`` is cut there and marked truncated.
    """
    started = clock()
    host: str | None = None
    own_transport = transport is None
    http = transport if transport is not None else new_transport()
    try:
        target = parse_target(url)
        host = target.host
        try:
            async with asyncio.timeout(deadline):
                final, content_type, body, truncated = await _fetch(target, resolver, http, clock, started + deadline)
        except TimeoutError:
            raise FetchError(FetchFailure.TIMEOUT) from None
    except FetchError as exc:
        log.info(
            "page fetch refused",
            extra={"host": host, "reason": exc.reason.value, "duration_ms": _elapsed_ms(clock, started)},
        )
        raise
    finally:
        if own_transport:
            await http.aclose()
    mime, charset = content_type
    title, text = await asyncio.to_thread(_extract, body, mime, charset)
    log.info("page fetched", extra={"host": host, "duration_ms": _elapsed_ms(clock, started)})
    return FetchedPage(url=final.url, title=title, text=text, truncated=truncated)


async def _fetch(
    target: Target, resolver: Resolver, http: httpx.AsyncBaseTransport, clock: Monotonic, deadline: float
) -> tuple[Target, tuple[str, str | None], bytes, bool]:
    visited = {target.url}
    for hop in range(MAX_REDIRECTS + 1):
        addresses = await _addresses(target, resolver)
        response = await _connect(target, addresses, http, deadline - clock())
        try:
            if response.status_code in REDIRECT_STATUSES:
                following = redirect_target(target, response.headers.get("location", ""))
                if following.url in visited:
                    raise FetchError(FetchFailure.REDIRECT_LOOP)
                if hop == MAX_REDIRECTS:
                    raise FetchError(FetchFailure.TOO_MANY_REDIRECTS)
                visited.add(following.url)
                target = following
                continue
            if response.status_code != 200:
                raise FetchError(FetchFailure.STATUS)
            content_type = _content_type(response.headers.get("content-type"))
            if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                raise FetchError(FetchFailure.ENCODING)
            body, truncated = await _read_capped(response, clock, deadline)
            return target, content_type, body, truncated
        except httpx.TimeoutException:
            raise FetchError(FetchFailure.TIMEOUT) from None
        except httpx.HTTPError:
            raise FetchError(FetchFailure.NETWORK) from None
        finally:
            await response.aclose()
    raise AssertionError("unreachable: the last hop returns or raises")  # pragma: no cover


async def _addresses(target: Target, resolver: Resolver) -> tuple[IPAddress, ...]:
    if target.address is not None:
        return (target.address,)
    try:
        answers = await resolver(target.host)
    except (OSError, UnicodeError):
        raise FetchError(FetchFailure.DNS) from None
    return check_resolved(answers)


async def _connect(
    target: Target, addresses: Sequence[IPAddress], http: httpx.AsyncBaseTransport, remaining: float
) -> httpx.Response:
    """Send the request to each checked address in turn until one connects."""
    for address in addresses:
        try:
            return await http.handle_async_request(_request(target, address, remaining))
        except httpx.ConnectError:
            continue  # refused, unreachable, or a certificate that isn't for this name
        except httpx.TimeoutException:
            raise FetchError(FetchFailure.TIMEOUT) from None
        except httpx.InvalidURL:
            raise FetchError(FetchFailure.INVALID_URL) from None
        except httpx.HTTPError:
            raise FetchError(FetchFailure.NETWORK) from None
    raise FetchError(FetchFailure.NETWORK)


def _request(target: Target, address: IPAddress, remaining: float) -> httpx.Request:
    """A request to ``address`` itself that names ``target.host`` in ``Host`` and in TLS."""
    ip = f"[{address}]" if address.version == 6 else str(address)
    timeout = max(remaining, 0.0)
    return httpx.Request(
        "GET",
        f"https://{ip}{target.path}",
        headers={**_HEADERS, "Host": target.netloc},
        extensions={
            "sni_hostname": target.host,
            "timeout": {"connect": timeout, "read": timeout, "write": timeout, "pool": timeout},
        },
    )


def _content_type(value: str | None) -> tuple[str, str | None]:
    mime, _, params = (value or "").partition(";")
    mime = mime.strip().lower()
    if mime not in ALLOWED_TYPES:
        raise FetchError(FetchFailure.CONTENT_TYPE)
    charset = None
    for param in params.split(";"):
        key, _, val = param.partition("=")
        if key.strip().lower() == "charset":
            charset = val.strip().strip("\"'") or None
    return mime, charset


async def _read_capped(response: httpx.Response, clock: Monotonic, deadline: float) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_raw():
        if clock() > deadline:
            raise FetchError(FetchFailure.TIMEOUT)
        room = MAX_BYTES - size
        if len(chunk) > room:
            chunks.append(chunk[:room])
            return b"".join(chunks), True
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), False


def _elapsed_ms(clock: Monotonic, started: float) -> int:
    return round((clock() - started) * 1000)


def decode(body: bytes, mime: str, charset: str | None) -> str:
    """Decode by the declared charset (the header, or an HTML ``<meta>``), else UTF-8.

    Bytes that don't decode become U+FFFD, as does a character the 2 MB cap cut in half.
    """
    if charset is None and mime != "text/plain":
        found = _META_CHARSET.search(body[:1024])
        charset = found.group(1).decode("ascii") if found else None
    if charset:
        try:
            return body.decode(charset, errors="replace").removeprefix("﻿")
        except LookupError:
            pass  # a charset Python doesn't know, or not a text encoding
    return body.decode("utf-8", errors="replace").removeprefix("﻿")


def _extract(body: bytes, mime: str, charset: str | None) -> tuple[str | None, str]:
    text = decode(body, mime, charset)
    if mime == "text/plain":
        return None, _collapse(text)
    return extract_html(text)


def extract_html(html: str) -> tuple[str | None, str]:
    """The page's title and readable text: no script, style, or noscript, one line per block."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    title = " ".join("".join(parser.title).split()) or None
    return title, _collapse("".join(parser.parts))


def _collapse(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


class _TextExtractor(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript"})
    BLOCK = frozenset(
        {
            "address", "article", "aside", "blockquote", "br", "caption", "dd", "details", "div", "dl", "dt",
            "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
            "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table", "td", "th", "tr", "ul",
        }
    )  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._skip = 0
        self._in_title = False
        self._seen_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title" and not self._seen_title:
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(self._skip - 1, 0)
        elif tag == "title" and self._in_title:
            self._in_title = False
            self._seen_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title.append(data)
        else:
            self.parts.append(_WHITESPACE.sub(" ", data))
