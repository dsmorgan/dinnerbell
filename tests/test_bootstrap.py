# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-start bootstrap."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from dinnerbell.app_factory import create_app
from dinnerbell.config import Settings
from dinnerbell.db.engine import create_app_engine, create_sessionmaker
from dinnerbell.db.models import AuditRow, HouseholdRow, MemberRow
from dinnerbell.services.bootstrap import bootstrap
from tests.conftest import BASE_URL, make_settings
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider, generate_key


async def run(settings: Settings, clock: FakeClock, **overrides: str) -> None:
    engine = create_app_engine(settings.async_database_url)
    try:
        async with create_sessionmaker(engine)() as db:
            await bootstrap(
                db,
                household_name=overrides.get("household_name", settings.household_name),
                timezone=overrides.get("timezone", settings.timezone),
                admin_email=overrides.get("admin_email", settings.admin_email),
                now=clock(),
            )
    finally:
        await engine.dispose()


async def count(settings: Settings, model: type) -> int:
    engine = create_app_engine(settings.async_database_url)
    try:
        async with create_sessionmaker(engine)() as db:
            return int(await db.scalar(select(func.count()).select_from(model)) or 0)
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_creates_household_and_admin_once(migrated: Settings, clock: FakeClock) -> None:
    await run(migrated, clock)
    await run(migrated, clock)
    assert await count(migrated, HouseholdRow) == 1
    assert await count(migrated, MemberRow) == 1
    assert await count(migrated, AuditRow) == 2

    engine = create_app_engine(migrated.async_database_url)
    try:
        async with create_sessionmaker(engine)() as db:
            household = await db.scalar(select(HouseholdRow))
            admin = await db.scalar(select(MemberRow))
    finally:
        await engine.dispose()
    assert household is not None and admin is not None
    assert (household.name, household.timezone) == ("Home", "America/Chicago")
    assert (admin.email, admin.role, admin.active, admin.oidc_subject) == ("admin@example.com", "admin", True, None)
    assert admin.display_name == "admin"
    assert admin.household_id == household.id


@pytest.mark.anyio
async def test_new_admin_email_adds_another_admin(migrated: Settings, clock: FakeClock) -> None:
    await run(migrated, clock)
    await run(migrated, clock, admin_email="second@example.com", household_name="Ignored")
    assert await count(migrated, HouseholdRow) == 1
    assert await count(migrated, MemberRow) == 2


@pytest.mark.parametrize("settings_overrides", [{"household_name": "Casa Morgan"}])
def test_household_row_is_authoritative_after_first_start(
    client: TestClient, migrated: Settings, clock: FakeClock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: name and time zone had two sources. The env values only seed the row."""
    assert "Casa Morgan" in client.get("/").text

    changed = make_settings(tmp_path, household_name="Ignored Name", timezone="UTC")
    assert changed.database_url == migrated.database_url
    provider = FakeProvider(key=generate_key("k"), clock=clock)
    app = create_app(changed, clock=clock, oidc_transport=httpx.ASGITransport(app=provider.app))
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as restarted:
        signed_out = restarted.get("/")
        assert "Casa Morgan" in signed_out.text
        assert "Ignored Name" not in signed_out.text
        assert "Casa Morgan" in restarted.get("/no-such-page").text
        assert str(app.state.jobs.zone) == "America/Chicago"
    # The restart says each setting was ignored, and names neither household name (issue 334).
    warnings = warnings_from(caplog)
    assert any("DINNERBELL_TIMEZONE is UTC" in w and "America/Chicago" in w for w in warnings)
    assert any("DINNERBELL_HOUSEHOLD_NAME differs" in w for w in warnings)
    assert not any("Casa Morgan" in r.getMessage() or "Ignored Name" in r.getMessage() for r in caplog.records)


def warnings_from(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "dinnerbell.services.bootstrap" and r.levelno == logging.WARNING
    ]


@pytest.mark.anyio
async def test_a_changed_time_zone_setting_warns_with_both_zones(
    migrated: Settings, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue 334: DINNERBELL_TIMEZONE is read on first start only; a later change says so."""
    await run(migrated, clock)
    assert warnings_from(caplog) == []
    caplog.clear()
    await run(migrated, clock, timezone="Europe/Berlin")
    [warning] = warnings_from(caplog)
    assert "DINNERBELL_TIMEZONE" in warning
    assert "Europe/Berlin" in warning and "America/Chicago" in warning
    assert "first start" in warning and "run dinnerbell set-timezone ZONE" in warning


@pytest.mark.anyio
async def test_a_changed_household_name_warns_without_either_name(
    migrated: Settings, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """The name identifies the household, so the warning says only that it differs (SEC-5)."""
    await run(migrated, clock, household_name="Place 1")
    caplog.clear()
    await run(migrated, clock, household_name="Place 2")
    [warning] = warnings_from(caplog)
    assert "DINNERBELL_HOUSEHOLD_NAME" in warning and "first start" in warning
    assert "run dinnerbell set-household-name NAME" in warning
    assert not any("Place" in record.getMessage() for record in caplog.records)


@pytest.mark.anyio
async def test_matching_settings_and_first_start_log_no_warning(
    migrated: Settings, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    await run(migrated, clock, household_name="Place 1", timezone="Europe/Berlin")
    await run(migrated, clock, household_name="Place 1", timezone="Europe/Berlin")
    assert warnings_from(caplog) == []


@pytest.mark.anyio
async def test_both_settings_changed_warn_once_each(
    migrated: Settings, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    await run(migrated, clock)
    caplog.clear()
    await run(migrated, clock, household_name="Place 2", timezone="UTC")
    warnings = warnings_from(caplog)
    assert len(warnings) == 2
    assert sum("DINNERBELL_TIMEZONE" in w for w in warnings) == 1
    assert sum("DINNERBELL_HOUSEHOLD_NAME" in w for w in warnings) == 1


@pytest.mark.anyio
async def test_a_new_admin_email_warns_about_an_earlier_unclaimed_invitation(
    migrated: Settings, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """#360: the earlier email was most likely a typo, which anyone with it could still claim.
    The warning names the member ID, never the email (SEC-5)."""
    await run(migrated, clock, admin_email="rilye@example.com")
    caplog.clear()
    await run(migrated, clock, admin_email="riley@example.com")
    [warning] = warnings_from(caplog)
    assert "DINNERBELL_ADMIN_EMAIL changed" in warning and "deactivate member 1" in warning
    assert not any("@" in record.getMessage() for record in caplog.records)
    record = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert record.member_id == 1  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_a_new_admin_email_after_the_first_admin_signed_in_warns_nothing(
    migrated: Settings, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    from sqlalchemy import text

    await run(migrated, clock)
    engine = create_app_engine(migrated.async_database_url)
    try:
        async with create_sessionmaker(engine)() as db:
            await db.execute(text("UPDATE member SET oidc_subject = 'sub-admin'"))
            await db.commit()
    finally:
        await engine.dispose()
    await run(migrated, clock, admin_email="second@example.com")
    assert warnings_from(caplog) == []
