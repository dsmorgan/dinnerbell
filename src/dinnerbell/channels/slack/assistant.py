# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optional assistant in Slack (ADR 0009, spec 6.14): direct messages to Dinnerbell, and
mentions of it in the household channel. ``SlackConnection`` builds this only when the operator
set ``DINNERBELL_ANTHROPIC_API_KEY``; without it, nothing here is registered.

Each message is acknowledged at once (Bolt acknowledges events itself) and answered in a tracked
task. Dinnerbell ignores, with no reply and nothing stored or sent:

- anything from a bot, Dinnerbell included, and anything with a ``subtype`` or ``edited``: edits,
  deletions, joins, and bot messages;
- a mention outside the household channel, a mention with no text or with files, and a message
  from another workspace;
- a Slack user who isn't an active member: a guest, a deactivated member, or someone no member
  is linked to;
- everything, while the assistant isn't available (the flag or Admin's switch is off).

A DM with a file, or with no text, gets ``TEXT_ONLY`` and is sent nowhere.

**Conversations** are Slack threads, keyed by channel and thread root (``slack_key``). A
top-level DM or mention starts one, and Dinnerbell replies in its thread. A mention in a thread
roots at that thread's root, which is a Dinnerbell post when the thread is under a call or a
reminder. DMs are private to their member. A channel thread is shared: each message acts as its
author. Dinnerbell's first reply in a channel thread says it sees only messages that mention it.

