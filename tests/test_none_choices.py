# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every menu choice or placeholder that picks nothing is in square brackets, on the web and in
Slack (#239), so it can't be mistaken for a real pick. The words live in ``domain.labels``.

The web check reads the templates: every ``<option>`` whose value is empty, or whose words say
none, any, choose, and the like, every empty-valued radio button, every string an option's
``{{ ... }}`` can show (literals and ``labels``), and every entry of a literal or ``labels``
sequence that a ``{% for %}`` turns into options. An option expression it can't read fails. The Slack check walks every
sample payload (``preview.KINDS``, the same ones the golden files hold): every select, overflow,
and radio button's options, and every placeholder. Every select has a bracketed placeholder, even
one that starts on a choice, since one drawn without its initial option would show Slack's own
"Select an item". It also reads the Slack package's source for ``option(...)`` and placeholder
words that no sample shows, and for any select written without a placeholder.
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


def bracketed(text: str) -> bool:
    text = text.strip()
    return text.startswith("[") and text.endswith("]") and len(text) > 2


def none_like(text: str) -> bool:
    return bool(NONE_LIKE.search(text))


# The web ------------------------------------------------------------------------------------------

OPTION = re.compile(r"<option\b([^>]*)>([^<]*)</option>")
EMPTY_RADIO = re.compile(r'<input type="radio"[^>]*\bvalue=""[^>]*>\s*<span>(.*?)</span>', re.DOTALL)
OUTPUT = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
TAG = re.compile(r"\{%.*?%\}", re.DOTALL)
FOR = re.compile(r"\{%-?\s*for\s+.+?\s+in\s+(.+?)(?:\s+if\s+.*?)?\s*-?%\}(.*?)\{%-?\s*endfor", re.DOTALL)
FILTER = re.compile(r"\|\s*\w+(\([^()]*\))?")


def from_labels(node: ast.expr) -> list[str] | None:
    """The words a ``labels`` name, call, or lookup stands for, or None when ``node`` isn't one.
    A lookup in a mapping could be any of its values."""
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"items", "values"}:
        node = node.func.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return [str(getattr(labels, node.func.attr)("Somewhere"))] if node.func.value.id == "labels" else None
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "labels":
        found = getattr(labels, node.attr)
        if isinstance(found, str):
            return [found]
        if isinstance(found, dict):
            return [v for v in found.values() if isinstance(v, str)]
        return [v for v in found if isinstance(v, str)] if isinstance(found, (tuple, list, frozenset)) else []
    return None


def shown(expression: str, where: str) -> list[str]:
    """Every string a Jinja ``{{ expression }}`` can show: its literals, except those it only
    compares, and the words of the ``labels`` it names. An expression this can't read fails, so
    a menu's words stay where the check sees them."""
    try:
        tree = ast.parse(FILTER.sub("", expression).replace("~", "+").strip(), mode="eval")
    except SyntaxError:
        pytest.fail(f"{where}: can't read {{{{{expression}}}}}; put a menu's words in labels")
    words: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Compare):
            return  # a value it tests, not words it shows
        if isinstance(node, ast.IfExp):
            visit(node.body)
            visit(node.orelse)
            return
        found = from_labels(node) if isinstance(node, ast.expr) else None
        if found is not None:
            words.extend(found)
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            words.append(node.value)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree.body)
    return words


def resolve(text: str, where: str) -> tuple[str, list[str]]:
    """An option's text as it reads, with each expression shown as its first words (dynamic
    values, such as a loop's names, show as nothing), and every string it could show."""
    pieces: list[str] = []

    def first(m: re.Match[str]) -> str:
        words = shown(m.group(1), where)
        pieces.extend(words)
        return words[0] if words else ""

    plain = OUTPUT.sub(first, TAG.sub("", text)).strip()
    return plain, [*pieces, OUTPUT.sub("", TAG.sub("", text))]


def web_choices() -> Iterator[tuple[str, str, bool]]:
    """Each menu choice the templates can show: where, its words, and whether its value is empty.
    That's every ``<option>`` and empty-valued radio button, each string its text can show, and
    each entry of a literal or ``labels`` sequence that a loop turns into options."""
    for path in sorted(TEMPLATES.rglob("*.html")):
        source = path.read_text()

        def at(offset: int) -> str:
            return f"{path.name}:{source.count(chr(10), 0, offset) + 1}"  # noqa: B023

        for m in OPTION.finditer(source):
            text, words = resolve(m.group(2), at(m.start()))
            yield at(m.start()), text, 'value=""' in m.group(1)
            for word in words:
                yield at(m.start()), word, False
        for m in EMPTY_RADIO.finditer(source):
            text, words = resolve(m.group(1), at(m.start()))
            yield at(m.start()), text, True
            for word in words:
                yield at(m.start()), word, False
        for m in FOR.finditer(source):
            if "<option" not in m.group(2):
                continue
            yield from loop_choices(m.group(1), at(m.start()))


def loop_choices(iterable: str, where: str) -> Iterator[tuple[str, str, bool]]:
    """The choices a ``{% for %}`` over a literal, such as ``(("pickup", "Pickup"), ...)``, or over
    ``labels``, makes: each pair's value first, then its words."""
    try:
        node = ast.parse(FILTER.sub("", iterable).strip(), mode="eval").body
    except SyntaxError:
        return
    found = from_labels(node)
    if found is not None:
        for word in found:
            yield where, word, False
        return
    try:
        entries = ast.literal_eval(node)
    except ValueError:
        return  # the page's own values, such as members or slots
    for entry in entries:
        if isinstance(entry, str):
            yield where, entry, False
        else:
            value, *words = entry
            for word in words:
                yield where, word, value == ""


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
        # Even one with an initial option here may be drawn without one, as for a coordinator gone.
        placeholder = element.get("placeholder")
        assert placeholder is not None, f"{where} has no placeholder, so it can show Slack's own"
        assert bracketed(placeholder["text"]), f"{where}: placeholder {placeholder['text']!r}"


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


def select_literals() -> Iterator[tuple[str, str, bool]]:
    """Each select the Slack package's source writes as a dict: where, the function it's in, and
    whether it has a placeholder, or a ``**`` spread that can carry one, such as
    ``**CHOOSE_SOMEONE``."""
    for path in sorted(SLACK.glob("*.py")):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Dict):
                    continue
                kind = next(
                    (
                        v
                        for k, v in zip(node.keys, node.values, strict=True)
                        if isinstance(k, ast.Constant) and k.value == "type"
                    ),
                    None,
                )
                entries = {k.value for k in node.keys if isinstance(k, ast.Constant)}
                if isinstance(kind, ast.Constant) and kind.value in SELECTS:
                    yield f"{path.name}:{node.lineno}", function.name, "placeholder" in entries or None in node.keys


def test_every_slack_select_has_a_placeholder() -> None:
    """A select drawn without an initial option shows its placeholder, or Slack's own "Select an
    item" without one, so every select has one, whether or not a sample starts it on a choice."""
    found = list(select_literals())
    assert len(found) > 10
    assert [where for where, _, placeholder in found if not placeholder] == []


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
