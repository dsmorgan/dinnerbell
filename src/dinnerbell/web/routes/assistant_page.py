# SPDX-License-Identifier: AGPL-3.0-or-later
"""The **Assistant** page (ADR 0009, spec 6.14): one private conversation per member, its draft
cards with **Confirm** and **Cancel**, and **Start over**.

Every route here is a 404 unless the assistant answers this member: the operator's key, Admin's
switch, and an active member or admin (``runtime.available``). A guest is refused before that,
as on every member page.

Each form works without JavaScript. With HTMX, sending a message appends the exchange to the
conversation (or replaces it, at Conversation hours 0, when nothing is kept), and Confirm or
Cancel swaps the draft's card for its result, as **Undo** does a direct action's record (#374).
Where the browser can read a response as it
arrives, ``app.js`` asks for the reply as it's written instead (``web.assistant_stream``, #370).
A catalog draft's Confirm always loads the add-only import preview as a whole page. A reply shows
a small, safe subset of Markdown (``web.reply_markdown``, #284): escaped first, never linked,
never HTML. The member's own messages stay plain text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response, StreamingResponse

from dinnerbell.domain.assistant import web_key
from dinnerbell.domain.assistant_drafts import PROPOSAL_CONFIRMED, day_words, offer_confirmed_text
from dinnerbell.domain.events import EventRuleViolation, IllegalTransition, InvalidResponse, friendly
from dinnerbell.domain.members import Member
from dinnerbell.domain.offers import OfferPath
from dinnerbell.services import activity
from dinnerbell.services.assistant import direct, drafts, memory
from dinnerbell.services.assistant import history as web_history
from dinnerbell.services.assistant.call_drafts import ActionConfirmed
from dinnerbell.services.assistant.conversation import MAX_MESSAGE_CHARS, Assistant, Reply, at_cap_notice, converse
from dinnerbell.services.assistant.drafts import (
    CatalogReady,
    DraftUnavailable,
    DraftView,
    OfferConfirmed,
    ProposalConfirmed,
)
from dinnerbell.services.assistant.history import NOT_HERE, Shown, WebConversation
from dinnerbell.services.assistant.runtime import available
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.variety import household_today
from dinnerbell.web import assistant_stream
from dinnerbell.web.auth import Context, CSRFChecked, Database, Principal, SignedIn
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import RowId
from dinnerbell.web.redirects import local_redirect
from dinnerbell.web.render import is_htmx, render
from dinnerbell.web.routes.catalog import draft_import_page

router = APIRouter(prefix="/assistant")

MAX_FORM_CHARS = 20_000
"""The message field's limit on the server: well above the service's ``MAX_MESSAGE_CHARS``, even
with each line break sent as CRLF, so a long message gets the service's reply saying so, not a
bare 400. The textarea's ``maxlength`` is the service's limit."""
SEND_TIMEOUT_MS = 120_000
"""How long the page waits for a reply. A message can take several model calls, well past the
15 seconds base.html gives every other request."""
NOTICES = {
    "cleared": (
        "Started over. The conversation is gone, and its drafts are cancelled. What the assistant did stays done."
    ),
    "forgot": "Removed. The assistant won't read that note again.",
}
SERVICE_REFUSALS = (EventRuleViolation, InvalidResponse, Forbidden)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a draft card shows after Confirm or Cancel."""

    text: str
    ok: bool
    link: tuple[str, str] | None = None
    """(href, label): where the result is, such as the call."""


def _assistant(request: Request) -> Assistant | None:
    """What ``converse`` needs, from the composition root: None when there's no key."""
    built: Assistant | None = getattr(request.app.state, "assistant", None)
    return built


async def _require(request: Request, db: Database, principal: Principal) -> Assistant:
    """The assistant, or a 404 when it doesn't answer this member."""
    assistant = _assistant(request)
    if assistant is None or not await available(db, assistant.config, principal.member):
        raise NotFound(NOT_HERE)
    return assistant


