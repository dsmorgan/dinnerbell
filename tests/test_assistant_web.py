# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Assistant on the web (ADR 0009): shown only with the key and the flag, admins only,
every change audited, and this month's usage by member."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, HouseholdRow
from tests.conftest import csrf_from, invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.test_admin import sign_in_as
from tests.test_auth_flow import rows

FORM = {"enabled": "on", "cap_dollars": "12", "conversation_hours": "6"}


def save(client: TestClient, data: dict[str, str]) -> Any:
    return client.post("/admin/assistant", data=data | {"csrf_token": csrf_from(client)})


@pytest.mark.parametrize("settings_overrides", [{"anthropic_api_key": "sk-ant-test-not-a-real-key"}])
@pytest.mark.flag("assistant", on=False)
def test_with_the_flag_off_there_is_no_tab_and_no_page(
    client: TestClient, provider: FakeProvider, settings: Settings
) -> None:
    sign_in(client, provider)
    assert "/admin/assistant" not in client.get("/admin/members").text
    assert client.get("/admin/assistant").status_code == 404
    assert save(client, FORM).status_code == 404


@pytest.mark.flag("assistant", on=True)
def test_without_the_key_there_is_no_tab_and_no_page(
    client: TestClient, provider: FakeProvider, settings: Settings
) -> None:
    assert settings.anthropic_api_key is None
    sign_in(client, provider)
    assert "/admin/assistant" not in client.get("/admin/members").text
    assert client.get("/admin/assistant").status_code == 404


@pytest.mark.parametrize("settings_overrides", [{"anthropic_api_key": "sk-ant-test-not-a-real-key"}])
@pytest.mark.flag("assistant", on=True)
class TestAdminAssistant:
    def test_the_admin_turns_it_on_and_the_change_is_audited(
        self, client: TestClient, provider: FakeProvider, migrated: Settings
    ) -> None:
        sign_in(client, provider)
        assert 'href="/admin/assistant"' in client.get("/admin/members").text
        first = page(client.get("/admin/assistant"))
        assert "Turn on the assistant" in first and "claude-sonnet-5-5" in first
        assert "$0.00 of $5 this month" in first and "Nobody has used the assistant this month." in first
        response = save(client, FORM)
        assert response.status_code == 303 and response.headers["location"] == "/admin/assistant?notice=saved"
        shown = page(client.get("/admin/assistant?notice=saved"))
        assert "Assistant settings saved." in shown and "$0.00 of $12 this month" in shown
        [household] = rows(migrated, HouseholdRow)
        assert household.settings["assistant_enabled"] is True
        assert household.settings["assistant_monthly_cap_dollars"] == 12
        assert household.settings["assistant_conversation_hours"] == 6
        [audit] = [a for a in rows(migrated, AuditRow) if a.action == "household.assistant_updated"]
        assert audit.payload["assistant_enabled"] == {"from": False, "to": True}

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            (FORM | {"cap_dollars": "0"}, "The monthly cap is $1 to $100."),
            (FORM | {"conversation_hours": "200"}, "Conversation hours run 0 to 168"),
            (FORM | {"cap_dollars": "five"}, "The monthly cap must be a whole number."),
            (FORM | {"conversation_hours": ""}, "Enter conversation hours."),
        ],
    )
    def test_a_bad_value_is_refused_and_shown_again(
        self, client: TestClient, provider: FakeProvider, migrated: Settings, data: dict[str, str], message: str
    ) -> None:
        sign_in(client, provider)
        response = save(client, data)
        assert response.status_code == 400
        assert message in page(response)
        assert f'value="{data["cap_dollars"]}"' in response.text
        assert not [a for a in rows(migrated, AuditRow) if a.action == "household.assistant_updated"]

    def test_a_member_cant_open_or_change_it(self, client: TestClient, provider: FakeProvider) -> None:
        sign_in(client, provider)
        assert invite(client, "sam@example.com", "Sam").status_code == 303
        sign_in_as(client, provider, "subject-sam", "sam@example.com")
        assert client.get("/admin/assistant").status_code == 403
        assert save(client, FORM).status_code == 403

    def test_saving_needs_the_csrf_token(self, client: TestClient, provider: FakeProvider) -> None:
        sign_in(client, provider)
        assert client.post("/admin/assistant", data=FORM).status_code == 403
