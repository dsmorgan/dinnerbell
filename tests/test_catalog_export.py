# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catalog export (issue #27): the catalog as a catalog file, from the web and the CLI.

An export must import cleanly: into an empty database it recreates the same catalog, byte for
byte on the next export, and into its own source it changes nothing. It never holds personal data.
"""

# ruff: noqa: S311

from __future__ import annotations

import asyncio
import hashlib
import io
import random
import re
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from dinnerbell import main as cli
from dinnerbell.config import Settings
from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.engine import read_snapshot
from dinnerbell.db.meals import MealLogRepository
from dinnerbell.db.migrate import run_migrations
from dinnerbell.db.models import CatalogItemCookRow, CatalogItemRow, TagRow
from dinnerbell.db.preferences import PreferenceRepository
from dinnerbell.db.repositories import SessionRepository
from dinnerbell.domain.catalog import (
    CatalogItem,
    InvalidCatalogInput,
    ItemKind,
    Mode,
    Tag,
    TagKind,
    normalize_item_name,
    normalize_notes,
    normalize_tag_name,
)
from dinnerbell.domain.catalog_export import CatalogExport, export_catalog
from dinnerbell.domain.catalog_file import HomeMealEntry, RestaurantEntry, _Loader, parse_catalog
from dinnerbell.domain.catalog_import import ImportPlan
from dinnerbell.domain.meals import MemberRef, Slot, TagRef
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.common import audit
from dinnerbell.services.database import Database, open_database
from tests.conftest import ADMIN_EMAIL, SECRET_KEY, invite, make_settings, page, sign_in
from tests.support.calls import furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.slack import SLACK_IDS, link
from tests.support.web import add_item, submit
from tests.test_admin import sign_in_as
from tests.test_config import REQUIRED

EXAMPLE = (Path(__file__).resolve().parents[1] / "seed" / "example.yaml").read_bytes()
EDGES = """\
version: 1
restaurants:
  - name: "yes"
    cuisine: [Café]
    tags: ["spicy: very", "#1"]
    modes: [delivery]
    order_url: https://order.example.com/a?b=c&d=e#f
    notes: |-
      Line one.
        Indented line, then a tab:\tthere.
      Ünïcödé, 中文, and an emoji 🍜.
  - name: "123"
    cuisine: [thai]
    active: false
    notes: "trailing spaces on a line  \\nnext line"
home_meals:
  - name: "- dash: and 'quotes' \\"both\\""
    cuisine: [Mexican]
    servings: 4
    cooks: [admin@example.com]
    notes: null
    url: https://example.com/recipe
"""


# The format ---------------------------------------------------------------------------------------


def tag(tag_id: int, kind: TagKind, name: str) -> Tag:
    return Tag(id=tag_id, household_id=1, kind=kind, name=name)


TAGS = [
    tag(1, TagKind.CUISINE, "Thai"),
    tag(2, TagKind.OTHER, "spicy"),
    tag(3, TagKind.PROTEIN, "chicken"),
    tag(4, TagKind.CUISINE, "Lao"),
    tag(5, TagKind.DIETARY, "nut-free"),
]
ITEMS = [
    CatalogItem(
        id=1,
        household_id=1,
        kind=ItemKind.RESTAURANT,
        name="Siam Garden",
        modes=frozenset({Mode.DELIVERY, Mode.PICKUP}),
        tag_ids=frozenset({2, 1, 4}),
        cook_ids=frozenset(),
        url="https://example.com/menu",
        notes="Ask for medium.\nPad see ew is safe.",
    ),
    CatalogItem(
        id=2,
        household_id=1,
        kind=ItemKind.HOME,
        name="chicken larb",
        modes=frozenset({Mode.COOK}),
        tag_ids=frozenset({4, 3}),
        cook_ids=frozenset({7}),
        default_servings=4,
        active=False,
    ),
]
EXPECTED = """\
# A Dinnerbell catalog file. The format is described in docs/catalog-format.md in the Dinnerbell source.
# Cooks aren't exported: an import keeps the cooks each item already has.
version: 1
tags:
  cuisine: [Lao, Thai]
  protein: [chicken]
  dietary: [nut-free]
  other: [spicy]
restaurants:
  - name: Siam Garden
    cuisine: [Lao, Thai]
    tags: [spicy]
    modes: [pickup, delivery]
    url: https://example.com/menu
    order_url: null
    notes: |-
      Ask for medium.
      Pad see ew is safe.
    active: true