def _normalized(text: str) -> str:
    """A browser sends each line break in a textarea as CRLF; count it as the one it is."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


async def _page(
    request: Request,
    context: AppContext,
    db: Database,
    principal: Principal,
    *,
    conversation: WebConversation | None = None,
    latest: tuple[Shown, ...] = (),
    notice: str | None = None,
    outcome: Outcome | None = None,
    status_code: int = 200,
) -> Response:
    """The whole page. ``latest`` is an exchange the page shows once, after ``conversation``:
    one the service didn't keep, such as at Conversation hours 0 or a message at the cap."""
    assistant = await _require(request, db, principal)
    member, now = principal.member, context.clock()
    if conversation is None:
        conversation = await web_history.web_conversation(db, assistant.config, member, now)
    turns = (*conversation.turns, *latest)
    records, cards = await _cards(db, member, turns, now)
    page: dict[str, Any] = {
        "conversation": conversation,
        "turns": turns,
        "records": records,
        "drafts": cards,
        "notice": notice,
        "outcome": outcome,
        "cap_notice": await at_cap_notice(db, assistant.config, member, now),
        "memory": await memory.my_memory(db, assistant.config, member, now),
        "send_timeout": SEND_TIMEOUT_MS,
        "max_chars": MAX_MESSAGE_CHARS,
    }
    return await render(request, "assistant.html", page, status_code=status_code)


@router.get("")
async def assistant_page(
    request: Request, principal: SignedIn, context: Context, db: Database, notice: str = ""
) -> Response:
    return await _page(request, context, db, principal, notice=NOTICES.get(notice))


async def _cards(
    db: AsyncSession, member: Member, turns: Sequence[Shown], now: datetime
) -> tuple[dict[int, DraftView], list[DraftView]]:
    """The direct actions' records (#374) by ID, for the log to show where their entries are, and
    the cards after the log: drafts still waiting, and records no entry shows whose **Undo**
    still works, such as at Conversation hours 0, oldest first."""
    key = web_key(member.id)
    records = {r.id: r for r in await direct.records(db, member, key, now)}
    shown = {t.record for t in turns if t.record is not None}
    loose = [r for r in records.values() if r.id not in shown and r.undo]
    waiting = await drafts.waiting(db, member, key, now)
    return records, sorted([*waiting, *loose], key=lambda d: d.id)


def _kept(after: WebConversation, asked: str, reply: Reply) -> bool:
    """Whether the service kept this exchange, so the page shows it after a reload."""
    return after.turns[-2:] == (Shown(True, asked), Shown(False, reply.text))


@router.post("")
async def send(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    message: Annotated[str, Form(max_length=MAX_FORM_CHARS)] = "",
) -> Response:
    """Send a message. With HTMX, the reply comes back as the exchange to add to the page.
    Without it, a kept exchange redirects to the page, so a reload doesn't send it again; one
    the service didn't keep shows on the page this once."""
    assistant = await _require(request, db, principal)
    member = principal.member
    text = _normalized(message).strip()
    if assistant_stream.wants_stream(request):
        return await _streamed(request, context, db, principal, assistant, text)
    before = await web_history.web_conversation(db, assistant.config, member, context.clock())
    reply = await converse(db, assistant, member, web_key(member.id), private=True, text=text)
    if reply is None:
        if text:  # turned off just now: a 404, as every route here is, so the box keeps the text
            raise NotFound(NOT_HERE)
        return Response(status_code=204) if is_htmx(request) else local_redirect("/assistant")
    exchange = (Shown(True, text), Shown(False, reply.text))
    if is_htmx(request):
        cards: tuple[DraftView, ...] | list[DraftView] = reply.drafts
        if not before.keeps_history:  # the exchange replaces what's shown, so keep the waiting cards
            _, cards = await _cards(db, member, (), context.clock())
        response = await render(request, "_assistant_exchange.html", {"turns": exchange, "drafts": cards})
        if not before.keeps_history:
            response.headers["HX-Reswap"] = "innerHTML"
        return response
    after = await web_history.web_conversation(db, assistant.config, member, context.clock())
    if before.keeps_history and _kept(after, text, reply):
        return local_redirect("/assistant#ask")
    return await _page(request, context, db, principal, conversation=before, latest=exchange)


