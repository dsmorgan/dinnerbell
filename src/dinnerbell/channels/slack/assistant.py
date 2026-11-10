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
When the root is a Dinnerbell post, its call is the conversation's default call. A reply that
carries drafts goes only to its member: in a channel thread, as an ephemeral.

**Drafts** come as a card each after the reply, in its thread: an ephemeral in a channel
thread, and an ordinary message in a DM. A card has the draft's title, what **Confirm** does, and
**Confirm** and **Cancel**, whose value is the draft's ID only (``ids.draft_value``).
``Interactions`` takes the tap (acknowledged at once, then a tracked task, after its workspace,
channel, and member checks) and hands it to ``draft_tap``, which calls ``confirm_draft`` or
``cancel_draft`` as the tapper: the service checks that the draft is theirs. The card is then
replaced with what happened, through the tap's ``response_url`` for an ephemeral and with
``chat.update`` in a DM, so a result never shows in the channel. A catalog draft's Confirm leaves
**Review the import**, a link Dinnerbell builds from its own base URL (``import_link``) to the
web's add-only import preview: the one clickable link the assistant's messages have.

**Replies** are plain text: the model's text is escaped, so it can't make a mention, a channel
ping, or a link, and every URL and bare domain in it is defanged (``hxxps[:]//evil[.]example``), so
Slack can't turn one into a link either. It's cut to Slack's length. A member has one message
answered at a time, across all their conversations: while one is, a new one isn't sent, and
Dinnerbell says once, in the new one's thread, that it's still on the last one.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from slack_bolt.async_app import AsyncApp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.api import SlackApi, SlackUnavailable
from dinnerbell.channels.slack.blocks import (
    ELLIPSIS,
    MAX_FALLBACK,
    Block,
    Mrkdwn,
    actions,
    button,
    clip_mrkdwn,
    context,
    escape,
    safe_url,
    section,
)
from dinnerbell.channels.slack.ids import is_channel_id
from dinnerbell.channels.slack.interactions import ASSISTANT_OFF, SHOWN_ERRORS, Source, Tap, posted_offer_reply
from dinnerbell.domain.assistant import InvalidConversationKey, slack_key
from dinnerbell.domain.events import IllegalTransition, friendly
from dinnerbell.domain.members import Member
from dinnerbell.domain.notifications import MessageRef, PermanentDeliveryError
from dinnerbell.logs import member_id_var
from dinnerbell.services import activity, outbox, slack_members
from dinnerbell.services.assistant.conversation import FAILED, Assistant, Reply, converse
from dinnerbell.services.assistant.drafts import (
    USED,
    CatalogReady,
    Confirmed,
    DraftView,
    OfferConfirmed,
    cancel_draft,
    confirm_draft,
)
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
MAX_DRAFT_TITLE = 150
MAX_DRAFT_LINE = 300
IF_CONFIRMED = "If you confirm:"
CANCELLED = "Cancelled."
PROPOSED = "Proposed. It's in the vote now."
REVIEW_IMPORT = "Review the import"
SIGN_IN_FIRST = (
    "If the page says it doesn't exist, sign in to Dinnerbell on the web in that browser, then open the link again."
)
IMPORT_READY = (
    "The import preview is ready on the web. It only adds new items, and nothing is saved until you import them there."
)
SLACK_TS = re.compile(r"\d{1,12}\.\d{1,8}")
_ENTITY = re.compile(r"<([^<>]*)>")
_PING = re.compile(r"@(here|channel|everyone)(?![^\W_])", re.IGNORECASE)
"""A ping as Slack reads one: the exact word, followed by anything but a letter or digit, or by
nothing. So ``@hereford`` and ``sam@channelx`` stay as they are, while ``@here.`` and ``@here_``
are broken. It runs after NFKC, so a fullwidth at sign is caught, ``@hereⅧ`` reads as
``@hereVIII``, which Slack doesn't ping, and a combining mark that composes with the last letter
(``@here`` and U+0301) makes a precomposed one (``@heré``), which isn't the word any more and is
left alone. A mark that composes with nothing stays separate, and the ping before it is broken."""
_SPACES = re.compile(r"[ \t]{2,}")
WORD_JOINER = "\u2060"
_HTTP = re.compile(r"\bhttp(s?)(?=:)", re.IGNORECASE)
_SCHEME = re.compile(r":(?=//)")
_BARE_SCHEME = re.compile(r"\b(mailto|tel|sms|slack):", re.IGNORECASE)
_WWW = re.compile(r"\bwww[.\uff0e\u3002\uff61]", re.IGNORECASE)
DOTS = frozenset(".\uff0e\u3002\uff61")
"""A dot, and the full-width and ideographic dots that domain names also accept as one."""
ABBREVIATIONS = ("e.g.", "i.e.", "a.m.", "p.m.", "u.s.", "vs.", "etc.")
"""Whole tokens, in any case, whose dots stay as they are. Every other dot before a letter goes."""
_IP = re.compile(r"\b\d{1,3}(?:[.\u3002]\d{1,3}){3}\b")
"""An IPv4 address, with dots or ideographic dots (what NFKC leaves of the other variants)."""
ZWJ = "\u200d"
SKIN_TONES = range(0x1F3FB, 0x1F400)


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


def _is_letter(char: str) -> bool:
    return unicodedata.category(char).startswith("L")


def _is_mark(char: str) -> bool:
    return unicodedata.category(char).startswith("M")


def _next_unmarked(text: str, start: int) -> int:
    """The index of the first character at or after ``start`` that isn't a combining mark."""
    while start < len(text) and _is_mark(text[start]):
        start += 1
    return start


def _kept_dots(text: str) -> set[int]:
    """The indexes of the dots in ``ABBREVIATIONS`` that stand as whole tokens: no letter or
    digit before them, and none after (combining marks skipped)."""
    kept: set[int] = set()
    for abbreviation in ABBREVIATIONS:
        size = len(abbreviation)
        for start in range(len(text) - size + 1):
            if text[start : start + size].lower() != abbreviation:
                continue
            before = text[start - 1] if start else ""
            after = _next_unmarked(text, start + size)
            if before and (before.isalnum() or _is_mark(before)):
                continue
            if after < len(text) and text[after].isalnum():
                continue
            kept.update(start + n for n, char in enumerate(abbreviation) if char == ".")
    return kept


def _emoji(char: str) -> bool:
    """An emoji, near enough: a symbol (``So``) or a skin-tone modifier."""
    return unicodedata.category(char) == "So" or ord(char) in SKIN_TONES


def without_format_characters(text: str) -> str:
    """``text`` without Unicode format characters (category ``Cf``): soft hyphens, zero-width
    spaces and joiners, word joiners, byte order marks, and bidi controls, which can hide a
    domain's dot from any check. Tag characters go too, flags included: they're invisible and
    could carry hidden text, so a subdivision flag shows as a plain black flag. Only a zero-width
    joiner between two emoji (the one in 🧑‍🍳) stays: it can't carry text or sit next to a dot."""
    out: list[str] = []
    for index, char in enumerate(text):
        if unicodedata.category(char) != "Cf":
            out.append(char)
            continue
        if char == ZWJ and index + 1 < len(text) and _emoji(text[index + 1]):
            before = len(out) - 1
            while before >= 0 and unicodedata.category(out[before]).startswith("M"):
                before -= 1  # a variation selector, as in 👁️‍🗨️
            if before >= 0 and _emoji(out[before]):
                out.append(char)
    return "".join(out)


def defang_dots(text: str) -> str:
    """Every dot-like character (``DOTS``) whose next character that isn't a combining mark is a
    letter, in any script, becomes ``[.]``, whatever comes before it, except the dots of
    ``ABBREVIATIONS``. A hand-written scan, not a pattern: ``$3.50`` and ``Done. Next`` keep
    their dots, and ``Done.Next`` doesn't."""
    kept = _kept_dots(text)
    out: list[str] = []
    for index, char in enumerate(text):
        if char in DOTS and index not in kept:
            following = _next_unmarked(text, index + 1)
            if following < len(text) and _is_letter(text[following]):
                out.append("[.]")
                continue
        out.append(char)
    return "".join(out)


def defang(text: str) -> str:
    """``text`` with every URL and bare domain made unlinkable, without needing to recognize
    one. Format characters go first (``without_format_characters``), then the text is
    NFKC-normalized, as IDNA maps a hostname, so ``ⓔⓧⓐⓜⓟⓛⓔ``, fullwidth letters, and
    ligatures such as ``ﬁ`` read as the ASCII letters a hostname parser sees. NFKC changes some
    text a member sees, and that's accepted: ``½`` becomes 1, a fraction slash, and 2, ``…`` three dots, fullwidth
    text ASCII, and a letter with a separate accent one precomposed letter; bidi marks such as
    the right-to-left mark are gone with the other format characters. Then ``http`` and
    ``https`` become ``hxxp`` and ``hxxps``; ``://``, and ``mailto:`` and the like, get
    ``[:]``; ``www.`` and each dot of an IP address become ``[.]``; and so does every dot before
    a letter (``defang_dots``). Slack links none of them."""
    text = without_format_characters(unicodedata.normalize("NFKC", without_format_characters(text)))
    text = _HTTP.sub(lambda m: f"hxxp{m.group(1).lower()}", text)
    text = _SCHEME.sub("[:]", text)
    text = _BARE_SCHEME.sub(lambda m: f"{m.group(1)}[:]", text)
    text = _WWW.sub(lambda m: f"{m.group(0)[:3]}[.]", text)
    text = _IP.sub(lambda m: re.sub(r"[.\u3002]", "[.]", m.group(0)), text)
    return defang_dots(text)


def reply_text(reply: Reply) -> str:
    """The reply's text for Slack (``safe_reply``). Its drafts come after it, a card each
    (``draft_card``)."""
    return safe_reply(reply.text)


def draft_card(draft: DraftView) -> dict[str, Any]:
    """A draft's card: its title and what **Confirm** does, through ``safe_reply`` as the model's
    words are, then **Confirm** and **Cancel**, whose value is the draft's ID and nothing else."""
    title = safe_reply(draft.title, MAX_DRAFT_TITLE) or ELLIPSIS
    lines = [f"• {safe_reply(line, MAX_DRAFT_LINE)}" for line in draft.confirm_lines]
    text = "\n".join([f"*{title}*", *([IF_CONFIRMED, *lines] if lines else [])])
    value = ids.draft_value(draft.id)
    buttons = actions(
        button("Confirm", ids.DRAFT_CONFIRM, value, style="primary"),
        button("Cancel", ids.DRAFT_CANCEL, value),
        block_id=f"draft_{draft.id}",
    )
    return {"text": title, "blocks": [section(text), buttons]}


def result_card(text: str, *blocks: Block) -> dict[str, Any]:
    """What a draft card becomes once tapped: ``text``, mrkdwn that's safe already, and no buttons
    but a link's."""
    return {"text": text, "blocks": [section(text), *blocks]}


def import_card(url: str | None) -> dict[str, Any]:
    """A confirmed catalog draft's card: **Review the import**, a link Dinnerbell built from its
    own base URL, and a line for Slack's in-app browser, which isn't signed in to Dinnerbell."""
    if url is None:  # pragma: no cover - Confirm builds the link before it confirms
        raise RuntimeError("a catalog draft confirmed without its link")
    review = button(REVIEW_IMPORT, ids.DRAFT_IMPORT_LINK, url=url, style="primary")
    text = f"{IMPORT_READY} <{url}|{REVIEW_IMPORT}>"
    return result_card(text, actions(review, block_id="import"), context(SIGN_IN_FIRST))


def confirmed_text(done: Confirmed) -> str:
    """What a Confirm did, for its card: the same words as the form's reply, through
    ``safe_reply``, as item names are what people typed, and one named like a domain must not
    become a link."""
    if isinstance(done, OfferConfirmed):
        return safe_reply(posted_offer_reply(done.posted))
    return PROPOSED


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


ImportLink = Callable[[int, int], str]
"""The web link to a confirmed catalog draft's import preview, from a draft ID and its member's,
built from the configured base URL (the composition root passes the web's ``draft_import_url``)."""


class SlackAssistant:
    """DMs and mentions, and the buttons on draft cards, for ``SlackConnection``. ``spawn`` runs
    work as a tracked task (the interactions' own, so shutdown waits for it), ``team_id`` is the
    workspace the bot token belongs to, None until the first check, and ``import_link`` makes a
    catalog draft's **Review the import** link."""

    def __init__(
        self,
        api: SlackApi,
        *,
        assistant: Assistant,
        sessionmaker: async_sessionmaker[AsyncSession],
        channel_id: str,
        team_id: Callable[[], str | None],
        spawn: Callable[[Coroutine[Any, Any, None]], None],
        import_link: ImportLink,
    ) -> None:
        self.api = api
        self.import_link = import_link
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
        """Channel threads Dinnerbell has answered in publicly since it started, by conversation
        key: the mention-only note has reached the thread."""
        self.noted = _Bounded()
        """Channel threads and members, ``key:user``, whose private reply had the mention-only
        note, so it isn't repeated to them until a public reply gives it to the thread."""
        self.answering: set[int] = set()
        """Members with a message being answered, in any conversation: one at a time each."""
        self.told: set[int] = set()
        """Of those, the ones already told ``STILL_ON_IT``, so a burst gets one notice."""
        self.draft_lock = asyncio.Lock()
        """Draft taps, one at a time, so a double tap's second finds the first's result."""
        self.settled = _Bounded()
        """Drafts confirmed or cancelled from a card since start, by member and draft ID: a
        second tap on one leaves the card showing what the first did."""

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
            reply = await self._converse(member, key, incoming, text)
        finally:
            self.answering.discard(member.id)
            self.told.discard(member.id)
        if reply is None:
            return
        body = reply_text(reply)
        cards = [draft_card(draft) for draft in reply.drafts]
        private = bool(cards) and not incoming.private  # drafts, and the reply on them, go only to their member
        noted = f"{key}:{incoming.user}"
        first = not incoming.private and key not in self.replied and not (private and noted in self.noted)
        if first and self.bot_user_id:
            note = mention_only_line(self.bot_user_id)
            room = MAX_REPLY - len(note) - 2
            body = f"{clip_mrkdwn(body, room) if len(body) > room else body}\n\n{note}".lstrip()
        if private:  # spec 6.14
            if body and await self._ephemeral(incoming, member, body) and first:
                self.noted.add(noted)  # this member has seen the note; the thread hasn't yet
            for card in cards:
                await self._ephemeral(incoming, member, card["text"], card["blocks"])
            return
        if (body or not cards) and await self._post(incoming, member, body) and not incoming.private:
            self.replied.add(key)
        for card in cards:
            await self._post(incoming, member, card["text"], card["blocks"])

    async def _call_id(self, member: Member, incoming: Incoming) -> int | None:
        """The call a thread's root post is about, when the root is a Dinnerbell post such as a
        call message or a reminder: the conversation's default call (spec 6.14)."""
        if incoming.root == incoming.ts:
            return None  # a top-level message is the member's own, not a Dinnerbell post
        async with self.sessionmaker() as db:
            return await outbox.call_for_post(db, member.household_id, MessageRef(incoming.channel, incoming.root))

    async def _converse(self, member: Member, key: str, incoming: Incoming, text: str) -> Reply | None:
        """``converse``'s reply, or None when there's nothing to say."""
        try:
            call_id = await self._call_id(member, incoming)
            async with self.sessionmaker() as db:
                reply = await converse(
                    db, self.assistant, member, key, private=incoming.private, text=text, call_id=call_id
                )
        except Forbidden:
            log.warning("slack message to the assistant refused: the conversation isn't theirs")
            return None
        except InvalidConversationKey:  # pragma: no cover - slack_key made it, so it fits
            return None
        except Exception:
            log.exception("the assistant broke answering a slack message")
            return Reply(FAILED)
        return reply

    async def _post(self, incoming: Incoming, member: Member, text: str, blocks: list[Block] | None = None) -> bool:
        """Post ``text`` in the message's thread, already safe for Slack, with a draft card's
        ``blocks``. False if Slack refused. Nothing unfurls: neither links nor media."""
        message: dict[str, Any] = {"text": text or ELLIPSIS}
        if blocks:
            message["blocks"] = blocks
        try:
            await self.api.post(incoming.channel, message, thread_ts=incoming.root, unfurl_media=False)
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
        await self._ephemeral(incoming, member, STILL_ON_IT)

    async def _ephemeral(
        self, incoming: Incoming, member: Member, text: str, blocks: list[Block] | None = None
    ) -> bool:
        """Post ``text``, already safe, in the message's thread, for its author's eyes only, with
        a draft card's ``blocks``. False if Slack refused."""
        try:
            await self.api.ephemeral(
                incoming.channel, incoming.user, text or ELLIPSIS, blocks or None, thread_ts=incoming.root
            )
        except SLACK_ERRORS as exc:
            log.warning("slack assistant reply failed", extra={"error": str(exc), "member_id": member.id})
            return False
        return True

    # Draft cards -------------------------------------------------------------------------------

    async def draft_tap(self, member: Member, source: Source, action_id: str, value: object, tap: Tap) -> Mrkdwn | None:
        """**Confirm** or **Cancel** on a draft card, for ``Interactions``, which checked the
        workspace, the channel or DM, and that the tapper is an active member. The service checks
        that the draft is theirs, waiting, and unexpired, and its refusal replaces the card in its
        own words (``safe_reply``). While the assistant isn't available, the card stays and the
        tapper is told. Taps run one at a time, through replacing the card, so a double tap's
        second finds the first's card in place and leaves it."""
        draft_id = ids.parse_draft(value)
        if source.home or source.channel_id is None:
            raise ids.BadValue("draft cards are drawn only in messages")
        if source.channel_id == self.channel_id and not tap.ephemeral:
            raise ids.BadValue("a draft card in the channel is only ever an ephemeral")
        settled = f"{member.id}:{draft_id}"
        async with self.draft_lock:
            async with self.sessionmaker() as db:
                if not await available(db, self.assistant.config, member):
                    return Mrkdwn(ASSISTANT_OFF)
                refusal: Exception | None = None
                # Built first, so nothing that can fail comes between using a catalog draft and
                # showing its link.
                link = self._import_url(draft_id, member) if action_id == ids.DRAFT_CONFIRM else None
                try:
                    if action_id == ids.DRAFT_CONFIRM:
                        done = await confirm_draft(db, self.assistant.clock, self.assistant.config, member, draft_id)
                        catalog = isinstance(done, CatalogReady)
                        card = import_card(link) if catalog else result_card(confirmed_text(done))
                    else:
                        await cancel_draft(db, self.assistant.clock, member, draft_id)
                        card = result_card(CANCELLED)
                except SHOWN_ERRORS as exc:
                    refusal = exc
                except IllegalTransition as exc:
                    refusal = friendly(exc)
            if refusal is not None:
                if str(refusal) == USED and settled in self.settled:
                    return None  # a double tap: the card shows what the first tap did
                activity.refused(refusal, str(refusal), member, action=action_id)
                card = result_card(safe_reply(str(refusal)))
            replaced, fallback = await self._replace_card(source, tap, card, member)
            if replaced and refusal is None:
                self.settled.add(settled)  # only once the card itself shows what happened
            return fallback

    def _import_url(self, draft_id: int, member: Member) -> str:
        url = safe_url(self.import_link(draft_id, member.id))
        if url is None:  # pragma: no cover - the web builds it from the base URL, which is checked
            raise RuntimeError("the import link isn't a usable URL")
        return url

    async def _replace_card(
        self, source: Source, tap: Tap, card: dict[str, Any], member: Member
    ) -> tuple[bool, Mrkdwn | None]:
        """Replace the tapped card with ``card``: an ephemeral through the tap's ``response_url``,
        a DM card with ``chat.update``. Returns whether it was replaced, and what to tell the
        tapper instead when it wasn't. If Slack won't replace it, the card goes to the tapper
        alone, in the card's thread when the tap says which; failing that, its text goes back to
        ``Interactions``, which tells only the tapper. Nothing here ever posts in the channel for
        everyone."""
        channel = source.channel_id
        try:
            if channel == self.channel_id:
                if tap.response_url is not None:
                    await self.api.replace_original(tap.response_url, card)
                    return True, None
            elif channel is not None and tap.message_ts is not None:
                await self.api.update(MessageRef(channel, tap.message_ts), card)
                return True, None
        except SLACK_ERRORS as exc:
            extra = {"error": str(exc), "member_id": member.id}
            log.info("slack draft card not replaced; saying it instead", extra=extra)
        if channel is not None and tap.thread_ts is not None:
            try:
                await self.api.ephemeral(channel, source.user_id, card["text"], card["blocks"], thread_ts=tap.thread_ts)
                return False, None
            except SLACK_ERRORS as exc:
                extra = {"error": str(exc), "member_id": member.id}
                log.info("slack draft result not posted in its thread", extra=extra)
        return False, Mrkdwn(card["text"])

    # App Home ----------------------------------------------------------------------------------

    async def home_hint(self, db: AsyncSession, member: Member) -> str | None:
        """App Home's line about the assistant, while it answers ``member``; otherwise None."""
        if not await available(db, self.assistant.config, member):
            return None
        return home_line(self.bot_user_id, self.channel_id)
