# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``flag`` marker: ``@pytest.mark.flag("name", on=True)`` runs a test with a feature flag
on (or off), and records that the flag has a test in that state.

The ``settings`` fixture applies the marks. ``flag_tests`` reads them from the test sources
instead of from collection, so the coverage check in tests/test_flags.py gives the same answer
however many tests a run selects.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]


class BadFlagMark(Exception):
    """A ``flag`` mark this module can't read: the name and ``on`` must be literals."""


def features_for(marks: Iterable[pytest.Mark]) -> frozenset[str] | None:
    """The flags a test's ``flag`` marks turn on, or None when it has no marks."""
    marks = list(marks)
    if not marks:
        return None
    on: set[str] = set()
    for mark in marks:
        if len(mark.args) != 1 or set(mark.kwargs) != {"on"} or not isinstance(mark.kwargs["on"], bool):
            raise BadFlagMark(f"use @pytest.mark.flag('name', on=True|False), not {mark}")
        if mark.kwargs["on"]:
            on.add(str(mark.args[0]))
    return frozenset(on)


def _flag_call(decorator: ast.expr) -> ast.Call | None:
    if (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "flag"
        and isinstance(decorator.func.value, ast.Attribute)
        and decorator.func.value.attr == "mark"
    ):
        return decorator
    return None


def marks_in(source: str, filename: str = "<test>") -> set[tuple[str, bool]]:
    """Every ``(name, on)`` pair that a ``flag`` mark on a test function or class declares."""
    found: set[tuple[str, bool]] = set()
    for node in ast.walk(ast.parse(source, filename)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for decorator in node.decorator_list:
            call = _flag_call(decorator)
            if call is None:
                continue
            on = {k.arg: k.value for k in call.keywords}.get("on")
            name = call.args[0] if len(call.args) == 1 else None
            if not (
                isinstance(name, ast.Constant)
                and isinstance(name.value, str)
                and isinstance(on, ast.Constant)
                and isinstance(on.value, bool)
            ):
                raise BadFlagMark(f"{filename}:{call.lineno}: a flag mark needs a literal name and on=True or on=False")
            found.add((name.value, on.value))
    return found


def flag_tests(root: Path = TESTS) -> set[tuple[str, bool]]:
    """Every ``(name, on)`` pair marked anywhere under ``root``."""
    found: set[tuple[str, bool]] = set()
    for path in sorted(root.rglob("test_*.py")):
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
