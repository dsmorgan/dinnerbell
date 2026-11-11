# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assistant replies streamed into Slack (#266, spec 6.14): the ``markdown_text`` pipeline, which
must give, piece by piece, exactly what it gives for the whole reply and never a link, mention,
or ping, however the model's text is cut; how ``CallText`` holds and joins a call's text; and
the stream in a DM and a channel thread, with its fallbacks. Slack is ``FakeSlack`` and the
model is ``FakeModel``."""

from __future__ import annotations

import asyncio
import random
import unicodedata
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.channels.slack import assistant as slack_assistant
from dinnerbell.channels.slack.assistant import (
    ELLIPSIS,
    LOOKALIKE_LT,
    MAX_REPLY,
    STILL_ON_IT,
    MarkdownLines,
    clip_markdown,
    escape_markdown,
    mention_only_line,
    safe_markdown,
    safe_reply,
)
from dinnerbell.channels.slack.working import STATUS
from dinnerbell.config import Settings
from dinnerbell.db.models import AssistantConversationRow
from dinnerbell.domain.assistant import AssistantSettings, web_key
from dinnerbell.services.assistant import admin
from dinnerbell.services.assistant.client import ModelEvents, ModelReply
from dinnerbell.services.assistant.conversation import (
    FAILED,
    OUT_OF_TIME,
    REFUSED,
    TOO_LONG_ANSWER,
    Assistant,
    Reply,
    _reply_text,
    converse,
)
from dinnerbell.services.assistant.drafts import DraftView
from dinnerbell.services.assistant.prompt import system_prompt
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.assistant.streaming import HOLD_CHARS, CallText
from dinnerbell.services.database import Database, open_database
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_model import FakeModel, answer, stopped
from tests.support.fake_slack import BOT_USER, CHANNEL, TEAM, Failure, message_event, next_ts, send
from tests.support.slack import Slack, wire
from tests.test_assistant_security import (
    FRAGMENTS,
    assert_markdown_safe,
    assert_slack_safe,
    hostile_texts,
    markdown_visible,
)

pytestmark = pytest.mark.anyio

KEY = "sk-ant-test-not-a-real-key"
SAM_DM = "D02SAM"
STREAMING = ("chat.startStream", "chat.appendStream", "chat.stopStream")
LONG = "\n".join(f"Line {n}: the noodle place does pickup until nine, and the rice bowls are good." for n in range(6))
"""A reply long enough to pass the hold, in whole lines."""


# The pipeline, piece by piece -------------------------------------------------------------------

ALPHABET = (
    *"ab .:/\n\t@<>[]()&\\*_`!#~",
    "\n\n",
    "  \n",
    "https://",
    "hxxp",
    "www.",
    "here",
    "channel",
    "everyone",
    "e.g.",
    "U.S.",
    "1.2.3.4",
    "\u200d",  # zero-width joiner
    "\u200b",  # zero-width space
    "\u00ad",  # soft hyphen
    "\u2060",  # word joiner
    "\u202e",  # right-to-left override
    "\U000e0041",  # a tag character
    "\U0001f9d1",  # an emoji
    "\U0001f3fd",  # a skin tone
    "\u0301",  # a combining acute accent
    "\u1100",  # a Hangul leading consonant, which NFKC composes with a vowel
    "\u1161",  # a Hangul vowel
    "\uff0e",  # a fullwidth dot
    "\u3002",  # an ideographic dot
    "\uff20",  # a fullwidth at sign
    "\u00a0",  # a no-break space
    "\u203e",  # an overline, which NFKC makes a space and a mark
    "\ufb01",  # the fi ligature
    "\u2028",  # a line separator
    "\r",
    "&lt;",
    "&amp;",
    "Member 2",
)


def hostile(rng: random.Random, size: int) -> str:
    parts = [*ALPHABET, *FRAGMENTS]
    return "".join(rng.choice(parts) for _ in range(size))


def streamed(text: str, cuts: list[int], limit: int = MAX_REPLY) -> list[str]:
    """What ``MarkdownLines`` gives for ``text`` fed in pieces cut at ``cuts``."""
    lines = MarkdownLines(limit)
    chunks: list[str] = []
    start = 0
    for cut in [*sorted(cuts), len(text)]:
        chunks.append(lines.feed(text[start:cut]))
        start = cut
    chunks.append(lines.finish())
    return [chunk for chunk in chunks if chunk]


def random_cuts(rng: random.Random, text: str) -> list[int]:
    return sorted(rng.sample(range(len(text) + 1), min(len(text) + 1, rng.randint(0, 12))))


@pytest.mark.parametrize("seed", range(8))
def test_streamed_pieces_join_to_exactly_the_whole_replys_markdown(seed: int) -> None:
    """Property: for random hostile text cut into random pieces, at any limit, the chunks
    joined are ``safe_markdown`` of the whole text, and each chunk alone is safe."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not a secret
    for _ in range(600):  # 4,800 cases in all
        text = hostile(rng, rng.randint(0, 50))
        limit = rng.choice([MAX_REPLY, MAX_REPLY, 60, 17, 5, 2, 1])
        cuts = random_cuts(rng, text)
        chunks = streamed(text, cuts, limit)
        joined = "".join(chunks)
        assert joined == safe_markdown(text, limit), (text, limit)
        assert len(joined) <= limit
        whole = streamed(text, cuts, 10**9)
        for chunk in whole:
            assert_markdown_safe(chunk, abbreviations=True)
        if joined.endswith(ELLIPSIS) and joined != "".join(whole):
            assert "".join(whole).startswith(joined[: -len(ELLIPSIS)])  # cut at an escape's start
        else:
            assert chunks == whole


