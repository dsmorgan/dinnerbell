# SPDX-License-Identifier: AGPL-3.0-or-later
"""The catalog file format, version 1 (docs/catalog-format.md).

The pydantic models here are the format's single source of truth: they validate imports and
generate ``seed/catalog.schema.json``. Parsing never raises on bad input; it returns every
problem with its location so a person, script, or chatbot can fix the whole file at once.
"""

from __future__ import annotations

import json
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from pydantic.json_schema import WithJsonSchema
from pydantic_core import ErrorDetails

from dinnerbell.domain.catalog import (
    MAX_ITEM_NAME_LENGTH,
    MAX_NOTES_LENGTH,
    MAX_SERVINGS,
    MAX_TAG_NAME_LENGTH,
    MAX_URL_LENGTH,
    ItemKind,
    normalize_item_name,
    normalize_notes,
    normalize_tag_name,
    normalize_url,
)
from dinnerbell.domain.members import InvalidMemberInput, normalize_email

FORMAT_VERSION = 1
MAX_FILE_BYTES = 1_000_000
MAX_DEPTH = 20
"""Real catalog files nest about 5 levels deep."""
MAX_NODES = 50_000
SCHEMA_TITLE = "Dinnerbell catalog file, version 1"
URL_PATTERN = r"^[Hh][Tt][Tt][Pp][Ss]?://\S+$"
SECTIONS = {"restaurants": ItemKind.RESTAURANT, "home_meals": ItemKind.HOME}
TOP_LEVEL_FIELDS = ("version", "tags", *SECTIONS)


def _cook_email(value: str) -> str:
    try:
        return normalize_email(value)
    except InvalidMemberInput:
        raise ValueError(f'"{value}" is not an email address') from None


TagName = Annotated[
    str,
    AfterValidator(normalize_tag_name),
    WithJsonSchema({"type": "string", "minLength": 1, "maxLength": MAX_TAG_NAME_LENGTH}),
]
TagList = list[TagName]
ItemName = Annotated[
    str,
    AfterValidator(normalize_item_name),
    WithJsonSchema({"type": "string", "minLength": 1, "maxLength": MAX_ITEM_NAME_LENGTH}),
]
WebURL = Annotated[
    str,
    AfterValidator(normalize_url),
    WithJsonSchema({"type": "string", "format": "uri", "pattern": URL_PATTERN, "maxLength": MAX_URL_LENGTH}),
]
Notes = Annotated[
    str,
    AfterValidator(normalize_notes),
    WithJsonSchema({"type": "string", "maxLength": MAX_NOTES_LENGTH}),
]
CookEmail = Annotated[str, AfterValidator(_cook_email), WithJsonSchema({"type": "string", "format": "email"})]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, coerce_numbers_to_str=True, frozen=True)


class TagSection(_Strict):
    """Tags to create, grouped by kind. Tag names are unique across kinds, ignoring case."""

    cuisine: TagList | None = None
    protein: TagList | None = None
    dietary: TagList | None = None
    other: TagList | None = None


class RestaurantEntry(_Strict):
    """A place the household orders from or picks up at."""

    name: ItemName = Field(description="Display name. Unique among restaurants, ignoring case.")
    cuisine: TagList = Field(min_length=1, description="One or more cuisine tags. Variety tracking uses these.")
    tags: TagList | None = Field(default=None, description="Any other tags: protein, dietary, or other.")
    modes: list[Literal["pickup", "delivery"]] = Field(
        default=["pickup"], min_length=1, description="pickup, delivery, or both."
    )
    url: WebURL | None = Field(default=None, description="Menu or website link.")
    order_url: WebURL | None = Field(default=None, description="Link to order online, such as a delivery app page.")
    notes: Notes | None = Field(default=None, description=f"Free text up to {MAX_NOTES_LENGTH} characters.")
    active: bool = Field(default=True, description="false hides the item from proposals without deleting its history.")


