# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch a public HTTPS page as text, for the assistant's menu links (ADR 0009, spec 6.14).

Every hop resolves its host, refuses the fetch unless every address is public, and connects to
an address it checked: the request goes to the IP itself, with the original name in ``Host``
and in TLS (SNI and certificate verification, through httpcore's ``sni_hostname`` extension).
Nothing re-resolves the name between the check and the connection. Connections are never
reused, so each hop's certificate is verified for that hop's own name.

No cookies, no credentials, no proxies or CA bundles from the environment, and a fixed
User-Agent. Logs carry the host, the reason code, and the duration, never the URL's path or the
page.
"""

from __future__ import annotations

import asyncio
import codecs
import functools
import logging
import re
import socket
import time
import weakref
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
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
    "MAX_CONCURRENT_FETCHES",
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
MAX_CONCURRENT_FETCHES = 4
MAX_TITLE_LENGTH = 300
USER_AGENT = "Dinnerbell (+https://github.com/dsmorgan/dinnerbell)"
ALLOWED_TYPES = frozenset({"text/html", "text/plain", "application/xhtml+xml"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html, application/xhtml+xml, text/plain;q=0.9",
    "Accept-Encoding": "identity",
}

_DNS_POOL = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCHES, thread_name_prefix="dinnerbell-dns")
"""Lookups run here, not in the default executor: a lookup the deadline gave up on keeps its
thread until the system resolver returns, and must not starve the rest of the app."""
_LIMITS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()

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
    lookup = functools.partial(socket.getaddrinfo, host, HTTPS_PORT, type=socket.SOCK_STREAM)
    infos = await loop.run_in_executor(_DNS_POOL, lookup)
    return [str(info[4][0]) for info in infos]


def new_transport() -> httpx.AsyncHTTPTransport:
    """HTTP/1.1, TLS verified against the bundled CAs, no retries, and no idle connections kept.

    ``trust_env=False`` keeps ``SSL_CERT_FILE`` and ``SSL_CERT_DIR`` from replacing the CA bundle.
    The transport never reads proxy variables; only an ``httpx`` client would.
    """
    return httpx.AsyncHTTPTransport(limits=httpx.Limits(max_keepalive_connections=0), retries=0, trust_env=False)


def _fetch_slots() -> asyncio.Semaphore:
    """At most ``MAX_CONCURRENT_FETCHES`` fetches at once, per event loop."""
    loop = asyncio.get_running_loop()
    slots = _LIMITS.get(loop)
    if slots is None:
        slots = _LIMITS[loop] = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
    return slots


async def fetch_page(
    url: str,
    *,
    resolver: Resolver = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Monotonic = time.monotonic,
    deadline: float = DEADLINE_SECONDS,
) -> FetchedPage:
    """Fetch ``url`` as text, or raise ``FetchError``.

    ``deadline`` bounds the whole fetch, waiting for a free slot, every hop, and the body
    together, not each read. A body past ``MAX_BYTES`` is cut there and marked truncated.
    """
    started = clock()
    host: str | None = None
    own_transport = transport is None
    http = transport if transport is not None else new_transport()
    try:
        target = parse_target(url)
        host = target.host
        page = await _fetch_page(target, resolver, http, clock, started, deadline)
    except FetchError as exc:
        _log_refusal(exc, host, clock, started)
        raise
    finally:
        if own_transport:
            await http.aclose()
    log.info("page fetched", extra={"host": host, "duration_ms": _elapsed_ms(clock, started)})
    return page


async def _fetch_page(
    target: Target,
    resolver: Resolver,
    http: httpx.AsyncBaseTransport,
    clock: Monotonic,
    started: float,
    deadline: float,
) -> FetchedPage:
    """Wait for a slot and fetch within the deadline, then extract the text while holding the slot."""
    slots = _fetch_slots()
    acquired = False
    try:
        try:
            async with asyncio.timeout(deadline):
                await slots.acquire()
                acquired = True
                final, (mime, charset), body, truncated = await _fetch(
                    target, resolver, http, clock, started + deadline
                )
        except TimeoutError:
            raise FetchError(FetchFailure.TIMEOUT) from None
        try:
            title, text = await asyncio.to_thread(_extract, body, mime, charset)
        except Exception:
            raise FetchError(FetchFailure.ENCODING) from None
    finally:
        if acquired:
            slots.release()
    return FetchedPage(url=final.url, title=title, text=text, truncated=truncated)


def _log_refusal(exc: FetchError, host: str | None, clock: Monotonic, started: float) -> None:
    extra: dict[str, object] = {"host": host, "reason": exc.reason.value, "duration_ms": _elapsed_ms(clock, started)}
    if exc.detail is not None:
        extra["detail"] = exc.detail.value
    log.info("page fetch refused", extra=extra)


async def _fetch(
    target: Target, resolver: Resolver, http: httpx.AsyncBaseTransport, clock: Monotonic, deadline: float
) -> tuple[Target, tuple[str, str | None], bytes, bool]:
    visited = {target.url}
    for hop in range(MAX_REDIRECTS + 1):
        addresses = await _addresses(target, resolver, redirected=hop > 0)
        response = await _connect(target, addresses, http, deadline - clock())
        try:
            if response.status_code in REDIRECT_STATUSES:
                following = _redirect(target, response.headers.get("location", ""))
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


def _redirect(current: Target, location: str) -> Target:
    """The redirect's target. A refusal is the redirect's fault, not the member's link."""
    try:
        return redirect_target(current, location)
    except FetchError as exc:
        raise FetchError(FetchFailure.REDIRECT, detail=exc.detail or exc.reason) from None


async def _addresses(target: Target, resolver: Resolver, *, redirected: bool) -> tuple[IPAddress, ...]:
    if target.address is not None:
        return (target.address,)
    try:
        try:
            answers = await resolver(target.host)
        except (OSError, UnicodeError):
            raise FetchError(FetchFailure.DNS) from None
        return check_resolved(answers)
    except FetchError as exc:
        if redirected:
            raise FetchError(FetchFailure.REDIRECT, detail=exc.reason) from None
        raise


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


# --- Decoding ----------------------------------------------------------------------------------

_WEB_ENCODINGS = frozenset(
    {
        "utf-8", "utf-16-le", "utf-16-be", "cp874", "cp1250", "cp1251", "cp1252", "cp1253", "cp1254",
        "cp1255", "cp1256", "cp1257", "cp1258", "iso8859-2", "iso8859-3", "iso8859-4", "iso8859-5",
        "iso8859-6", "iso8859-7", "iso8859-8", "iso8859-10", "iso8859-13", "iso8859-14", "iso8859-15",
        "iso8859-16", "koi8-r", "koi8-u", "mac-roman", "shift_jis", "euc_jp", "iso2022_jp", "gb18030",
        "gbk", "big5", "euc_kr",
    }
)  # fmt: skip
"""The WHATWG Encoding Standard's encodings, by Python's codec name. Any other label, such as
``punycode`` or ``idna``, is ignored: those are slow, or raise instead of replacing bad bytes."""
_LABEL_ALIASES = {"windows-874": "cp874", "x-mac-roman": "mac-roman", "x-sjis": "shift_jis"}
"""WHATWG labels Python's codec registry doesn't know."""
_CODEC_ALIASES = {
    "iso8859-1": "cp1252",
    "ascii": "cp1252",
    "iso8859-9": "cp1254",
    "iso8859-11": "cp874",
    "gb2312": "gbk",
    "utf-16": "utf-16-le",
}
"""Where WHATWG sends a label to a different encoding than Python would."""
_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_ENCODING_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,40}")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)
_XML_ENCODING = re.compile(rb"""\A<\?xml[^>]+encoding\s*=\s*["']([A-Za-z0-9_.:-]+)["']""")


