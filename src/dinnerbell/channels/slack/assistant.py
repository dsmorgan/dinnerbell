# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optional assistant in Slack (ADR 0009, spec 6.14): direct messages to Dinnerbell, and
mentions of it in the household channel. ``SlackConnection`` builds this only when the operator
set ``DINNERBELL_ANTHROPIC_API_KEY``; without it, nothing here is registered.

Each message is acknowledged at once (Bolt acknowledges events itself) and answered in a tracked
task. Dinnerbell ignores, with no reply and nothing stored or sent:

- anything from a bot, Dinnerbell included, and anything with a ``subtype``: edits, deletions,
  joins, and bot messages;
- a mention outside the household channel, and a message from another workspace;
- a Slack user who isn't an active member: a guest, a deactivated member, or someone no member
  is linked to;
- everything, while the assistant isn't available (the flag or Admin's switch is off).

**Conversations** are Slack threads, keyed by channel and thread root (``slack_key``). A
top-level DM or mention starts one, and Dinnerbell replies in its thread. A mention in a thread
roots at that thread's root, which is a Dinnerbell post when the thread is under a call or a
reminder. DMs are private to their member. A channel thread is shared: each message acts as its
author. Dinnerbell's first reply in a channel thread says it sees only messages that mention it.

**Replies** are plain text: the model's text is escaped, so it can't make a mention, a channel
ping, or a link, and it's cut to Slack's length. While a member's message is being answered, a
second one from them in the same conversation isn't sent; Dinnerbell says once that it's still
on the last one.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from slack_bolt.async_app import AsyncApp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.channels.slack.api import SlackApi, SlackUnavailable
from dinnerbell.channels.slack.blocks import ELLIPSIS, MAX_FALLBACK, clip_mrkdwn, escape
from dinnerbell.channels.slack.ids import is_channel_id
from dinnerbell.domain.assistant import InvalidConversationKey, slack_key
from dinnerbell.domain.members import Member
from dinnerbell.domain.notifications import PermanentDeliveryError
from dinnerbell.logs import member_id_var
from dinnerbell.services import slack_members
from dinnerbell.services.assistant.conversation import FAILED, Assistant, converse
from dinnerbell.services.assistant.runtime import available
from dinnerbell.services.errors import Forbidden

log = logging.getLogger(__name__)

SLACK_ERRORS = (SlackUnavailable, PermanentDeliveryError)
MAX_REPLY = MAX_FALLBACK
"""The most characters a reply has: Slack's limit for a message's text."""
REMEMBERED = 2048
"""How many messages, and channel threads replied in, are remembered; the oldest go first."""
STILL_ON_IT = "I'm still on your last message. Send this one again once I've answered."
SLACK_TS = re.compile(r"\d{1,12}\.\d{1,8}")
_ENTITY = re.compile(r"<([^<>]*)>")
_PING = re.compile(r"@(here|channel|everyone)\b", re.IGNORECASE)
_SPACES = re.compile(r"[ \t]{2,}")
WORD_JOINER = "⁠"


def mention_only_line(bot_user_id: str) -> str:
    """The line Dinnerbell's first reply in a channel thread ends with (spec 6.14)."""
    return f"_I only see messages in this thread that mention <@{bot_user_id}>._"


def home_line(bot_user_id: str | None, channel_id: str) -> str:
    """App Home's line, while the assistant answers the member. mrkdwn that Dinnerbell builds."""
    bot = f"<@{bot_user_id}>" if bot_user_id else "Dinnerbell"
    return f"Need ideas for a meal? Message Dinnerbell, or mention {bot} in <#{channel_id}>."


def readable(text: str, bot_user_id: str | None) -> str:
    """A Slack message as the plain text a member would type on the web. Dinnerbell's own
    mention goes. Anyone else's mention becomes "@someone", so no Slack ID or name leaves the
    house. A channel reads as its name, a link as its label and address, and ``&amp;``,
    ``&lt;``, and ``&gt;`` as the characters they stand for."""

    def swap(match: re.Match[str]) -> str:
        inner = match.group(1)
        target, _, label = inner.partition("|")
        if target.startswith("@"):
            return "" if target[1:] == bot_user_id else "@someone"
        if target.startswith("#"):
            return f"#{label}" if label else "#channel"
        if target.startswith("!"):
            special = target[1:]
            if special in {"here", "channel", "everyone"}:
                return f"@{special}"
            return label  # a user group's handle, or a date's fallback text
        if label and label != target:
            return f"{label} ({target})"
        return target

    plain = _ENTITY.sub(swap, text)
    plain = plain.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return "\n".join(_SPACES.sub(" ", line).strip() for line in plain.splitlines()).strip()


