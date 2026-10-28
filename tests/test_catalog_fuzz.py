# SPDX-License-Identifier: AGPL-3.0-or-later
"""The parser and planner never raise, whatever they're given.

Seeded pseudo-random input keeps failures reproducible; nothing here needs cryptographic randomness.
"""

# ruff: noqa: S311

from __future__ import annotations

import random
from pathlib import Path

import pytest
import yaml

from dinnerbell.domain.catalog_file import parse_catalog
from dinnerbell.domain.catalog_import import CatalogSnapshot, plan_import

EXAMPLE = (Path(__file__).resolve().parents[1] / "seed" / "example.yaml").read_text()
YAML_BITS = [
    "version: 1\n",
    "restaurants:\n",
    "home_meals:\n",
    "tags:\n",
    "  - ",
    "name: ",
    "cuisine: ",
    "[",
    "]",
    "{",
    "}",
    ",",
    ":",
    "? ",
    "- ",
    "&a ",
    "*a",
    "<<: ",
    "!!str ",
    "!!python/name:os.system ",
    "'",
    '"',
    "\n",
    "  ",
    "#",
    "|",
    ">",
    "%YAML 1.1\n",
    "---\n",
    "...\n",
    "null",
    "~",
    "true",
    "1e3",
    "0x1F",
    "\t",
    "\x00",
    "﻿",
    "é",
    "restaurants[x]",
    "home_meals[]",
    "modes",
    "pickup",
    "cooks: [a@b.c]",
    "servings: -1",
]


def plan(source: str | bytes) -> None:
    plan_import(parse_catalog(source), CatalogSnapshot.empty())


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("version: 1\n? [a]\n: 1\n", "keys must be plain text"),
        ("version: 1\n? {a: 1}\n: 1\n", "keys must be plain text"),
        ("version: 1\n1: x\n", "keys must be plain text"),
        ("version: 1\nrestaurants:\n  - <<: {name: A, cuisine: [Thai]}\n", "merge keys (<<) aren't supported"),
    ],
)
def test_odd_keys_are_located_errors(source: str, message: str) -> None:
    [error] = parse_catalog(source).errors
    assert error.message == message
    assert error.location.startswith("line ")


@pytest.mark.parametrize("key", ["restaurants[x]", "home_meals[]", "restaurants[1", "tags[0]", "[0]"])
def test_bracketed_unknown_keys_plan_without_crashing(key: str) -> None:
    parsed = parse_catalog(f"version: 1\n'{key}': 1\nrestaurants:\n  - {{name: A, cuisine: [Thai], modes: [x]}}\n")
    result = plan_import(parsed, CatalogSnapshot.empty())
    assert {e.location for e in result.errors} == {key, "restaurants[0].modes[0]"}
    assert [e.location for e in result.errors][-1] == key  # unknown sections sort last


def test_random_junk_never_raises() -> None:
    rng = random.Random(20261022)
    for _ in range(3000):
        source = "".join(rng.choice(YAML_BITS) for _ in range(rng.randint(1, 40)))
        plan(source)
        plan(source.encode("utf-8", "surrogatepass")[: rng.randint(0, 200)])


def random_value(rng: random.Random, depth: int = 0) -> object:
    choices: list[object] = [None, True, 0, -1, 2**70, 1.5, "", "x", "Thai", "pickup", "a@example.com", [], {}]
    if depth < 4:
        choices += [
            [random_value(rng, depth + 1) for _ in range(rng.randint(0, 3))],
            {
                rng.choice(
                    ["name", "cuisine", "tags", "modes", "url", "cooks", "servings", "active", "x"]
                ): random_value(rng, depth + 1)
                for _ in range(rng.randint(0, 4))
            },
        ]
    return rng.choice(choices)


# Slices of one run, so the workers share the loop. Each slice has its own seed and the same size.
@pytest.mark.parametrize("slice_", range(5))
def test_random_structures_never_raise(slice_: int) -> None:
    rng = random.Random(7 + 1000 * slice_)
    for _ in range(400):
        document = {
            "version": rng.choice([1, 1, 1, 2, "1", None]),
            rng.choice(["restaurants", "home_meals", "tags"]): random_value(rng),
            rng.choice(["restaurants", "home_meals"]): [random_value(rng) for _ in range(rng.randint(0, 3))],
        }
        plan(yaml.safe_dump(document))


@pytest.mark.parametrize("slice_", range(5))
def test_mutated_example_never_raises(slice_: int) -> None:
    rng = random.Random(11 + 1000 * slice_)
    for _ in range(300):
        chars = list(EXAMPLE)
        for _ in range(rng.randint(1, 8)):
            position = rng.randrange(len(chars))
            operation = rng.randrange(3)
            if operation == 0:
                del chars[position]
            elif operation == 1:
                chars.insert(position, rng.choice(YAML_BITS))
            else:
                chars[position] = rng.choice(" :-[]{}\n'\"&*!#|>")
        plan("".join(chars))


@pytest.mark.parametrize(
    "key",
    [
        "restaurants[" + "9" * 5000 + "]",  # past Python's 4,300-digit int() limit
        "restaurants[" + chr(0x0660) + "]",  # an Arabic-Indic zero, which a plain \\d matches
        "home_meals[" + chr(0x0661) + "]",
        "restaurants[1234567890]",  # longer than any real list position
    ],
)
def test_huge_or_non_ascii_positions_are_unknown_keys(key: str) -> None:
    # An explicit "? key" lets the key run past YAML's 1,024-character limit for plain keys.
    source = (
        f"version: 1\n? '{key}'\n: 1\nrestaurants:\n"
        "  - {name: A, cuisine: [Thai]}\n  - {name: B, cuisine: [Thai], modes: [x]}\n"
    )
    result = plan_import(parse_catalog(source), CatalogSnapshot.empty())
    assert [e.location for e in result.errors] == ["restaurants[1].modes[0]", key]  # unknown keys sort last
    assert result.errors[-1].message == "unknown field"
