# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which web addresses Dinnerbell may fetch (ADR 0009, spec 6.14 "Menu links").

Only ``https`` on port 443, with no user name or password, to a host whose every address is
public. The services layer resolves names and connects; this module only decides.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from urllib.parse import urljoin, urlsplit

HTTPS_PORT = 443
MAX_URL_LENGTH = 2048
MAX_HOST_LENGTH = 253

IPAddress = IPv4Address | IPv6Address

_CGNAT = ip_network("100.64.0.0/10")
_NAT64 = ip_network("64:ff9b::/96")
"""The well-known NAT64 prefix (RFC 6052): the last 32 bits are the IPv4 address behind it."""

_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
_INTERNAL_SUFFIXES = ("localhost", "local", "localdomain", "internal", "home.arpa", "lan", "invalid")
"""Names that never belong to a public site (RFC 6761, RFC 6762, RFC 8375, and common LAN use)."""


class FetchFailure(StrEnum):
    """Why a fetch was refused or failed. The value is what logs record."""

    INVALID_URL = "invalid_url"
    SCHEME = "scheme"
    USERINFO = "userinfo"
    PORT = "port"
    HOST = "host"
    ADDRESS = "address"
    DNS = "dns"
    REDIRECT = "redirect"
    REDIRECT_LOOP = "redirect_loop"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    STATUS = "status"
    CONTENT_TYPE = "content_type"
    ENCODING = "encoding"
    TIMEOUT = "timeout"
    NETWORK = "network"


_MESSAGES = {
    FetchFailure.INVALID_URL: "That link isn't a valid web address.",
    FetchFailure.SCHEME: "Dinnerbell reads only links that start with https://.",
    FetchFailure.USERINFO: "Dinnerbell doesn't read links with a user name or password in them.",
    FetchFailure.PORT: "Dinnerbell reads only links on the standard HTTPS port.",
    FetchFailure.HOST: "That link doesn't name a public website.",
    FetchFailure.ADDRESS: "That link doesn't lead to a public website.",
    FetchFailure.DNS: "Dinnerbell couldn't find that website.",
    FetchFailure.REDIRECT: "That page redirected somewhere Dinnerbell couldn't follow.",
    FetchFailure.REDIRECT_LOOP: "That page redirects in a loop.",
    FetchFailure.TOO_MANY_REDIRECTS: "That page redirects too many times.",
    FetchFailure.STATUS: "That website didn't return the page.",
    FetchFailure.CONTENT_TYPE: "That link isn't a web page or plain text.",
    FetchFailure.ENCODING: "That website sent the page in a form Dinnerbell can't read.",
    FetchFailure.TIMEOUT: "That website took too long to answer.",
    FetchFailure.NETWORK: "Dinnerbell couldn't connect to that website.",
}


class FetchError(Exception):
    """A refused or failed fetch. ``message`` is safe to show the member; it never names an address."""

    def __init__(self, reason: FetchFailure) -> None:
        self.reason = reason
        self.message = _MESSAGES[reason]
        super().__init__(self.message)


@dataclass(frozen=True)
class Target:
    """A URL that passed every check that doesn't need DNS.

    ``host`` is lowercase ASCII (IDNA) with no trailing dot, or an IP literal without brackets.
    ``path`` is the request target: the path and any query, never empty. ``address`` is set when
    the host is an IP literal, which is public by construction.
    """

    host: str
    path: str
    address: IPAddress | None = None

    @property
    def netloc(self) -> str:
        """The host as a URL and the ``Host`` header write it: IPv6 literals in brackets."""
        return f"[{self.host}]" if isinstance(self.address, IPv6Address) else self.host

    @property
    def url(self) -> str:
        return f"https://{self.netloc}{self.path}"


def is_public_address(address: IPAddress) -> bool:
    """True only for a globally routable unicast address.

    IPv4-mapped and NAT64 addresses are judged by the IPv4 address inside them. Other transition
    forms (6to4, Teredo) and scoped IPv6 addresses are never public, since where they lead depends
    on relays or interfaces Dinnerbell can't check.
    """
    if isinstance(address, IPv6Address):
        if address.scope_id:
            return False
        if address.ipv4_mapped is not None:
            return is_public_address(address.ipv4_mapped)
        if address in _NAT64:
            return is_public_address(IPv4Address(int(address) & 0xFFFFFFFF))
        if address.sixtofour is not None or address.teredo is not None or address.is_site_local:
            return False
    elif address in _CGNAT:
        return False
    return address.is_global and not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def check_address(address: IPAddress) -> IPAddress:
    if not is_public_address(address):
        raise FetchError(FetchFailure.ADDRESS)
    return address


def check_resolved(answers: Iterable[str]) -> tuple[IPAddress, ...]:
    """The addresses a name resolved to, in order, if every one is public.

    One private answer refuses the whole name: a name that mixes public and private addresses
    is how DNS rebinding reaches inside, so Dinnerbell never picks the public one and carries on.
    """
    addresses: list[IPAddress] = []
    for answer in answers:
        try:
            address = ip_address(answer)
        except ValueError:
            raise FetchError(FetchFailure.ADDRESS) from None
        check_address(address)
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise FetchError(FetchFailure.DNS)
    return tuple(addresses)


def parse_target(url: str) -> Target:
    """Check a URL that doesn't need DNS, or raise ``FetchError``.

    The port is 443 only, written or not: menus live on the standard port, and allowing others
    would let a link probe services on a public host that were never meant for browsers.
    """
    url = url.strip()
    if not url or len(url) > MAX_URL_LENGTH or any(_unsafe_character(char) for char in url):
        raise FetchError(FetchFailure.INVALID_URL)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FetchError(FetchFailure.INVALID_URL) from None
    if parts.scheme != "https":
        raise FetchError(FetchFailure.SCHEME)
    if "@" in parts.netloc:
        raise FetchError(FetchFailure.USERINFO)
    if not parts.hostname:
        raise FetchError(FetchFailure.INVALID_URL)
    if port is not None and port != HTTPS_PORT:
        raise FetchError(FetchFailure.PORT)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    try:
        address = ip_address(parts.hostname)
    except ValueError:
        return Target(host=_normalize_hostname(parts.hostname), path=path)
    return Target(host=str(address), path=path, address=check_address(address))


def redirect_target(current: Target, location: str) -> Target:
    """Where a redirect's ``Location`` leads, checked like the first URL."""
    location = location.strip()
    if not location:
        raise FetchError(FetchFailure.REDIRECT)
    return parse_target(urljoin(current.url, location))


def _unsafe_character(char: str) -> bool:
    return char.isspace() or unicodedata.category(char).startswith("C")


def _normalize_hostname(hostname: str) -> str:
    """Lowercase ASCII with no trailing dot, or ``FetchError`` for a name no public site has.

    Non-ASCII names are IDNA-encoded with the standard library's codec. Names with one label, or
    whose last label is a number (``127.1``, ``2130706433``), are refused: resolvers read those
    as IPv4 addresses or search-domain names, never as public sites.
    """
    try:
        name = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise FetchError(FetchFailure.HOST) from None
    name = name.removesuffix(".")
    labels = name.split(".")
    if (
        len(name) > MAX_HOST_LENGTH
        or len(labels) < 2
        or not all(_LABEL.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
        or any(name == suffix or name.endswith(f".{suffix}") for suffix in _INTERNAL_SUFFIXES)
    ):
        raise FetchError(FetchFailure.HOST)
    return name
