# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign in with a code (spec 6.15): ``/auth/code``.

Under ``/auth/``, so ``RateLimitMiddleware`` counts every attempt, a wrong code included,
against the per-address limit. The code travels only in POST bodies, never a URL, so it can't
reach the access log, the browser's history, or a referrer. Every failure gets one message,
and every wrong code also counts toward ``CodeThrottle``, which slows every attempt once more
than 200 wrong codes from all addresses arrive in an hour. A waiting attempt holds no database
connection. An address with an attempt waiting already, or any attempt past ``MAX_SLEEPERS``
waiting in all, is refused with 429 at once.

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
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import PlainTextResponse, Response

from dinnerbell.domain.sign_in_codes import MAX_TYPED_LENGTH
from dinnerbell.services import sign_in_codes, web_app
from dinnerbell.web.auth import Context, CSRFCheckedIfSignedIn, Database, MaybePrincipal, session_cookie_header
from dinnerbell.web.context import AppContext
from dinnerbell.web.middleware import TOO_MANY_SIGN_INS as TOO_MANY
from dinnerbell.web.ratelimit import client_key
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


async def _wait_turn(request: Request, context: AppContext, db: AsyncSession) -> Response | None:
    """Wait out the throttle's delay, if any, holding no database connection: the switch check
    and the session lookup have used this request's session, so it's closed first, and the
    service opens a new connection afterward. A 429 when this address already has an attempt
    waiting, keyed as the per-address limit keys it, so ``trusted_proxies`` counts here too."""
    throttle = context.code_throttle
    if not throttle.delay(context.clock()):
        return None
    await db.close()
    address = client_key(getattr(request.state, "client_ip", "unknown"))
    if await throttle.wait(context.clock(), address):
        return None
    retry_after = int(throttle.delay(context.clock())) + 1
    return PlainTextResponse(TOO_MANY, status_code=429, headers={"Retry-After": str(retry_after)})


@router.post("/code")
async def check_code(
    request: Request, principal: CSRFCheckedIfSignedIn, context: Context, db: Database, code: TypedCode = ""
) -> Response:
    """Step 1: say whose code it is, without using it."""
    await web_app.require_web_app(db, _household_id(principal))
    if refused := await _wait_turn(request, context, db):
        return refused
    member = await sign_in_codes.peek(db, code, key=context.code_key, now=context.clock())
    if member is None:
        context.code_throttle.failed(context.clock())
        return await _form(request, error=DIDNT_WORK)
    return await render(request, "sign_in_code.html", {"member": member, "code": code})


@router.post("/code/continue")
async def use_code(
    request: Request, principal: CSRFCheckedIfSignedIn, context: Context, db: Database, code: TypedCode = ""
) -> Response:
    """Step 2, **Continue**: use the code and sign this device in."""
    await web_app.require_web_app(db, _household_id(principal))
    if refused := await _wait_turn(request, context, db):
        return refused
    signed_in = await sign_in_codes.redeem(
        db,
        code,
        key=context.code_key,
        now=context.clock(),
        replaces_token=request.cookies.get(context.cookies.session),
        user_agent=request.headers.get("user-agent"),
    )
    if signed_in is None:
        context.code_throttle.failed(context.clock())
        return await _form(request, error=DIDNT_WORK)
    response = RedirectResponse("/", status_code=303)
    response.headers.append("Set-Cookie", session_cookie_header(context.cookies, context.settings, signed_in.token))
    return response
