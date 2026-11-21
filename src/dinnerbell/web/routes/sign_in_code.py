# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign in with a code (spec 6.15): ``/auth/code``.

Under ``/auth/``, so ``RateLimitMiddleware`` counts every attempt, a wrong code included,
against the per-address limit. The code travels only in POST bodies, never a URL, so it can't
reach the access log, the browser's history, or a referrer. Every failure gets one message.
Every wrong code also counts toward ``WrongCodes``: past 200 in an hour from all addresses,
Dinnerbell logs a warning and Admin > Web app says so. Nothing slows down or locks out.

A device signing in has no session yet to bind a CSRF token to, so ``OriginCheckMiddleware``
protects these forms: a POST from another site is refused before it gets here. A device that
does have a session sends that session's token too (``csrf_if_signed_in``), as Sign out does.
Signing in replaces the device's session cookie with a new session.

While Admin's **Web app** switch is off, every route here is the usual 404.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.domain.sign_in_codes import MAX_TYPED_LENGTH
from dinnerbell.services import sign_in_codes, web_app
from dinnerbell.web.auth import Context, CSRFCheckedIfSignedIn, Database, MaybePrincipal, session_cookie_header
from dinnerbell.web.render import render

router = APIRouter(prefix="/auth")

TypedCode = Annotated[str, Form(max_length=MAX_TYPED_LENGTH)]
DIDNT_WORK = "That code didn't work. Check it and try again, or ask for a new one. A code works once, for 15 minutes."


def _household_id(principal: MaybePrincipal) -> int | None:
    return principal.household.id if principal is not None else None


async def _form(request: Request, *, error: str | None = None) -> Response:
    return await render(request, "sign_in_code.html", {"error": error}, status_code=400 if error else 200)


@router.get("/code")
async def code_page(request: Request, principal: MaybePrincipal, db: Database) -> Response:
    await web_app.require_web_app(db, _household_id(principal))
    return await _form(request)


@router.post("/code")
async def check_code(
    request: Request, principal: CSRFCheckedIfSignedIn, context: Context, db: Database, code: TypedCode = ""
) -> Response:
    """Step 1: say whose code it is, without using it."""
    await web_app.require_web_app(db, _household_id(principal))
    member = await sign_in_codes.peek(db, code, key=context.code_key, now=context.clock())
    if member is None:
        context.wrong_codes.failed(context.clock())
        return await _form(request, error=DIDNT_WORK)
    return await render(request, "sign_in_code.html", {"member": member, "code": code})


@router.post("/code/continue")
async def use_code(
    request: Request, principal: CSRFCheckedIfSignedIn, context: Context, db: Database, code: TypedCode = ""
) -> Response:
    """Step 2, **Continue**: use the code and sign this device in."""
    await web_app.require_web_app(db, _household_id(principal))
    signed_in = await sign_in_codes.redeem(
        db,
        code,
        key=context.code_key,
        now=context.clock(),
        replaces_token=request.cookies.get(context.cookies.session),
        user_agent=request.headers.get("user-agent"),
    )
    if signed_in is None:
        context.wrong_codes.failed(context.clock())
        return await _form(request, error=DIDNT_WORK)
    response = RedirectResponse("/", status_code=303)
    response.headers.append("Set-Cookie", session_cookie_header(context.cookies, context.settings, signed_in.token))
    return response
