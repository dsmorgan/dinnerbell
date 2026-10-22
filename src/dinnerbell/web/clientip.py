# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find the real client address behind trusted reverse proxies.

``X-Forwarded-For`` is honored only when the direct peer is a trusted proxy, and then only
up to the first untrusted hop, read from the right. Anything a client writes to the left of
that is ignored.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
UNKNOWN = "unknown"


def _parse(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _trusted(address: ipaddress.IPv4Address | ipaddress.IPv6Address, proxies: Sequence[IPNetwork]) -> bool:
    return any(address.version == net.version and address in net for net in proxies)


def resolve_client_ip(peer: str | None, forwarded_for: str | None, proxies: Sequence[IPNetwork]) -> str:
    peer_address = _parse(peer) if peer else None
    if peer_address is None:
        return peer or UNKNOWN
    if not forwarded_for or not _trusted(peer_address, proxies):
        return str(peer_address)
    client = peer_address
    for hop in reversed(forwarded_for.split(",")):
        address = _parse(hop)
        if address is None:
            return str(client)
        client = address
        if not _trusted(address, proxies):
            return str(address)
    return str(client)
