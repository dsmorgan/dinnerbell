# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell catalog validate`` and ``dinnerbell catalog schema``: no configuration or database needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dinnerbell import main as cli
from dinnerbell.domain.catalog_file import render_schema

EXAMPLE = Path(__file__).resolve().parents[1] / "seed" / "example.yaml"


def test_validate_example(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["catalog", "validate", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "is valid." in out
    assert "4 restaurants, 4 home meals" in out
    assert "25 tags: 12 cuisine, 6 protein, 3 dietary, 4 other" in out


def test_validate_reports_undeclared_tags_and_cooks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "version: 1\nhome_meals:\n  - {name: Pie, cuisine: [American], tags: [sweet], cooks: [a@example.com]}\n"
    )
    assert cli.main(["catalog", "validate", str(path)]) == 0
    out = capsys.readouterr().out
    assert "1 home meal" in out and "0 restaurants" in out
    assert "Not declared under tags, so they'd be created: American (cuisine), sweet (other)" in out
    assert "1 cook email will be matched to members at import" in out


def test_validate_reports_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "version: 1\nrestaurants:\n  - {name: A, cuisine: [Thai], modes: [boat]}\n  - {name: a, cuisine: [Thai]}\n"
    )
    assert cli.main(["catalog", "validate", str(path)]) == 1
    out = capsys.readouterr().out
    assert "1 problem. Nothing would be imported." in out
    assert '  restaurants[0].modes[0]: "boat" is not one of pickup, delivery' in out


def test_validate_unreadable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["catalog", "validate", str(tmp_path / "missing.yaml")]) == 2
    assert "Can't read" in capsys.readouterr().err


def test_catalog_commands_need_no_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    """No DINNERBELL_* variables are set in tests, so this would fail if settings were loaded."""
    assert cli.main(["catalog", "schema"]) == 0
    out = capsys.readouterr().out
    assert out == render_schema()
    assert json.loads(out)["title"] == "Dinnerbell catalog file, version 1"


def test_catalog_requires_an_action() -> None:
    with pytest.raises(SystemExit):
        cli.main(["catalog"])