home_meals:
  - name: chicken larb
    cuisine: [Lao]
    tags: [chicken]
    servings: 4
    url: null
    notes: null
    active: false
"""


def test_the_layout_matches_the_docs() -> None:
    """Every field written, tags and items sorted by name, lists inline, no cooks."""
    exported = export_catalog(TAGS, ITEMS)
    assert exported.text == EXPECTED
    assert (exported.restaurants, exported.home_meals, exported.tags) == (1, 1, 5)


def test_input_order_doesnt_change_the_output() -> None:
    shuffled = random.Random(4).sample
    assert export_catalog(shuffled(TAGS, len(TAGS)), shuffled(ITEMS, len(ITEMS))).text == EXPECTED


def test_an_empty_catalog_is_a_valid_file() -> None:
    exported = export_catalog([], [])
    assert "restaurants: []\nhome_meals: []\n" in exported.text
    parsed = parse_catalog(exported.data)
    assert parsed.errors == () and parsed.entries == ()


def test_the_output_is_plain_yaml_the_hardened_loader_accepts() -> None:
    """No anchors, aliases, or tags, even when items share every value."""
    twin = CatalogItem(**{**{f: getattr(ITEMS[0], f) for f in CatalogItem.__slots__}, "id": 3, "name": "Siam Too"})
    text = export_catalog(TAGS, [ITEMS[0], twin]).text
    assert not re.search(r"[&*]id\d|!!|!<", text)
    assert yaml.safe_load(text)["restaurants"][1]["name"] == "Siam Too"
    assert parse_catalog(text).errors == ()


def test_a_link_to_an_unknown_tag_is_skipped() -> None:
    """Reads happen in one snapshot, so this can't happen; if it did, export still works."""
    dangling = CatalogItem(
        **{**{f: getattr(ITEMS[1], f) for f in CatalogItem.__slots__}, "tag_ids": frozenset({4, 99})}
    )
    text = export_catalog(TAGS, [ITEMS[0], dangling]).text
    assert "  - name: chicken larb\n    cuisine: [Lao]\n    tags: []\n" in text


def test_values_are_counted_as_import_counts_them() -> None:
    exported = export_catalog(TAGS, ITEMS)
    loader = _Loader(exported.text)
    try:
        loader.get_single_data()
    finally:
        loader.dispose()
    assert exported.values == loader._nodes
    assert exported.too_big_to_import is None


def _many(count: int, notes: str | None) -> list[CatalogItem]:
    return [
        CatalogItem(
            id=n,
            household_id=1,
            kind=ItemKind.RESTAURANT,
            name=f"Place {n:05}",
            modes=frozenset({Mode.PICKUP}),
            tag_ids=frozenset({1}),
            cook_ids=frozenset(),
            notes=notes,
        )
        for n in range(count)
    ]


def test_an_export_over_the_import_limits_says_so() -> None:
    big = export_catalog(TAGS, _many(2_100, "n" * 480))
    assert len(big.data) > 1_000_000 and big.values < 50_000
    assert (
        big.too_big_to_import
        == "This export is over import's limit of 1 MB. To import it, split it into smaller files."
    )
    assert parse_catalog(big.data).errors[0].message.startswith("The file is larger than 1 MB")

    many = export_catalog(TAGS, _many(2_700, None))
    assert len(many.data) < 1_000_000 and many.values > 50_000
    assert many.too_big_to_import == (
        "This export is over import's limit of 50,000 values. To import it, split it into smaller files."
    )
    assert "more than 50,000 values" in parse_catalog(many.data).errors[0].message


ALPHABET = [*"abcXYZ019 -_:#'\"[]{},&*!|>%@`?\t\n\\", "é", "中", "🍜", "\xa0", "\u2028", "ß", "İ"]


def _random_text(rng: random.Random, longest: int) -> str:
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, longest)))


def _valid(rng: random.Random, normalize: Callable[[str], str | None], longest: int) -> str:
    while True:
        try:
            value = normalize(_random_text(rng, longest))
        except InvalidCatalogInput:
            continue
        if value:
            return str(value)