@pytest.mark.parametrize("seed", range(4))
def test_what_a_member_reads_streamed_is_what_they_read_posted(seed: int) -> None:
    """Property: the streamed Markdown reads, once Slack applies its escapes, as the posted
    mrkdwn reply does once Slack decodes its entities, with ``<`` as ``LOOKALIKE_LT``."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not a secret
    for _ in range(500):
        text = hostile(rng, rng.randint(0, 40))
        markdown = "".join(streamed(text, random_cuts(rng, text)))
        posted = safe_reply(text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        assert markdown_visible(markdown) == posted.replace("<", LOOKALIKE_LT)


@pytest.mark.parametrize("seed", range(4))
def test_no_cut_of_the_security_suites_hostile_replies_lets_anything_through(seed: int) -> None:
    for words in hostile_texts(seed + 40, 250):
        text = words.replace(" ", "\n", 3)
        for cut in range(0, len(text) + 1, max(1, len(text) // 9)):
            chunks = streamed(text, [cut])
            assert "".join(chunks) == safe_markdown(text)
            for chunk in chunks:
                assert_markdown_safe(chunk, abbreviations=True)


HAND_WRITTEN = (
    "See https://evil.example/pay\nnow",
    "go to\nwww.evil.example\ntoday",
    "pay.evil.example\n",
    "<!channel>\n<!here|here> and <@U03KIM>\nhi",
    "@here\n@channel.\n@everyone_\n",
    "[the menu](https://evil.example)\n[ref]: https://evil.example\n![img](https://evil.example/a.png)",
    "<https://evil.example>\n<mailto:a@evil.example>\n",
    "&lt;@U03KIM&gt;\n&#60;!channel&#62;\n",
    "\U0001f9d1\u200d\n\U0001f373 cook\n",
    "\U0001f9d1\n\u200d\U0001f373\n",
    "flag\U000e0041\n\U000e0042x\n",
    "e\n\u0301 accent\n",
    "\u1100\n\u1161 hangul\n",
    "\u1100\u1161\n",
    "\ufb01.example\n\uff45\uff58.com\n",
    "evil\uff0e\nexample\n",
    "1.2.3.4\n10\u30020\u30020\u30021\n",
    "Done.\nNext.Line\ne.g.\nU.S.\n",
    "back\\\\slash \\[x\\](y)\n\\",
    "  \n\n  leading and trailing  \n\n  ",
)


@pytest.mark.parametrize("text", HAND_WRITTEN)
def test_hand_written_hostile_text_is_safe_cut_anywhere(text: str) -> None:
    """Every single cut and every pair of cuts: split URLs, ``<!channel>``, ``[text](url)``,
    joiners and tag characters at a cut, and pairs NFKC composes across one."""
    whole = safe_markdown(text)
    assert_markdown_safe(whole, abbreviations=True)
    for one in range(len(text) + 1):
        assert "".join(streamed(text, [one])) == whole, one
        for two in range(one, len(text) + 1):
            chunks = streamed(text, [one, two])
            assert "".join(chunks) == whole, (one, two)
            for chunk in chunks:
                assert_markdown_safe(chunk, abbreviations=True)
    every = streamed(text, list(range(len(text) + 1)))  # a character at a time
    assert "".join(every) == whole


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("[menu](https://e.example)", "\\[menu\\](https://e.example)"),
        ("![a](b)", "!\\[a\\](b)"),
        ("[ref]: x", "\\[ref\\]: x"),
        ("<https://e.example>", f"{LOOKALIKE_LT}https://e.example>"),
        ("<@U03KIM>", f"{LOOKALIKE_LT}@U03KIM>"),
        ("<!channel>", f"{LOOKALIKE_LT}!channel>"),
        ("&lt;b&gt;", "\\&lt;b\\&gt;"),
        ("\\[", "\\\\\\["),
        ("**bold** and `code`", "**bold** and `code`"),
    ],
)
def test_escape_markdown(raw: str, escaped: str) -> None:
    assert escape_markdown(raw) == escaped


def test_safe_markdown_defangs_and_breaks_pings_as_safe_reply_does() -> None:
    out = safe_markdown("Visit https://evil.example or www.evil.example, @here")
    assert out == "Visit hxxps\\[:\\]//evil\\[.\\]example or www\\[.\\]evil\\[.\\]example, @\u2060here"
    assert_markdown_safe(out)


def test_clip_markdown_never_splits_an_escape() -> None:
    assert clip_markdown("ab\\[cd", 4) == "ab" + ELLIPSIS  # not "ab\\" and an ellipsis
    assert clip_markdown("ab\\[cd", 5) == "ab\\[" + ELLIPSIS
    assert clip_markdown("abc", 3) == "abc"


# CallText --------------------------------------------------------------------------------------


class Heard:
    def __init__(self) -> None:
        self.pieces: list[str] = []
        self.tool_rounds = 0

    def call_started(self, drafts: int) -> None:
        pass

    def lines(self, text: str) -> None:
        self.pieces.append(text)

    def tool_round(self) -> None:
        self.tool_rounds += 1


async def tell(events: ModelEvents, blocks: list[Mapping[str, Any]], rng: random.Random) -> None:
    """``blocks`` as a stream reports them, each text in random pieces."""
    model = FakeModel()
    model.delta = rng.randint(1, 9)
    await model.write(ModelReply(tuple(blocks), "end_turn", "m", ()), events)


@pytest.mark.parametrize("seed", range(4))
async def test_a_calls_shown_text_is_always_a_prefix_of_its_reply(seed: int) -> None:
    """Property: whatever the text blocks, ``CallText`` passes on whole lines that join to a
    prefix of the reply as ``_reply_text`` joins it, each ending in something that isn't
    whitespace, and each after the first starting with whitespace that holds a line break."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not a secret
    for _ in range(250):
        blocks: list[Mapping[str, Any]] = [
            {"type": "text", "text": hostile(rng, rng.randint(0, 80)) + rng.choice(["", "\n", "  "])}
            for _ in range(rng.randint(1, 3))
        ]
        heard = Heard()
        await tell(CallText(heard, lambda text: text), blocks, rng)
        whole = _reply_text(blocks)
        assert whole.startswith("".join(heard.pieces).lstrip())
        for n, piece in enumerate(heard.pieces):
            assert piece and not piece[-1].isspace()
            if n:
                assert piece[0].isspace() and "\n" in piece[: len(piece) - len(piece.lstrip())]


