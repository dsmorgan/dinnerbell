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

    Only IPv4-mapped addresses (``::ffff:a.b.c.d``), which a dual-stack listener reports for
    IPv4 peers, are unwrapped. 6to4 and Teredo addresses are deliberately left alone: the IPv4
    address inside them is chosen by the client, so unwrapping it would let a client pose as a
    trusted proxy or pick another client's rate-limit bucket. Zone IDs (``fe80::1%eth0``) are
    dropped.
    """
    if isinstance(address, ipaddress.IPv4Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
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
