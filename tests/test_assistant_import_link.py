# SPDX-License-Identifier: AGPL-3.0-or-later
"""The link a Slack Confirm gives to a catalog draft's import (spec 6.14, **Drafts**):
``draft_import_url`` signs the draft and its member, and ``GET /catalog/import/draft`` shows
that member the add-only preview and changes nothing. Anyone else, a tampered or expired link,
or the assistant turned off gets the same 404."""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AssistantDraftRow, CatalogItemRow
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.assistant import AssistantSettings, web_key
from dinnerbell.domain.members import Member, Role
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.assistant import admin, drafts, history, retention
from dinnerbell.services.assistant.drafts import CatalogReady
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.database import open_database
from dinnerbell.services.errors import NotFound
from dinnerbell.services.household import current_household
from dinnerbell.web.routes.catalog import DRAFT_LINK_LIFETIME, _draft_link_signer, draft_import_url
from tests.conftest import BASE_URL, invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.links import MENU_LINK, link_draft
from tests.support.web import submit
from tests.test_admin import sign_in_as
from tests.test_auth_flow import rows

KEY = "sk-ant-test-not-a-real-key"
DRAFTED = (
    "version: 1\nrestaurants:\n  - name: Noodle Bar\n    cuisine: [Chinese]\n    notes: Changed by the draft\n"
    "  - name: Dumpling House\n    cuisine: [Chinese]\n"
)
NOODLE_BAR = "version: 1\nrestaurants:\n  - name: Noodle Bar\n    cuisine: [Chinese]\n"
pytestmark = [pytest.mark.parametrize("settings_overrides", [{"anthropic_api_key": KEY}])]


@dataclass(frozen=True)
class Drafted:
    draft_id: int
    member_id: int
    url: str
    content: str


def run(settings: Settings, work: Any) -> Any:
    """Run ``work(db, admin)`` against the app's database, as the bootstrap admin."""

    async def go() -> Any:
        database = open_database(settings.async_database_url)
        try:
            async with database.sessionmaker() as db:
                household = await current_household(db)
                members = await MemberRepository(db).list(household.id)
                alex = next(m for m in members if m.email == settings.admin_email)
                return await work(db, alex)
        finally:
            await database.close()

    return asyncio.run(go())


def confirmed(settings: Settings, clock: FakeClock, *, then_add: bool = True) -> Drafted:
    """Alex drafts Noodle Bar and Dumpling House with the assistant on, Noodle Bar then appears in
    the catalog, and Alex confirms in Slack: the link Slack would show."""
    config = AssistantConfig.from_settings(settings)

    async def work(db: Any, alex: Member) -> Drafted:
        await admin.update_assistant(db, config, alex, AssistantSettings(enabled=True), now=clock())
        draft = await drafts.draft_catalog_items(db, alex, web_key(alex.id), DRAFTED, now=clock())
        if then_add:
            await catalog_service.apply_import(db, alex.household_id, alex, NOODLE_BAR, now=clock())
        ready = await drafts.confirm_draft(db, clock, config, alex, draft.id)
        assert isinstance(ready, CatalogReady)
        return Drafted(draft.id, alex.id, draft_import_url(settings, clock, draft.id, alex.id), ready.content)

    result: Drafted = run(settings, work)
    return result


def switch_off(settings: Settings, clock: FakeClock) -> None:
    async def work(db: Any, alex: Member) -> None:
        values = AssistantSettings(enabled=False)
        await admin.update_assistant(db, AssistantConfig.from_settings(settings), alex, values, now=clock())

    run(settings, work)