**Replies** are plain text: the model's text is escaped, so it can't make a mention, a channel
ping, or a link, and every URL and bare domain in it is defanged (``hxxps://evil[.]example``), so
Slack can't turn one into a link either. It's cut to Slack's length. A member has one message
answered at a time, across all their conversations: while one is, a new one isn't sent, and
Dinnerbell says once, in the new one's thread, that it's still on the last one.
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
TEXT_ONLY = "I can read only text messages."
SLACK_TS = re.compile(r"\d{1,12}\.\d{1,8}")
_ENTITY = re.compile(r"<([^<>]*)>")
_PING = re.compile(r"@(here|channel|everyone)\b", re.IGNORECASE)
_SPACES = re.compile(r"[ \t]{2,}")
WORD_JOINER = "\u2060"
_HTTP = re.compile(r"\bhttp(s?)(?=:)", re.IGNORECASE)
_SCHEME = re.compile(r"(?<=[A-Za-z0-9+.-]):(?=/)")
_DOTS = "[.\uff0e\u3002\uff61]"
"""A dot, or a full-width or ideographic one, which Slack may also read as a domain's."""
_DOMAIN = re.compile(rf"(?<![\w.-])(?:[\w-]+{_DOTS})+[^\W\d_][\w-]*[^\W_](?![\w-])")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def mention_only_line(bot_user_id: str) -> str:
    """The line Dinnerbell's first reply in a channel thread ends with (spec 6.14)."""
    return f"_I only see messages in this thread that mention <@{bot_user_id}>._"


def home_line(bot_user_id: str | None, channel_id: str) -> str:
    """App Home's line, while the assistant answers the member. mrkdwn that Dinnerbell builds."""
    bot = f"<@{bot_user_id}>" if bot_user_id else "Dinnerbell"
    return f"Need ideas for a meal? Message Dinnerbell, or mention {bot} in <#{channel_id}>."


def readable(text: str, bot_user_id: str | None) -> str:
    """A Slack message as the plain text a member would type on the web. Dinnerbell's own
    mention goes. Anyone else's mention becomes "@someone", a user group "@group", and a channel
    "#channel", so no Slack ID or name leaves the house. A link reads as its label and address,
    and ``&amp;``, ``&lt;``, and ``&gt;`` as the characters they stand for.

    TODO(#17): map a mentioned member to the conversation's pseudonym ("Member 2"), so the
    meaning survives and ``restore_names`` brings the name back. That needs the pseudonym map,
    which ``services.assistant`` keeps."""

    def swap(match: re.Match[str]) -> str:
        inner = match.group(1)
        target, _, label = inner.partition("|")
        if target.startswith("@"):
            return "" if target[1:] == bot_user_id else "@someone"
        if target.startswith("#"):
            return "#channel"
        if target.startswith("!"):
            special = target[1:]
            if special in {"here", "channel", "everyone"}:
                return f"@{special}"
            if special.startswith("subteam^"):
                return "@group"
            return label  # a date's fallback text
        if label and label != target:
            return f"{label} ({target})"
        return target

    plain = _ENTITY.sub(swap, text)
    plain = plain.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return "\n".join(_SPACES.sub(" ", line).strip() for line in plain.splitlines()).strip()


def defang(text: str) -> str:
    """``text`` with every URL and bare domain made unlinkable: ``http`` and ``https`` become
    ``hxxp`` and ``hxxps``, any other scheme's ``:`` becomes ``[:]``, and each dot of a domain
    (an email's included) or an IP address becomes ``[.]``. Slack links none of them."""
    text = _HTTP.sub(lambda m: f"hxxp{m.group(1).lower()}", text)
    text = _SCHEME.sub("[:]", text)
    text = _DOMAIN.sub(lambda m: re.sub(_DOTS, "[.]", m.group(0)), text)
    return _IP.sub(lambda m: m.group(0).replace(".", "[.]"), text)


def safe_reply(text: str, limit: int = MAX_REPLY) -> str:
    """The model's text, ready for Slack. Every URL and bare domain is defanged, since model text
    can carry an injected link and Dinnerbell would post it in its own voice; ``&``, ``<``, and
    ``>`` are escaped, so it can make no mention, ``<!channel>`` or ``<!here>`` ping, or link of
    its own; a bare ``@here``, ``@channel``, or ``@everyone`` is broken with a word joiner, so it
    never pings either; and it's cut to ``limit`` characters without splitting an escape.

    A later change may add a Links line that Dinnerbell builds itself from the catalog URLs that
    tools returned; those would be the only clickable links in a reply."""
    safe = _PING.sub(lambda m: f"@{WORD_JOINER}{m.group(1)}", escape(defang(text.strip())))
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
    unreadable: bool = False
    """A DM with a file or without text: it gets ``TEXT_ONLY``, and nothing is sent anywhere."""


class _Bounded:
    """A set that forgets its oldest entries past ``REMEMBERED``."""

    def __init__(self) -> None:
        self._items: dict[str, None] = {}

    def __contains__(self, item: str) -> bool:
        return item in self._items

    def __len__(self) -> int:
        return len(self._items)

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
        self.answering: set[int] = set()
        """Members with a message being answered, in any conversation: one at a time each."""
        self.told: set[int] = set()
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
        if event.get("bot_id") or event.get("bot_profile") or event.get("edited"):
            return None  # bots, Dinnerbell included; edits
        subtype = event.get("subtype")
        if subtype and (mention or subtype != "file_share"):
            return None  # deletions, joins, bot messages, and edits that come as a subtype
        user, channel, ts, text = (event.get(k) for k in ("user", "channel", "ts", "text"))
        if not (isinstance(user, str) and isinstance(channel, str) and isinstance(ts, str)):
            return None
        if user == self.bot_user_id or not is_channel_id(channel) or not SLACK_TS.fullmatch(ts):
            return None
        root = event.get("thread_ts") or ts
        if not isinstance(root, str) or not SLACK_TS.fullmatch(root):
            return None
        has_text = isinstance(text, str) and bool(text.strip())
        if mention:
            if channel != self.channel_id:
                return None  # Dinnerbell answers mentions only in the household channel
            if not has_text or event.get("files"):
                return None  # a channel thread stays quiet about what it can't read
            return Incoming(user, channel, ts, root, str(text), private=False)
        if event.get("channel_type") != "im" or not channel.startswith("D"):
            return None  # the household channel's messages come as mentions, if at all
        unreadable = subtype == "file_share" or bool(event.get("files")) or not has_text
        return Incoming(user, channel, ts, root, str(text or ""), private=True, unreadable=unreadable)

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
        """Answer ``incoming``, logging anything that breaks (never the message's text)."""
        try:
            await self._answer(incoming)
        except Exception:
            log.exception("the assistant's slack message failed")

    async def _answer(self, incoming: Incoming) -> None:
        member = await self._member(incoming.user)
        if member is None:
            log.debug("slack message to the assistant ignored")
            return
        member_id_var.set(member.id)
        if incoming.unreadable:
            await self._post(incoming, member, TEXT_ONLY)
            return
        try:
            key = slack_key(incoming.channel, incoming.root)
        except InvalidConversationKey:
            return
        text = readable(incoming.text, self.bot_user_id)
        if not text:
            return
        if member.id in self.answering:
            if member.id not in self.told:
                self.told.add(member.id)
                await self._busy(incoming, member)
            return
        self.answering.add(member.id)
        try:
            reply_text = await self._converse(member, key, incoming, text)
        finally:
            self.answering.discard(member.id)
            self.told.discard(member.id)
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
        """Post ``text`` in the message's thread, already safe for Slack. False if Slack refused.
        Nothing unfurls: neither links nor media."""
        try:
            await self.api.post(
                incoming.channel, {"text": text or ELLIPSIS}, thread_ts=incoming.root, unfurl_media=False
            )
        except SLACK_ERRORS as exc:
            log.warning("slack assistant reply failed", extra={"error": str(exc), "member_id": member.id})
            return False
        return True

    async def _busy(self, incoming: Incoming, member: Member) -> None:
        """``STILL_ON_IT``, in the new message's thread: in a DM, a reply; in the channel, an
        ephemeral only the member sees."""
        if incoming.private:
            await self._post(incoming, member, STILL_ON_IT)
            return
        try:
            await self.api.ephemeral(incoming.channel, incoming.user, STILL_ON_IT, thread_ts=incoming.root)
        except SLACK_ERRORS as exc:
            log.warning("slack assistant reply failed", extra={"error": str(exc), "member_id": member.id})

    # App Home ----------------------------------------------------------------------------------

    async def home_hint(self, db: AsyncSession, member: Member) -> str | None:
        """App Home's line about the assistant, while it answers ``member``; otherwise None."""
        if not await available(db, self.assistant.config, member):
            return None
        return home_line(self.bot_user_id, self.channel_id)
