# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optional assistant in Slack (ADR 0009, spec 6.14): DMs and mentions, which messages are
ignored, conversation keys, replies in the thread, App Home's line, the assistant manifests, and
the app's wiring. Slack is ``FakeSlack`` and the model is ``FakeModel``; nothing reaches either."""

from __future__ import annotations

import asyncio
import copy
import random
import re
import unicodedata
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from dinnerbell.app_factory import create_app
from dinnerbell.channels.slack import assistant as slack_assistant
from dinnerbell.channels.slack.assistant import (
    DOTS,
    MAX_REPLY,
    STILL_ON_IT,
    TEXT_ONLY,
    WORD_JOINER,
    defang,
    home_line,
    mention_only_line,
    readable,
    reply_message,
    safe_reply,
)
from dinnerbell.config import Settings
from dinnerbell.db.migrate import run_migrations
from dinnerbell.db.models import AssistantConversationRow
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.assistant import AssistantSettings
from dinnerbell.domain.members import Role
from dinnerbell.services.assistant import admin
from dinnerbell.services.assistant.anthropic_client import AnthropicModelClient
from dinnerbell.services.assistant.client import ModelReply
from dinnerbell.services.assistant.conversation import FAILED, Assistant, Reply
from dinnerbell.services.assistant.drafts import DraftView
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.database import Database, open_database
from tests.conftest import make_settings
from tests.support.calls import OPENS, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer
from tests.support.fake_oidc import FakeProvider
from tests.support.fake_slack import (
    APP_TOKEN,
    BOT_TOKEN,
    BOT_USER,
    CHANNEL,
    OTHER_TEAM,
    TEAM,
    Failure,
    FakeSlack,
    FakeSocket,
    message_event,
    next_ts,
    send,
)
from tests.support.slack import BASE_URL, SLACK_IDS, Slack, link, wire

pytestmark = pytest.mark.anyio

KEY = "sk-ant-test-not-a-real-key"
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SAM_DM = "D02SAM"


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"anthropic_api_key": KEY}


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> AsyncIterator[House]:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    yield house
    await house.close()


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def helper(settings: Settings, model: FakeModel, house: House) -> Assistant:
    return Assistant(AssistantConfig.from_settings(settings), model, house.clock)


@pytest.fixture
async def slack(house: House, helper: Assistant) -> Slack:
    return await wire(house, assistant=helper)


async def switch_on(house: House, helper: Assistant, **values: Any) -> None:
    async with house.sessionmaker() as db:
        await admin.update_assistant(
            db, helper.config, house["Alex"], AssistantSettings(**({"enabled": True} | values)), now=house.clock()
        )


async def conversations(house: House) -> list[AssistantConversationRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AssistantConversationRow).order_by(AssistantConversationRow.id)))


def posts(slack: Slack) -> list[dict[str, Any]]:
    return [c.args for c in slack.fake.of("chat.postMessage")]


def user_texts(model: FakeModel) -> list[str]:
    """The current request's text of each request, after its header line."""
    return [
        request["messages"][-1]["content"].split("\n\n", 1)[1]
        for request in model.requests
        if isinstance(request["messages"][-1]["content"], str)
    ]


async def nothing_happened(slack: Slack, model: FakeModel, house: House) -> None:
    assert model.requests == []
    assert await conversations(house) == []
    assert posts(slack) == []


# Ignored ------------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("settings_overrides", [{}])
async def test_without_a_key_nothing_is_built_or_listened_for(
    settings: Settings, house: House, model: FakeModel
) -> None:
    assert settings.anthropic_api_key is None
    slack = await wire(house)
    assert slack.conn.assistant is None and slack.conn.interactions.home_hint is None
    await slack.dm("Sam", "What Thai places do we have?")
    await slack.mention("Sam", f"<@{BOT_USER}> what's for dinner?")
    await nothing_happened(slack, model, house)


@pytest.mark.flag("assistant", on=False)
async def test_with_the_flag_off_dms_and_mentions_are_ignored(
    settings: Settings, house: House, model: FakeModel, slack: Slack
) -> None:
    assert settings.anthropic_api_key is not None
    await slack.dm("Sam", "What Thai places do we have?")
    await slack.mention("Sam", f"<@{BOT_USER}> what's for dinner?")
    await slack.open_home("Sam")
    await nothing_happened(slack, model, house)
    assert "Message Dinnerbell" not in str(slack.last_home("Sam"))


@pytest.mark.flag("assistant", on=True)
async def test_with_the_switch_off_dms_and_mentions_are_ignored(
    settings: Settings, house: House, model: FakeModel, slack: Slack
) -> None:
    await slack.dm("Sam", "What Thai places do we have?")
    await slack.mention("Sam", f"<@{BOT_USER}> what's for dinner?")
    await slack.open_home("Sam")
    await nothing_happened(slack, model, house)
    assert "Message Dinnerbell" not in str(slack.last_home("Sam"))


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize(
    "event",
    [
        pytest.param({"bot_id": "B0OTHER"}, id="another-bot"),
        pytest.param({"bot_id": "B0BOT", "user": BOT_USER}, id="dinnerbell-itself"),
        pytest.param({"user": BOT_USER}, id="dinnerbells-user"),
        pytest.param({"subtype": "message_changed"}, id="an-edit"),
        pytest.param({"subtype": "message_deleted"}, id="a-deletion"),
        pytest.param({"subtype": "channel_join"}, id="a-join"),
        pytest.param({"subtype": "bot_message"}, id="a-bot-message"),
        pytest.param({"bot_profile": {"id": "B0X"}}, id="a-bot-profile"),
        pytest.param({"team": OTHER_TEAM}, id="another-workspace"),
        pytest.param({"channel_type": "mpim", "channel": "G0GROUP"}, id="a-group-dm"),
        pytest.param({"channel_type": "channel", "channel": CHANNEL}, id="a-channel-message"),
        pytest.param({"ts": "not-a-ts"}, id="a-bad-ts"),
        pytest.param({"thread_ts": "nope"}, id="a-bad-thread"),
        pytest.param({"channel": "nope"}, id="a-bad-channel"),
        pytest.param({"edited": {"user": "U02SAM", "ts": "1.2"}}, id="edited"),
        pytest.param({"subtype": "file_share", "bot_id": "B0OTHER"}, id="a-bots-file"),
        pytest.param({"user": None}, id="no-user"),
    ],
)
async def test_dms_dinnerbell_never_answers(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack, event: dict[str, Any]
) -> None:
    await switch_on(house, helper)
    event = dict(event)
    user = event.pop("user", SLACK_IDS["Sam"])
    payload = message_event(user, "What Thai places do we have?", channel=SAM_DM)
    payload["team_id"] = event.pop("team", TEAM)
    payload["event"].update(event)
    await send(slack.conn.app, "events_api", payload)
    for _ in range(3):  # Bolt runs auto-acknowledged event listeners in its own future
        await asyncio.sleep(0)
    await slack.conn.interactions.settle()
    await nothing_happened(slack, model, house)
    assert len(seen(slack)) == 0  # dropped as it arrived, before any lookup


