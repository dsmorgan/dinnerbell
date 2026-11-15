# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Assistant on the web (ADR 0009): shown only with the key, admins only, every
change audited, and this month's usage by member."""

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


def test_without_the_key_there_is_no_tab_and_no_page(
    client: TestClient, provider: FakeProvider, settings: Settings
) -> None:
    assert settings.anthropic_api_key is None
    sign_in(client, provider)
    assert "/admin/assistant" not in client.get("/admin/members").text
    assert client.get("/admin/assistant").status_code == 404


@pytest.mark.parametrize("settings_overrides", [{"anthropic_api_key": "sk-ant-test-not-a-real-key"}])
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

    def test_the_page_says_what_goes_to_anthropic(self, client: TestClient, provider: FakeProvider) -> None:
        """The disclosure matches docs/configuration.md: active members' display names with their
        labels (#277), never a guest's, and no one's ratings, a member's own data only when they ask
        in private (#253), dietary needs named (#278), and the household hints the meal-history
        tool sends (#264)."""
        sign_in(client, provider)
        shown = " ".join(page(client.get("/admin/assistant")).split())
        assert (
            'with members shown as "Member 1", "Member 2", and so on. Each request also lists every active '
            'member\'s display name with their label, such as "Member 2: Theodore", so the assistant can tell who a '
            "member means by a name they type. Guests and former members are never listed. A name someone types, or "
            "the assistant writes in a reply, stays in the conversation as written until Conversation hours delete "
            "it. It never sends emails, Slack IDs, preferences, vetoes, or anyone's ratings."
        ) in shown
        assert (
            "The one exception is a member's own preferences, vetoes, dietary needs, what they can usually do "
            "(cook, pick up, or order), ratings, feedback, and away days, which it sends only when that member asks "
            "about them in their own conversation, on the Assistant page or in a direct message. Dietary needs can "
            "reveal health or religion."
        ) in shown
        assert (
            "To answer questions about what the household has eaten, it sends each item's household hint, "
            'such as "Well liked", the same words every member sees on Catalog.'
        ) in shown

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


@pytest.mark.parametrize(
    "settings_overrides",
    [{"anthropic_api_key": "sk-ant-test-not-a-real-key", "assistant_model": "claude-opus-5-5"}],
)
def test_a_cap_below_one_message_saves_and_says_so(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    """#253, #254: with Opus, every message's worst case is more than $1, a channel thread's included.
    The $1 cap still saves, and the page says it's too low for any message, from the reservation
    code itself, and the least cap that works."""
    from dinnerbell.db.models import MemberRow
    from dinnerbell.db.repositories import to_member
    from dinnerbell.domain.assistant import format_dollars
    from dinnerbell.services.assistant.conversation import least_call_micros, least_private_call_micros

    sign_in(client, provider)
    members = [to_member(row) for row in rows(migrated, MemberRow)]
    least = least_private_call_micros("claude-opus-5-5", members)
    channel = least_call_micros("claude-opus-5-5", private=False, members=members)
    assert 1_000_000 < channel < least < 2_000_000  # since #254, a channel thread's too
    assert 'id="cap-low"' not in page(client.get("/admin/assistant"))  # $5 by default
    assert save(client, FORM | {"cap_dollars": "1"}).status_code == 303
    [household] = rows(migrated, HouseholdRow)
    assert household.settings["assistant_monthly_cap_dollars"] == 1
    shown = " ".join(page(client.get("/admin/assistant")).split())
    assert 'id="cap-low"' in shown
    assert (
        f"With claude-opus-5-5, one message can cost up to {format_dollars(channel)} in a channel thread and "
        f"{format_dollars(least)} on the Assistant page or in a direct message, more than the monthly cap, so the "
        "assistant can't answer any message. A cap of $2 covers the smallest message; long conversations and "
        "requests that use several tools need more. Or ask the operator for a cheaper model."
    ) in shown
    assert "one private message can cost" not in shown
    assert save(client, FORM | {"cap_dollars": "2"}).status_code == 303
    assert 'id="cap-low"' not in page(client.get("/admin/assistant"))


def test_the_cap_alert_says_which_messages_it_stops() -> None:
    """A cap between a channel thread's smallest message and a private one's stops only private
    messages, and the page says so; one below both stops every message."""
    from dinnerbell.domain.assistant import AssistantSettings
    from dinnerbell.services.assistant.admin import AssistantAdmin

    def view(cap: int) -> AssistantAdmin:
        settings = AssistantSettings(enabled=True, cap_dollars=cap)
        return AssistantAdmin(
            settings, "2026-10", 0, (), least_call_micros=2_500_000, least_channel_call_micros=1_500_000
        )

    assert (view(2).cap_below_one_call, view(2).cap_below_any_call) == (True, False)
    assert (view(1).cap_below_one_call, view(1).cap_below_any_call) == (True, True)
    assert (view(3).cap_below_one_call, view(3).cap_below_any_call) == (False, False)
