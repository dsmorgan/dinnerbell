# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``flag`` marker: ``@pytest.mark.flag("name", on=True)`` runs a test with a feature flag
on (or off), and records that the flag has a test in that state.

The ``settings`` fixture applies the marks. ``flag_tests`` reads them from the test sources
instead of from collection, so the coverage check in tests/test_flags.py gives the same answer
however many tests a run selects. Both sides follow the same rules, so a mark can't count as
coverage without running the test in that state:

- A mark decorates a ``test*`` function or a ``Test*`` class, and nothing else: no other
  class, no module ``pytestmark``, and no ``pytest.param(marks=...)``.
- A test's marks for one flag, its own and its classes', all agree.
- A marked test uses the ``settings`` fixture, which applies the marks and fails the test if
  the flags it ends up with disagree with them. It can't parametrize ``features`` or a
  ``settings_overrides`` with ``features`` in it.
- A marked test is never skipped or expected to fail by a mark. A ``pytest.skip()`` call in
  the test's body can't be caught; don't write one.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

TESTS = Path(__file__).resolve().parents[1]
SELF_TEST = TESTS / "test_flags.py"
"""Builds flag marks as data to test these rules, so the scan skips it. Nothing in it counts."""
SKIPPING = ("skip", "skipif", "xfail")


class BadFlagMark(Exception):
    """A ``flag`` mark that breaks the rules above."""


def features_for(marks: Iterable[pytest.Mark]) -> frozenset[str] | None:
    """The flags a test's ``flag`` marks turn on, or None when it has no marks. Raises
    ``BadFlagMark`` for a malformed mark, or for marks that disagree about one flag."""
    marks = list(marks)
    if not marks:
        return None
    states: dict[str, bool] = {}
    for mark in marks:
        if len(mark.args) != 1 or set(mark.kwargs) != {"on"} or not isinstance(mark.kwargs["on"], bool):
            raise BadFlagMark(f"use @pytest.mark.flag('name', on=True|False), not {mark}")
        name, on = str(mark.args[0]), mark.kwargs["on"]
        if states.setdefault(name, on) != on:
            raise BadFlagMark(f"flag {name!r} is marked both on and off for one test")
    return frozenset(name for name, on in states.items() if on)


def disagreements(marks: Iterable[pytest.Mark], features: Iterable[str]) -> list[str]:
    """Each mark the test's settings don't honor: the flag is on when marked off, or off when
    marked on."""
    on = set(features)
    return [
        f"flag {m.args[0]!r} is marked on={m.kwargs['on']}, but the settings have it the other way"
        for m in marks
        if (m.args[0] in on) != m.kwargs["on"]
    ]


def _module_marks(module: object) -> list[pytest.Mark]:
    marks = getattr(module, "pytestmark", [])
    marks = marks if isinstance(marks, list) else [marks]
    return [getattr(m, "mark", m) for m in marks]


def item_problems(item: Any) -> list[str]:
    """Why a collected test's ``flag`` marks can't be trusted, if they can't."""
    marks = list(item.iter_markers("flag"))
    if not marks:
        return []
    where = item.nodeid
    problems: list[str] = []
    try:
        features_for(marks)
    except BadFlagMark as exc:
        problems.append(f"{where}: {exc}")
    if any(m.name == "flag" for m in _module_marks(getattr(item, "module", None))):
        problems.append(f"{where}: mark the test or its class with flag, not the module's pytestmark")
    if "settings" not in item.fixturenames:
        problems.append(f"{where}: a flag-marked test must use the settings fixture, which applies the mark")
    params = getattr(getattr(item, "callspec", None), "params", {})
    overrides = params.get("settings_overrides")
    if "features" in params or (isinstance(overrides, dict) and "features" in overrides):
        problems.append(f"{where}: a flag-marked test can't parametrize its features; the marks set them")
    for name in SKIPPING:
        if any(True for _ in item.iter_markers(name)):
            problems.append(f"{where}: a flag-marked test must run, so it can't be marked {name}")
    return problems


def _flag_call(node: ast.AST) -> ast.Call | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "flag"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "mark"
    ):
        return node
    return None


def _literal(call: ast.Call, filename: str) -> tuple[str, bool]:
    on = {k.arg: k.value for k in call.keywords}.get("on")
    name = call.args[0] if len(call.args) == 1 else None
    if not (
        isinstance(name, ast.Constant)
        and isinstance(name.value, str)
        and isinstance(on, ast.Constant)
        and isinstance(on.value, bool)
        and len(call.keywords) == 1
    ):
        raise BadFlagMark(f"{filename}:{call.lineno}: a flag mark needs a literal name and on=True or on=False")
    return name.value, on.value


def _tests(
    nodes: Iterable[ast.stmt], inherited: dict[str, bool], filename: str
) -> Iterator[tuple[str, dict[str, bool]]]:
    """Each test function under ``nodes`` with the flag states its marks and its classes' set."""
    for node in nodes:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        if isinstance(node, ast.ClassDef) and not node.name.startswith("Test"):
            if any(_flag_call(d) for d in node.decorator_list):
                raise BadFlagMark(f"{filename}:{node.lineno}: only a Test* class's flag marks reach its tests")
            continue  # pytest doesn't collect it
        states = dict(inherited)
        for decorator in node.decorator_list:
            call = _flag_call(decorator)
            if call is None:
                continue
            name, on = _literal(call, filename)
            if states.setdefault(name, on) != on:
                raise BadFlagMark(f"{filename}:{call.lineno}: flag {name!r} is marked both on and off for one test")
        if isinstance(node, ast.ClassDef):
            yield from _tests(node.body, states, filename)
        elif node.name.startswith("test"):
            yield node.name, states


def marks_in(source: str, filename: str = "<test>") -> set[tuple[str, bool]]:
    """Every ``(name, on)`` state some test function in ``source`` runs a flag in."""
    tree = ast.parse(source, filename)
    decorators = {
        id(d)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        for d in node.decorator_list
    }
    for node in ast.walk(tree):
        call = _flag_call(node)
        if call is not None and id(call) not in decorators:
            raise BadFlagMark(f"{filename}:{call.lineno}: a flag mark must decorate a test function or class")
    return {(name, on) for _, states in _tests(tree.body, {}, filename) for name, on in states.items()}


def flag_tests(root: Path = TESTS) -> set[tuple[str, bool]]:
    """Every ``(name, on)`` state some test under ``root`` runs a flag in."""
    found: set[tuple[str, bool]] = set()
    for path in sorted(root.rglob("test_*.py")):
        if path == SELF_TEST:
            continue
        found |= marks_in(path.read_text(), str(path))
    return found


def missing_flag_tests(registry: Mapping[str, object], marked: set[tuple[str, bool]]) -> list[str]:
    """Problems with flag coverage: a registered flag without a test in each state, or a mark
    naming a flag that isn't registered."""
    problems = [
        f"flag {name!r} has no test with on={state}"
        for name in sorted(registry)
        for state in (True, False)
        if (name, state) not in marked
    ]
    problems += [f"a test marks unregistered flag {name!r}" for name in sorted({n for n, _ in marked} - set(registry))]
    return problems