@pytest.mark.parametrize("seed", range(4))
async def test_a_calls_lines_streamed_and_finished_are_the_whole_replys_markdown(seed: int) -> None:
    """Property, end to end without Slack: ``CallText``'s whole lines, fed to ``MarkdownLines``
    as ``SlackStream`` feeds them, then the rest of the reply, join to ``safe_markdown`` of the
    reply, and each chunk is safe."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not a secret
    for _ in range(250):
        blocks: list[Mapping[str, Any]] = [
            {"type": "text", "text": rng.choice(["", " ", "\n"]) + hostile(rng, rng.randint(0, 120))}
            for _ in range(rng.randint(1, 3))
        ]
        heard = Heard()
        await tell(CallText(heard, lambda text: text), blocks, rng)
        lines = MarkdownLines()
        chunks = [lines.feed(piece, whole_lines=True) for piece in heard.pieces]
        whole = _reply_text(blocks)
        shown = "".join(heard.pieces).lstrip()
        assert whole.startswith(shown)
        chunks += [lines.feed(whole[len(shown) :]), lines.finish()]
        assert "".join(chunks) == safe_markdown(whole)
        for chunk in chunks:
            assert_markdown_safe(chunk, abbreviations=True)


async def test_names_go_back_into_whole_lines() -> None:
    heard = Heard()
    events = CallText(heard, lambda text: text.replace("Member 2", "Kim"))
    text = "Member 2 can cook.\n" + "x" * HOLD_CHARS + "\nAsk Member 2\nlater"
    await tell(events, [{"type": "text", "text": text}], random.Random(1))  # noqa: S311 - reproducible test data
    assert "".join(heard.pieces) == "Kim can cook.\n" + "x" * HOLD_CHARS + "\nAsk Kim"


async def test_short_text_is_held_and_a_tool_call_drops_it_unseen() -> None:
    heard = Heard()
    events = CallText(heard, lambda text: text)
    await tell(events, [{"type": "text", "text": "One line.\nTwo lines.\n"}], random.Random(1))  # noqa: S311 - reproducible test data
    assert heard.pieces == []  # under HOLD_CHARS
    events.tool_started()
    assert heard.tool_rounds == 0  # nothing was shown, so nothing to take back


# In Slack --------------------------------------------------------------------------------------


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
    slack = await wire(house, assistant=helper)
    async with house.sessionmaker() as db:
        on = AssistantSettings(enabled=True)
        await admin.update_assistant(db, helper.config, house["Alex"], on, now=house.clock())
    return slack


def streams(slack: Slack) -> list[str]:
    """Each streamed message's ``markdown_text``, start to stop, in order."""
    out: list[str] = []
    for call in slack.fake.of(*STREAMING):
        if call.method == "chat.startStream":
            out.append("")
        out[-1] += call.args.get("markdown_text") or ""
    return out


