# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Assistant on the web (ADR 0009): shown only with the key, admins only, every
change audited, and this month's usage by member."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dinnerbell.config import Settings
from dinnerbell.db.models import AssistantUsageRow, AuditRow, HouseholdRow, MemberRow
from dinnerbell.db.repositories import to_member
from dinnerbell.domain.assistant import format_dollars, month_key
from dinnerbell.services.assistant.conversation import least_call_micros, least_private_call_micros
from tests.conftest import csrf_from, invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.test_admin import sign_in_as
from tests.test_auth_flow import rows

FORM = {"enabled": "on", "cap_dollars": "12", "conversation_hours": "6"}


def save(client: TestClient, data: dict[str, str]) -> Any:
    return client.post("/admin/assistant", data=data | {"csrf_token": csrf_from(client)})


def spend(settings: Settings, clock: FakeClock, micros: int) -> None:
    """Add ``micros`` to this month's spending, as one settled row."""
    [household] = rows(settings, HouseholdRow)
    now = clock()
    engine = create_engine(settings.sync_database_url)
    try:
        with Session(engine) as db:
            db.add(
                AssistantUsageRow(
                    household_id=household.id,
                    member_id=None,
                    model="earlier",
                    month=month_key(now, ZoneInfo(household.timezone)),
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_micros=micros,
                    state="settled",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.commit()
    finally:
        engine.dispose()


def least_calls(settings: Settings, model: str) -> tuple[int, int]:
    """What the smallest private message, and the smallest channel-thread message, set aside."""
    members = [to_member(row) for row in rows(settings, MemberRow)]
    return least_private_call_micros(model, members), least_call_micros(model, private=False, members=members)


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
        """The page sums up docs/configuration.md's disclosure in a few lines (#352): active members'
        display names go (#277), no emails or Slack IDs, and a member's own data only when they ask in
        private (#253), dietary needs named (#278). The docs keep the whole list."""
        sign_in(client, provider)
        shown = " ".join(page(client.get("/admin/assistant")).split())
        assert (
            "acting only as the member asking, never with admin rights. It changes nothing until they confirm. "
            "Anthropic gets the conversation, what the assistant looks up, and active members' names, never "
            "emails or Slack IDs. A member's own preferences, ratings, and dietary needs, which can reveal "
            "health or religion, go only when they ask in private."
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


def test_whats_left_below_one_call_says_which_messages_wait() -> None:
    """#362: with the cap above the smallest message but what's left below it, the usage note
    says so; it says nothing while the whole cap is below, which the settings alert covers."""
    from dinnerbell.domain.assistant import AssistantSettings
    from dinnerbell.services.assistant.admin import AssistantAdmin

    def view(cap: int, spent: int) -> AssistantAdmin:
        settings = AssistantSettings(enabled=True, cap_dollars=cap)
        return AssistantAdmin(
            settings, "2026-10", spent, (), least_call_micros=400_000, least_channel_call_micros=300_000
        )

    assert (view(2, 1_600_000).left_below_one_call, view(2, 1_600_000).left_below_any_call) == (False, False)
    assert (view(2, 1_600_001).left_below_one_call, view(2, 1_600_001).left_below_any_call) == (True, False)
    assert (view(2, 1_700_000).left_below_one_call, view(2, 1_700_000).left_below_any_call) == (True, False)
    assert (view(2, 1_700_001).left_below_one_call, view(2, 1_700_001).left_below_any_call) == (True, True)
    assert view(2, 2_500_000).left_micros == 0 and view(2, 2_500_000).left_below_any_call
    tiny = AssistantSettings(enabled=True, cap_dollars=1)
    below = AssistantAdmin(tiny, "2026-10", 900_000, (), least_call_micros=1_500_000, least_channel_call_micros=1)
    assert below.cap_below_one_call and not below.left_below_one_call and not below.left_below_any_call
    # The whole cap below a channel message too: only the settings alert speaks, never "next month".
    none = AssistantAdmin(
        tiny, "2026-10", 900_000, (), least_call_micros=1_500_000, least_channel_call_micros=1_200_000
    )
    assert none.cap_below_any_call and not none.left_below_one_call and not none.left_below_any_call
    # A cap between the two minimums: private messages never fit, and channel threads wait for next month.
    straddle = AssistantAdmin(
        tiny, "2026-10", 900_000, (), least_call_micros=1_500_000, least_channel_call_micros=500_000
    )
    assert straddle.cap_below_one_call and not straddle.left_below_one_call and straddle.left_below_any_call


@pytest.mark.parametrize("settings_overrides", [{"anthropic_api_key": "sk-ant-test-not-a-real-key"}])
def test_usage_says_why_members_get_no_answers_with_room_left(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    """#362: "$1.65 of $2" with no answers. Once what's left is below what the smallest private
    message sets aside, Usage says members wait for next month or a higher cap, and why."""
    sign_in(client, provider)
    assert save(client, FORM | {"cap_dollars": "2"}).status_code == 303
    private, channel = least_calls(migrated, "claude-sonnet-5-5")
    spend(migrated, clock, 2_000_000 - private)  # exactly the smallest private message left
    assert 'id="left-low"' not in page(client.get("/admin/assistant"))
    spend(migrated, clock, 1)
    shown = " ".join(page(client.get("/admin/assistant")).split())
    left = private - 1
    assert (
        "The Assistant page and direct messages are paused until next month or a higher cap: each message first "
        f"sets aside about {format_dollars(private)} with claude-sonnet-5-5, and {dollars_down(left)} is left. "
        "Short channel-thread messages still fit."
    ) in shown
    spend(migrated, clock, left - channel)  # exactly a channel thread's smallest message left
    shown = " ".join(page(client.get("/admin/assistant")).split())
    assert "Short channel-thread messages still fit." in shown and "The assistant is paused" not in shown
    spend(migrated, clock, 1)  # a micro-dollar short of a channel thread's too
    shown = " ".join(page(client.get("/admin/assistant")).split())
    assert (
        "The assistant is paused until next month or a higher cap: its smallest message sets aside about "
        f"{format_dollars(channel)} with claude-sonnet-5-5, and {dollars_down(channel - 1)} is left."
    ) in shown
    assert 'id="cap-low"' not in shown


def dollars_down(micros: int) -> str:
    cents = micros // 10_000
    return f"${cents // 100}.{cents % 100:02d}"
