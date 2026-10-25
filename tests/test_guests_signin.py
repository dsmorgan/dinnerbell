# SPDX-License-Identifier: AGPL-3.0-or-later
"""A guest matches by Slack user at sign-in only with Slack as the issuer (#38), not merely
because a Slack team ID is set."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from dinnerbell.config import Settings
from dinnerbell.services.guests import SlackPerson
from tests.conftest import sign_in
from tests.support.fake_oidc import FakeProvider

TEAM = "T-HOUSE"


@pytest.mark.parametrize("settings_overrides", [{"slack_team_id": TEAM}])  # another issuer, though a team is set
def test_a_guest_matches_by_slack_user_only_with_slack_as_the_issuer(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    engine = create_engine(migrated.sync_database_url)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO member (household_id, display_name, email, slack_user_id, role, can_cook, can_pickup, "
                "can_order, mute_dm_reminders, feedback_reminder, active, slack_link_locked, created_at, updated_at) "
                "VALUES (1, 'Pat', NULL, 'U07PAT', 'guest', 0, 0, 0, 0, 0, 1, 0, '2026-10-22', '2026-10-22')"
            )
        )
    engine.dispose()
    claims = {
        "sub": "sub-pat",
        "email": "pat@elsewhere.example",
        "https://slack.com/team_id": TEAM,
        "https://slack.com/user_id": "U07PAT",
    }
    assert sign_in(client, provider, claims=claims).status_code == 403


class AnyoneInTheChannel:
    """``SlackPeople`` that says everyone asked about is in the channel, and records who was."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def person(self, slack_user_id: str) -> SlackPerson:
        self.asked.append(slack_user_id)
        return SlackPerson(slack_user_id, "Nova")

    async def channel_people(self) -> list[SlackPerson]:
        return []


@pytest.mark.parametrize("settings_overrides", [{"slack_team_id": TEAM}])
def test_signing_in_joins_no_one_without_slack_as_the_issuer(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    """Another provider's tokens can carry Slack's claim names: joining from the channel (option
    B) never asks Slack about them, so they make no guest."""
    people = AnyoneInTheChannel()
    state = client.app.state  # type: ignore[attr-defined]
    state.dinnerbell = replace(state.dinnerbell, slack_people=people)
    claims = {
        "sub": "sub-nova",
        "email": "nova@elsewhere.example",
        "https://slack.com/team_id": TEAM,
        "https://slack.com/user_id": "U09NOVA",
    }
    assert sign_in(client, provider, claims=claims).status_code == 403
    assert people.asked == []
    engine = create_engine(migrated.sync_database_url)
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM member WHERE slack_user_id = 'U09NOVA'")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM audit WHERE action LIKE 'member.join%'")).scalar() == 0
    engine.dispose()