def path(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


def token_in(response: httpx.Response) -> str | None:
    match = re.search(r'name="token" value="([^"]+)"', response.text)
    return match.group(1) if match else None


@pytest.mark.flag("assistant", on=True)
def test_the_link_is_built_from_the_base_url_and_signs_the_draft_and_member(
    migrated: Settings, clock: FakeClock
) -> None:
    url = draft_import_url(migrated, clock, 12, 34)
    assert url.startswith(f"{migrated.base_url}/catalog/import/draft?t=")
    (token,) = parse_qs(urlsplit(url).query)["t"]
    assert _draft_link_signer(migrated, clock).loads(token) == {"draft": "12", "member": "34"}
    clock.now += DRAFT_LINK_LIFETIME + timedelta(seconds=1)
    assert _draft_link_signer(migrated, clock).loads(token) is None


@pytest.mark.flag("assistant", on=True)
def test_the_drafts_member_gets_the_add_only_preview_and_apply_only_adds(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    sign_in(client, provider)
    drafted = confirmed(migrated, clock)
    response = client.get(path(drafted.url))
    assert response.status_code == 200
    text = page(response)
    assert 'name="add_only" value="1"' in text and "only adds new items" in text
    assert '<a href="/catalog" aria-current="page">Catalog</a>' in text  # the nav says Catalog
    assert "Dumpling House" in text and "Changed by the draft" in text  # the file, shown as it is
    token = token_in(response)
    assert token
    before = {row.name: row.notes for row in rows(migrated, CatalogItemRow)}
    assert before["Noodle Bar"] is None and "Dumpling House" not in before  # the GET saved nothing
    applied = submit(client, "/catalog/import/apply", {"content": drafted.content, "token": token, "add_only": "1"})
    assert applied.status_code == 303
    assert applied.headers["location"].startswith("/catalog?notice=imported&created=1&updated=0")
    after = {row.name: row.notes for row in rows(migrated, CatalogItemRow)}
    assert after["Noodle Bar"] is None and "Dumpling House" in after


@pytest.mark.flag("assistant", on=True)
def test_an_apply_without_the_add_only_mark_still_only_adds(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    """A browser that drops the hidden ``add_only`` field: the signed token still marks it."""
    sign_in(client, provider)
    drafted = confirmed(migrated, clock)
    token = token_in(client.get(path(drafted.url)))
    applied = submit(client, "/catalog/import/apply", {"content": drafted.content, "token": token})
    assert applied.status_code == 303
    assert {row.name: row.notes for row in rows(migrated, CatalogItemRow)}["Noodle Bar"] is None


@pytest.mark.flag("assistant", on=True)
def test_signed_out_or_someone_else_gets_404(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    drafted = confirmed(migrated, clock)
    assert client.get(path(drafted.url)).status_code == 404  # signed out
    sign_in(client, provider)
    assert invite(client, "kim@example.com", "Kim").status_code == 303
    sign_in_as(client, provider, "kim", "kim@example.com")
    assert client.get(path(drafted.url)).status_code == 404
    assert "Dumpling House" not in {row.name for row in rows(migrated, CatalogItemRow)}


@pytest.mark.flag("assistant", on=True)
def test_a_tampered_expired_or_unconfirmed_link_gets_404(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    sign_in(client, provider)
    drafted = confirmed(migrated, clock)
    (token,) = parse_qs(urlsplit(drafted.url).query)["t"]
    body, mac = token.split(".")
    signer = _draft_link_signer(migrated, clock)
    another = signer.dumps({"draft": str(drafted.draft_id + 1), "member": str(drafted.member_id)})
    for bad in (
        f"{body}.{mac[:-2]}xx",  # a forged signature
        f"{body[:-2]}xx.{mac}",  # a changed body
        "",
        "x" * 2000,
        signer.dumps({"draft": "none", "member": str(drafted.member_id)}),
        signer.dumps({"draft": str(drafted.draft_id), "member": str(drafted.member_id + 1)}),
        another,  # no such draft
    ):
        assert client.get(f"/catalog/import/draft?{urlencode({'t': bad})}").status_code == 404, bad
    assert client.get("/catalog/import/draft").status_code == 404
    clock.now += DRAFT_LINK_LIFETIME + timedelta(seconds=1)
    assert client.get(path(drafted.url)).status_code == 404


@pytest.mark.flag("assistant", on=True)
def test_with_the_assistant_turned_off_the_link_gets_404(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    sign_in(client, provider)
    drafted = confirmed(migrated, clock)
    assert client.get(path(drafted.url)).status_code == 200
    switch_off(migrated, clock)
    assert client.get(path(drafted.url)).status_code == 404


@pytest.mark.flag("assistant", on=True)
def test_only_a_confirmed_catalog_draft_of_the_members_is_ready(
    client: TestClient, migrated: Settings, clock: FakeClock
) -> None:
    config = AssistantConfig.from_settings(migrated)

    async def work(db: Any, alex: Member) -> None:
        await admin.update_assistant(db, config, alex, AssistantSettings(enabled=True), now=clock())
        draft = await drafts.draft_catalog_items(db, alex, web_key(alex.id), DRAFTED, now=clock())
        with pytest.raises(NotFound):  # still waiting for Confirm
            await drafts.ready_for_import(db, config, alex, draft.id, now=clock())
        await drafts.confirm_draft(db, clock, config, alex, draft.id)
        ready = await drafts.ready_for_import(db, config, alex, draft.id, now=clock())
        assert ready.add_only and ready.member_id == alex.id and ready.content == drafted_content(ready)
        with pytest.raises(NotFound):
            await drafts.ready_for_import(db, AssistantConfig(key_set=True, flag_on=False), alex, draft.id, now=clock())
        with pytest.raises(NotFound):
            await drafts.ready_for_import(db, config, alex, draft.id + 1, now=clock())
        with pytest.raises(NotFound):  # a guest's, or an inactive member's, never is
            await drafts.ready_for_import(db, config, replace(alex, role=Role.GUEST), draft.id, now=clock())
        with pytest.raises(NotFound):  # someone else's
            await drafts.ready_for_import(db, config, replace(alex, id=alex.id + 100), draft.id, now=clock())
        with pytest.raises(NotFound):  # kept past its day, before the purge has run
            await drafts.ready_for_import(db, config, alex, draft.id, now=clock() + timedelta(hours=25))

    run(migrated, work)
    [row] = rows(migrated, AssistantDraftRow)
    assert row.used_at is not None and row.confirmed_at == row.used_at


@pytest.mark.flag("assistant", on=True)
def test_a_cancelled_catalog_draft_never_opens_the_import_even_with_a_signed_link(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    """``used_at`` is set by Cancel and by **Start over** as by Confirm. Only ``confirmed_at``
    opens the import preview, so a link that names a cancelled draft, signed and fresh, is a 404
    like any other, and its import changes nothing."""
    sign_in(client, provider)
    config = AssistantConfig.from_settings(migrated)

    async def work(db: Any, alex: Member) -> tuple[int, int]:
        await admin.update_assistant(db, config, alex, AssistantSettings(enabled=True), now=clock())
        cancelled = await drafts.draft_catalog_items(db, alex, web_key(alex.id), DRAFTED, now=clock())
        await drafts.cancel_draft(db, clock, alex, cancelled.id)
        started_over = await drafts.draft_catalog_items(db, alex, web_key(alex.id), NOODLE_BAR, now=clock())
        assert await history.start_over(db, config, alex, clock()) == 1
        for draft_id in (cancelled.id, started_over.id):
            with pytest.raises(NotFound):
                await drafts.ready_for_import(db, config, alex, draft_id, now=clock())
        return cancelled.id, alex.id

    draft_id, member_id = run(migrated, work)
    for used in (draft_id, draft_id + 1):
        assert client.get(path(draft_import_url(migrated, clock, used, member_id))).status_code == 404
    drafts_now = rows(migrated, AssistantDraftRow)
    assert {(r.used_at is not None, r.confirmed_at) for r in drafts_now} == {(True, None)}
    assert {r.name for r in rows(migrated, CatalogItemRow)}.isdisjoint({"Noodle Bar", "Dumpling House"})


def drafted_content(ready: CatalogReady) -> str:
    assert "Dumpling House" in ready.content
    return ready.content


def test_the_app_builds_slacks_import_link_from_the_base_url(migrated: Settings, clock: FakeClock) -> None:
    """``app_factory`` gives Slack ``draft_import_url`` for the configured base URL."""
    from dinnerbell import app_factory
    from dinnerbell.services.health import SlackState

    configured = migrated.model_copy(
        update={
            "slack_bot_token": migrated.secret_key,
            "slack_app_token": migrated.secret_key,
            "slack_channel": "C0DINNER",
        }
    )
    helper = app_factory._assistant(configured, object(), clock)  # type: ignore[arg-type]
    assert helper is not None
    conn = app_factory._slack(configured, None, clock, SlackState(), None, app_factory.BoltSocket, helper)  # type: ignore[arg-type]
    assert conn is not None and conn.assistant is not None
    url = conn.assistant.import_link(5, 6)
    assert url.startswith(f"{BASE_URL}/catalog/import/draft?t=")
    (token,) = parse_qs(urlsplit(url).query)["t"]
    assert _draft_link_signer(configured, clock).loads(token) == {"draft": "5", "member": "6"}


def purge(settings: Settings, clock: FakeClock) -> int:
    async def go() -> int:
        database = open_database(settings.async_database_url)
        try:
            return await retention.purge_expired_drafts(database.sessionmaker, clock())
        finally:
            await database.close()

    removed: int = asyncio.run(go())
    return removed


@pytest.mark.flag("assistant", on=True)
def test_a_confirmed_catalog_draft_is_kept_for_its_link(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    """Confirmed at minute 29 of its 30: 23 hours on, the purge keeps it and the link works; 25
    hours on, the purge has deleted it and the link gets 404."""
    sign_in(client, provider)
    config = AssistantConfig.from_settings(migrated)

    async def draft(db: Any, alex: Member) -> int:
        await admin.update_assistant(db, config, alex, AssistantSettings(enabled=True), now=clock())
        made = await drafts.draft_catalog_items(db, alex, web_key(alex.id), DRAFTED, now=clock())
        return made.id

    async def confirm(db: Any, alex: Member) -> str:
        assert isinstance(await drafts.confirm_draft(db, clock, config, alex, draft_id), CatalogReady)
        return draft_import_url(migrated, clock, draft_id, alex.id)

    draft_id: int = run(migrated, draft)
    clock.now += timedelta(minutes=29)
    url: str = run(migrated, confirm)
    confirmed_at = clock.now
    clock.now = confirmed_at + timedelta(hours=23)
    assert purge(migrated, clock) == 0
    assert client.get(path(url)).status_code == 200
    clock.now = confirmed_at + timedelta(hours=25)
    assert purge(migrated, clock) == 1
    assert client.get(path(url)).status_code == 404
    assert rows(migrated, AssistantDraftRow) == []


@pytest.mark.flag("assistant", on=True)
def test_a_link_drafts_import_link_shows_only_what_dinnerbell_kept(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    """A menu link's draft, confirmed in Slack: the link opens its add-only preview, with the
    member's link and none of the page's, and Apply adds it."""
    sign_in(client, provider)
    config = AssistantConfig.from_settings(migrated)

    async def work(db: Any, alex: Member) -> str:
        await admin.update_assistant(db, config, alex, AssistantSettings(enabled=True), now=clock())
        drafted = await link_draft(db, alex, now=clock())
        assert isinstance(await drafts.confirm_draft(db, clock, config, alex, drafted.view.id), CatalogReady)
        return draft_import_url(migrated, clock, drafted.view.id, alex.id)

    url: str = run(migrated, work)
    response = client.get(path(url))
    assert response.status_code == 200
    text = page(response)
    assert "Basil House" in text and MENU_LINK in text
    assert "pay.example.net" not in text and "Pay ahead" not in text
    token = token_in(response)
    content = re.search(r'<textarea name="content"[^>]*>\n?(.*?)</textarea>', response.text, re.S)
    assert token and content
    applied = submit(client, "/catalog/import/apply", {"content": html.unescape(content.group(1)), "token": token})
    assert applied.status_code == 303 and applied.headers["location"].startswith("/catalog?notice=imported&created=1")
    basil = next(row for row in rows(migrated, CatalogItemRow) if row.name == "Basil House")
    assert basil.url == MENU_LINK and basil.order_url is None and basil.notes is None
