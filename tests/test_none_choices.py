# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every menu choice or placeholder that picks nothing is in square brackets, on the web and in
Slack (#239), so it can't be mistaken for a real pick. The words live in ``domain.labels``.

The web check reads the templates: every ``<option>`` whose value is empty, or whose words say
none, any, choose, and the like, and every empty-valued radio button. The Slack check walks every
sample payload (``preview.KINDS``, the same ones the golden files hold): every select, overflow,
and radio button's options, every placeholder, and every select that can show empty, which needs
a placeholder, since Slack's own "Select an item" has no brackets. It also reads the Slack
package's source for ``option(...)`` and placeholder words that no sample shows.
"""

from __future__ import annotations

import ast
import importlib
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dinnerbell.channels.slack import preview as samples
from dinnerbell.channels.slack import views
from dinnerbell.domain import labels

SRC = Path(__file__).parent.parent / "src" / "dinnerbell"
TEMPLATES = SRC / "web" / "templates"
SLACK = SRC / "channels" / "slack"

NONE_LIKE = re.compile(
    r"\b(none|nobody|no one|nothing|no \w+|either way|whichever|any|all|choose|pick|select|keep|instead|"
    r"not set|unset|blank|empty)\b",
    re.IGNORECASE,
)
"""Words that say a choice picks nothing. A real choice that uses one gets brackets too."""

SELECTS = {
    "static_select",
    "multi_static_select",
    "external_select",
    "multi_external_select",
    "users_select",
    "multi_users_select",
    "conversations_select",
    "multi_conversations_select",
    "channels_select",
    "multi_channels_select",
}
WITH_OPTIONS = {*SELECTS, "overflow", "radio_buttons"}

UNTOUCHED = {("details_modal", "guest_needs"), ("details_modal_empty", "guest_needs")}
"""Details' guest dietary needs leave with the guest count (#240), so they keep Slack's own
placeholder until then."""


def bracketed(text: str) -> bool:
    text = text.strip()
    return text.startswith("[") and text.endswith("]") and len(text) > 2


def none_like(text: str) -> bool:
    return bool(NONE_LIKE.search(text))


# The web ------------------------------------------------------------------------------------------

OPTION = re.compile(r"<option\b([^>]*)>([^<]*)</option>")
EMPTY_RADIO = re.compile(r'<input type="radio"[^>]*\bvalue=""[^>]*>\s*<span>(.*?)</span>', re.DOTALL)
LABEL = re.compile(r"\{\{\s*labels\.(\w+)(\(.*?\))?\s*\}\}")
EXPRESSION = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def resolve(text: str) -> str:
    """``text`` with each ``{{ labels.X }}`` (or ``labels.f(...)``) replaced by its words, and any
    other Jinja dropped, since a loop's own values are real choices."""

    def word(m: re.Match[str]) -> str:
        found = getattr(labels, m.group(1))
        return str(found("Somewhere") if m.group(2) else found)

    return EXPRESSION.sub("", LABEL.sub(word, text)).strip()


def web_choices() -> Iterator[tuple[str, str, bool]]:
    """Each option and empty radio button in the templates: where, its words, and whether its
    value is empty."""
    for path in sorted(TEMPLATES.rglob("*.html")):
        source = path.read_text()
        for m in OPTION.finditer(source):
            line = source.count("\n", 0, m.start()) + 1
            yield f"{path.name}:{line}", resolve(m.group(2)), 'value=""' in m.group(1)
        for m in EMPTY_RADIO.finditer(source):
            line = source.count("\n", 0, m.start()) + 1
            yield f"{path.name}:{line}", resolve(m.group(1)), True


def test_web_none_choices_are_bracketed() -> None:
    choices = list(web_choices())
    assert len(choices) > 20  # the scan finds the templates' menus
    wrong = [(where, text) for where, text, empty in choices if (empty or none_like(text)) and not bracketed(text)]
    assert wrong == []


def test_web_and_slack_share_the_words() -> None:
    """The choices both surfaces offer come from ``labels``, so they read the same (#239)."""
    event = (TEMPLATES / "_event.html").read_text()
    for name in ("NOTHING_FROM_CATALOG", "NO_SAVED_GUEST", "CHOOSE_CUISINE", "NO_CUISINE", "WHICHEVER_WAY"):
        assert f"labels.{name}" in event, name
    views = (SLACK / "views.py").read_text()
    for name in ("NOTHING_FROM_CATALOG", "NO_SAVED_GUEST", "NO_ANSWER", "CHOOSE_CUISINE", "NO_CUISINE"):
        assert f"labels.{name}" in views, name


# Slack --------------------------------------------------------------------------------------------


def elements(node: Any, block_id: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Every element with options in a payload, with the input block it's in."""
    if isinstance(node, dict):
        block_id = node.get("block_id", block_id) if node.get("type") == "input" else block_id
        if node.get("type") in WITH_OPTIONS:
            yield block_id, node
        for value in node.values():
            yield from elements(value, block_id)
    elif isinstance(node, list):
        for value in node:
            yield from elements(value, block_id)


def option_texts(element: dict[str, Any]) -> Iterator[str]:
    groups = [element.get("options") or [], *(g["options"] for g in element.get("option_groups") or [])]
    for options in groups:
        for o in options:
            yield o["text"]["text"]


@pytest.mark.parametrize("kind", samples.KINDS)
def test_slack_none_choices_are_bracketed(kind: str) -> None:
    for block_id, element in elements(samples.preview(kind)):
        where = f"{kind} {block_id or element['type']}"
        for text in option_texts(element):
            assert bracketed(text) or not none_like(text), f"{where}: {text!r}"
        if element["type"] not in SELECTS:
            continue
        placeholder = element.get("placeholder")
        if placeholder is not None:
            assert bracketed(placeholder["text"]), f"{where}: placeholder {placeholder['text']!r}"
        elif (kind, block_id) not in UNTOUCHED:
            assert any(k.startswith("initial_") for k in element), f"{where} can show empty with no placeholder"


def words(module: Any, node: ast.expr) -> str | None:
    """The words ``node`` stands for, when the source names them: a literal, a module constant,
    or one from ``labels``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and isinstance(getattr(module, node.id, None), str):
        return str(getattr(module, node.id))
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "labels":
        found = getattr(labels, node.attr)
        return found if isinstance(found, str) else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return str(getattr(labels, node.func.attr)("Somewhere")) if node.func.value.id == "labels" else None
    return None


def slack_source() -> Iterator[tuple[str, str, bool]]:
    """Each ``option(...)`` text and placeholder the Slack package names: where, its words, and
    whether it's a placeholder."""
    for path in sorted(SLACK.glob("*.py")):
        module = importlib.import_module(f"dinnerbell.channels.slack.{path.stem}")
        for node in ast.walk(ast.parse(path.read_text())):
            called = node.func.id if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) else None
            if isinstance(node, ast.Call) and node.args and called in {"option", "_placeholder"}:
                text = words(module, node.args[0])
                if text is not None:
                    yield f"{path.name}:{node.lineno}", text, called == "_placeholder"
            if isinstance(node, ast.Dict):
                entries = {
                    k.value: v for k, v in zip(node.keys, node.values, strict=True) if isinstance(k, ast.Constant)
                }
                kind, placeholder = entries.get("type"), entries.get("placeholder")
                if isinstance(kind, ast.Constant) and kind.value in SELECTS and isinstance(placeholder, ast.Call):
                    text = words(module, placeholder.args[0]) if placeholder.args else None
                    if text is not None:
                        yield f"{path.name}:{node.lineno}", text, True


CHECKBOXES = {o["text"]["text"] for o in views.CLEAR_TIMES.values()}
"""**No earliest time** and **No latest time** are checkboxes that clear a time, not choices in a
menu, so they read as plain words."""


def test_slack_source_none_choices_are_bracketed() -> None:
    """Catches what no sample draws, such as **[Either way]** on a pick whose way was chosen."""
    found = [(where, text, placeholder) for where, text, placeholder in slack_source() if text not in CHECKBOXES]
    assert any(text == labels.EITHER_WAY for _, text, _ in found)
    wrong = [
        (where, text) for where, text, placeholder in found if (placeholder or none_like(text)) and not bracketed(text)
    ]
    assert wrong == []