def safe_reply(text: str, limit: int = MAX_REPLY) -> str:
    """The model's text, ready for Slack: ``&``, ``<``, and ``>`` escaped, so it can make no
    mention, ``<!channel>`` or ``<!here>`` ping, or link of its own; a bare ``@here``,
    ``@channel``, or ``@everyone`` broken with a word joiner, so it never pings either; and cut
    to ``limit`` characters without splitting an escape."""
    safe = _PING.sub(lambda m: f"@{WORD_JOINER}{m.group(1)}", escape(text.strip()))
    return clip_mrkdwn(safe, limit) if len(safe) > limit else safe


@dataclass(frozen=True, slots=True)
class Incoming:
    """One message to answer, as the event gave it."""

    user: str
    channel: str
    ts: str
    root: str
    """The thread's root: the message's ``thread_ts``, or its own ``ts`` at the top level."""
    text: str
    private: bool
    """A DM (private to its member), rather than a mention in the household channel."""


class _Bounded:
    """A set that forgets its oldest entries past ``REMEMBERED``."""

    def __init__(self) -> None:
        self._items: dict[str, None] = {}

    def __contains__(self, item: str) -> bool:
        return item in self._items

    def add(self, item: str) -> None:
        self._items.pop(item, None)
        self._items[item] = None
        while len(self._items) > REMEMBERED:
            del self._items[next(iter(self._items))]