async def _streamed(
    request: Request, context: AppContext, db: Database, principal: Principal, assistant: Assistant, text: str
) -> Response:
    """``send``, with the reply shown as the model writes it (``web.assistant_stream``). What's
    stored, and the exchange and cards it ends with, are what the HTMX response would have."""
    member = principal.member
    if not text:
        return Response(status_code=204)
    before = await web_history.web_conversation(db, assistant.config, member, context.clock())
    pending = await render(request, "_assistant_pending.html", {"t": Shown(True, text)}, fragment=True)
    # The answer gets its own session, and the request's holds nothing while the reply streams.
    await db.close()
    stream = assistant_stream.WebStream()

    async def answer() -> tuple[Reply, Sequence[DraftView]] | None:
        async with context.sessionmaker() as own:
            reply = await converse(own, assistant, member, web_key(member.id), private=True, text=text, stream=stream)
            if reply is None or before.keeps_history:
                return None if reply is None else (reply, reply.drafts)
            # The exchange replaces what's shown, so keep the waiting cards.
            _, cards = await _cards(own, member, (), context.clock())
            return reply, cards

    async def done(result: tuple[Reply, Sequence[DraftView]]) -> str:
        reply, cards = result
        shown = await render(
            request, "_assistant_exchange.html", {"turns": (Shown(False, reply.text),), "drafts": cards}, fragment=True
        )
        return bytes(shown.body).decode()

    answers: assistant_stream.Answers = request.app.state.web_answers
    task = answers.start(answer(), stream, member.id)
    body = assistant_stream.events(
        stream, task, bytes(pending.body).decode(), replace=not before.keeps_history, done=done
    )
    return StreamingResponse(body, media_type=assistant_stream.STREAM_TYPE, headers=assistant_stream.STREAM_HEADERS)


@router.post("/start-over")
async def start_over(request: Request, principal: CSRFChecked, context: Context, db: Database) -> Response:
    assistant = await _require(request, db, principal)
    await web_history.start_over(db, assistant.config, principal.member, context.clock(), assistant=assistant)
    return local_redirect("/assistant?notice=cleared")


# Draft cards ------------------------------------------------------------------------------------


def _confirmed(
    principal: Principal, context: AppContext, result: ProposalConfirmed | OfferConfirmed | ActionConfirmed
) -> Outcome:
    if isinstance(result, ActionConfirmed):
        if result.event_id is None:  # the member's own data (#253): no call to open
            return Outcome(result.text, True, result.link)
        label = "Open the offer" if result.offer else "Open the call"
        return Outcome(result.text, True, (f"/events/{result.event_id}", label))
    if isinstance(result, ProposalConfirmed):
        return Outcome(PROPOSAL_CONFIRMED, True, (f"/events/{result.event_id}", "Open the call"))
    posted = result.posted
    when = day_words(posted.day, household_today(principal.household, context.clock()))
    text = offer_confirmed_text(posted.path, item=posted.item_name, slot=posted.slot, when=when)
    label = "Open the call" if posted.path is OfferPath.ATTACH else "Open the offer"
    return Outcome(text, True, (f"/events/{posted.event_id}", label))


async def _card(
    request: Request,
    context: AppContext,
    db: Database,
    principal: Principal,
    draft_id: int,
    outcome: Outcome,
    *,
    status_code: int,
    before: DraftView | None = None,
) -> Response:
    """The draft's card after Confirm or Cancel, for HTMX, or the page with the outcome on top.
    A draft the service refused but left waiting keeps its buttons, with the refusal on it. A used
    one keeps the title it had ``before`` (#363), with the outcome under it."""
    if not is_htmx(request):
        return await _page(request, context, db, principal, outcome=outcome, status_code=status_code)
    still = await _waiting(db, context, principal, draft_id)
    page = {"draft_id": draft_id, "d": still, "used": before, "outcome": outcome}
    return await render(request, "_assistant_draft.html", page, status_code=status_code)


async def _waiting(db: Database, context: AppContext, principal: Principal, draft_id: int) -> DraftView | None:
    """The member's draft ``draft_id`` on its card, while it still waits for Confirm."""
    member = principal.member
    waiting = await drafts.waiting(db, member, web_key(member.id), context.clock())
    return next((d for d in waiting if d.id == draft_id), None)


def _refused(request: Request, principal: Principal, exc: BaseException, shown: str, draft_id: int) -> Outcome:
    activity.refused(exc, shown, principal.member, path=request.url.path, draft_id=draft_id)
    return Outcome(shown, False)


