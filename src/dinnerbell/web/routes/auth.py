# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in with OpenID Connect, and sign-out."""

from __future__ import annotations

import hmac
import logging
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.domain.identity import IdentityClaims, Refusal, Refuse
from dinnerbell.services import auth as auth_service
from dinnerbell.services import guests
from dinnerbell.web.auth import (
    Context,
    CSRFCheckedIfSignedIn,
    Database,
    MaybePrincipal,
    clear_cookie_header,
    cookie_attributes,
    session_cookie_header,
)
from dinnerbell.web.context import LOGIN_STATE_LIFETIME, AppContext
from dinnerbell.web.oidc import OIDCError
from dinnerbell.web.render import error_page, render

router = APIRouter(prefix="/auth")
log = logging.getLogger(__name__)

TRY_AGAIN = "Go back to the start page and sign in again."

REFUSAL_MESSAGES = {
    Refusal.UNKNOWN_EMAIL: (
        "This household hasn't invited you yet",
        "Ask the household admin to invite the email address you use to sign in, then try again.",
    ),
    Refusal.EMAIL_NOT_VERIFIED: (
        "Your email address isn't verified",
        "Your sign-in provider didn't confirm your email address. Verify it with the provider, then try again.",
    ),
    Refusal.INACTIVE: (
        "Your access is turned off",
        "Your account in this household is deactivated. Ask the household admin to reactivate it.",
    ),
    Refusal.WRONG_TEAM: (
        "Wrong workspace",
        "You signed in to a different Slack workspace. Sign in with the household's workspace instead.",
    ),
    Refusal.EMAIL_LINKED_ELSEWHERE: (
        "That email is already linked",
        "The member with this email signed in with a different account. Use that account, or ask the admin.",
    ),
}


@router.get("/login")
async def login(request: Request, context: Context, principal: MaybePrincipal) -> Response:
    if principal is not None:
        return RedirectResponse("/", status_code=303)
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    try:
        auth_request = await context.oidc.authorization_request(state=state, nonce=nonce)
    except OIDCError as exc:
        log.error("could not start sign-in", extra={"reason": exc.reason})
        return await _provider_problem(request)
    response = RedirectResponse(auth_request.url, status_code=302)
    cookie = context.login_state.dumps({"state": state, "nonce": nonce, "verifier": auth_request.code_verifier})
    max_age = int(LOGIN_STATE_LIFETIME.total_seconds())
    response.headers.append(
        "Set-Cookie", f"{context.cookies.login}={cookie}; Max-Age={max_age}; {cookie_attributes(context.settings)}"
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    context: Context,
    db: Database,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    login_state = context.login_state.loads(request.cookies.get(context.cookies.login))
    response = await _complete(request, context, db, login_state, code, state, error)
    response.headers.append("Set-Cookie", clear_cookie_header(context.cookies.login, context.settings))
    return response


async def _complete(
    request: Request,
    context: AppContext,
    db: Database,
    login_state: dict[str, str | None] | None,
    code: str | None,
    state: str | None,
    error: str | None,
) -> Response:
    if error is not None:
        log.info("provider returned an error", extra={"error": error[:64]})
        return await error_page(
            request, 400, "Sign-in didn't finish", f"The sign-in was cancelled or failed. {TRY_AGAIN}"
        )
    expected_state = login_state.get("state") if login_state else None
    nonce = login_state.get("nonce") if login_state else None
    if not expected_state or not nonce or not state or not hmac.compare_digest(expected_state, state):
        log.warning("sign-in state missing or mismatched")
        return await error_page(
            request, 400, "Sign-in expired", f"The sign-in took too long or started elsewhere. {TRY_AGAIN}"
        )
    if not code:
        return await error_page(request, 400, "Sign-in didn't finish", TRY_AGAIN)
    try:
        id_token = await context.oidc.exchange_code(code, login_state.get("verifier") if login_state else None)
        claims = await context.oidc.verify_id_token(id_token, nonce=nonce)
    except OIDCError as exc:
        log.warning("sign-in verification failed", extra={"reason": exc.reason})
        if exc.provider_unavailable:
            return await _provider_problem(request)
        return await error_page(request, 400, "Sign-in couldn't be verified", TRY_AGAIN)
    outcome = await auth_service.sign_in(
        db,
        claims,
        required_team_id=context.settings.slack_team_id,
        now=context.clock(),
        replaces_token=request.cookies.get(context.cookies.session),
    )
    if isinstance(outcome, Refuse) and await _join(context, db, claims, outcome):
        # #38, option B: someone in the Dinnerbell channel joined as a guest; sign them in.
        outcome = await auth_service.sign_in(
            db,
            claims,
            required_team_id=context.settings.slack_team_id,
            now=context.clock(),
            replaces_token=request.cookies.get(context.cookies.session),
        )
    if isinstance(outcome, Refuse):
        title, message = REFUSAL_MESSAGES[outcome.reason]
        return await render(request, "refused.html", {"title": title, "message": message}, status_code=403)
    response = RedirectResponse("/", status_code=303)
    response.headers.append("Set-Cookie", session_cookie_header(context.cookies, context.settings, outcome.token))
    return response


JOINABLE = frozenset({Refusal.UNKNOWN_EMAIL, Refusal.EMAIL_NOT_VERIFIED})
"""Refusals that joining from the channel can answer: Dinnerbell doesn't know this person yet."""


async def _join(context: AppContext, db: Database, claims: IdentityClaims, refusal: Refuse) -> bool:
    """Sign in with Slack works for anyone in the Dinnerbell channel (#38, option B): with Slack
    as the provider, its workspace checked, and joining on, Slack is asked whether this is a
    real person of the workspace in the channel, and if so they join as a guest. Rate-limited
    per Slack user, and audited either way."""
    slack_id = claims.slack_user_id
    people = context.slack_people
    if refusal.reason not in JOINABLE or people is None or not context.settings.slack_team_id or not slack_id:
        return False
    now = context.clock()
    if not context.join_limiter.allow(slack_id, now):
        await guests.record_refusal(db, slack_id, guests.NotJoinable.TOO_MANY, via="web", now=now)
        return False
    if not await guests.joining_open(db):
        return False
    person = await people.person(slack_id)
    if not isinstance(person, guests.SlackPerson):
        await guests.record_refusal(db, slack_id, person, via="web", now=now)
        return False
    try:
        await guests.join(db, person, via="web", now=now)
    except guests.JoinRefused:
        return False
    return True


async def _provider_problem(request: Request) -> Response:
    return await error_page(
        request, 502, "Can't reach the sign-in provider", "The sign-in provider didn't respond. Try again in a minute."
    )


@router.post("/logout")
async def logout(context: Context, db: Database, request: Request, principal: CSRFCheckedIfSignedIn) -> Response:
    token = request.cookies.get(context.cookies.session)
    if principal is not None and token:
        await auth_service.sign_out(db, token)
        log.info("member signed out")
    response = RedirectResponse("/", status_code=303)
    response.headers.append("Set-Cookie", clear_cookie_header(context.cookies.session, context.settings))
    return response
