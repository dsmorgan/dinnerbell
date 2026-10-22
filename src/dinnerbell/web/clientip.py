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
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
UNKNOWN = "unknown"


def normalize(address: IPAddress) -> IPAddress:
    """Reduce an address to the host it identifies.

    IPv4-mapped (``::ffff:a.b.c.d``), 6to4, and Teredo addresses carry an IPv4 client; use it,
    so a dual-stack listener doesn't lump every IPv4 client into one IPv6 /64. Zone IDs
    (``fe80::1%eth0``) are dropped.
    """
    if isinstance(address, ipaddress.IPv4Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if address.scope_id is not None:
        return ipaddress.IPv6Address(str(address).split("%", 1)[0])
    return address


def _parse(value: str) -> IPAddress | None:
    try:
        return normalize(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _trusted(address: IPAddress, proxies: Sequence[IPNetwork]) -> bool:
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
