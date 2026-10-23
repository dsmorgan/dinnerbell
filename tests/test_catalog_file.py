# SPDX-License-Identifier: AGPL-3.0-or-later
"""The catalog file format (docs/catalog-format.md): parsing, validation rules, and the schema."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from dinnerbell.domain.catalog import ItemKind
from dinnerbell.domain.catalog_file import (
    MAX_DEPTH,
    MAX_FILE_BYTES,
    MAX_NODES,
    CatalogFile,
    FileError,
    HomeMealEntry,
    ParsedCatalog,
    RestaurantEntry,
    catalog_json_schema,
    parse_catalog,
    render_schema,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "seed" / "example.yaml"
SCHEMA = ROOT / "seed" / "catalog.schema.json"


def errors(source: str | bytes) -> list[str]:
    return [str(error) for error in parse_catalog(source).errors]


def one_error(source: str | bytes) -> str:
    found = errors(source)
    assert len(found) == 1, found
    return found[0]


def restaurant(body: str) -> str:
    return f"version: 1\nrestaurants:\n  - name: Siam Garden\n    cuisine: [Thai]\n{body}"


def home(body: str) -> str:
    return f"version: 1\nhome_meals:\n  - name: Tacos\n    cuisine: [Mexican]\n{body}"


# Whole files ---------------------------------------------------------------------------


def test_example_file_parses_cleanly() -> None:
    parsed = parse_catalog(EXAMPLE.read_bytes())
    assert parsed.errors == ()
    assert parsed.tags is not None and "Thai" in (parsed.tags.cuisine or [])
    kinds = [entry.kind for entry in parsed.entries]
    assert kinds.count(ItemKind.RESTAURANT) == 4 and kinds.count(ItemKind.HOME) == 4
    assert parsed.entries[0].location == "restaurants[0]"
    assert parsed.entries[4].location == "home_meals[0]"


def test_doc_example_parses_cleanly() -> None:
    doc = (ROOT / "docs" / "catalog-format.md").read_text()
    block = doc.split("```yaml\nversion: 1", 1)[1].split("```", 1)[0]
    assert parse_catalog("version: 1" + block).errors == ()


def test_minimal_file() -> None:
    parsed = parse_catalog("version: 1\n")
    assert parsed.errors == () and parsed.entries == () and parsed.tags is None


def test_empty_sections_are_allowed() -> None:
    assert errors("version: 1\ntags:\nrestaurants:\nhome_meals:\n") == []
    assert errors("version: 1\ntags: {cuisine: []}\nrestaurants: []\n") == []


def test_entries_keep_file_order_across_sections() -> None:
    parsed = parse_catalog(
        "version: 1\nhome_meals:\n  - {name: A, cuisine: [X]}\nrestaurants:\n  - {name: B, cuisine: [X]}\n"
    )
    assert [e.location for e in parsed.entries] == ["home_meals[0]", "restaurants[0]"]


def test_byte_order_mark_and_crlf_are_accepted() -> None:
    assert errors(b"\xef\xbb\xbfversion: 1\r\nrestaurants:\r\n  - {name: A, cuisine: [Thai]}\r\n") == []


def test_from_file_wraps_a_model() -> None:
    model = CatalogFile(
        version=1,
        restaurants=[RestaurantEntry(name="A", cuisine=["Thai"])],
        home_meals=[HomeMealEntry(name="B", cuisine=["Thai"])],
    )
    parsed = ParsedCatalog.from_file(model)
    assert [e.location for e in parsed.entries] == ["restaurants[0]", "home_meals[0]"]
    assert parsed.errors == ()


# Input limits and YAML safety -------------------------------------------------------------


def test_file_too_large() -> None:
    big = "version: 1\n" + "#" * MAX_FILE_BYTES
    assert "larger than 1 MB" in one_error(big)


def test_not_utf8() -> None:
    assert "UTF-8" in one_error(b"version: 1\nrestaurants:\n  - name: Caf\xe9\n")


def test_empty_file() -> None:
    assert "empty" in one_error("   \n# just a comment\n")


def test_yaml_syntax_error_has_line() -> None:
    message = one_error("version: 1\nrestaurants:\n  - name: [unclosed\n")
    assert message.startswith("line ")


def test_top_level_must_be_a_mapping() -> None:
    assert "mapping" in one_error("- just\n- a list\n")


def test_aliases_are_refused() -> None:
    message = one_error("version: 1\ntags:\n  cuisine: &c [Thai]\n  other: *c\n")
    assert "aliases" in message


def quickly(source: str | bytes, seconds: float = 1.0) -> FileError:
    """Parse ``source`` and return its single error, failing if parsing takes too long."""
    started = time.perf_counter()
    found = parse_catalog(source).errors
    assert time.perf_counter() - started < seconds
    assert len(found) == 1, found
    return found[0]


def test_billion_laughs_is_refused_quickly() -> None:
    lines = ["version: 1", 'a0: &a0 ["x","x","x","x","x","x","x","x","x"]']
    lines += [f"a{i}: &a{i} [*a{i - 1},*a{i - 1},*a{i - 1},*a{i - 1},*a{i - 1}]" for i in range(1, 10)]
    error = quickly("\n".join(lines))
    assert error.location == "line 3"
    assert "aliases" in error.message


def test_deep_nesting_is_refused_quickly() -> None:
    depth = 200_000  # far past the interpreter's recursion limit, well under 1 MB
    error = quickly("version: 1\nrestaurants: " + "[" * depth + "]" * depth + "\n")
    assert error.location == "line 2"
    assert error.message == f"values are nested more than {MAX_DEPTH} levels deep"


def test_nesting_at_the_limit_is_still_parsed() -> None:
    nested = "[" * (MAX_DEPTH - 1) + "]" * (MAX_DEPTH - 1)
    assert errors(f"version: 1\nrestaurants: {nested}\n") != []  # invalid content, but no depth error
    assert all("nested" not in e for e in errors(f"version: 1\nrestaurants: {nested}\n"))


def test_recursion_error_never_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    import dinnerbell.domain.catalog_file as catalog_file

    def explode(text: str) -> object:
        raise RecursionError

    monkeypatch.setattr(catalog_file, "_load_yaml", explode)
    assert "nested more than" in one_error("version: 1\n")


def test_too_many_values_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Count composed values instead of timing the parse, so a slow CI runner can't make this flaky.
    # The file has twice the cap; composing must stop just past the cap, not run to the end.
    import dinnerbell.domain.catalog_file as catalog_file

    composed = 0
    original = catalog_file._Loader.compose_node

    def counting(self: catalog_file._Loader, parent: object, index: object) -> object:
        nonlocal composed
        composed += 1
        return original(self, parent, index)  # type: ignore[arg-type]

    monkeypatch.setattr(catalog_file._Loader, "compose_node", counting)
    found = parse_catalog("version: 1\ntags:\n  other: [" + ",".join(["x"] * (2 * MAX_NODES)) + "]\n").errors
    assert len(found) == 1, found
    assert found[0].location == "line 3"
    assert "more than 50,000 values" in found[0].message
    assert composed <= MAX_NODES + 10


def test_python_tags_are_refused() -> None:
    assert errors("version: 1\nnotes: !!python/object/apply:os.system ['true']\n")


def test_duplicate_keys_are_refused() -> None:
    message = one_error("version: 1\nrestaurants: []\nrestaurants: []\n")
    assert "restaurants" in message and "more than once" in message


# Top level -------------------------------------------------------------------------------


def test_version_is_required() -> None:
    assert one_error("restaurants: []\n") == "version: required field is missing"


@pytest.mark.parametrize("value", ["2", "'1'", "one"])
def test_version_must_be_1(value: str) -> None:
    assert one_error(f"version: {value}\n") == "version: must be 1 for this format"


def test_unknown_top_level_field() -> None:
    assert one_error("version: 1\nrecipes: []\n") == "recipes: unknown field"


def test_sections_must_be_lists() -> None:
    assert one_error("version: 1\nrestaurants: {name: A}\n") == "restaurants: must be a list"


def test_entry_must_be_a_mapping() -> None:
    assert one_error("version: 1\nhome_meals:\n  - just a name\n") == "home_meals[0]: must be a mapping of fields"


# Tags section ---------------------------------------------------------------------------------


def test_unknown_tag_kind() -> None:
    assert one_error("version: 1\ntags:\n  allergens: [nuts]\n") == "tags.allergens: unknown field"


def test_tag_names_validated() -> None:
    assert one_error("version: 1\ntags:\n  other: ['  ']\n") == "tags.other[0]: Enter a tag name."
    assert "at most 40" in one_error(f"version: 1\ntags:\n  other: [{'x' * 41}]\n")


def test_numbers_are_accepted_as_names() -> None:
    parsed = parse_catalog("version: 1\nrestaurants:\n  - {name: 1984, cuisine: [Thai]}\n")
    assert parsed.errors == ()
    assert parsed.entries[0].entry.name == "1984"


# Entries -----------------------------------------------------------------------------------


def test_name_required() -> None:
    assert (
        one_error("version: 1\nrestaurants:\n  - cuisine: [Thai]\n") == "restaurants[0].name: required field is missing"
    )


def test_name_blank_or_null() -> None:
    assert one_error("version: 1\nrestaurants:\n  - {name: '   ', cuisine: [Thai]}\n") == (
        "restaurants[0].name: Enter a name."
    )
    assert one_error("version: 1\nrestaurants:\n  - {name: null, cuisine: [Thai]}\n") == (
        "restaurants[0].name: is empty"
    )


def test_name_too_long() -> None:
    assert "at most 100" in one_error(f"version: 1\nrestaurants:\n  - {{name: {'x' * 101}, cuisine: [Thai]}}\n")


def test_cuisine_required() -> None:
    assert one_error("version: 1\nhome_meals:\n  - name: Tacos\n") == "home_meals[0].cuisine: required field is missing"


def test_cuisine_needs_a_value() -> None:
    assert one_error("version: 1\nhome_meals:\n  - {name: Tacos, cuisine: []}\n") == (
        "home_meals[0].cuisine: needs at least one value"
    )


def test_cuisine_must_be_a_list() -> None:
    assert one_error("version: 1\nhome_meals:\n  - {name: Tacos, cuisine: Mexican}\n") == (
        "home_meals[0].cuisine: must be a list"
    )


def test_modes_default_to_pickup() -> None:
    entry = parse_catalog(restaurant("")).entries[0].entry
    assert isinstance(entry, RestaurantEntry)
    assert entry.modes == ["pickup"]
    assert "modes" not in entry.model_fields_set


def test_modes_reject_unknown_values() -> None:
    assert one_error(restaurant("    modes: [pickup, drive-thru]\n")) == (
        'restaurants[0].modes[1]: "drive-thru" is not one of pickup, delivery'
    )


def test_modes_cannot_be_empty() -> None:
    assert one_error(restaurant("    modes: []\n")) == "restaurants[0].modes: needs at least one value"


def test_cook_is_not_a_restaurant_mode() -> None:
    assert "is not one of pickup, delivery" in one_error(restaurant("    modes: [cook]\n"))


@pytest.mark.parametrize("field", ["modes: [pickup]", "order_url: https://example.com"])
def test_restaurant_only_fields_are_refused_on_home_meals(field: str) -> None:
    name = field.split(":")[0]
    assert one_error(home(f"    {field}\n")) == f"home_meals[0].{name}: unknown field for a home meal"


@pytest.mark.parametrize("field", ["servings: 4", "cooks: [a@example.com]"])
def test_home_only_fields_are_refused_on_restaurants(field: str) -> None:
    name = field.split(":")[0]
    assert one_error(restaurant(f"    {field}\n")) == f"restaurants[0].{name}: unknown field for a restaurant"


def test_misspelled_field() -> None:
    assert one_error(restaurant("    mode: [pickup]\n")) == "restaurants[0].mode: unknown field for a restaurant"


@pytest.mark.parametrize(
    "url", ["ftp://example.com/menu", "javascript:alert(1)", "example.com/menu", "https://", "https://a b.com"]
)
def test_urls_must_be_http_or_https(url: str) -> None:
    message = one_error(restaurant(f"    url: '{url}'\n"))
    assert message.startswith("restaurants[0].url: ")


def test_order_url_validated_too() -> None:
    assert one_error(restaurant("    order_url: 'mailto:a@example.com'\n")) == (
        "restaurants[0].order_url: A link must start with http:// or https://."
    )


def test_uppercase_scheme_is_accepted() -> None:
    assert errors(restaurant("    url: HTTPS://example.com/menu\n")) == []


def test_notes_length() -> None:
    assert errors(restaurant(f"    notes: {'x' * 500}\n")) == []
    assert (
        one_error(restaurant(f"    notes: {'x' * 501}\n")) == "restaurants[0].notes: Notes are at most 500 characters."
    )


def test_servings_must_be_positive_whole_number() -> None:
    assert one_error(home("    servings: 0\n")) == "home_meals[0].servings: must be at least 1"
    assert one_error(home("    servings: 100\n")) == "home_meals[0].servings: must be at most 99"
    assert one_error(home("    servings: lots\n")) == "home_meals[0].servings: must be a whole number"
    assert one_error(home("    servings: 2.5\n")) == "home_meals[0].servings: must be a whole number"


def test_cooks_must_be_emails() -> None:
    assert one_error(home("    cooks: [david]\n")) == 'home_meals[0].cooks[0]: "david" is not an email address'


def test_active_must_be_boolean() -> None:
    assert one_error(restaurant("    active: sometimes\n")) == "restaurants[0].active: must be true or false"
    entry = parse_catalog(restaurant("    active: false\n")).entries[0].entry
    assert entry.active is False


def test_repeated_tags_keep_their_positions() -> None:
    """Repeats stay in the list so later error locations, such as tags[2], stay accurate."""
    entry = parse_catalog("version: 1\nrestaurants:\n  - {name: A, cuisine: [Thai, thai, ' THAI ']}\n").entries[0]
    assert entry.entry.cuisine == ["Thai", "thai", "THAI"]


def test_every_error_is_reported_at_once() -> None:
    source = (
        "version: 1\n"
        "restaurants:\n"
        "  - {name: A, cuisine: [Thai], modes: [boat]}\n"
        "  - {name: B, cuisine: [Thai]}\n"
        "  - {cuisine: [Thai], url: 'ftp://x'}\n"
        "home_meals:\n"
        "  - {name: C, cuisine: [Thai], servings: 0}\n"
    )
    parsed = parse_catalog(source)
    locations = [error.location for error in parsed.errors]
    assert locations == [
        "restaurants[0].modes[0]",
        "restaurants[2].name",
        "restaurants[2].url",
        "home_meals[0].servings",
    ]
    assert [e.location for e in parsed.entries] == ["restaurants[1]"]  # only the valid entry is kept


def test_errors_name_the_entry() -> None:
    [error] = parse_catalog(restaurant("    modes: [boat]\n")).errors
    assert error.entry == "Siam Garden"


# JSON Schema ----------------------------------------------------------------------------------


def test_committed_schema_is_current() -> None:
    """Regenerate with: uv run dinnerbell catalog schema > seed/catalog.schema.json"""
    assert SCHEMA.read_text() == render_schema(), "seed/catalog.schema.json is stale; regenerate it"


def test_schema_describes_the_rules() -> None:
    schema = catalog_json_schema()
    assert schema["required"] == ["version"]
    assert schema["properties"]["version"]["const"] == 1
    assert schema["additionalProperties"] is False
    restaurant_schema = schema["$defs"]["RestaurantEntry"]
    assert set(restaurant_schema["required"]) == {"name", "cuisine"}
    assert restaurant_schema["properties"]["modes"]["items"]["enum"] == ["pickup", "delivery"]
    assert restaurant_schema["properties"]["modes"]["default"] == ["pickup"]
    assert restaurant_schema["additionalProperties"] is False
    home_schema = schema["$defs"]["HomeMealEntry"]
    assert "order_url" not in home_schema["properties"] and "modes" not in home_schema["properties"]
    assert home_schema["properties"]["notes"]["anyOf"][0]["maxLength"] == 500
    url = restaurant_schema["properties"]["url"]["anyOf"][0]
    assert url["pattern"].startswith("^[Hh][Tt][Tt][Pp]")


def test_schema_is_valid_json_with_metadata() -> None:
    schema = json.loads(render_schema())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "Dinnerbell catalog file, version 1"


def test_long_bad_cook_email_is_shortened_in_the_message() -> None:
    message = one_error(home(f"    cooks: [{'x' * 300}]\n"))
    assert message.startswith('home_meals[0].cooks[0]: "xxx') and '..." is not an email address' in message
    assert len(message) < 120
