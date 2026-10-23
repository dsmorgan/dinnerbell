# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feature flags (ADR 0006): a registry in code, turned on with ``DINNERBELL_FEATURES``.

Use a flag only when a feature must ship to production turned off; test runs ``:main`` and
needs none. Every flag here needs tests that run it both on and off, marked with
``@pytest.mark.flag("name", on=True)`` and ``on=False`` (tests/test_flags.py checks). Remove a
flag one release after it turns on in production.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Flag:
    name: str
    description: str
    added_in: str
    """The release that added the flag, such as ``0.6.0``."""


_DEFINED: tuple[Flag, ...] = ()

FLAGS: Mapping[str, Flag] = MappingProxyType({flag.name: flag for flag in _DEFINED})
"""Every flag the code knows, by name."""


class UnknownFlag(ValueError):
    """A flag name that isn't in ``FLAGS``."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown feature flag {name[:40]!r}")
        self.name = name


class HasFeatures(Protocol):
    @property
    def features(self) -> Set[str]: ...


def check(names: Iterable[str]) -> frozenset[str]:
    """Return ``names`` as a set, or raise ``UnknownFlag`` for the first unregistered one."""
    found = frozenset(names)
    for name in sorted(found):
        if name not in FLAGS:
            raise UnknownFlag(name)
    return found


def enabled(source: HasFeatures | Set[str], name: str) -> bool:
    """True when flag ``name`` is on in ``source``: the settings, or a set of flag names.

    Raises ``UnknownFlag`` for a name that isn't registered, so a typo fails loudly instead of
    reading as off."""
    if name not in FLAGS:
        raise UnknownFlag(name)
    features = source if isinstance(source, Set) else source.features
    return name in features
