# SPDX-License-Identifier: AGPL-3.0-or-later
"""DINNERBELL_SEED_FILE: import a catalog file on first start, only while the catalog is empty."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from dinnerbell.app_factory import create_app
from dinnerbell.config import Settings
from dinnerbell.db.migrate import run_migrations
from dinnerbell.db.models import AuditRow, CatalogItemRow, TagRow
from tests.conftest import BASE_URL, make_settings
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.test_auth_flow import rows

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "seed" / "example.yaml"


def start(settings: Settings, clock: FakeClock, provider: FakeProvider) -> None:
    """Start and stop the app once, as a container start would."""
    app = create_app(settings, clock=clock, oidc_transport=httpx.ASGITransport(app=provider.app))
    with TestClient(app, base_url=BASE_URL):
        pass


def seeded(tmp_path: Path, seed: Path) -> Settings:
    settings = make_settings(tmp_path, seed_file=seed)
    run_migrations(settings.sync_database_url, settings.database_path)
    return settings


def test_seed_imports_on_first_start(
    tmp_path: Path, clock: FakeClock, provider: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    settings = seeded(tmp_path, EXAMPLE)
    with caplog.at_level(logging.INFO, logger="dinnerbell"):
        start(settings, clock, provider)
    assert len(rows(settings, CatalogItemRow)) == 8 and len(rows(settings, TagRow)) == 25
    [audit] = [a for a in rows(settings, AuditRow) if a.action == "catalog.seeded"]
    assert audit.actor_member_id is None and audit.payload["items_created"] == 8
    [record] = [r for r in caplog.records if r.getMessage() == "catalog seeded"]
    assert record.items_created == 8 and record.tags_created == 25  # type: ignore[attr-defined]


def test_seed_is_skipped_once_the_catalog_has_entries(
    tmp_path: Path, clock: FakeClock, provider: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text("version: 1\nrestaurants:\n  - {name: First, cuisine: [Thai]}\n")
    settings = seeded(tmp_path, seed)
    start(settings, clock, provider)
    seed.write_text("version: 1\nrestaurants:\n  - {name: Second, cuisine: [Thai]}\n")
    with caplog.at_level(logging.INFO, logger="dinnerbell"):
        start(settings, clock, provider)
    assert {i.name for i in rows(settings, CatalogItemRow)} == {"First"}
    assert any(r.getMessage() == "catalog already has entries; seed file skipped" for r in caplog.records)


def test_tags_alone_count_as_not_empty(tmp_path: Path, clock: FakeClock, provider: FakeProvider) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text("version: 1\ntags:\n  other: [quick]\n")
    settings = seeded(tmp_path, seed)
    start(settings, clock, provider)
    seed.write_text("version: 1\nrestaurants:\n  - {name: Later, cuisine: [Thai]}\n")
    start(settings, clock, provider)
    assert rows(settings, CatalogItemRow) == []


def test_invalid_seed_is_logged_and_the_app_still_starts(
    tmp_path: Path, clock: FakeClock, provider: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text(
        "version: 1\nrestaurants:\n  - {name: A, cuisine: [Thai], modes: [boat]}\n  - {name: B, cuisine: [Thai]}\n"
    )
    settings = seeded(tmp_path, seed)
    with caplog.at_level(logging.INFO, logger="dinnerbell"):
        start(settings, clock, provider)
    assert rows(settings, CatalogItemRow) == []
    [record] = [r for r in caplog.records if r.getMessage() == "seed file has errors; nothing was imported"]
    assert record.levelno == logging.ERROR
    assert record.errors == ['restaurants[0].modes[0]: "boat" is not one of pickup, delivery']  # type: ignore[attr-defined]


def test_missing_seed_is_logged(
    tmp_path: Path, clock: FakeClock, provider: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    settings = seeded(tmp_path, tmp_path / "nope.yaml")
    with caplog.at_level(logging.INFO, logger="dinnerbell"):
        start(settings, clock, provider)
    assert any(r.getMessage() == "could not read the seed file" for r in caplog.records)
    assert rows(settings, CatalogItemRow) == []


def test_seed_log_never_contains_emails(
    tmp_path: Path, clock: FakeClock, provider: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    from dinnerbell.logs import JsonFormatter

    seed = tmp_path / "seed.yaml"
    seed.write_text("version: 1\nhome_meals:\n  - {name: Pie, cuisine: [American], cooks: [stranger@example.com]}\n")
    settings = seeded(tmp_path, seed)
    with caplog.at_level(logging.INFO, logger="dinnerbell"):
        start(settings, clock, provider)
    formatted = "\n".join(JsonFormatter().format(r) for r in caplog.records if r.name.startswith("dinnerbell"))
    assert "stranger@example.com" not in formatted
    assert "home_meals[0].cooks[0]" in formatted


def test_unexpected_seed_failure_never_stops_startup(
    tmp_path: Path,
    clock: FakeClock,
    provider: FakeProvider,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dinnerbell.services import catalog as catalog_service

    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("parser bug")

    monkeypatch.setattr(catalog_service, "parse_catalog", broken)
    settings = seeded(tmp_path, EXAMPLE)
    with caplog.at_level(logging.INFO, logger="dinnerbell"):
        start(settings, clock, provider)
    assert rows(settings, CatalogItemRow) == []
    [record] = [r for r in caplog.records if r.getMessage() == "seed import failed; nothing was imported"]
    assert record.levelno == logging.ERROR and record.exc_info is not None