class SlackAssistant:
    """DMs and mentions, for ``SlackConnection``. ``spawn`` runs work as a tracked task (the
    interactions' own, so shutdown waits for it), and ``team_id`` is the workspace the bot
    token belongs to, None until the first check."""

    def __init__(
        self,
        api: SlackApi,
        *,
        assistant: Assistant,
        sessionmaker: async_sessionmaker[AsyncSession],
        channel_id: str,
        team_id: Callable[[], str | None],
        spawn: Callable[[Coroutine[Any, Any, None]], None],
    ) -> None:
        self.api = api
        self.assistant = assistant
        self.sessionmaker = sessionmaker
        self.channel_id = channel_id
        self.team_id = team_id
        self.spawn = spawn
        self.bot_user_id: str | None = None
        """Dinnerbell's own Slack user, from ``auth.test``; set by ``SlackConnection.check``."""
        self.seen = _Bounded()
        """Messages already taken, by channel and ``ts``: Slack can deliver an event again."""
        self.replied = _Bounded()
        """Channel threads Dinnerbell has answered in since it started, by conversation key."""
        self.answering: set[tuple[int, str]] = set()
        """(member, conversation key) while that member's message is being answered."""
        self.told: set[tuple[int, str]] = set()
        """Of those, the ones already told ``STILL_ON_IT``, so a burst gets one notice."""

    def register(self, app: AsyncApp) -> None:
        async def message(body: dict[str, Any], event: dict[str, Any]) -> None:
            self.received(body, event, mention=False)

        async def app_mention(body: dict[str, Any], event: dict[str, Any]) -> None:
            self.received(body, event, mention=True)

        app.event("message")(message)
        app.event("app_mention")(app_mention)

    # Events ------------------------------------------------------------------------------------

    def received(self, body: dict[str, Any], event: dict[str, Any], *, mention: bool) -> None:
        """Take a ``message`` (only a DM's) or ``app_mention`` event, or ignore it."""
        incoming = self._incoming(body, event, mention=mention)
        if incoming is None:
            return
        if incoming.channel + ":" + incoming.ts in self.seen:
            return  # delivered again, as after a reconnect
        self.seen.add(incoming.channel + ":" + incoming.ts)
        self.spawn(self.answer(incoming))

    def _incoming(self, body: dict[str, Any], event: dict[str, Any], *, mention: bool) -> Incoming | None:
        team = self.team_id()
        if team is None or body.get("team_id") != team:
            return None
        if event.get("bot_id") or event.get("subtype") or event.get("bot_profile"):
            return None  # bots, Dinnerbell included; edits, deletions, joins
        user, channel, ts, text = (event.get(k) for k in ("user", "channel", "ts", "text"))
        if not (isinstance(user, str) and isinstance(channel, str) and isinstance(ts, str) and isinstance(text, str)):
            return None
        if user == self.bot_user_id or not is_channel_id(channel) or not SLACK_TS.fullmatch(ts):
            return None
        root = event.get("thread_ts") or ts
        if not isinstance(root, str) or not SLACK_TS.fullmatch(root):
            return None
        if mention:
            if channel != self.channel_id:
                return None  # Dinnerbell answers mentions only in the household channel
            return Incoming(user, channel, ts, root, text, private=False)
        if event.get("channel_type") != "im" or not channel.startswith("D"):
            return None  # the household channel's messages come as mentions, if at all
        return Incoming(user, channel, ts, root, text, private=True)

    # Answering ---------------------------------------------------------------------------------

    async def _member(self, user: str) -> Member | None:
        """The active member behind ``user`` while the assistant answers them, or None."""
        async with self.sessionmaker() as db:
            member = await slack_members.member_for_slack_user(db, user)
            if member is None or not member.is_member:
                return None  # unlinked, a guest, or deactivated: no reply, nothing kept or sent
            if not await available(db, self.assistant.config, member):
                return None
        return member

    async def answer(self, incoming: Incoming) -> None:
        member = await self._member(incoming.user)
        if member is None:
            log.debug("slack message to the assistant ignored")
            return
        member_id_var.set(member.id)
        try:
            key = slack_key(incoming.channel, incoming.root)
        except InvalidConversationKey:
            return
        text = readable(incoming.text, self.bot_user_id)
        if not text:
            return
        busy = (member.id, key)
        if busy in self.answering:
            if busy not in self.told:
                self.told.add(busy)
                await self._post(incoming, member, STILL_ON_IT)
            return
        self.answering.add(busy)
        try:
            reply_text = await self._converse(member, key, incoming, text)
        finally:
            self.answering.discard(busy)
            self.told.discard(busy)
        if reply_text is None:
            return
        body = safe_reply(reply_text)
        first = not incoming.private and key not in self.replied
        if first and self.bot_user_id:
            note = mention_only_line(self.bot_user_id)
            room = MAX_REPLY - len(note) - 2
            body = f"{clip_mrkdwn(body, room) if len(body) > room else body}\n\n{note}"
        if await self._post(incoming, member, body) and not incoming.private:
            self.replied.add(key)

    async def _converse(self, member: Member, key: str, incoming: Incoming, text: str) -> str | None:
        """``converse``'s reply text, or None when there's nothing to say."""
        try:
            async with self.sessionmaker() as db:
                reply = await converse(db, self.assistant, member, key, private=incoming.private, text=text)
        except Forbidden:
            log.warning("slack message to the assistant refused: the conversation isn't theirs")
            return None
        except InvalidConversationKey:  # pragma: no cover - slack_key made it, so it fits
            return None
        except Exception:
            log.exception("the assistant broke answering a slack message")
            return FAILED
        return None if reply is None else reply.text

    async def _post(self, incoming: Incoming, member: Member, text: str) -> bool:
        """Post ``text`` in the message's thread, already safe for Slack. False if Slack refused."""
        try:
            await self.api.post(incoming.channel, {"text": text or ELLIPSIS}, thread_ts=incoming.root)
        except SLACK_ERRORS as exc:
            log.warning("slack assistant reply failed", extra={"error": str(exc), "member_id": member.id})
            return False
        return True

    # App Home ----------------------------------------------------------------------------------

    async def home_hint(self, db: AsyncSession, member: Member) -> str | None:
        """App Home's line about the assistant, while it answers ``member``; otherwise None."""
        if not await available(db, self.assistant.config, member):
            return None
        return home_line(self.bot_user_id, self.channel_id)
