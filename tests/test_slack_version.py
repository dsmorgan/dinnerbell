# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which version is answering in Slack: ``/dinnerbell version``, the help footer, and the Home
footer, on a release and on a ``main`` build, with and without an instance label (issue #46)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import create_engine, text

from dinnerbell import __version__
from dinnerbell.channels.slack import blocks as bk
from dinnerbell.channels.slack.interactions import JOIN_FIRST, WRONG_WORKSPACE
from dinnerbell.config import Settings
from dinnerbell.db.migrate import head_revision
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.health import schema_revision
from tests.support.blockkit import check_mrkdwn, check_view
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_slack import OTHER_TEAM
from tests.support.slack import Slack, wire

pytestmark = pytest.mark.anyio

REPO = "https://github.com/dsmorgan/dinnerbell"
MAIN_BUILD = f"{__version__}+abc1234"


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


@pytest.fixture
async def release(house: House) -> AsyncIterator[Slack]:
    """Production running a release image: no build stamp, no label."""
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


@pytest.fixture
async def test_build(house: House) -> AsyncIterator[Slack]:
    """Test running a ``main`` build, with its own command and label."""
    wired = await wire(house, command="/dinnerbell-test", instance_label="TEST", version=MAIN_BUILD)
    yield wired
    await wired.conn.stop()


def reply(response: object) -> str:
    payload = json.loads(getattr(response, "body", "") or "{}")
    assert payload["response_type"] == "ephemeral"
    text = payload["text"]
    assert isinstance(text, str)
    check_mrkdwn(text, "reply")
    return text


async def test_version_on_a_release(release: Slack) -> None:
    text = reply(await release.command("Kim", "version"))
    assert text == (
        f"Dinnerbell {__version__} · database {head_revision()} · <{REPO}/releases/tag/v{__version__}|release notes>"
    )
    assert release.fake.calls == []


async def test_version_on_a_labelled_main_build(test_build: Slack) -> None:
    text = reply(await test_build.command("Kim", "VERSION", command="/dinnerbell-test"))
    assert text == (
        f"Dinnerbell {MAIN_BUILD} · TEST · database {head_revision()} · <{REPO}/commit/abc1234|commit abc1234>"
    )


async def test_version_needs_the_workspace_and_a_known_member(release: Slack) -> None:
    """Someone Dinnerbell doesn't know gets the short join reply: no version, revision, or URL."""
    for word in ("version", "help", "prefs", "catalog", "history"):
        stranger = reply(await release.command("U9STRANGER", word))
        assert stranger == JOIN_FIRST and "http" not in stranger and head_revision() not in stranger
    assert reply(await release.command("Kim", "version", team=OTHER_TEAM)) == WRONG_WORKSPACE


@pytest.mark.parametrize("text", ["", "help", "what"])
async def test_help_lists_version_and_ends_with_the_footer(release: Slack, text: str) -> None:
    lines = reply(await release.command("Kim", text)).splitlines()
    assert "• `/dinnerbell version` says which version of Dinnerbell is answering." in lines
    assert lines[-1] == f"Dinnerbell {__version__}"


async def test_help_footer_names_the_build_and_label(test_build: Slack) -> None:
    lines = reply(await test_build.command("Kim", "help", command="/dinnerbell-test")).splitlines()
    assert "• `/dinnerbell-test version` says which version of Dinnerbell is answering." in lines
    assert lines[-1] == f"Dinnerbell {MAIN_BUILD} · TEST"


@pytest.mark.parametrize("who", ["Sam", "U9STRANGER"])
async def test_home_ends_with_the_footer(release: Slack, who: str) -> None:
    await release.open_home(who)
    home = release.last_home(who)
    check_view(home)
    assert home["blocks"][-1] == bk.context(f"Dinnerbell {__version__}")


@pytest.mark.parametrize("who", ["Sam", "U9STRANGER"])
async def test_labelled_home_footer_names_the_build_and_label(test_build: Slack, who: str) -> None:
    await test_build.open_home(who)
    home = test_build.last_home(who)
    check_view(home)
    assert home["blocks"][-1] == bk.context(f"Dinnerbell {MAIN_BUILD} · TEST")


def test_a_full_view_gets_no_footer() -> None:
    """The last block of a full Home may be a call's buttons, so the footer never replaces it."""
    full = {"type": "home", "blocks": [bk.divider()] * (bk.MAX_VIEW_BLOCKS - 1) + [bk.actions(block_id="last")]}
    assert bk.with_version_footer(full, "0.3.0") == full
    one_short = {"type": "home", "blocks": full["blocks"][1:]}
    footed = bk.with_version_footer(one_short, "0.3.0")
    assert footed["blocks"] == [*one_short["blocks"], bk.context("Dinnerbell 0.3.0")]
    assert len(one_short["blocks"]) == bk.MAX_VIEW_BLOCKS - 1  # the view passed in is left alone


def test_version_link_and_line() -> None:
    assert bk.version_link("1.2.3") == f"<{REPO}/releases/tag/v1.2.3|release notes>"
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert bk.version_link(f"1.2.3+{sha}") == f"<{REPO}/commit/{sha}|commit {sha}>"
    assert bk.version_line("1.2.3", "") == "Dinnerbell 1.2.3"
    assert bk.version_line("1.2.3", "<!here>") == "Dinnerbell 1.2.3 · &lt;!here&gt;"


def test_schema_revision_is_what_serve_migrates_to(migrated: Settings) -> None:
    engine = create_engine(migrated.sync_database_url)
    try:
        with engine.connect() as connection:
            recorded = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()
    assert schema_revision() == recorded == head_revision()