def posts(slack: Slack) -> list[dict[str, Any]]:
    return [c.args for c in slack.fake.of("chat.postMessage")]


async def transcript(house: House) -> list[dict[str, Any]]:
    async with house.sessionmaker() as db:
        (row,) = list(await db.scalars(select(AssistantConversationRow)))
        return list(row.transcript)


@pytest.mark.flag("assistant", on=True)
async def test_a_long_dm_reply_streams_line_by_line(house: House, model: FakeModel, slack: Slack) -> None:
    model.script = [answer(LONG)]
    root = next_ts()
    await slack.dm("Sam", "Where can we get noodles?", ts=root)
    assert streams(slack) == [safe_markdown(LONG)]
    (start,) = slack.fake.of("chat.startStream")
    assert (start.args["channel"], start.args["thread_ts"]) == (SAM_DM, root)
    assert "recipient_user_id" not in start.args  # a DM needs no recipient
    assert len(slack.fake.of("chat.appendStream")) >= 1  # it grew as it was written
    assert posts(slack) == []  # nothing posted besides
    methods = [c.method for c in slack.fake.calls if c.method in (*STREAMING, "assistant.threads.setStatus")]
    assert methods[0] == "assistant.threads.setStatus" and methods[-1] == "assistant.threads.setStatus"
    assert slack.fake.calls[-1].args.get("status") == ""  # cleared after the stream, as after a post
    assert (await transcript(house))[-1]["text"] == LONG  # kept exactly as before


@pytest.mark.flag("assistant", on=True)
async def test_a_short_reply_is_posted_as_before(model: FakeModel, slack: Slack) -> None:
    model.script = [answer("Two places do noodles.")]
    await slack.dm("Sam", "Noodles?")
    assert slack.fake.of(*STREAMING) == []
    assert [p["text"] for p in posts(slack)] == ["Two places do noodles."]


