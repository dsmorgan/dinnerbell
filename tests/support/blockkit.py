# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check Block Kit JSON against Slack's documented limits, so a message that Slack would
reject with invalid_blocks fails a test instead. Limits are from docs.slack.dev/reference/block-kit
and docs.slack.dev/reference/views/modal-views."""

from __future__ import annotations

import re
from typing import Any

MAX_BLOCKS = {"message": 50, "modal": 100, "home": 100}
TEXT_TYPES = {"plain_text", "mrkdwn"}
ELEMENTS = {
    "button",
    "overflow",
    "static_select",
    "multi_static_select",
    "timepicker",
    "datepicker",
    "number_input",
    "plain_text_input",
    "radio_buttons",
    "checkboxes",
}
_LINK = re.compile(r"<([^<>]*)>")


def check_text(obj: Any, limit: int, where: str, *, plain_only: bool = False) -> None:
    assert isinstance(obj, dict), f"{where}: text object expected"
    assert obj.get("type") in ({"plain_text"} if plain_only else TEXT_TYPES), f"{where}: bad text type {obj}"
    text = obj.get("text")
    assert isinstance(text, str) and text, f"{where}: empty text"
    assert len(text) <= limit, f"{where}: {len(text)} > {limit} characters"
    if obj["type"] == "mrkdwn":
        check_mrkdwn(text, where)


def check_mrkdwn(text: str, where: str) -> None:
    """Every ``<...>`` must be a mention, channel, or http(s) link, and no stray angle brackets."""
    for match in _LINK.finditer(text):
        inner = match.group(1)
        ok = (
            re.fullmatch(r"@[UW][A-Z0-9]+", inner)
            or re.fullmatch(r"#[CG][A-Z0-9]+", inner)
            or re.fullmatch(r"https?://[^\s|<>]+\|[^<>]+", inner)
        )
        assert ok, f"{where}: unexpected control sequence <{inner}>"
    stripped = _LINK.sub("", text)
    assert "<" not in stripped and ">" not in stripped, f"{where}: unescaped angle bracket in {text!r}"
    assert "<!" not in text, f"{where}: special mention in {text!r}"


def check_option(option: Any, where: str) -> None:
    check_text(option["text"], 75, f"{where}.text", plain_only=True)
    assert isinstance(option["value"], str) and 0 < len(option["value"]) <= 150, where


def check_image(element: dict[str, Any], where: str) -> None:
    """An image element: a Slack file by ID, or a URL (not used), and alt text."""
    assert set(element) == {"type", "slack_file", "alt_text"}, f"{where}: {sorted(element)}"
    assert set(element["slack_file"]) == {"id"} and element["slack_file"]["id"], f"{where}: slack_file"
    assert 0 < len(element["alt_text"]) <= 2000, f"{where}: alt_text"


def check_element(element: Any, where: str, action_ids: set[str]) -> None:
    kind = element.get("type")
    assert kind in ELEMENTS, f"{where}: unexpected element {kind}"
    action_id = element.get("action_id")
    assert isinstance(action_id, str) and 0 < len(action_id) <= 255, f"{where}: action_id"
    assert action_id not in action_ids, f"{where}: duplicate action_id {action_id}"
    action_ids.add(action_id)
    if kind == "button":
        check_text(element["text"], 75, f"{where}.text", plain_only=True)
        assert len(element.get("value", "")) <= 2000, f"{where}: value too long"
        assert len(element.get("url", "")) <= 3000, f"{where}: url too long"
        assert element.get("style") in {None, "primary", "danger"}, where
        if "accessibility_label" in element:
            label = element["accessibility_label"]
            assert isinstance(label, str) and 0 < len(label) <= 75, f"{where}: accessibility_label"
        if "confirm" in element:
            confirm = element["confirm"]
            check_text(confirm["title"], 100, f"{where}.confirm.title", plain_only=True)
            check_text(confirm["text"], 300, f"{where}.confirm.text")
            check_text(confirm["confirm"], 30, f"{where}.confirm.confirm", plain_only=True)
            check_text(confirm["deny"], 30, f"{where}.confirm.deny", plain_only=True)
    if kind == "datepicker" and "initial_date" in element:
        assert re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", element["initial_date"]), f"{where}: initial_date"
    if kind == "overflow":
        # Slack's reference says up to five; Slack has also rejected menus with fewer than two.
        assert 2 <= len(element["options"]) <= 5, f"{where}: an overflow menu has 2 to 5 options"
        for option in element["options"]:
            check_option(option, f"{where}.option")
        assert {"value", "style", "url"}.isdisjoint(element), f"{where}: overflow menus take options only"
    if kind in {"static_select", "multi_static_select"}:
        assert ("options" in element) != ("option_groups" in element), f"{where}: options xor option_groups"
        groups = element.get("option_groups") or [{"options": element["options"]}]
        assert len(groups) <= 100
        values = []
        for group in groups:
            if "label" in group:
                check_text(group["label"], 75, f"{where}.label", plain_only=True)
            assert 0 < len(group["options"]) <= 100, f"{where}: option count"
            for option in group["options"]:
                check_option(option, f"{where}.option")
                values.append(option["value"])
        for initial in element.get("initial_options", []) + (
            [element["initial_option"]] if "initial_option" in element else []
        ):
            assert initial in [o for g in groups for o in g["options"]], f"{where}: initial option not offered"
    if kind in {"radio_buttons", "checkboxes"}:
        assert 0 < len(element["options"]) <= 10, f"{where}: {kind} take 1 to 10 options"
        for option in element["options"]:
            check_option(option, f"{where}.option")
        initial = element.get("initial_options", []) + (
            [element["initial_option"]] if "initial_option" in element else []
        )
        assert kind == "checkboxes" or "initial_options" not in element, f"{where}: radio buttons take one"
        for chosen in initial:
            assert chosen in element["options"], f"{where}: initial option not offered"
    if kind == "plain_text_input":
        assert 1 <= element.get("max_length", 3000) <= 3000
        assert len(element.get("initial_value", "")) <= element.get("max_length", 3000)
    if kind == "timepicker" and "initial_time" in element:
        assert re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", element["initial_time"]), where
    if kind == "number_input":
        assert isinstance(element["is_decimal_allowed"], bool)


def check_blocks(blocks: Any, surface: str = "message") -> None:
    assert isinstance(blocks, list) and blocks, "blocks must be a non-empty list"
    assert len(blocks) <= MAX_BLOCKS[surface], f"{len(blocks)} blocks on a {surface}"
    block_ids: set[str] = set()
    for index, block in enumerate(blocks):
        where = f"block {index} ({block.get('type')})"
        if "block_id" in block:
            assert len(block["block_id"]) <= 255 and block["block_id"] not in block_ids, f"{where}: block_id"
            block_ids.add(block["block_id"])
        kind = block["type"]
        action_ids: set[str] = set()
        if kind == "header":
            check_text(block["text"], 150, where, plain_only=True)
        elif kind == "section":
            assert "text" in block or "fields" in block, where
            if "text" in block:
                check_text(block["text"], 3000, where)
            for field in block.get("fields", []):
                check_text(field, 2000, f"{where}.field")
            assert len(block.get("fields", [])) <= 10, where
            if "accessory" in block:
                if block["accessory"].get("type") == "image":
                    check_image(block["accessory"], f"{where}.accessory")
                else:
                    check_element(block["accessory"], f"{where}.accessory", action_ids)
        elif kind == "context":
            assert 0 < len(block["elements"]) <= 10, where
            for element in block["elements"]:
                if element.get("type") == "image":
                    check_image(element, f"{where}.element")
                else:
                    check_text(element, 3000, f"{where}.element")
        elif kind == "actions":
            assert 0 < len(block["elements"]) <= 25, where
            for element in block["elements"]:
                check_element(element, where, action_ids)
        elif kind == "input":
            assert surface != "message", "input blocks only in views here"
            check_text(block["label"], 2000, f"{where}.label", plain_only=True)
            if "hint" in block:
                check_text(block["hint"], 2000, f"{where}.hint", plain_only=True)
            check_element(block["element"], where, action_ids)
        elif kind == "image":
            check_image(block, where)
        elif kind == "divider":
            pass
        else:
            raise AssertionError(f"{where}: unexpected block type")


def check_message(payload: dict[str, Any]) -> None:
    assert isinstance(payload.get("text"), str) and payload["text"], "messages need fallback text"
    assert len(payload["text"]) <= 4000
    check_mrkdwn(payload["text"], "fallback text")
    if "blocks" in payload:  # a short reply can be text alone
        check_blocks(payload["blocks"])


def check_view(view: dict[str, Any]) -> None:
    kind = view["type"]
    assert kind in {"modal", "home"}
    check_blocks(view["blocks"], kind)
    if kind == "modal":
        for key in ("title", "submit", "close"):
            check_text(view[key], 24, key, plain_only=True)
        assert len(view.get("private_metadata", "")) <= 3000
        assert len(view.get("callback_id", "")) <= 255
