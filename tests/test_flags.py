# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feature flags (ADR 0006): the registry, ``enabled``, and the rule that every registered flag
has tests that run it both on and off."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dinnerbell.config import Settings
from dinnerbell.domain import flags
from dinnerbell.domain.flags import Flag, UnknownFlag, enabled
from tests.support.flags import BadFlagMark, features_for, flag_tests, marks_in, missing_flag_tests

DEMO = Flag("demo", "A flag that exists only in these tests.", "0.0.0")


@pytest.fixture
def demo_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flags, "FLAGS", {DEMO.name: DEMO})


def test_every_registered_flag_has_tests_on_and_off() -> None:
    assert missing_flag_tests(flags.FLAGS, flag_tests()) == []


def test_registered_flags_are_well_formed() -> None:
    for name, flag in flags.FLAGS.items():
        assert name == flag.name
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name), name
        assert flag.description
        assert re.fullmatch(r"\d+\.\d+\.\d+", flag.added_in), name


def test_enabled_raises_on_an_unregistered_name() -> None:
    with pytest.raises(UnknownFlag, match="unknown feature flag 'no_such_flag'"):
        enabled(frozenset({"no_such_flag"}), "no_such_flag")
    with pytest.raises(UnknownFlag):
        flags.check(["no_such_flag"])


@pytest.mark.usefixtures("demo_registry")
def test_enabled_reads_settings_or_a_set(tmp_path: Path) -> None:
    from tests.conftest import make_settings

    assert enabled(frozenset({"demo"}), "demo")
    assert not enabled(frozenset(), "demo")
    assert enabled(make_settings(tmp_path, features="demo"), "demo")
    assert not enabled(make_settings(tmp_path), "demo")


@pytest.mark.usefixtures("demo_registry")
def test_the_flag_mark_sets_the_settings(request: pytest.FixtureRequest) -> None:
    # Applied at run time, so the source scan below doesn't count this test as covering "demo".
    request.applymarker(pytest.mark.flag("demo", on=True))
    settings: Settings = request.getfixturevalue("settings")
    assert settings.features == frozenset({"demo"})


def test_features_for_marks() -> None:
    assert features_for([]) is None
    on, off = pytest.mark.flag("a", on=True).mark, pytest.mark.flag("b", on=False).mark
    assert features_for([on, off]) == frozenset({"a"})
    assert features_for([off]) == frozenset()
    with pytest.raises(BadFlagMark):
        features_for([pytest.mark.flag("a").mark])
    with pytest.raises(BadFlagMark):
        features_for([pytest.mark.flag("a", on="yes").mark])


SOURCE = """
import pytest

@pytest.mark.flag("demo", on=True)
def test_on(): ...

class TestOff:
    @pytest.mark.flag("demo", on=False)
    async def test_off(self): ...

@pytest.mark.parametrize("x", [1])
def test_unmarked(x): ...
"""


def test_the_source_scan_finds_marks_on_functions_and_methods() -> None:
    assert marks_in(SOURCE) == {("demo", True), ("demo", False)}
    with pytest.raises(BadFlagMark, match="literal name"):
        marks_in("import pytest\nNAME = 'demo'\n@pytest.mark.flag(NAME, on=True)\ndef test_x(): ...\n")
    with pytest.raises(BadFlagMark):
        marks_in("import pytest\n@pytest.mark.flag('demo', on=1)\ndef test_x(): ...\n")


def test_the_coverage_check_catches_a_missing_state_and_a_stale_mark() -> None:
    registry = {"demo": DEMO}
    assert missing_flag_tests(registry, {("demo", True), ("demo", False)}) == []
    assert missing_flag_tests(registry, {("demo", True)}) == ["flag 'demo' has no test with on=False"]
    assert missing_flag_tests(registry, set()) == [
        "flag 'demo' has no test with on=True",
        "flag 'demo' has no test with on=False",
    ]
    assert missing_flag_tests({}, {("gone", True)}) == ["a test marks unregistered flag 'gone'"]