class HomeMealEntry(_Strict):
    """A meal someone in the household makes."""

    name: ItemName = Field(description="What's being made. Unique among home meals, ignoring case.")
    cuisine: TagList = Field(min_length=1, description="One or more cuisine tags.")
    tags: TagList | None = Field(default=None, description="Any other tags.")
    servings: int | None = Field(
        default=None, ge=1, le=MAX_SERVINGS, description="Typical number of servings one batch makes."
    )
    cooks: list[CookEmail] | None = Field(
        default=None, description="Email addresses of members who can make it. Unknown emails are skipped."
    )
    url: WebURL | None = Field(default=None, description="Recipe link.")
    notes: Notes | None = Field(default=None, description=f"Free text up to {MAX_NOTES_LENGTH} characters.")
    active: bool = Field(default=True, description="false hides the item from proposals without deleting its history.")


Entry = RestaurantEntry | HomeMealEntry


class CatalogFile(_Strict):
    """Tags, restaurants, and home meals to import into Dinnerbell. See docs/catalog-format.md."""

    version: Literal[1] = Field(description="Format version. Always 1 for this format.")
    tags: TagSection | None = None
    restaurants: list[RestaurantEntry] | None = None
    home_meals: list[HomeMealEntry] | None = None


@dataclass(frozen=True, slots=True)
class FileError:
    """One problem in a catalog file. ``location`` reads like ``restaurants[3].modes``."""

    location: str
    message: str
    entry: str | None = None

    def __str__(self) -> str:
        return f"{self.location}: {self.message}" if self.location else self.message


@dataclass(frozen=True, slots=True)
class LocatedEntry:
    location: str
    entry: Entry

    @property
    def kind(self) -> ItemKind:
        return ItemKind.RESTAURANT if isinstance(self.entry, RestaurantEntry) else ItemKind.HOME


@dataclass(frozen=True, slots=True)
class ParsedCatalog:
    """The parts of a file that validated, in file order, plus every problem found.

    Invalid entries are left out of ``entries``; any error makes the whole import fail.
    """

    tags: TagSection | None
    entries: tuple[LocatedEntry, ...]
    errors: tuple[FileError, ...]

    @classmethod
    def from_file(cls, file: CatalogFile) -> ParsedCatalog:
        entries = [LocatedEntry(f"restaurants[{i}]", e) for i, e in enumerate(file.restaurants or [])]
        entries += [LocatedEntry(f"home_meals[{i}]", e) for i, e in enumerate(file.home_meals or [])]
        return cls(tags=file.tags, entries=tuple(entries), errors=())


def _failed(error: FileError) -> ParsedCatalog:
    return ParsedCatalog(tags=None, entries=(), errors=(error,))


def parse_catalog(source: str | bytes) -> ParsedCatalog:
    """Parse and validate a catalog file. Never raises for bad input."""
    text = _decode(source)
    if isinstance(text, FileError):
        return _failed(text)
    try:
        raw = _load_yaml(text)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        location = f"line {mark.line + 1}" if mark else ""
        return _failed(FileError(location, exc.problem or exc.context or "the file isn't valid YAML"))
    except yaml.YAMLError as exc:
        return _failed(FileError("", f"The file isn't valid YAML: {exc}"))
    except RecursionError:  # a backstop; MAX_DEPTH stops nesting long before the stack runs out
        return _failed(FileError("", f"Values are nested more than {MAX_DEPTH} levels deep."))
    if raw is None:
        return _failed(FileError("", "The file is empty. Start it with version: 1."))
    if not isinstance(raw, dict):
        return _failed(FileError("", "The file must be a mapping that starts with version: 1, not a list or text."))
    return _validate(raw)


