# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feature flags (ADR 0006): the registry, ``enabled``, and the rule that every registered flag
has tests that run it both on and off."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dinnerbell.config import Settings
from dinnerbell.domain import flags
from dinnerbell.domain.flags import Flag, UnknownFlag, enabled
from tests import conftest
from tests.support.flags import BadFlagMark, features_for, flag_tests, item_problems, marks_in, missing_flag_tests

DEMO = Flag("demo", "A flag that exists only in these tests.", "0.0.0")


@pytest.fixture
def demo_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flags, "FLAGS", {DEMO.name: DEMO})


def test_every_registered_flag_has_tests_on_and_off() -> None:
    assert missing_flag_tests(flags.FLAGS, flag_tests()) == []


def test_the_scan_reads_every_other_test_file(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text(SOURCE)
    (tmp_path / "test_flags.py").write_text(SOURCE)  # this file builds marks as data; skipped
    assert flag_tests(tmp_path) == {("demo", True), ("demo", False)}
    (tmp_path / "test_a.py").write_text("x = 1\n")
    assert flag_tests(tmp_path) == set()


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
@pytest.mark.parametrize("features", [frozenset({"demo"})])
def test_the_settings_fixture_applies_the_marks_features(
    request: pytest.FixtureRequest, features: frozenset[str]
) -> None:
    # ``features`` stands in for what the marks give (features_for, below), without a real mark.
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


def test_marks_that_disagree_are_refused() -> None:
    """A class marked on with a method marked off would run the "off" test with the flag on."""
    method, cls = pytest.mark.flag("a", on=False).mark, pytest.mark.flag("a", on=True).mark
    with pytest.raises(BadFlagMark, match="both on and off"):
        features_for([method, cls])
    assert features_for([cls, cls]) == frozenset({"a"})
    source = "import pytest\n@pytest.mark.flag('a', on=True)\nclass TestA:\n"
    source += "    @pytest.mark.flag('a', on=False)\n    def test_off(self): ...\n"
    with pytest.raises(BadFlagMark, match="both on and off"):
        marks_in(source)


@dataclass
class FakeItem:
    """Just enough of ``pytest.Item`` for ``item_problems``."""

    marks: list[pytest.MarkDecorator]
    fixturenames: list[str] = field(default_factory=lambda: ["settings"])
    module: Any = None
    nodeid: str = "tests/test_x.py::test_x"

    def iter_markers(self, name: str) -> list[pytest.Mark]:
        return [m.mark for m in self.marks if m.mark.name == name]


ON = pytest.mark.flag("demo", on=True)


def test_a_marked_test_that_ignores_the_settings_fixture_is_refused() -> None:
    assert item_problems(FakeItem([ON])) == []
    assert item_problems(FakeItem([])) == []
    (problem,) = item_problems(FakeItem([ON], fixturenames=["tmp_path"]))
    assert problem.endswith("must use the settings fixture, which applies the mark")


@pytest.mark.parametrize("mark", [pytest.mark.skip(), pytest.mark.skipif(True, reason="x"), pytest.mark.xfail()])
def test_a_marked_test_that_might_not_run_is_refused(mark: pytest.MarkDecorator) -> None:
    (problem,) = item_problems(FakeItem([ON, mark]))
    assert f"can't be marked {mark.mark.name}" in problem


def test_module_and_param_marks_are_refused() -> None:
    module = SimpleNamespace(pytestmark=[ON])
    (problem,) = item_problems(FakeItem([ON], module=module))
    assert "not the module's pytestmark" in problem
    with pytest.raises(BadFlagMark, match="must decorate"):
        marks_in("import pytest\npytestmark = pytest.mark.flag('demo', on=True)\ndef test_x(): ...\n")
    with pytest.raises(BadFlagMark, match="must decorate"):
        marks_in(
            "import pytest\n@pytest.mark.parametrize('x', [pytest.param(1, marks=pytest.mark.flag('demo', on=True))])\n"
            "def test_x(x): ...\n"
        )


def test_bad_marks_stop_the_run() -> None:
    conftest.pytest_collection_modifyitems([FakeItem([ON])])  # type: ignore[list-item]
    with pytest.raises(pytest.UsageError, match="settings fixture"):
        conftest.pytest_collection_modifyitems([FakeItem([ON], fixturenames=[])])  # type: ignore[list-item]


SOURCE = """
import pytest

@pytest.mark.flag("demo", on=True)
def test_on(): ...

@pytest.mark.flag("demo", on=False)
class TestOff:
    async def test_off(self): ...

@pytest.mark.flag("other", on=True)
class TestNothing:
    def helper(self): ...

@pytest.mark.parametrize("x", [1])
def test_unmarked(x): ...
"""


def test_the_source_scan_finds_marks_on_functions_and_methods() -> None:
    # A class mark counts for its tests; a marked class with no tests counts for nothing.
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