def web_encoding(label: str | None) -> str | None:
    """The Python codec for a WHATWG encoding label, or None for anything else."""
    if label is None or not _ENCODING_LABEL.fullmatch(label.strip()):
        return None
    label = label.strip().lower()
    try:
        name = codecs.lookup(_LABEL_ALIASES.get(label, label)).name
    except LookupError:
        return None
    name = _CODEC_ALIASES.get(name, name)
    return name if name in _WEB_ENCODINGS else None


def decode(body: bytes, mime: str, charset: str | None) -> str:
    """Decode as a browser would, in this order: a byte-order mark, the header's charset, then
    for HTML a ``<meta>`` charset and for XHTML the XML declaration, in the first 1 KB, then
    UTF-8. A ``<meta>`` or XML declaration that says UTF-16 means UTF-8, since the bytes that
    spell it out are ASCII.

    Bytes that don't decode become U+FFFD, as does a character the 2 MB cap cut in half.
    """
    encoding = None
    for bom, name in _BOMS:
        if body.startswith(bom):
            encoding, body = name, body[len(bom) :]
            break
    encoding = encoding or web_encoding(charset)
    if encoding is None and mime != "text/plain":
        pattern = _XML_ENCODING if mime == "application/xhtml+xml" else _META_CHARSET
        found = pattern.search(body[:1024])
        if found:
            encoding = web_encoding(found.group(1).decode("ascii"))
            if encoding is not None and encoding.startswith("utf-16"):
                encoding = "utf-8"
    try:
        return body.decode(encoding or "utf-8", errors="replace")
    except (UnicodeError, LookupError):  # pragma: no cover - every allowed codec replaces
        return body.decode("utf-8", errors="replace")