def _decode(source: str | bytes) -> str | FileError:
    data = source.encode() if isinstance(source, str) else source
    if len(data) > MAX_FILE_BYTES:
        return FileError("", "The file is larger than 1 MB. Split it into smaller files and import each one.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return FileError("", "The file isn't UTF-8 text. Save it as UTF-8 and try again.")
    return text.replace("\r\n", "\n")


class _Loader(yaml.SafeLoader):
    """``yaml.SafeLoader`` hardened for untrusted input.

    - No aliases: they can expand a small file exponentially ("billion laughs").
    - At most ``MAX_DEPTH`` levels of nesting, so deeply nested input can't exhaust the stack.
    - At most ``MAX_NODES`` values in total.
    - No duplicate keys: YAML would silently keep the last one, dropping entries.
    """

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0
        self._nodes = 0

    def _refuse(self, problem: str) -> yaml.composer.ComposerError:
        mark = self.peek_event().start_mark  # type: ignore[no-untyped-call]
        return yaml.composer.ComposerError(None, None, problem, mark)

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        if self.check_event(yaml.AliasEvent):
            raise self._refuse("anchors and aliases (*name) are not supported")
        self._nodes += 1
        if self._nodes > MAX_NODES:
            raise self._refuse(f"the file has more than {MAX_NODES:,} values")
        if self._depth >= MAX_DEPTH:
            raise self._refuse(f"values are nested more than {MAX_DEPTH} levels deep")
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if isinstance(key, Hashable) and key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f'"{key}" appears more than once in the same mapping', key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def _load_yaml(text: str) -> Any:
    loader = _Loader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _validate(raw: dict[Any, Any]) -> ParsedCatalog:
    errors = [FileError(str(key), "unknown field") for key in raw if key not in TOP_LEVEL_FIELDS]
    if "version" not in raw:
        errors.append(FileError("version", "required field is missing"))
    elif type(raw["version"]) is not int or raw["version"] != FORMAT_VERSION:
        errors.append(FileError("version", "must be 1 for this format"))
    tags = None
    if raw.get("tags") is not None:
        try:
            tags = TagSection.model_validate(raw["tags"])
        except ValidationError as exc:
            errors += _errors(exc, "tags", "")
    entries: list[LocatedEntry] = []
    for section in (key for key in raw if key in SECTIONS):
        items = raw[section]
        if items is None:
            continue
        if not isinstance(items, list):
            errors.append(FileError(section, "must be a list"))
            continue
        model = RestaurantEntry if SECTIONS[section] is ItemKind.RESTAURANT else HomeMealEntry
        for index, item in enumerate(items):
            location = f"{section}[{index}]"
            try:
                entries.append(LocatedEntry(location, model.model_validate(item)))
            except ValidationError as exc:
                errors += _errors(exc, location, f" for a {SECTIONS[section].label.lower()}", _entry_name(item))
    return ParsedCatalog(tags=tags, entries=tuple(entries), errors=tuple(errors))


def _entry_name(item: object) -> str | None:
    name = item.get("name") if isinstance(item, dict) else None
    return str(name) if isinstance(name, str | int | float) and not isinstance(name, bool) else None


def _location(prefix: str, loc: Iterable[int | str]) -> str:
    out = prefix
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def _errors(exc: ValidationError, prefix: str, unknown_suffix: str, entry: str | None = None) -> list[FileError]:
    return [
        FileError(_location(prefix, error["loc"]), _message(error, unknown_suffix), entry)
        for error in exc.errors(include_url=False)
    ]


def _shown(value: object) -> str:
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _message(error: ErrorDetails, unknown_suffix: str) -> str:
    kind = error["type"]
    ctx = error.get("ctx", {})
    simple = {
        "missing": "required field is missing",
        "list_type": "must be a list",
        "model_type": "must be a mapping of fields",
        "dict_type": "must be a mapping of fields",
        "bool_type": "must be true or false",
        "bool_parsing": "must be true or false",
        "int_type": "must be a whole number",
        "int_parsing": "must be a whole number",
        "int_from_float": "must be a whole number",
        "too_short": "needs at least one value",
    }
    if kind == "extra_forbidden":
        return f"unknown field{unknown_suffix}"
    if kind in simple:
        return simple[kind]
    if kind == "string_type":
        return "is empty" if error["input"] is None else "must be text"
    if kind == "literal_error":
        allowed = str(ctx.get("expected", "")).replace("'", "").replace(" or ", ", ")
        return f'"{_shown(error["input"])}" is not one of {allowed}'
    if kind == "greater_than_equal":
        return f"must be at least {ctx['ge']}"
    if kind == "less_than_equal":
        return f"must be at most {ctx['le']}"
    return str(error["msg"]).removeprefix("Value error, ")


def catalog_json_schema() -> dict[str, Any]:
    schema = CatalogFile.model_json_schema()
    schema.pop("title", None)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": SCHEMA_TITLE, **schema}


def render_schema() -> str:
    """The exact text of ``seed/catalog.schema.json``."""
    return json.dumps(catalog_json_schema(), indent=2, ensure_ascii=False) + "\n"