@pytest.mark.parametrize("seed", range(200))
def test_any_valid_catalog_survives_export_and_parse(seed: int) -> None:
    """Names, tags, and notes with YAML's special characters come back exactly as stored."""
    rng = random.Random(seed)
    tags: dict[str, Tag] = {}
    for kind in (TagKind.CUISINE, TagKind.OTHER, TagKind.CUISINE):
        name = _valid(rng, normalize_tag_name, 12)
        tags.setdefault(name.casefold(), tag(len(tags) + 1, kind, name))
    cuisine = next(t for t in tags.values() if t.kind is TagKind.CUISINE)
    items = []
    for item_id, kind in enumerate((ItemKind.RESTAURANT, ItemKind.HOME), start=1):
        items.append(
            CatalogItem(
                id=item_id,
                household_id=1,
                kind=kind,
                name=_valid(rng, normalize_item_name, 30),
                modes=frozenset({Mode.PICKUP} if kind is ItemKind.RESTAURANT else {Mode.COOK}),
                tag_ids=frozenset(t.id for t in tags.values()),
                cook_ids=frozenset(),
                notes=_valid(rng, normalize_notes, 60),
            )
        )
    parsed = parse_catalog(export_catalog(tags.values(), items).data)
    assert parsed.errors == ()
    assert parsed.tags is not None
    assert sorted([*(parsed.tags.cuisine or []), *(parsed.tags.other or [])]) == sorted(t.name for t in tags.values())
    by_name = {entry.entry.name: entry.entry for entry in parsed.entries}
    for item in items:
        entry = by_name[item.name]
        assert isinstance(entry, RestaurantEntry if item.kind is ItemKind.RESTAURANT else HomeMealEntry)
        assert entry.notes == item.notes
        assert cuisine.name in entry.cuisine


# Round trips --------------------------------------------------------------------------------------


@pytest.fixture
async def databases(tmp_path: Path, clock: FakeClock) -> AsyncIterator[dict[str, Database]]:
    """Two migrated databases, each with a household and only the admin as a member."""
    opened: dict[str, Database] = {}
    for name in ("source", "copy"):
        settings = make_settings(tmp_path / name)
        run_migrations(settings.sync_database_url, settings.database_path)
        opened[name] = open_database(settings.async_database_url)
        async with opened[name].sessionmaker() as db:
            await bootstrap(db, household_name="Home", timezone="UTC", admin_email=ADMIN_EMAIL, now=clock())
            await db.commit()
    yield opened
    for database in opened.values():
        await database.close()


async def apply(database: Database, source: str | bytes, clock: FakeClock) -> ImportPlan:
    async with database.sessionmaker() as db:
        return await catalog_service.apply_import(db, 1, None, source, now=clock())


async def preview(database: Database, source: str | bytes) -> ImportPlan:
    async with database.sessionmaker() as db:
        return await catalog_service.preview_import(db, 1, source)


async def export(database: Database) -> CatalogExport:
    async with database.sessionmaker() as db:
        return await catalog_service.export(db, 1)


@pytest.mark.anyio
async def test_seed_export_import_export_is_byte_identical(databases: dict[str, Database], clock: FakeClock) -> None:
    source, copy = databases["source"], databases["copy"]
    await apply(source, EXAMPLE, clock)
    await apply(source, EDGES, clock)
    first = await export(source)
    assert (first.restaurants, first.home_meals) == (6, 5)

    created = await apply(copy, first.data, clock)
    assert created.summary()["items_created"] == 11 and created.unknown_cooks == ()
    assert (await export(copy)).data == first.data

    for database in (source, copy):
        plan = await preview(database, first.data)
        assert plan.ok and not plan.has_changes, plan.summary()
        assert len(plan.unchanged) == 11


@pytest.mark.anyio
async def test_importing_an_export_restores_what_it_says_and_deletes_nothing(
    databases: dict[str, Database], clock: FakeClock
) -> None:
    """Every field in the export replaces what's stored, so edits made since are undone, cleared
    fields come back, and items added since stay. Cooks aren't in the file, so they stay too."""
    source = databases["source"]
    await apply(source, EXAMPLE, clock)
    await apply(source, EDGES, clock)
    saved = await export(source)
    await apply(
        source,
        "version: 1\nrestaurants:\n"
        "  - {name: 'yes', cuisine: [Café], tags: [], modes: [pickup], order_url: null, notes: null, active: false}\n"
        "  - {name: Added Since, cuisine: [Thai]}\n",
        clock,
    )

    plan = await preview(source, saved.data)
    assert plan.ok and plan.items_to_create == () and plan.tags_to_create == ()
    [update] = plan.items_to_update
    assert update.state.name == "yes"
    assert {change.field for change in update.changes} == {"tags", "modes", "order_url", "notes", "active"}
    await apply(source, saved.data, clock)

    assert "  - name: Added Since\n" in (await export(source)).text
    async with source.sessionmaker() as db:
        cooks = list(await db.scalars(select(CatalogItemCookRow.item_id)))
        dish = await db.scalar(select(CatalogItemRow.id).where(CatalogItemRow.name.startswith("- dash")))
    assert cooks == [dish]  # the admin cooks it, from EDGES; the export didn't clear that