def _extract(body: bytes, mime: str, charset: str | None) -> tuple[str | None, str]:
    text = decode(body, mime, charset)
    if mime == "text/plain":
        return None, _collapse(text)
    return extract_html(text)


# --- Text --------------------------------------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")
_SPACES = re.compile(r"[^\S\n]+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r" +([,.;:!?)\]])")


def extract_html(html: str) -> tuple[str | None, str]:
    """The head's title and the page's readable text.

    Script, style, noscript, and template are dropped. Blocks start new lines, other elements
    are separated by a space, and ``<pre>`` keeps its line breaks. A title over 300 characters
    is cut there; one whose ``</title>`` never comes is dropped, and the text after it kept.
    """
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    if parser.unclosed_title:
        parser = _TextExtractor(title_is_text=True)
        parser.feed(html)
        parser.close()
    title = " ".join("".join(parser.title).split())[:MAX_TITLE_LENGTH].strip() or None
    text = _collapse("".join(parser.parts))
    return title, _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)


def _collapse(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


class _TextExtractor(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "template"})
    FOREIGN = frozenset({"svg", "math"})
    BLOCK = frozenset(
        {
            "address", "article", "aside", "blockquote", "body", "br", "caption", "dd", "details", "div", "dl",
            "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
            "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table", "tr", "ul",
        }
    )  # fmt: skip
    FORMATTING = frozenset(
        {"abbr", "b", "bdi", "bdo", "cite", "code", "em", "font", "i", "mark", "q", "s", "small", "strong", "sub",
         "sup", "u", "wbr"}
    )  # fmt: skip
    """Elements inside a word or phrase, which get no separator."""

    def __init__(self, *, title_is_text: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        if title_is_text:
            # The first pass found a <title> that never closes; read what follows it as the page.
            # typeshed marks it Final, but html.parser reads it from self on each start tag.
            self.RCDATA_CONTENT_ELEMENTS = ("textarea",)  # type: ignore[misc]
        self.title_is_text = title_is_text
        self.parts: list[str] = []
        self.title: list[str] = []
        self.unclosed_title = False
        self._skip = 0
        self._foreign = 0
        self._pre = 0
        self._in_title = False
        self._title_done = False
        self._in_body = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self._in_body = True
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.FOREIGN:
            self._foreign += 1
        elif tag == "title":
            if not (self.title_is_text or self._title_done or self._foreign or self._in_body):
                self._in_title = True
                self.unclosed_title = True
            return
        if tag == "pre":
            self._pre += 1
        self._separate(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(self._skip - 1, 0)
        elif tag in self.FOREIGN:
            self._foreign = max(self._foreign - 1, 0)
        elif tag == "title":
            if self._in_title:
                self._in_title = False
                self._title_done = True
                self.unclosed_title = False
            return
        if tag == "pre":
            self._pre = max(self._pre - 1, 0)
        self._separate(tag)

    def _separate(self, tag: str) -> None:
        if tag in self.BLOCK:
            self.parts.append("\n")
        elif tag not in self.FORMATTING:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title.append(data)
        elif self._pre:
            self.parts.append(_SPACES.sub(" ", data))
        else:
            self._in_body = self._in_body or bool(data.strip())
            self.parts.append(_WHITESPACE.sub(" ", data))
