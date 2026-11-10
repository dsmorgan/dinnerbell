# SPDX-License-Identifier: AGPL-3.0-or-later
"""The **Assistant** page (ADR 0009, spec 6.14): one private conversation per member, its draft
cards with **Confirm** and **Cancel**, and **Start over**.

Every route here is a 404 unless the assistant answers this member: the operator's key and
flag, Admin's switch, and an active member or admin (``runtime.available``). A guest is refused
before that, as on every member page.

Each form works without JavaScript. With HTMX, sending a message appends the exchange to the
conversation (or replaces it, at Conversation hours 0, when nothing is kept), and Confirm or
Cancel swaps the draft's card for its result. A catalog draft's Confirm always loads the
add-only import preview as a whole page. Reply text is plain text: the template escapes it, and
nothing turns it into HTML or links.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from starlette.responses import Response

from dinnerbell.domain.assistant import web_key
from dinnerbell.domain.assistant_drafts import day_words
from dinnerbell.domain.events import EventRuleViolation, IllegalTransition, InvalidResponse, friendly
from dinnerbell.domain.offers import OfferPath
from dinnerbell.services import activity
from dinnerbell.services.assistant import drafts
from dinnerbell.services.assistant import history as web_history
from dinnerbell.services.assistant.conversation import Assistant, Reply, converse
from dinnerbell.services.assistant.drafts import (
    CatalogReady,
    DraftUnavailable,
    DraftView,
    OfferConfirmed,
    ProposalConfirmed,
)
from dinnerbell.services.assistant.history import Shown, WebConversation
from dinnerbell.services.assistant.runtime import available
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.variety import household_today
from dinnerbell.web.auth import Context, CSRFChecked, Database, Principal, SignedIn
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import RowId
from dinnerbell.web.redirects import local_redirect
from dinnerbell.web.render import is_htmx, render
from dinnerbell.web.routes.catalog import draft_import_page

router = APIRouter(prefix="/assistant")

MAX_FORM_CHARS = 10_000
"""The message field's limit. The service refuses anything over its own, smaller limit with a
reply that says so; this only keeps a stray huge field from reaching it."""
SEND_TIMEOUT_MS = 120_000
"""How long the page waits for a reply. A message can take several model calls, well past the
15 seconds base.html gives every other request."""
NOT_HERE = "That page doesn't exist."
NOTICES = {"cleared": "Started over. The conversation is gone, and its drafts are cancelled."}
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
    waiting = await drafts.waiting(db, member, web_key(member.id), now)
    page: dict[str, Any] = {
        "conversation": conversation,
        "turns": (*conversation.turns, *latest),
        "drafts": waiting,
        "notice": notice,
        "outcome": outcome,
        "send_timeout": SEND_TIMEOUT_MS,
        "max_chars": MAX_FORM_CHARS,
    }
    return await render(request, "assistant.html", page, status_code=status_code)


@router.get("")
async def assistant_page(
    request: Request, principal: SignedIn, context: Context, db: Database, notice: str = ""
) -> Response:
    return await _page(request, context, db, principal, notice=NOTICES.get(notice))


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
    before = await web_history.web_conversation(db, assistant.config, member, context.clock())
    reply = await converse(db, assistant, member, web_key(member.id), private=True, text=text)
    if reply is None:  # an empty message, or the assistant turned off just now
        return Response(status_code=204) if is_htmx(request) else local_redirect("/assistant")
    exchange = (Shown(True, text), Shown(False, reply.text))
    if is_htmx(request):
        cards: tuple[DraftView, ...] | list[DraftView] = reply.drafts
        if not before.keeps_history:  # the exchange replaces what's shown, so keep the waiting cards
            cards = await drafts.waiting(db, member, web_key(member.id), context.clock())
        response = await render(request, "_assistant_exchange.html", {"turns": exchange, "drafts": cards})
        if not before.keeps_history:
            response.headers["HX-Reswap"] = "innerHTML"
        return response
    after = await web_history.web_conversation(db, assistant.config, member, context.clock())
    if before.keeps_history and _kept(after, text, reply):
        return local_redirect("/assistant#ask")
    return await _page(request, context, db, principal, conversation=before, latest=exchange)


@router.post("/start-over")
async def start_over(request: Request, principal: CSRFChecked, context: Context, db: Database) -> Response:
    assistant = await _require(request, db, principal)
    await web_history.start_over(db, assistant.config, principal.member, context.clock())
    return local_redirect("/assistant?notice=cleared")


# Draft cards ------------------------------------------------------------------------------------


def _confirmed(principal: Principal, context: AppContext, result: ProposalConfirmed | OfferConfirmed) -> Outcome:
    if isinstance(result, ProposalConfirmed):
        link = (f"/events/{result.event_id}", "Open the call")
        return Outcome("Confirmed. Your proposal is on the call.", True, link)
    posted = result.posted
    when = day_words(posted.day, household_today(principal.household, context.clock()))
    slot = posted.slot.value
    if posted.path is OfferPath.ATTACH:
        text = f"Confirmed. {posted.item_name} is a pick on {when}'s {slot} call, and you're in to make it happen."
        return Outcome(text, True, (f"/events/{posted.event_id}", "Open the call"))
    if posted.path is OfferPath.REPLACE:
        text = f"Confirmed. Your {slot} offer for {when} is posted, and it replaces that day's scheduled {slot} call."
    else:
        text = f"Confirmed. Your {slot} offer for {when} is posted."
    return Outcome(text, True, (f"/events/{posted.event_id}", "Open the offer"))


async def _card(
    request: Request,
    context: AppContext,
    db: Database,
    principal: Principal,
    draft_id: int,
    outcome: Outcome,
    *,
    status_code: int,
) -> Response:
    """The draft's card after Confirm or Cancel, for HTMX, or the page with the outcome on top.
    A draft the service refused but left waiting keeps its buttons, with the refusal on it."""
    if not is_htmx(request):
        return await _page(request, context, db, principal, outcome=outcome, status_code=status_code)
    member = principal.member
    waiting = await drafts.waiting(db, member, web_key(member.id), context.clock())
    still = next((d for d in waiting if d.id == draft_id), None)
    page = {"draft_id": draft_id, "d": still, "outcome": outcome}
    return await render(request, "_assistant_draft.html", page, status_code=status_code)


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
    return await _card(request, context, db, principal, draft_id, outcome, status_code=200)


@router.post("/drafts/{draft_id}/cancel")
async def cancel(request: Request, principal: CSRFChecked, context: Context, db: Database, draft_id: RowId) -> Response:
    await _require(request, db, principal)
    try:
        await drafts.cancel_draft(db, context.clock, principal.member, draft_id)
    except (NotFound, DraftUnavailable, Forbidden) as exc:
        outcome = _refused(request, principal, exc, str(exc), draft_id)
        return await _card(request, context, db, principal, draft_id, outcome, status_code=409)
    outcome = Outcome("Cancelled. Nothing changed.", True)
    return await _card(request, context, db, principal, draft_id, outcome, status_code=200)