@router.post("/drafts/{draft_id}/confirm")
async def confirm(
    request: Request, principal: CSRFChecked, context: Context, db: Database, draft_id: RowId
) -> Response:
    """**Confirm**: run the draft through the same service as the form. A catalog draft opens
    the add-only import preview; anything else shows the service's result, or its refusal as
    it is, on the card."""
    assistant = await _require(request, db, principal)
    before = await _waiting(db, context, principal, draft_id)
    try:
        result = await drafts.confirm_draft(db, context.clock, assistant.config, principal.member, draft_id)
    except (NotFound, DraftUnavailable) as exc:
        outcome = _refused(request, principal, exc, str(exc), draft_id)
        return await _card(request, context, db, principal, draft_id, outcome, status_code=409)
    except IllegalTransition as exc:
        outcome = _refused(request, principal, friendly(exc), str(friendly(exc)), draft_id)
        return await _card(request, context, db, principal, draft_id, outcome, status_code=409)
    except SERVICE_REFUSALS as exc:
        outcome = _refused(request, principal, exc, str(exc), draft_id)
        return await _card(request, context, db, principal, draft_id, outcome, status_code=409)
    if isinstance(result, CatalogReady):
        return await draft_import_page(request, context, principal, result)
    outcome = _confirmed(principal, context, result)
    return await _card(request, context, db, principal, draft_id, outcome, status_code=200, before=before)


@router.post("/drafts/{draft_id}/cancel")
async def cancel(request: Request, principal: CSRFChecked, context: Context, db: Database, draft_id: RowId) -> Response:
    await _require(request, db, principal)
    before = await _waiting(db, context, principal, draft_id)
    try:
        await drafts.cancel_draft(db, context.clock, principal.member, draft_id)
    except (NotFound, DraftUnavailable, Forbidden) as exc:
        outcome = _refused(request, principal, exc, str(exc), draft_id)
        return await _card(request, context, db, principal, draft_id, outcome, status_code=409)
    outcome = Outcome("Cancelled. Nothing changed.", True)
    return await _card(request, context, db, principal, draft_id, outcome, status_code=200, before=before)


# Direct actions' records (#374) ----------------------------------------------------------------


@router.post("/records/{record_id}/undo")
async def undo(request: Request, principal: CSRFChecked, context: Context, db: Database, record_id: RowId) -> Response:
    """**Undo** on something the assistant did at once: its inverse, through the same service as
    the form. The record's card shows what Undo did, or its refusal, and loses **Undo** once used."""
    assistant = await _require(request, db, principal)
    try:
        done = await direct.undo(db, context.clock, assistant.config, principal.member, record_id)
    except (NotFound, DraftUnavailable) as exc:
        outcome = _refused(request, principal, exc, str(exc), record_id)
        return await _record_card(request, context, db, principal, record_id, outcome, status_code=409)
    except IllegalTransition as exc:
        outcome = _refused(request, principal, friendly(exc), str(friendly(exc)), record_id)
        return await _record_card(request, context, db, principal, record_id, outcome, status_code=409)
    except SERVICE_REFUSALS as exc:
        outcome = _refused(request, principal, exc, str(exc), record_id)
        return await _record_card(request, context, db, principal, record_id, outcome, status_code=409)
    link = done.link or ((f"/events/{done.event_id}", "Open the call") if done.event_id is not None else None)
    outcome = Outcome(done.text, True, link)
    return await _record_card(request, context, db, principal, record_id, outcome, status_code=200)


async def _record_card(
    request: Request,
    context: AppContext,
    db: Database,
    principal: Principal,
    record_id: int,
    outcome: Outcome,
    *,
    status_code: int,
) -> Response:
    """The record's card after **Undo**, for HTMX, or the page with the outcome on top. A record
    that's gone, or someone else's, gets an empty card with the outcome."""
    if not is_htmx(request):
        return await _page(request, context, db, principal, outcome=outcome, status_code=status_code)
    member = principal.member
    found = await direct.records(db, member, web_key(member.id), context.clock())
    record = next((r for r in found if r.id == record_id), None)
    if record is None:
        record = DraftView(id=record_id, kind="", title="", confirm_lines=(), record=True)
    page = {"r": record, "outcome": outcome}
    return await render(request, "_assistant_record.html", page, status_code=status_code)


# Memory (#367) ----------------------------------------------------------------------------------


@router.post("/memory/{fact_id}/remove")
async def remove_fact(
    request: Request, principal: CSRFChecked, context: Context, db: Database, fact_id: RowId
) -> Response:
    """**Remove** one of the member's own notes. Someone else's, or one that's gone, changes nothing."""
    assistant = await _require(request, db, principal)
    await memory.remove_my_fact(db, assistant.config, principal.member, fact_id)
    return local_redirect("/assistant?notice=forgot#memory")