def seen(slack: Slack) -> Any:
    assert slack.conn.assistant is not None
    return slack.conn.assistant.seen


@pytest.mark.flag("assistant", on=True)
async def test_a_dm_of_only_the_mention_is_taken_but_sends_nothing(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    await slack.dm("Sam", f"<@{BOT_USER}>  ")
    await nothing_happened(slack, model, house)
    assert len(seen(slack)) == 1


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize(
    "event",
    [
        pytest.param({"subtype": "file_share", "files": [{"id": "F1"}]}, id="a-file"),
        pytest.param({"files": [{"id": "F1"}], "text": "Look"}, id="a-file-with-a-caption"),
        pytest.param({"text": None}, id="no-text"),
        pytest.param({"text": "   "}, id="blank"),
    ],
)
async def test_a_dm_without_text_gets_the_text_only_line_and_sends_nothing(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack, event: dict[str, Any]
) -> None:
    await switch_on(house, helper)
    payload = message_event("U02SAM", "", channel=SAM_DM)
    payload["event"].update(event)
    await send(slack.conn.app, "events_api", payload)
    await _until(lambda: len(posts(slack)) == 1)
    await slack.conn.interactions.settle()
    (reply,) = posts(slack)
    assert reply["text"] == TEXT_ONLY and reply["thread_ts"] == payload["event"]["ts"]
    assert model.requests == [] and await conversations(house) == []


@pytest.mark.flag("assistant", on=True)
async def test_a_guests_file_gets_nothing(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    async with house.sessionmaker() as db:
        await MemberRepository(db).update(
            house.household.id, house["Sam"].id, house.clock(), role=Role.GUEST, email=None
        )
        await db.commit()
    await slack.dm("Sam", "", subtype="file_share", files=[{"id": "F1"}])
    await nothing_happened(slack, model, house)


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"channel": "C0OTHER"}, id="another-channel"),
        pytest.param({"channel": SAM_DM}, id="a-dm"),
        pytest.param({"team": OTHER_TEAM}, id="another-workspace"),
        pytest.param({"bot_id": "B0OTHER"}, id="a-bot"),
        pytest.param({"subtype": "thread_broadcast"}, id="a-subtype"),
        pytest.param({"subtype": "file_share"}, id="a-file-share"),
        pytest.param({"edited": {"user": "U02SAM", "ts": "1.2"}}, id="edited"),
        pytest.param({"files": [{"id": "F1"}]}, id="with-a-file"),
        pytest.param({"text": None}, id="no-text"),
    ],
)
async def test_mentions_dinnerbell_never_answers(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack, options: dict[str, Any]
) -> None:
    await switch_on(house, helper)
    options = dict(options)
    payload = message_event(
        SLACK_IDS["Sam"],
        f"<@{BOT_USER}> what's for dinner?",
        kind="app_mention",
        channel=options.pop("channel", CHANNEL),
        channel_type=None,
    )
    payload["team_id"] = options.pop("team", TEAM)
    payload["event"].update(options)
    await send(slack.conn.app, "events_api", payload)
    for _ in range(3):
        await asyncio.sleep(0)
    await slack.conn.interactions.settle()
    await nothing_happened(slack, model, house)
    assert len(seen(slack)) == 0


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("who", ["guest", "inactive", "unlinked", "stranger"])
async def test_only_active_members_get_an_answer(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack, who: str
) -> None:
    await switch_on(house, helper)
    async with house.sessionmaker() as db:
        repo = MemberRepository(db)
        if who == "guest":
            await repo.update(house.household.id, house["Kim"].id, house.clock(), role=Role.GUEST, email=None)
        elif who == "inactive":
            await repo.update(house.household.id, house["Kim"].id, house.clock(), active=False)
        await db.commit()
    if who == "unlinked":
        await link(house, "Kim", None)
    sender = "U09NOBODY" if who == "stranger" else "Kim"
    await slack.dm(sender, "What Thai places do we have?")
    await slack.mention(sender, f"<@{BOT_USER}> what's for dinner?")
    await nothing_happened(slack, model, house)
    assert len(seen(slack)) == 2  # taken, then dropped at the member check
    await slack.open_home(sender)
    assert all("Message Dinnerbell" not in str(c.args["view"]) for c in slack.fake.of("views.publish"))


# Conversation keys -------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_a_top_level_dm_starts_a_private_conversation_answered_in_its_thread(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    model.script = [answer("Siam Garden is Thai."), answer("It does pickup.")]
    root = next_ts()
    await slack.dm("Sam", "What Thai places do we have?", ts=root)
    (reply,) = posts(slack)
    assert (reply["channel"], reply["thread_ts"], reply["text"]) == (SAM_DM, root, "Siam Garden is Thai.")
    (row,) = await conversations(house)
    assert (row.key, row.private, row.owner_member_id) == (f"slack:{SAM_DM}:{root}", True, house["Sam"].id)

    await slack.dm("Sam", "Does it do pickup?", thread_ts=root)  # a reply in the DM's thread
    second = posts(slack)[1]
    assert (second["thread_ts"], second["text"]) == (root, "It does pickup.")
    (row,) = await conversations(house)
    assert len(row.transcript) == 4  # both exchanges, in the one conversation
    assert "mention" not in second["text"] and "mention" not in reply["text"]  # DMs never say it

    await slack.dm("Sam", "Something else?")  # a new top-level DM: a new conversation
    assert len(await conversations(house)) == 2


