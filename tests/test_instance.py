# SPDX-License-Identifier: AGPL-3.0-or-later
"""A second instance for testing (ADR 0006): the configured slash command, and the instance
label on the web, in Slack, in /healthz, and in the logs. With no label, output is unchanged."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dinnerbell import main as cli
from dinnerbell.channels.slack.blocks import instance_banner
from dinnerbell.config import Settings
from dinnerbell.logs import JsonFormatter, configure_logging
from dinnerbell.services.database import Database, open_database
from tests.conftest import sign_in
from tests.support.calls import OPENS, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.slack import Slack, wire
from tests.test_config import REQUIRED
from tests.test_healthz import wait_for_tick

LABEL = "TEST"
TEST_COMMAND = "/dinnerbell-test"
BANNER = instance_banner(LABEL)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


@pytest.fixture
async def test_slack(house: House) -> AsyncIterator[Slack]:
    """Slack as a test instance sees it: its own command and the TEST label."""
    wired = await wire(house, command=TEST_COMMAND, instance_label=LABEL)
    yield wired
    await wired.conn.stop()


@pytest.fixture
async def plain_slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


def reply_text(response: Any) -> str:
    return str(json.loads(response.body)["text"])


# The slash command -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_configured_command_is_the_one_registered(test_slack: Slack) -> None:
    response = await test_slack.command("Kim", "help", command=TEST_COMMAND)
    assert response.status == 200
    assert f"`{TEST_COMMAND} call` starts a dinner call" in reply_text(response)
    # Production's command belongs to the production app; this instance doesn't answer it.
    other = await test_slack.command("Kim", "help", command="/dinnerbell")
    assert other.status == 404
    assert test_slack.fake.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["", "help", "what"])
async def test_replies_name_the_configured_command(test_slack: Slack, text: str) -> None:
    reply = reply_text(await test_slack.command("Kim", text, command=TEST_COMMAND))
    assert f"`{TEST_COMMAND} call`" in reply
    assert "`/dinnerbell " not in reply


@pytest.mark.anyio
async def test_call_usage_names_the_configured_command(test_slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    await test_slack.command("Kim", "call dinner soonish", command=TEST_COMMAND)
    (usage,) = test_slack.ephemerals("Kim")
    assert usage.startswith(f"Try `{TEST_COMMAND} call`, `{TEST_COMMAND} call breakfast`")
    assert "`/dinnerbell " not in usage


# The label in Slack ------------------------------------------------------------------------------


async def run_a_call(slack: Slack, house: House) -> None:
    """Open a call, answer it, and move it to voting: a call post, its updates, the picks in
    the thread, and a reminder DM."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    await slack.drain()
    for who in ("Alex", "Sam"):
        await slack.click(who, "respond_in", str(event_id))
    await slack.click("Alex", "offer_pickup", str(event_id))
    house.clock.now = RESPOND_BY
    await house.tick()
    await slack.drain()


@pytest.mark.anyio
async def test_every_message_starts_with_the_label(test_slack: Slack, house: House) -> None:
    await run_a_call(test_slack, house)
    sent = test_slack.fake.of("chat.postMessage", "chat.update")
    assert {c.method for c in sent} == {"chat.postMessage", "chat.update"}
    assert any(c.args.get("thread_ts") for c in sent)  # replies in the thread too
    for call in sent:
        blocks = call.args["blocks"]
        if isinstance(blocks, str):
            blocks = json.loads(blocks)
        assert blocks[0] == BANNER, call.method
        assert BANNER not in blocks[1:]


@pytest.mark.anyio
async def test_without_a_label_messages_have_no_banner(plain_slack: Slack, house: House) -> None:
    await run_a_call(plain_slack, house)
    sent = plain_slack.fake.of("chat.postMessage", "chat.update")
    assert sent
    assert all("instance" not in json.dumps(c.args) for c in sent)


@pytest.mark.anyio
async def test_the_home_tab_shows_the_label(test_slack: Slack, plain_slack: Slack) -> None:
    await test_slack.open_home("Sam")
    header = test_slack.last_home("Sam")["blocks"][0]
    assert header["type"] == "header"
    assert header["text"]["text"].startswith(f"{LABEL} · Today · ")
    await test_slack.open_home("U9STRANGER")  # not a member: no header, so the banner leads
    assert test_slack.last_home("U9STRANGER")["blocks"][0] == BANNER
    await plain_slack.open_home("Sam")
    assert plain_slack.last_home("Sam")["blocks"][0]["text"]["text"].startswith("Today · ")
    await plain_slack.open_home("U9STRANGER")
    assert plain_slack.last_home("U9STRANGER")["blocks"][0]["type"] == "section"


# The label on the web, in /healthz, and in the logs ----------------------------------------------


@pytest.mark.parametrize("settings_overrides", [{"instance_label": LABEL}])
def test_pages_show_a_banner(client: TestClient, provider: FakeProvider) -> None:
    # Inside the sticky top bar, so it stays in view on scroll.
    banner = (
        '<header class="topbar">\n'
        '    <div class="instance-banner" role="note">TEST instance</div>\n'
        '    <a class="brand"'
    )
    signed_out = client.get("/")
    assert signed_out.status_code == 200
    sign_in(client, provider)
    for text in (signed_out.text, client.get("/").text):
        assert banner in text
        assert "<title>TEST · " in text


def test_without_a_label_pages_have_no_banner(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    text = client.get("/").text
    assert "instance" not in text
    # The template's output is unchanged around both insertions.
    assert '}\'>\n  <header class="topbar">\n    <a class="brand" href="/">' in text
    assert "<title>Today · " in text


@pytest.mark.parametrize("settings_overrides", [{"instance_label": LABEL, "slack_channel": "C0SECRETCH"}])
def test_healthz_names_the_instance_but_never_the_channel(client: TestClient) -> None:
    body = wait_for_tick(client)
    assert body["instance"] == LABEL
    assert set(body) == {"status", "version", "database", "scheduler", "slack", "backup", "outbox", "instance"}
    assert "C0SECRETCH" not in client.get("/healthz").text


def test_healthz_without_a_label_has_no_instance(client: TestClient) -> None:
    assert "instance" not in wait_for_tick(client)


def test_log_entries_carry_the_label() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    assert json.loads(JsonFormatter(LABEL).format(record))["instance"] == LABEL
    assert "instance" not in json.loads(JsonFormatter().format(record))
    record.instance = "PROD"  # an extra by the same name can't replace the label
    assert json.loads(JsonFormatter(LABEL).format(record))["instance"] == LABEL


def test_the_cli_configures_logs_with_the_label(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DINNERBELL_INSTANCE_LABEL", LABEL)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "configure_logging", lambda level, instance="": seen.append((level, instance)))
    monkeypatch.setattr(cli, "_migrate", lambda settings: True)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    assert cli.main(["serve"]) == 0
    assert seen == [("INFO", LABEL)]


def test_configure_logging_defaults_to_no_label() -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging("INFO")
        formatter = root.handlers[0].formatter
        assert isinstance(formatter, JsonFormatter) and formatter.instance == ""
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)