@pytest.mark.flag("assistant", on=True)
async def test_a_channel_thread_streams_to_the_thread_with_the_note_under_it(model: FakeModel, slack: Slack) -> None:
    model.script = [answer(LONG), answer(LONG)]
    root = next_ts()
    await slack.mention("Sam", f"<@{BOT_USER}> where can we get noodles?", ts=root)
    (start,) = slack.fake.of("chat.startStream")
    assert (start.args["channel"], start.args["thread_ts"]) == (CHANNEL, root)
    assert (start.args["recipient_user_id"], start.args["recipient_team_id"]) == ("U02SAM", TEAM)
    (stop,) = slack.fake.of("chat.stopStream")
    assert stop.args["blocks"] == [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": mention_only_line(BOT_USER)}]}
    ]
    await slack.mention("Kim", f"<@{BOT_USER}> and rice?", thread_ts=root)
    assert "blocks" not in slack.fake.of("chat.stopStream")[-1].args  # the note comes once
    assert streams(slack) == [safe_markdown(LONG)] * 2


@pytest.mark.flag("assistant", on=True)
async def test_a_channel_reply_with_drafts_never_streams(slack: Slack, monkeypatch: pytest.MonkeyPatch) -> None:
    drafted = Reply(LONG, (DraftView(1, "proposal", "A noodle place", ("Adds it.",)),))

    async def converse(*args: Any, stream: Any = None, **kwargs: Any) -> Reply:
        stream.call_started(0)
        stream.call_started(1)  # the final call starts with a draft made: this reply is the member's
        stream.lines(LONG)
        return drafted

    monkeypatch.setattr(slack_assistant, "converse", converse)
    await slack.mention("Sam", f"<@{BOT_USER}> propose noodles")
    assert slack.fake.of(*STREAMING) == [] and posts(slack) == []
    assert len(slack.fake.of("chat.postEphemeral")) == 2  # the reply and its card, as before


@pytest.mark.flag("assistant", on=True)
async def test_a_dm_reply_with_drafts_streams_then_posts_its_cards(
    model: FakeModel, slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def drafting(*args: Any, stream: Any = None, **kwargs: Any) -> Reply:
        stream.call_started(1)
        stream.lines(LONG.rsplit("\n", 1)[0])
        await asyncio.sleep(0)
        return Reply(LONG, (DraftView(1, "proposal", "A noodle place", ("Adds it.",)),))

    monkeypatch.setattr(slack_assistant, "converse", drafting)
    await slack.dm("Sam", "Propose noodles")
    assert streams(slack) == [safe_markdown(LONG)]
    order = [c.method for c in slack.fake.calls if c.method in ("chat.stopStream", "chat.postMessage")]
    assert order == ["chat.stopStream", "chat.postMessage"]  # the card after the stream
    assert posts(slack)[0]["blocks"]


@pytest.mark.flag("assistant", on=True)
async def test_a_tool_round_with_a_short_preface_shows_nothing_and_keeps_the_status(
    model: FakeModel, slack: Slack
) -> None:
    preface = stopped(
        "tool_use",
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "toolu_1", "name": "look_up_catalog", "input": {"query": "noodles"}},
    )
    model.script = [preface, answer(LONG)]
    await slack.dm("Sam", "Noodles?")
    assert streams(slack) == [safe_markdown(LONG)]  # only the final answer
    statuses = [c.args["status"] for c in slack.fake.of("assistant.threads.setStatus")]
    assert statuses == [STATUS, ""]


@pytest.mark.flag("assistant", on=True)
async def test_a_tool_round_after_streaming_began_stops_that_stream_and_shows_the_status_again(
    house: House, model: FakeModel, slack: Slack
) -> None:
    preface = stopped(
        "tool_use",
        {"type": "text", "text": LONG},
        {"type": "tool_use", "id": "toolu_1", "name": "look_up_catalog", "input": {"query": "noodles"}},
    )
    final = "\n".join(f"Answer line {n}, with the catalog's noodle places and their modes." for n in range(4))
    model.script = [preface, answer(final)]
    await slack.dm("Sam", "Noodles?")
    shown = streams(slack)
    assert len(shown) == 2 and shown[1] == safe_markdown(final)
    assert safe_markdown(LONG).startswith(shown[0])  # the preface stopped where it was
    order = [
        (c.method, c.args.get("status"))
        for c in slack.fake.calls
        if c.method in ("chat.stopStream", "assistant.threads.setStatus")
    ]
    assert order[1:3] == [("chat.stopStream", None), ("assistant.threads.setStatus", STATUS)]
    assert (await transcript(house))[-1]["text"] == final  # the reply, as before: not the preface


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize(("reason", "line"), [("refusal", REFUSED), ("max_tokens", TOO_LONG_ANSWER)])
async def test_a_reply_that_ends_in_dinnerbells_own_line_stops_the_stream_with_it(
    model: FakeModel, slack: Slack, reason: str, line: str
) -> None:
    model.script = [stopped(reason, {"type": "text", "text": LONG})]
    await slack.dm("Sam", "Noodles?")
    (shown,) = streams(slack)
    ending = "\n\n" + safe_markdown(line)
    assert shown.endswith(ending) and safe_markdown(LONG).startswith(shown[: -len(ending)])
    assert posts(slack) == []
    assert_markdown_safe(shown)


