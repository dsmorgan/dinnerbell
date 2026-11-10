# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feature flags (ADR 0006): a registry in code, turned on with ``DINNERBELL_FEATURES``.

Use a flag only when a feature must ship to production turned off; test runs ``:main`` and
needs none. Every flag here needs tests that run it both on and off, marked with
``@pytest.mark.flag("name", on=True)`` and ``on=False`` (tests/test_flags.py checks). Remove a
flag one release after it turns on in production, and add its name to ``RETIRED``, so an install
that still lists it starts, with a warning, instead of refusing.
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


ASSISTANT = "assistant"

_DEFINED: tuple[Flag, ...] = (
    Flag(
        ASSISTANT,
        "The optional assistant (ADR 0009): Admin's Assistant settings, its cost cap, and its "
        "conversations. It also needs DINNERBELL_ANTHROPIC_API_KEY.",
        "0.16.0",
    ),
)

FLAGS: Mapping[str, Flag] = MappingProxyType({flag.name: flag for flag in _DEFINED})
"""Every flag the code knows, by name."""

RETIRED: Mapping[str, str] = MappingProxyType({})
"""Every flag removed from ``FLAGS``, by name, with the release that removed it, such as
``0.14.0``. ``DINNERBELL_FEATURES`` may still name one after an upgrade: it's ignored, and the CLI
warns. Never take a name off this list, and never reuse one."""


class UnknownFlag(ValueError):
    """A flag name that isn't in ``FLAGS``."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown feature flag {name[:40]!r}")
        self.name = name


class HasFeatures(Protocol):
    @property
    def features(self) -> Set[str]: ...


def check(names: Iterable[str]) -> frozenset[str]:
    """Return the registered flags in ``names`` as a set, leaving out retired ones. Raise
    ``UnknownFlag`` for the first name that is neither registered nor retired, so a typo still
    stops startup."""
    found = frozenset(names)
    for name in sorted(found):
        if name not in FLAGS and name not in RETIRED:
            raise UnknownFlag(name)
    return frozenset(name for name in found if name not in RETIRED)


def retired(names: Iterable[str]) -> list[str]:
    """The retired flags in ``names``, sorted, for a warning."""
    return sorted(name for name in set(names) if name in RETIRED)


def enabled(source: HasFeatures | Set[str], name: str) -> bool:
    """True when flag ``name`` is on in ``source``: the settings, or a set of flag names.

    Raises ``UnknownFlag`` for a name that isn't registered, so a typo fails loudly instead of
    reading as off."""
    if name not in FLAGS:
        raise UnknownFlag(name)
    features = source if isinstance(source, Set) else source.features
    return name in features