@pytest.mark.flag("assistant", on=True)
async def test_a_top_level_mention_starts_a_shared_conversation_rooted_at_it(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    root = next_ts()
    await slack.mention("Sam", f"<@{BOT_USER}> what Thai places do we have?", ts=root)
    (reply,) = posts(slack)
    assert (reply["channel"], reply["thread_ts"]) == (CHANNEL, root)
    (row,) = await conversations(house)
    assert (row.key, row.private, row.owner_member_id) == (f"slack:{CHANNEL}:{root}", False, None)
    assert user_texts(model) == ["what Thai places do we have?"]  # the mention is stripped


@pytest.mark.flag("assistant", on=True)
async def test_a_mention_in_a_thread_joins_that_threads_conversation_as_its_author(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    root = next_ts()
    await slack.mention("Sam", f"<@{BOT_USER}> any Thai places?", ts=root)
    await slack.mention("Kim", f"Something cheaper <@{BOT_USER}>", thread_ts=root)
    first, second = posts(slack)
    assert first["thread_ts"] == second["thread_ts"] == root
    assert mention_only_line(BOT_USER) in first["text"]
    assert mention_only_line(BOT_USER) not in second["text"]  # only the first reply says it
    (row,) = await conversations(house)
    assert row.key == f"slack:{CHANNEL}:{root}"
    assert row.pseudonyms == {str(house["Sam"].id): 1, str(house["Kim"].id): 2}  # each acts as its author
    assert user_texts(model) == ["any Thai places?", "Something cheaper"]


@pytest.mark.flag("assistant", on=True)
async def test_a_mention_under_a_call_post_roots_at_the_post(
    settings: Settings,
    house: House,
    model: FakeModel,
    helper: Assistant,
    slack: Slack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await switch_on(house, helper)
    house.clock.now = OPENS
    await house.tick()
    await slack.drain()
    call_ts = _call_post_ts(slack)
    slack.fake.clear()
    seen_calls = record_call_ids(monkeypatch)
    await slack.mention("Lee", f"<@{BOT_USER}> who's cooking?", thread_ts=call_ts)
    (reply,) = posts(slack)
    assert (reply["channel"], reply["thread_ts"]) == (CHANNEL, call_ts)
    (row,) = await conversations(house)
    assert row.key == f"slack:{CHANNEL}:{call_ts}"
    assert seen_calls == [await dinner_id(house)]  # the post's call is the default call

    other = next_ts()
    await slack.mention("Lee", f"<@{BOT_USER}> hi", ts=other)  # top-level: no call
    await slack.mention("Lee", f"<@{BOT_USER}> and?", thread_ts=other)  # under a member's post: none
    assert seen_calls[1:] == [None, None]


def record_call_ids(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """Each ``call_id`` the Slack adapter passes ``converse``, which still runs."""
    seen: list[int | None] = []
    real = slack_assistant.converse

    async def recording(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("call_id"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(slack_assistant, "converse", recording)
    return seen


def fake_drafts(monkeypatch: pytest.MonkeyPatch, *titles: str) -> None:
    async def drafting(*args: Any, **kwargs: Any) -> Reply:
        drafts = tuple(DraftView(n, "proposal", title, ("Confirm adds it.",)) for n, title in enumerate(titles, 1))
        return Reply("Here's a draft for tonight.", drafts)

    monkeypatch.setattr(slack_assistant, "converse", drafting)


@pytest.mark.flag("assistant", on=True)
async def test_a_reply_with_drafts_in_the_channel_goes_only_to_its_author(
    settings: Settings, house: House, helper: Assistant, slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    await switch_on(house, helper)
    fake_drafts(monkeypatch, "Siam Garden", "evil.example")
    root = next_ts()
    await slack.mention("Sam", f"<@{BOT_USER}> propose Thai", ts=root)
    assert posts(slack) == []
    sent, *cards = [c.args for c in slack.fake.of("chat.postEphemeral")]
    for each in (sent, *cards):
        assert (each["channel"], each["user"], each["thread_ts"]) == (CHANNEL, "U02SAM", root)
    assert sent["text"].startswith("Here's a draft for tonight.")
    shown, note = sent["blocks"]
    assert shown == {"type": "markdown", "text": "Here's a draft for tonight."}
    assert note["type"] == "context" and "mention <@" in note["elements"][0]["text"]
    assert [card["text"] for card in cards] == ["Siam Garden", "evil[.]example"]
    assert all(card["blocks"] for card in cards)


@pytest.mark.flag("assistant", on=True)
async def test_a_reply_with_drafts_in_a_dm_is_a_normal_reply(
    settings: Settings, house: House, helper: Assistant, slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    await switch_on(house, helper)
    fake_drafts(monkeypatch, "Siam Garden")
    await slack.dm("Sam", "Propose Thai")
    reply, card = posts(slack)
    assert reply["text"] == "Here's a draft for tonight."
    assert reply["blocks"] == [{"type": "markdown", "text": "Here's a draft for tonight."}]
    assert card["text"] == "Siam Garden" and card["blocks"] and card["thread_ts"] == reply["thread_ts"]
    assert slack.fake.of("chat.postEphemeral") == []


def test_a_reply_with_only_drafts_has_no_text_and_no_blocks() -> None:
    assert reply_message("") == ("", [])


@pytest.mark.parametrize(
    ("text", "notified", "shown"),
    [
        ("**Tag**\n- fast food", "**Tag**\n- fast food", "**Tag**\n- fast food"),
        (
            "[see](https://evil.example)",
            "[see](hxxps[:]//evil[.]example)",
            "\\[see\\](hxxps\\[:\\]//evil\\[.\\]example)",
        ),
        ("<!channel> & @here", "&lt;!channel&gt; &amp; @\u2060here", "\u2039!channel> \\& @\u2060here"),
    ],
)
def test_a_reply_is_a_markdown_block_with_a_mrkdwn_copy_for_notifications(text: str, notified: str, shown: str) -> None:
    """#284: the block renders the model's Markdown, escaped as a streamed reply is, so no link,
    mention, or ping forms; the text, only in notifications, is mrkdwn as before."""
    assert reply_message(text) == (notified, [{"type": "markdown", "text": shown}])


def _call_post_ts(slack: Slack) -> str:
    """The ``ts`` FakeSlack gave the call message, the first post to the channel."""
    posted = [c for c in slack.fake.of("chat.postMessage") if c.args["channel"] == CHANNEL]
    assert posted, "the call didn't post"
    index = slack.fake.calls.index(posted[0])
    order = [c for c in slack.fake.calls[: index + 1] if c.method == "chat.postMessage"]
    return f"{len(order)}.000100"


# Replies -----------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_model_text_cant_ping_mention_or_link(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    loud = "<!channel> <!here> @here @Everyone <@U02SAM> <#C0DINNER> <https://evil.example|Menu> & more"
    model.script = [answer(loud)]
    await slack.dm("Sam", "Say something loud")
    (reply,) = posts(slack)
    text = reply["text"]
    assert "<" not in text and ">" not in text
    assert "&lt;!channel&gt;" in text and "&lt;@U02SAM&gt;" in text and "&amp; more" in text
    assert f"@{WORD_JOINER}here" in text and f"@{WORD_JOINER}Everyone" in text
    assert "@here" not in text.replace(f"@{WORD_JOINER}here", "")
    assert reply["unfurl_links"] is False and reply["unfurl_media"] is False


LINKS = [
    ("https://evil.example/login", "hxxps[:]//evil[.]example/login"),
    ("http://evil.example", "hxxp[:]//evil[.]example"),
    ("HTTPS://Evil.Example", "hxxps[:]//Evil[.]Example"),
    ("go to evil.example now", "go to evil[.]example now"),
    ("www.evil.example/menu?x=1", "www[.]evil[.]example/menu?x=1"),
    ("sub.evil.co.uk/a/b", "sub[.]evil[.]co[.]uk/a/b"),
    ("mail sam@evil.example", "mail sam@evil[.]example"),
    ("ftp://files.example", "ftp[:]//files[.]example"),
    ("http://10.0.0.1/x", "hxxp[:]//10[.]0[.]0[.]1/x"),
    ("evil\uff0eexample", "evil[.]example"),
    ("_evil.example_", "_evil[.]example_"),
    ("evil.example-", "evil[.]example-"),
    ("~evil.example~", "~evil[.]example~"),
    ("*evil.example*", "*evil[.]example*"),
    ("`evil.example`", "`evil[.]example`"),
    ("evil.example/path?x=1", "evil[.]example/path?x=1"),
    ("sub.evil.example", "sub[.]evil[.]example"),
    ("EVIL.EXAMPLE", "EVIL[.]EXAMPLE"),
    ("xn--fa-hia.example", "xn--fa-hia[.]example"),
    ("смотри пример.рф", "смотри пример[.]рф"),
    ("例え.jp", "例え[.]jp"),
    ("(evil.example), «evil.example»", "(evil[.]example), «evil[.]example»"),
    ("www.1example", "www[.]1example"),
    ("mailto:sam@evil.example", "mailto[:]sam@evil[.]example"),
    ("$12.50, e.g. 3.5 stars, v1.2", "$12.50, e.g. 3.5 stars, v1.2"),
]
NORMAL = ["$3.50", "3.5 stars", "e.g. tacos", "Done. Next", "i.e. the U.S. one"]


@pytest.mark.parametrize(("raw", "defanged"), LINKS)
def test_every_url_and_bare_domain_is_defanged(raw: str, defanged: str) -> None:
    assert defang(raw) == defanged
    assert safe_reply(raw) == defanged


@pytest.mark.parametrize("text", NORMAL)
def test_normal_text_keeps_its_dots(text: str) -> None:
    assert defang(text) == text


ACUTE = "\u0301"  # a combining mark
BYPASSES = [
    (f"evil{ACUTE}.example", "evi\u013a[.]example"),  # NFKC composes the mark onto its letter
    ("evil\u200b.example", "evil[.]example"),  # a zero-width space before the dot
    ("evil.\u00adexample", "evil[.]example"),  # a soft hyphen after it
    (f"evil.{ACUTE}example", f"evil[.]{ACUTE}example"),  # a mark after the dot
    ("evil.\u200d\u2060\ufeff\u202eexample", "evil[.]example"),  # joiners, a BOM, a bidi control
    ("ht\u200btps://evil.example", "hxxps[:]//evil[.]example"),
    ("www\u200b.evil.example", "www[.]evil[.]example"),
    ("evil\uff0eexample evil\u3002example evil\uff61example", "evil[.]example evil[.]example evil[.]example"),
    ("evil.пример evil.παράδειγμα", "evil[.]пример evil[.]παράδειγμα"),
    ("3.evil", "3[.]evil"),
    ("Done.Next", "Done[.]Next"),
    ("e.g.evil", "e[.]g[.]evil"),  # not a whole token, so not kept
    ("xe.g. tacos", "xe[.]g. tacos"),
    (f"e.g.{ACUTE}evil", f"e[.]g[.]{ACUTE}evil"),
    ("evil.\u24d4\u24e7\u24d0\u24dc\u24df\u24db\u24d4", "evil[.]example"),  # enclosed alphanumerics
    ("evil.\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45", "evil[.]example"),  # fullwidth letters
    ("evil.\ufb01sh", "evil[.]fish"),  # the fi ligature
    ("evil.\u2102om", "evil[.]Com"),  # a letterlike symbol
    ("\uff48\uff54\uff54\uff50\uff53\uff1a\uff0f\uff0fevil\uff0eexample", "hxxps[:]//evil[.]example"),
]


@pytest.mark.parametrize(("raw", "defanged"), BYPASSES)
def test_marks_and_invisible_characters_cant_hide_a_dot(raw: str, defanged: str) -> None:
    assert defang(raw) == defanged


@pytest.mark.parametrize(
    "text",
    ["e.g. tacos", "i.e., pizza", "at 7 p.m.", "8 a.m.!", "the U.S. menu", "Tacos vs. pizza", "pans etc.", "E.G. this"],
)
def test_the_abbreviations_keep_their_dots(text: str) -> None:
    assert defang(text) == text


KEPT = re.compile(r"(?i)(e\.g\.|i\.e\.|a\.m\.|p\.m\.|u\.s\.|vs\.|etc\.)")


def _kept(text: str) -> set[int]:
    """The rule, written out again: the dots of an allowlisted abbreviation with no letter, digit,
    or mark right before it, and no letter or digit after it, marks skipped."""
    kept: set[int] = set()
    for start in range(len(text)):
        found = KEPT.match(text, start)
        if found is None:
            continue
        before = text[start - 1] if start else " "
        after = found.end()
        while after < len(text) and unicodedata.category(text[after]).startswith("M"):
            after += 1
        if before.isalnum() or unicodedata.category(before).startswith("M"):
            continue
        if after < len(text) and text[after].isalnum():
            continue
        kept.update(start + n for n, char in enumerate(found.group(0)) if char == ".")
    return kept


def _dot_before_a_letter(text: str) -> bool:
    """Whether a dot-like character in ``text``, outside the allowlist, comes right before a
    letter, combining marks skipped. Format characters count as nothing, as Slack may drop them."""
    visible = "".join(c for c in text if unicodedata.category(c) != "Cf")
    kept = _kept(visible)
    for index, char in enumerate(visible):
        if char in DOTS and index not in kept:
            following = index + 1
            while following < len(visible) and unicodedata.category(visible[following]).startswith("M"):
                following += 1
            if following < len(visible) and unicodedata.category(visible[following]).startswith("L"):
                return True
    return False


PIECES = [
    *"abcxyzEVIL",
    *"пример",
    *"αβγ",
    *"例え",
    *"0123456789",
    *sorted(DOTS),
    ACUTE,
    "\u0308",
    "\u200b",
    "\u200c",
    "\u200d",
    "\u00ad",
    "\u2060",
    "\ufeff",
    "\u202e",
    "\U0001f3f4\U000e0067\U000e0062\U000e007f",  # a flag's tags
    "\U0001f9d1\u200d\U0001f373",  # an emoji joined by a ZWJ
    " ",
    "-",
    "_",
    "*",
    "/",
    ":",
    "\u24d4",  # So: an enclosed letter
    "\u2167",  # Nl: a Roman numeral
    "\u00b2",  # No: a superscript digit
    "\u2460",  # No: a circled digit
    "\uff45",  # a fullwidth letter
    "\uff11",  # a fullwidth digit
    "\ufb01",  # the fi ligature
    "\u2102",  # a letterlike symbol
    "\uff1a\uff0f\uff0f",  # a fullwidth ://
    "\uff1c",  # a fullwidth <
    "\ufe64",  # a small <
    "\uff06",  # a fullwidth &
    "\uff20here",  # a fullwidth @here
    "@here",
    "@Channel",
    "@hereford",  # a word that starts like a ping: Slack doesn't ping it
    "sam@channelx",
    "@everyone_",  # an underscore isn't a letter or digit: broken
    "<!channel>",
    "<@U02SAM>",
    "&lt;",
    "&",
    ">",
    "e.g.",
    "U.S.",
    "etc.",
    "vs.",
    "www.",
    "https://",
]
_LOOSE_AMP = re.compile(r"&(?!amp;|lt;|gt;)")
_BARE_PING = re.compile(r"@(here|channel|everyone)(?![^\W_])", re.IGNORECASE)
"""A ping as Slack reads one: the exact word, not followed by a letter or digit."""


@pytest.mark.parametrize("seed", range(20))
def test_no_reply_keeps_a_link_ping_or_markup(seed: int) -> None:
    """Property-style: random text of letters, digits, dots, marks, format characters,
    compatibility forms, markup, and pings, through ``safe_reply``."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not a secret
    for _ in range(200):
        text = "".join(rng.choice(PIECES) for _ in range(rng.randint(1, 40)))
        out = safe_reply(text)
        for form in (out, unicodedata.normalize("NFKC", out)):
            assert not _dot_before_a_letter(form), (text, out)
            assert "://" not in form, (text, out)
            assert "<" not in form and ">" not in form, (text, out)
            assert not _LOOSE_AMP.search(form), (text, out)
            assert not _BARE_PING.search(form), (text, out)
        assert all(c in (WORD_JOINER, "\u200d") for c in out if unicodedata.category(c) == "Cf"), (text, out)
        assert not any(0xE0000 <= ord(c) <= 0xE007F for c in out), (text, out)


EMOJI = [
    ("\U0001f9d1\u200d\U0001f373", "\U0001f9d1\u200d\U0001f373"),  # cook
    ("\U0001f9d1\U0001f3fd\u200d\U0001f373", "\U0001f9d1\U0001f3fd\u200d\U0001f373"),  # with a skin tone
    ("\U0001f441\ufe0f\u200d\U0001f5e8\ufe0f", "\U0001f441\ufe0f\u200d\U0001f5e8\ufe0f"),  # a variation selector
    ("\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f", "\U0001f3f4"),  # a plain flag
    ("\U0001f3f4\U000e0069\U000e0067\U000e006e\U000e006f\U000e0072\U000e0065\U000e007f", "\U0001f3f4"),  # any tags
    ("\U000e0001\U000e0020hi", "hi"),  # the language tag and a space tag
    ("a\u200d\U0001f373", "a\U0001f373"),  # not between two emoji
    ("\U0001f373\u200d.evil", "\U0001f373[.]evil"),
    ("\U000e0067.evil", "[.]evil"),  # a tag outside a flag
]


@pytest.mark.parametrize(("raw", "kept"), EMOJI)
def test_emoji_keep_their_joiners_but_lose_every_tag(raw: str, kept: str) -> None:
    assert defang(raw) == kept


@pytest.mark.parametrize(
    "raw", ["127.0.0.1", "127\u30020\u30020\u30021", "127\uff0e0\uff0e0\uff0e1", "127\uff610\uff610\uff611"]
)
def test_ip_addresses_with_any_dot_are_defanged(raw: str) -> None:
    assert defang(raw) == "127[.]0[.]0[.]1"


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("@here", f"@{WORD_JOINER}here"),
        ("@Channel!", f"@{WORD_JOINER}Channel!"),
        ("ping @everyone.", f"ping @{WORD_JOINER}everyone."),
        ("@here_now", f"@{WORD_JOINER}here_now"),
        ("@here\u0301", "@her\u00e9"),  # NFKC first: then it isn't @here at all
        ("\uff20here", f"@{WORD_JOINER}here"),  # a fullwidth at sign, after NFKC
        ("@hereford", "@hereford"),
        ("@here2", "@here2"),
        ("@here\u2167", "@hereVIII"),  # a Roman numeral, which NFKC spells out
        ("@channelx and @everyones", "@channelx and @everyones"),
        ("sam@channelx.example", "sam@channelx[.]example"),
        ("@hereé", "@hereé"),
    ],
)
def test_only_the_exact_ping_tokens_are_broken(raw: str, safe: str) -> None:
    """Slack pings ``@here``, ``@channel``, and ``@everyone`` only when no letter or digit
    follows. Those are always broken, and a word that only starts like one is left alone."""
    assert safe_reply(raw) == safe


def test_safe_reply_strips_format_characters_before_breaking_pings() -> None:
    reply = safe_reply("evil\u200b.example @\u200bhere")
    assert reply == f"evil[.]example @{WORD_JOINER}here"


@pytest.mark.flag("assistant", on=True)
async def test_no_reply_carries_a_link_dm_or_channel(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    text = " ".join(raw for raw, _ in LINKS)
    model.script = [answer(text), answer(text)]
    await slack.dm("Sam", "Links?")
    await slack.mention("Sam", f"<@{BOT_USER}> links?")
    for reply in posts(slack):
        for raw, defanged in LINKS[:-1]:
            assert defanged in reply["text"] and raw not in reply["text"]
        assert "http" not in reply["text"].lower().replace("hxxp", "")


@pytest.mark.flag("assistant", on=True)
async def test_a_url_the_member_typed_is_defanged_when_the_model_quotes_it(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    model.script = [answer('You said "see https://phish.example/pay".')]
    await slack.dm("Sam", "see <https://phish.example/pay>")
    assert user_texts(model) == ["see https://phish.example/pay"]  # the model reads it as typed
    (reply,) = posts(slack)
    assert "hxxps[:]//phish[.]example/pay" in reply["text"] and "https://" not in reply["text"]


@pytest.mark.flag("assistant", on=True)
async def test_a_restored_name_that_looks_like_a_domain_is_defanged(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    async with house.sessionmaker() as db:
        repo = MemberRepository(db)
        await repo.update(house.household.id, house["Sam"].id, house.clock(), display_name="sam.example")
        await db.commit()
    model.script = [answer("Member 1, you're in.")]
    await slack.dm("Sam", "Am I in?")
    (reply,) = posts(slack)
    assert reply["text"] == "sam[.]example, you're in."


@pytest.mark.flag("assistant", on=True)
async def test_a_long_reply_is_cut_to_slacks_limit(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    model.script = [answer("Tacos & " * 1_000)]
    await slack.mention("Sam", f"<@{BOT_USER}> go long")
    (reply,) = posts(slack)
    assert len(reply["text"]) <= MAX_REPLY
    assert reply["text"].endswith(mention_only_line(BOT_USER))
    assert "…" in reply["text"]


@pytest.mark.flag("assistant", on=True)
async def test_other_peoples_mentions_never_leave_the_house(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    """A mentioned member goes out as the conversation's pseudonym and comes back by name in the
    reply; anyone who isn't an active member, a group, or a channel goes out as a placeholder."""
    await switch_on(house, helper)
    async with house.sessionmaker() as db:  # Lee leaves: a deactivated member is "@someone" too
        await MemberRepository(db).update(house.household.id, house["Lee"].id, house.clock(), active=False)
        await db.commit()
    model.script = [answer("Member 2 can cook, and Member 1 can ask.")]
    await slack.dm(
        "Sam",
        "Can <@U03KIM> cook? Not <@U04LEE> or <@U99NOBODY>. Ask <!subteam^S01|@cooks> in "
        "<#C0DINNER|dinnerbell>, <!here>, and <@U03KIM|kim> again, me <@U02SAM>",
    )
    assert user_texts(model) == [
        "Can @Member 2 cook? Not @someone or @someone. Ask @group in #channel, @here, and @Member 2 again, me @Member 1"
    ]
    for secret in ("U03KIM", "U04LEE", "U99NOBODY", "U02SAM", "Kim", "Lee", '"Sam"', "kim"):
        assert secret not in model.sent, secret
    (reply,) = posts(slack)
    assert reply["text"] == "Kim can cook, and Sam can ask."
    [row] = await conversations(house)
    assert row.transcript[0]["text"].startswith("Can @Member 2 cook?")  # stored as sent
    assert row.pseudonyms == {str(house["Sam"].id): 1, str(house["Kim"].id): 2}


class Slow(FakeModel):
    """Holds every request until ``gate`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.started = 0

    async def create(self, request: Mapping[str, Any], *, time_left: float, events: Any = None) -> ModelReply:
        self.started += 1
        await self.gate.wait()
        return await super().create(request, time_left=time_left, events=events)


def slow_model(slack: Slack) -> Slow:
    slow = Slow()
    assert slack.conn.assistant is not None
    object.__setattr__(slack.conn.assistant.assistant, "client", slow)
    return slow


async def answering(slack: Slack) -> None:
    await _until(lambda: bool(slack.conn.assistant and slack.conn.assistant.answering))


@pytest.mark.flag("assistant", on=True)
async def test_a_member_has_one_answer_in_flight_across_conversations(
    settings: Settings, house: House, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    slow = slow_model(slack)
    app = slack.conn.app
    await send(app, "events_api", message_event("U02SAM", "First", channel=SAM_DM))
    await answering(slack)
    second = next_ts()
    await send(app, "events_api", message_event("U02SAM", "Second", channel=SAM_DM, ts=second))  # a new thread
    await _until(lambda: len(posts(slack)) == 1)
    assert posts(slack)[0]["text"] == STILL_ON_IT and posts(slack)[0]["thread_ts"] == second
    for text in ("Third", "Fourth"):  # one notice per answer in flight
        await send(app, "events_api", message_event("U02SAM", text, channel=SAM_DM))
    await send(app, "events_api", message_event("U03KIM", "Mine", channel="D03KIM"))  # Kim isn't held up
    await _until(lambda: slow.started == 2)
    slow.gate.set()
    await slack.conn.interactions.settle()
    texts = [p["text"] for p in posts(slack)]
    assert texts.count(STILL_ON_IT) == 1 and texts.count("OK") == 2
    assert sorted(user_texts(slow)) == ["First", "Mine"]  # nothing else was sent
    assert len(await conversations(house)) == 2
    await slack.dm("Sam", "Fifth", thread_ts=second)  # free again
    assert posts(slack)[-1]["text"] == "OK"


@pytest.mark.flag("assistant", on=True)
async def test_in_the_channel_the_busy_notice_is_an_ephemeral_in_the_thread(
    settings: Settings, house: House, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    slow = slow_model(slack)
    app = slack.conn.app
    await send(app, "events_api", message_event("U02SAM", "First", channel=SAM_DM))
    await answering(slack)
    root = next_ts()
    mention = message_event("U02SAM", f"<@{BOT_USER}> hi", kind="app_mention", channel=CHANNEL, channel_type=None)
    mention["event"]["ts"] = root
    await send(app, "events_api", mention)
    await _until(lambda: bool(slack.fake.of("chat.postEphemeral")))
    (notice,) = [c.args for c in slack.fake.of("chat.postEphemeral")]
    assert (notice["channel"], notice["user"], notice["thread_ts"], notice["text"]) == (
        CHANNEL,
        "U02SAM",
        root,
        STILL_ON_IT,
    )
    assert posts(slack) == []
    slow.gate.set()
    await slack.conn.interactions.settle()


@pytest.mark.flag("assistant", on=True)
async def test_a_refused_busy_notice_is_logged(
    settings: Settings, house: House, helper: Assistant, slack: Slack, caplog: Any
) -> None:
    await switch_on(house, helper)
    slow = slow_model(slack)
    slack.fake.fail("chat.postEphemeral", Failure("not_in_channel"))
    app = slack.conn.app
    await send(app, "events_api", message_event("U02SAM", "First", channel=SAM_DM))
    await answering(slack)
    mention = message_event("U02SAM", f"<@{BOT_USER}> hi", kind="app_mention", channel=CHANNEL, channel_type=None)
    await send(app, "events_api", mention)
    await _until(lambda: "slack assistant reply failed" in caplog.text)
    slow.gate.set()
    await slack.conn.interactions.settle()


@pytest.mark.flag("assistant", on=True)
async def test_anything_that_breaks_outside_converse_is_logged(
    settings: Settings, house: House, helper: Assistant, slack: Slack, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    await switch_on(house, helper)

    async def broken(*args: Any) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr(slack_assistant.slack_members, "member_for_slack_user", broken)
    await slack.dm("Sam", "Zq7x secret words")
    assert "the assistant's slack message failed" in caplog.text
    assert "Zq7x" not in caplog.text and posts(slack) == []


async def _until(check: Any) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while not check():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0)


@pytest.mark.flag("assistant", on=True)
async def test_a_message_delivered_twice_is_answered_once(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    payload = message_event("U02SAM", "What Thai places do we have?", channel=SAM_DM)
    await send(slack.conn.app, "events_api", payload)
    await send(slack.conn.app, "events_api", copy.deepcopy(payload))
    await _until(lambda: len(posts(slack)) == 1)
    await slack.conn.interactions.settle()
    assert len(model.requests) == 1 and len(posts(slack)) == 1


@pytest.mark.flag("assistant", on=True)
async def test_after_a_relink_the_new_member_cant_continue_the_old_members_dm(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack, caplog: Any
) -> None:
    await switch_on(house, helper)
    root = next_ts()
    await slack.dm("Sam", "Hi", ts=root)
    (before,) = await conversations(house)
    model.requests.clear()
    slack.fake.clear()
    await link(house, "Sam", None)  # the admin moves Sam's Slack user to Kim
    await link(house, "Kim", SLACK_IDS["Sam"])
    await slack.dm("Sam", "Still me?", thread_ts=root)  # now Kim, in Sam's old DM thread
    await nothing_happened_since(slack, model)
    (after,) = await conversations(house)
    assert (after.owner_member_id, after.transcript) == (house["Sam"].id, before.transcript)
    assert "isn't theirs" in caplog.text


async def nothing_happened_since(slack: Slack, model: FakeModel) -> None:
    assert model.requests == [] and posts(slack) == [] and slack.fake.of("chat.postEphemeral") == []


@pytest.mark.flag("assistant", on=True)
async def test_a_failure_in_converse_says_so(
    settings: Settings,
    house: House,
    model: FakeModel,
    helper: Assistant,
    slack: Slack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await switch_on(house, helper)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(slack_assistant, "converse", broken)
    await slack.dm("Sam", "Hi")
    (reply,) = posts(slack)
    assert reply["text"] == FAILED


@pytest.mark.flag("assistant", on=True)
async def test_a_refused_reply_is_logged_and_the_next_one_still_says_mention_only(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack, caplog: Any
) -> None:
    await switch_on(house, helper)
    slack.fake.fail("chat.postMessage", Failure("not_in_channel"))
    root = next_ts()
    await slack.mention("Sam", f"<@{BOT_USER}> hi", ts=root)
    assert "slack assistant reply failed" in caplog.text
    await slack.mention("Sam", f"<@{BOT_USER}> hello?", thread_ts=root)
    assert posts(slack)[-1]["text"].endswith(mention_only_line(BOT_USER))


@pytest.mark.flag("assistant", on=True)
async def test_at_zero_conversation_hours_slack_replies_but_keeps_nothing(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper, conversation_hours=0)
    await slack.dm("Sam", "Hi")
    assert len(posts(slack)) == 1 and await conversations(house) == []


# App Home ----------------------------------------------------------------------------------------


@pytest.mark.flag("assistant", on=True)
async def test_app_home_says_members_can_message_or_mention_dinnerbell(
    settings: Settings, house: House, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    await slack.open_home("Sam")
    line = home_line(BOT_USER, CHANNEL)
    blocks = slack.last_home("Sam")["blocks"]
    assert [b for b in blocks if b["type"] == "context" and b["elements"][0].get("text") == line] != []
    assert not any(b["type"] == "actions" and "assistant" in str(b) for b in blocks)  # no button


def test_home_line_without_the_bots_user() -> None:
    assert home_line(None, CHANNEL).endswith(f"Message Dinnerbell, or mention Dinnerbell in <#{CHANNEL}>.")


# Text helpers ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "plain"),
    [
        (f"<@{BOT_USER}> hi", "hi"),
        (f"<@{BOT_USER}|dinnerbell>   tacos?", "tacos?"),
        ("<@U02SAM> and <@W03KIM|kim>", "@someone and @someone"),
        ("<#C0DINNER>", "#channel"),
        ("<#C0DINNER|dinnerbell>", "#channel"),
        ("<!channel> <!everyone>", "@channel @everyone"),
        ("<!subteam^S01|@cooks> please", "@group please"),
        ("<!subteam^S01> please", "@group please"),
        ("<!date^1392734382^{date}|Feb 18, 2014>", "Feb 18, 2014"),
        ("<https://a.example>", "https://a.example"),
        ("<https://a.example|https://a.example>", "https://a.example"),
        ("<https://m.example|the menu>", "the menu (https://m.example)"),
        ("fish &amp; chips &lt;3 &gt; ok", "fish & chips <3 > ok"),
        ("one\n  two   three  ", "one\ntwo three"),
    ],
)
def test_readable(raw: str, plain: str) -> None:
    assert readable(raw, BOT_USER) == plain


def test_safe_reply_cuts_without_splitting_an_escape() -> None:
    cut = safe_reply("a" * 9 + "&", limit=12)
    assert cut == "a" * 9 + "…"
    assert safe_reply("  short  ") == "short"


# Manifests ---------------------------------------------------------------------------------------


def _manifest(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((DEPLOY / name).read_text())
    return loaded


@pytest.mark.parametrize(
    ("default", "variant"),
    [
        ("slack-app-manifest.yaml", "slack-app-manifest.assistant.yaml"),
        ("slack-app-manifest.test.yaml", "slack-app-manifest.assistant.test.yaml"),
    ],
)
def test_each_assistant_manifest_is_its_default_plus_the_assistants_additions(default: str, variant: str) -> None:
    expected = _manifest(default)
    assert expected["features"]["app_home"]["messages_tab_read_only_enabled"] is True
    expected["features"]["app_home"]["messages_tab_read_only_enabled"] = False
    expected["oauth_config"]["scopes"]["bot"] += ["im:history", "app_mentions:read", "reactions:write"]
    expected["settings"]["event_subscriptions"]["bot_events"] += ["message.im", "app_mention"]
    assert _manifest(variant) == expected
    for scope in ("im:history", "app_mentions:read", "reactions:write"):
        assert scope not in _manifest(default)["oauth_config"]["scopes"]["bot"]


def test_the_bundle_ships_the_assistant_manifests() -> None:
    script = (DEPLOY.parent / "scripts" / "build-bundle.sh").read_text()
    for name in ("slack-app-manifest.assistant.yaml", "slack-app-manifest.assistant.test.yaml"):
        assert f"deploy/{name} deploy/{name}\n" in script


# App wiring --------------------------------------------------------------------------------------


def _app_client(tmp_path: Path, clock: FakeClock, provider: FakeProvider, **values: Any) -> TestClient:
    configured = make_settings(
        tmp_path,
        slack_bot_token=BOT_TOKEN,
        slack_app_token=APP_TOKEN,
        slack_channel=CHANNEL,
        slack_team_id=TEAM,
        **values,
    )
    run_migrations(configured.sync_database_url, configured.database_path)
    app = create_app(
        configured,
        clock=clock,
        oidc_transport=httpx.ASGITransport(app=provider.app),
        slack_client=FakeSlack(),
        slack_socket=FakeSocket,
    )
    return TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False)


def test_the_app_builds_anthropics_client_only_with_a_key_and_closes_it(
    tmp_path: Path, clock: FakeClock, provider: FakeProvider
) -> None:
    with _app_client(tmp_path / "with", clock, provider, anthropic_api_key=KEY) as client:
        state = client.app.state  # type: ignore[attr-defined]
        built = state.assistant
        assert isinstance(built, Assistant) and isinstance(built.client, AnthropicModelClient)
        assert state.slack.assistant is not None and state.slack.assistant.assistant is built
    assert built.client._client.is_closed()

    with _app_client(tmp_path / "without", clock, provider) as client:
        state = client.app.state  # type: ignore[attr-defined]
        assert state.assistant is None and state.slack.assistant is None


def test_the_app_takes_a_model_client_for_tests(tmp_path: Path, clock: FakeClock, provider: FakeProvider) -> None:
    fake = FakeModel()
    configured = make_settings(tmp_path, anthropic_api_key=KEY)
    run_migrations(configured.sync_database_url, configured.database_path)
    app = create_app(configured, clock=clock, oidc_transport=httpx.ASGITransport(app=provider.app), model_client=fake)
    with TestClient(app, base_url=BASE_URL):
        assert app.state.assistant.client is fake


@pytest.mark.flag("assistant", on=True)
async def test_a_channel_id_too_long_for_a_key_is_ignored(
    settings: Settings, house: House, model: FakeModel, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    await slack.dm("Sam", "Hi", channel="D" + "2" * 24)
    await nothing_happened(slack, model, house)


def test_what_is_remembered_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_assistant, "REMEMBERED", 2)
    remembered = slack_assistant._Bounded()
    for item in ("a", "b", "a", "c"):
        remembered.add(item)
    assert "a" in remembered and "c" in remembered and "b" not in remembered


def test_a_failed_startup_closes_everything_it_opened(
    tmp_path: Path, clock: FakeClock, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slack started, then the scheduler failed: Slack stops, and Anthropic's client, the HTTP
    client, and the database close, newest first."""
    from dinnerbell import app_factory

    closed: list[str] = []

    class Client(FakeModel):
        async def close(self) -> None:
            closed.append("anthropic")

    def broken(jobs: Any) -> Any:
        raise RuntimeError("no scheduler")

    real_open = app_factory.open_database

    def recording_open(url: str) -> Any:
        database = real_open(url)
        real_close = database.close

        async def close() -> None:
            closed.append("database")
            await real_close()

        object.__setattr__(database, "close", close)
        return database

    monkeypatch.setattr(app_factory, "_anthropic", lambda settings: Client())
    monkeypatch.setattr(app_factory, "build_scheduler", broken)
    monkeypatch.setattr(app_factory, "open_database", recording_open)
    client = _app_client(tmp_path, clock, provider, anthropic_api_key=KEY)
    with pytest.raises(RuntimeError, match="no scheduler"), client:
        pass
    state = client.app.state  # type: ignore[attr-defined]
    assert state.slack._stopping and state.slack._task.cancelled()  # Slack started, then stopped
    assert state.dinnerbell.oidc._http.is_closed
    assert closed == ["anthropic", "database"]


def test_a_close_that_fails_at_shutdown_still_closes_the_rest(
    tmp_path: Path, clock: FakeClock, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anthropic's client fails to close: the scheduler and Slack stopped before it, and the HTTP
    client and the database still close after it, and the failure isn't swallowed."""
    from dinnerbell import app_factory

    closed: list[str] = []

    class Client(FakeModel):
        async def close(self) -> None:
            closed.append("anthropic")
            raise RuntimeError("close failed")

    real_open = app_factory.open_database

    def recording_open(url: str) -> Any:
        database = real_open(url)
        real_close = database.close

        async def close() -> None:
            closed.append("database")
            await real_close()

        object.__setattr__(database, "close", close)
        return database

    monkeypatch.setattr(app_factory, "_anthropic", lambda settings: Client())
    monkeypatch.setattr(app_factory, "open_database", recording_open)
    client = _app_client(tmp_path, clock, provider, anthropic_api_key=KEY)
    with pytest.raises(RuntimeError, match="close failed"), client:
        pass
    state = client.app.state  # type: ignore[attr-defined]
    assert state.slack._stopping  # and the leak check fails the test if the scheduler kept running
    assert state.dinnerbell.oidc._http.is_closed
    assert closed == ["anthropic", "database"]


def test_a_failed_bootstrap_closes_the_database(
    tmp_path: Path, clock: FakeClock, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dinnerbell import app_factory

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no household")

    monkeypatch.setattr(app_factory, "bootstrap", broken)
    configured = make_settings(tmp_path)
    run_migrations(configured.sync_database_url, configured.database_path)
    app = create_app(configured, clock=clock, oidc_transport=httpx.ASGITransport(app=provider.app))
    with pytest.raises(RuntimeError, match="no household"), TestClient(app, base_url=BASE_URL):
        pass  # the leak check fails the test if the database stays open


@pytest.mark.flag("assistant", on=True)
async def test_cancelling_an_answer_mid_converse_frees_the_member(
    settings: Settings, house: House, helper: Assistant, slack: Slack
) -> None:
    await switch_on(house, helper)
    slow = slow_model(slack)
    await send(slack.conn.app, "events_api", message_event("U02SAM", "First", channel=SAM_DM))
    await _until(lambda: slow.started == 1)
    assert slack.conn.assistant is not None and slack.conn.assistant.answering == {house["Sam"].id}
    for task in list(slack.conn.interactions.tasks):
        task.cancel()
    await slack.conn.interactions.settle()
    assert slack.conn.assistant.answering == set() and slack.conn.assistant.told == set()