class WritesThenTimesOut(FakeModel):
    """Writes ``LONG`` as a stream would, then runs out of time mid-call."""

    async def create(self, request: Mapping[str, Any], *, time_left: float, events: Any = None) -> ModelReply:
        if events is not None:
            await self.write(answer(LONG), events)
        raise TimeoutError


@pytest.mark.flag("assistant", on=True)
async def test_the_deadline_mid_stream_ends_it_with_the_out_of_time_line(slack: Slack) -> None:
    assert slack.conn.assistant is not None
    object.__setattr__(slack.conn.assistant.assistant, "client", WritesThenTimesOut())
    await slack.dm("Sam", "Noodles?")
    (shown,) = streams(slack)
    assert shown.endswith("\n\n" + safe_markdown(OUT_OF_TIME))
    assert safe_markdown(LONG).startswith(shown[: -len("\n\n" + safe_markdown(OUT_OF_TIME))])
    assert posts(slack) == []


@pytest.mark.flag("assistant", on=True)
async def test_a_reply_past_the_limit_is_cut_as_a_posted_one_is(model: FakeModel, slack: Slack) -> None:
    huge = "\n".join(f"Line {n} about the many noodle places in the catalog, one by one." for n in range(120))
    model.script = [answer(huge)]
    await slack.dm("Sam", "Everything?")
    (shown,) = streams(slack)
    assert shown == safe_markdown(huge) and len(shown) == MAX_REPLY and shown.endswith(ELLIPSIS)


@pytest.mark.flag("assistant", on=True)
@pytest.mark.parametrize("error", ["ratelimited", "channel_not_found", "internal_error"])
async def test_a_refused_start_posts_the_reply_as_before(model: FakeModel, slack: Slack, error: str) -> None:
    slack.fake.fail("chat.startStream", Failure(error))
    model.script = [answer(LONG)]
    await slack.dm("Sam", "Noodles?")
    assert slack.fake.of("chat.appendStream") == [] and slack.fake.of("chat.stopStream") == []
    assert [p["text"] for p in posts(slack)] == [safe_reply(LONG)]


@pytest.mark.flag("assistant", on=True)
async def test_a_refused_append_posts_only_what_wasnt_shown(model: FakeModel, slack: Slack) -> None:
    slack.fake.fail("chat.appendStream", Failure("msg_too_long"))
    model.script = [answer(LONG)]
    await slack.dm("Sam", "Noodles?")
    (start,) = slack.fake.of("chat.startStream")
    assert len(slack.fake.of("chat.appendStream")) == 1  # refused, and not tried again
    (posted,) = posts(slack)
    seen = markdown_visible(start.args["markdown_text"])  # all the member saw
    assert seen and LONG.startswith(seen)
    assert posted["text"] == safe_reply(LONG[len(seen) :])  # the rest, without what they saw
    assert slack.fake.of("chat.stopStream")[0].args.get("markdown_text") is None


@pytest.mark.flag("assistant", on=True)
async def test_a_refused_stop_posts_only_what_wasnt_shown(model: FakeModel, slack: Slack) -> None:
    slack.fake.fail("chat.stopStream", Failure("message_not_in_streaming_state"))
    model.script = [answer(LONG)]
    await slack.dm("Sam", "Noodles?")
    shown = "".join(c.args.get("markdown_text") or "" for c in slack.fake.of("chat.startStream", "chat.appendStream"))
    (posted,) = posts(slack)
    seen = markdown_visible(shown)
    assert posted["text"] == safe_reply(LONG[len(seen) :])
    assert_slack_safe(posted["text"])