@pytest.mark.anyio
async def test_a_merge_during_an_export_doesnt_split_it(
    databases: dict[str, Database], clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another request merges "spicy" into "comfort" after the export read the tags but before it
    read the items. The export still sees the catalog from before the merge, whole."""
    source = databases["source"]
    await apply(source, EXAMPLE, clock)
    read_items = CatalogRepository.items

    async def merge_first(repo: CatalogRepository, household_id: int) -> list[CatalogItem]:
        async with source.sessionmaker() as other:
            ids = {t.name: t.id for t in await CatalogRepository(other).tags(1)}
            await CatalogRepository(other).move_tag_links(1, ids["spicy"], ids["comfort"])
            await CatalogRepository(other).delete_tag(1, ids["spicy"])
            await other.commit()
        return await read_items(repo, household_id)

    monkeypatch.setattr(CatalogRepository, "items", merge_first)
    during = await export(source)
    monkeypatch.setattr(CatalogRepository, "items", read_items)
    assert "  other: [comfort, healthy, quick, spicy]\n" in during.text
    assert "    tags: [gluten-free option, spicy, vegetarian]\n" in during.text
    assert parse_catalog(during.data).errors == ()

    after = (await export(source)).text
    assert "  other: [comfort, healthy, quick]\n" in after and "spicy" not in after


@pytest.mark.anyio
async def test_a_snapshot_refuses_a_session_with_unsaved_changes(
    databases: dict[str, Database], clock: FakeClock
) -> None:
    async with databases["source"].sessionmaker() as db:
        db.add(TagRow(household_id=1, kind="other", name="unsaved", name_key="unsaved", created_at=clock()))
        with pytest.raises(RuntimeError, match="no unsaved changes"):
            async with read_snapshot(db):
                pass


@pytest.mark.anyio
async def test_an_export_holds_no_personal_data(migrated: Settings, clock: FakeClock) -> None:
    """Members, emails, Slack IDs, preferences, vetoes, history, audit rows, sessions, and secrets
    are all in the database, and none of them reach the export."""
    database = open_database(migrated.async_database_url)
    try:
        house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
        zeb = await house.add_member("Zebulon", can_cook=True)
        for name, slack_id in (SLACK_IDS | {"Zebulon": "U0ZEBULON9"}).items():
            if name in house.people:
                await link(house, name, slack_id)
        siam = house.items["Siam Garden"]
        async with database.sessionmaker() as db:
            prefs = PreferenceRepository(db)
            await prefs.set(1, zeb.id, TargetType.ITEM, siam.id, Stance(veto=True), clock())
            await prefs.set(1, house["Sam"].id, TargetType.TAG, house.tags["Thai"].id, Stance(score=-2), clock())
            await MealLogRepository(db).add(
                household_id=1,
                day=clock().date(),
                slot=Slot.DINNER,
                item_id=siam.id,
                cuisines=(TagRef(house.tags["Thai"].id, "Thai"),),
                participants=(MemberRef(zeb.id, "Zebulon Participant"),),
                logged_by=zeb.id,
                now=clock(),
            )
            await audit(db, 1, zeb, "test.marker", "member:zeb", {"note": "AUDIT-PAYLOAD-MARKER"}, clock())
            await SessionRepository(db).add(
                token_hash="SESSION-HASH-MARKER", member=zeb, now=clock(), expires_at=clock()
            )
            await db.commit()
        text = (await export(database)).text
    finally:
        await database.close()

    assert "name: Sam's Tacos" in text  # the catalog is there; Sam is only in the dish's name
    forbidden = [
        "@",
        "cooks:",
        "Alex",
        "Kim",
        "Lee",
        "Zebulon",
        *SLACK_IDS.values(),
        "U0ZEBULON9",
        "veto",
        "AUDIT-PAYLOAD-MARKER",
        "SESSION-HASH-MARKER",
        SECRET_KEY,
        migrated.oidc_client_secret.get_secret_value(),
    ]
    assert [value for value in forbidden if value.casefold() in text.casefold()] == []
    document = yaml.safe_load(text)
    assert list(document) == ["version", "tags", "restaurants", "home_meals"]
    fields = {key for section in ("restaurants", "home_meals") for entry in document[section] for key in entry}
    assert fields == {"name", "cuisine", "tags", "modes", "servings", "url", "order_url", "notes", "active"}


# The web ------------------------------------------------------------------------------------------


def test_signed_out_visitors_cant_download(client: TestClient) -> None:
    response = client.get("/catalog/export.yaml")
    assert response.status_code == 303 and response.headers["location"] == "/"


def test_any_member_downloads_the_catalog(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    assert add_item(client, "Siam Garden", cuisines="Thai", notes="Medium spice.").status_code == 303
    assert invite(client, "kim@example.com", "Kim").status_code == 303
    sign_in_as(client, provider, "kim-sub", "kim@example.com")

    # No download attribute: Content-Disposition saves the file, and a sign-in page or error isn't saved.
    assert 'href="/catalog/export.yaml">Download catalog</a>' in page(client.get("/catalog"))
    response = client.get("/catalog/export.yaml")
    assert response.status_code == 200
    assert re.fullmatch(
        r'attachment; filename="dinnerbell-catalog-\d{4}-\d{2}-\d{2}\.yaml"', response.headers["content-disposition"]
    )
    assert response.headers["content-type"] == "application/yaml; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "  - name: Siam Garden\n" in response.text and "notes: Medium spice.\n" in response.text
    assert "@" not in response.text

    # Uploaded back on the import page, it previews as nothing to do.
    files = {"upload": ("catalog.yaml", response.content, "application/yaml")}
    preview = submit(client, "/catalog/import/preview", {"content": ""}, files=files)
    assert preview.status_code == 200
    assert "The catalog already matches this file" in page(preview)
    assert 'formaction="/catalog/import/apply"' not in page(preview)


# The command --------------------------------------------------------------------------------------


@pytest.fixture
def cli_env_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Configuration in the environment, for a database that doesn't exist yet. The command must
    neither migrate nor configure logging, which writes to stdout."""
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "data" / "dinnerbell.db"
    monkeypatch.setenv("DINNERBELL_DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setattr(cli, "run_migrations", lambda *args: pytest.fail("export migrated the database"))
    monkeypatch.setattr(cli, "configure_logging", lambda *args: pytest.fail("export configured logging"))
    return path


@pytest.fixture
def cli_db(cli_env_only: Path, clock: FakeClock) -> Path:
    """A database with the example catalog."""
    path = cli_env_only
    run_migrations(f"sqlite:///{path}", path)

    async def seed() -> None:
        database = open_database(f"sqlite+aiosqlite:///{path}")
        async with database.sessionmaker() as db:
            await bootstrap(db, household_name="Home", timezone="UTC", admin_email=ADMIN_EMAIL, now=clock())
            await db.commit()
        await apply(database, EXAMPLE, clock)
        await database.close()

    asyncio.run(seed())
    return path


def expected(path: Path) -> str:
    async def read() -> str:
        database = open_database(f"sqlite+aiosqlite:///{path}")
        try:
            return (await export(database)).text
        finally:
            await database.close()

    return asyncio.run(read())


def test_the_command_writes_to_stdout(cli_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["catalog", "export"]) == 0
    out, err = capsys.readouterr()
    assert out == expected(cli_db)
    assert err == "Exported 4 restaurants, 4 home meals, 25 tags.\n"


def test_the_command_reads_without_writing(cli_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Opened read-only: the database's bytes, journal mode, and directory are unchanged."""
    with sqlite3.connect(cli_db) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    digest = hashlib.sha256(cli_db.read_bytes()).hexdigest()
    before = sorted(p.name for p in cli_db.parent.iterdir())
    assert cli.main(["catalog", "export"]) == 0
    assert hashlib.sha256(cli_db.read_bytes()).hexdigest() == digest
    assert sorted(p.name for p in cli_db.parent.iterdir()) == before
    with sqlite3.connect(cli_db) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_the_command_writes_a_file_and_replaces_it_only_with_force(
    cli_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "catalog.yaml"
    assert cli.main(["catalog", "export", "--output", str(output)]) == 0
    assert output.read_text() == expected(cli_db)
    assert capsys.readouterr() == ("", f"Exported 4 restaurants, 4 home meals, 25 tags to {output}.\n")

    output.write_text("my edits\n")
    assert cli.main(["catalog", "export", "-o", str(output)]) == 1
    assert "already exists. Nothing exported. Choose another file, or add --force" in capsys.readouterr().err
    assert output.read_text() == "my edits\n"

    assert cli.main(["catalog", "export", "-o", str(output), "--force"]) == 0
    assert output.read_text() == expected(cli_db)

    assert cli.main(["catalog", "export", "-o", str(tmp_path / "missing" / "c.yaml")]) == 2
    assert "Can't write" in capsys.readouterr().err


def test_the_command_refuses_a_missing_unreadable_or_empty_database(
    cli_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_db.unlink()
    assert cli.main(["catalog", "export"]) == 2
    assert capsys.readouterr() == ("", f"No database at {cli_db}. Nothing exported.\n")
    assert not cli_db.exists()  # not created by asking

    cli_db.write_bytes(b"not a database, just some bytes " * 64)
    assert cli.main(["catalog", "export"]) == 2
    assert "Can't read the database" in capsys.readouterr().err

    cli_db.unlink()
    run_migrations(f"sqlite:///{cli_db}", cli_db)  # a schema, but no household yet
    assert cli.main(["catalog", "export"]) == 2
    assert "has no household yet" in capsys.readouterr().err


def test_the_command_needs_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["catalog", "export"]) == 2
    assert "DINNERBELL_" in capsys.readouterr().err


def test_a_wal_database_gets_private_sidecar_files_the_app_can_use(
    cli_db: Path, capsys: pytest.CaptureFixture[str], clock: FakeClock
) -> None:
    """With the app stopped, a WAL database has no -wal or -shm file. A read-only open creates
    them; they must be owner-only, and the app must still open and write the database after."""
    with closing(sqlite3.connect(cli_db)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert sorted(p.name for p in cli_db.parent.iterdir()) == ["dinnerbell.db"]
    assert cli.main(["catalog", "export"]) == 0
    sidecars = {p.name: p.stat().st_mode & 0o777 for p in cli_db.parent.iterdir() if p != cli_db}
    assert sidecars == {"dinnerbell.db-wal": 0o600, "dinnerbell.db-shm": 0o600}

    async def write() -> None:
        database = open_database(f"sqlite+aiosqlite:///{cli_db}")
        try:
            async with database.sessionmaker() as db:
                await CatalogRepository(db).add_tag(1, TagKind.OTHER, "after export", clock())
                await db.commit()
        finally:
            await database.close()

    asyncio.run(write())
    assert "after export" in expected(cli_db)


def test_a_missing_household_is_the_only_lookup_error_reported_as_one(
    cli_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object) -> CatalogExport:
        raise KeyError(99)

    monkeypatch.setattr(catalog_service, "export", broken)
    with pytest.raises(KeyError):
        cli.main(["catalog", "export"])


def test_the_command_warns_when_import_would_refuse_the_file(
    cli_db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dinnerbell.domain import catalog_file

    monkeypatch.setattr(catalog_file, "MAX_NODES", 10)
    assert cli.main(["catalog", "export"]) == 0
    err = capsys.readouterr().err
    assert err.endswith(
        "Warning: This export is over import's limit of 10 values. To import it, split it into smaller files.\n"
    )


def test_force_replaces_the_file_in_one_step(
    cli_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "catalog.yaml"
    output.write_text("my edits\n")

    def fail(*args: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("os.replace", fail)
    assert cli.main(["catalog", "export", "-o", str(output), "--force"]) == 2
    assert "Can't write" in capsys.readouterr().err
    assert output.read_text() == "my edits\n"
    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["catalog.yaml", "data"]  # no temporary file left


class ClosedPipe:
    """Stands in for stdout when the reader has gone, such as ``| head``."""

    class Buffer:
        def write(self, data: bytes) -> int:
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self) -> None:
            pass

    buffer = Buffer()

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")


def test_a_closed_pipe_stops_quietly(
    cli_db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdout", ClosedPipe())
    assert cli.main(["catalog", "export"]) == 1
    assert capsys.readouterr().err == ""


def test_an_old_schema_gets_a_migrate_hint(cli_env_only: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.test_migrations import _upgrade_to

    _upgrade_to(make_settings(cli_env_only.parent.parent), "0001_initial")
    with sqlite3.connect(cli_env_only) as db:
        db.execute(
            "INSERT INTO household (id, name, timezone, settings, created_at)"
            " VALUES (1, 'Home', 'UTC', '{}', '2026-10-01 12:00:00.000000')"
        )
    assert cli.main(["catalog", "export"]) == 2
    err = capsys.readouterr().err
    assert "Can't read the database" in err
    assert "The database is at schema revision '0001_initial', and this Dinnerbell expects" in err
    assert "run `dinnerbell migrate`, then export again" in err