@pytest.mark.flag("assistant", on=True)
async def test_a_failure_in_converse_after_streaming_ends_the_stream_with_the_failed_line(
    slack: Slack, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def breaks(*args: Any, stream: Any = None, **kwargs: Any) -> Reply:
        stream.call_started(0)
        stream.lines(LONG.rsplit("\n", 1)[0])
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    monkeypatch.setattr(slack_assistant, "converse", breaks)
    await slack.dm("Sam", "Noodles?")
    (shown,) = streams(slack)
    assert shown.endswith("\n\n" + safe_markdown(FAILED))
    assert posts(slack) == []


@pytest.mark.flag("assistant", on=True)
async def test_a_cancelled_answer_mid_stream_stops_cleanly(slack: Slack) -> None:
    held = asyncio.Event()

    class Hangs(FakeModel):
        async def create(self, request: Mapping[str, Any], *, time_left: float, events: Any = None) -> ModelReply:
            await self.write(answer(LONG), events)
            await held.wait()
            return answer(LONG)

    assert slack.conn.assistant is not None
    object.__setattr__(slack.conn.assistant.assistant, "client", Hangs())
    await send(slack.conn.app, "events_api", message_event("U02SAM", "Noodles?", channel=SAM_DM))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while not slack.fake.of("chat.appendStream"):
        assert loop.time() < deadline
        await asyncio.sleep(0)
    for task in list(slack.conn.interactions.tasks):
        task.cancel()
    await slack.conn.interactions.settle()
    assert slack.conn.assistant.answering == set()
    assert posts(slack) == []


@pytest.mark.flag("assistant", on=True)
async def test_a_busy_member_still_gets_the_notice_while_a_reply_streams(model: FakeModel, slack: Slack) -> None:
    held = asyncio.Event()

    class Slow(FakeModel):
        async def create(self, request: Mapping[str, Any], *, time_left: float, events: Any = None) -> ModelReply:
            await self.write(answer(LONG), events)
            await held.wait()
            return answer(LONG)

    assert slack.conn.assistant is not None
    object.__setattr__(slack.conn.assistant.assistant, "client", Slow())
    await send(slack.conn.app, "events_api", message_event("U02SAM", "First", channel=SAM_DM))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while not slack.fake.of("chat.startStream"):
        assert loop.time() < deadline
        await asyncio.sleep(0)
    await send(slack.conn.app, "events_api", message_event("U02SAM", "Second", channel=SAM_DM))
    while not posts(slack):
        assert loop.time() < deadline
        await asyncio.sleep(0)
    assert posts(slack)[0]["text"] == STILL_ON_IT
    held.set()
    await slack.conn.interactions.settle()
    assert streams(slack) == [safe_markdown(LONG)]


@pytest.mark.flag("assistant", on=True)
async def test_the_web_never_listens_to_the_models_text(
    settings: Settings, house: House, model: FakeModel, helper: Assistant
) -> None:
    """``converse`` without a stream calls the client exactly as before: no ``events``."""
    seen: list[object] = []

    class Spy(FakeModel):
        async def create(self, request: Mapping[str, Any], *, time_left: float, **more: Any) -> ModelReply:
            seen.append(more)
            return answer(LONG)

    object.__setattr__(helper, "client", Spy())
    async with house.sessionmaker() as db:
        on = AssistantSettings(enabled=True)
        await admin.update_assistant(db, helper.config, house["Alex"], on, now=house.clock())
    async with house.sessionmaker() as db:
        reply = await converse(db, helper, house["Sam"], web_key(house["Sam"].id), private=True, text="Noodles?")
    assert reply is not None and reply.text == LONG
    assert seen == [{}]


def test_the_prompt_asks_for_no_text_before_a_tool_call() -> None:
    for keeps_history in (True, False):
        assert "Write no text before a tool call" in system_prompt(keeps_history=keeps_history)


def test_the_hostile_alphabet_reaches_every_tricky_category() -> None:
    categories = {unicodedata.category(char) for part in ALPHABET for char in part}
    assert {"Cf", "Mn", "Zs", "Zl", "So", "Lo"} <= categories
