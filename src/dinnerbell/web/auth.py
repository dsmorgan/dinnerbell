# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request authentication: the session cookie, the signed-in principal, and CSRF checks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import MutableHeaders
from starlette.types import Scope

from dinnerbell.config import Settings
from dinnerbell.domain.household import Household
from dinnerbell.domain.members import Member
from dinnerbell.domain.sessions import SESSION_LIFETIME
from dinnerbell.logs import member_id_var
from dinnerbell.services import auth as auth_service
from dinnerbell.web.context import AppContext, CookieNames, app_context
from dinnerbell.web.signing import csrf_token, tokens_match

CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
SET_SESSION = "set_session_cookie"
CLEAR_SESSION = "clear_session_cookie"


class NotSignedIn(Exception):
    pass


class AccessDenied(Exception):
    def __init__(self, message: str = "You don't have access to that page.") -> None:
        super().__init__(message)


class CSRFRejected(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    member: Member
    household: Household
    session_hash: str
    csrf_token: str


Context = Annotated[AppContext, Depends(app_context)]


async def get_db(context: Context) -> AsyncIterator[AsyncSession]:
    async with context.sessionmaker() as db:
        yield db


Database = Annotated[AsyncSession, Depends(get_db)]


async def optional_principal(request: Request, context: Context, db: Database) -> Principal | None:
    if hasattr(request.state, "principal"):
        cached: Principal | None = request.state.principal
        return cached
    token = request.cookies.get(context.cookies.session)
    principal = None
    if token:
        active = await auth_service.resolve_session(db, token, now=context.clock())
        if active is None:
            setattr(request.state, CLEAR_SESSION, True)
        else:
            principal = Principal(
                member=active.member,
                household=active.household,
                session_hash=active.token_hash,
                csrf_token=csrf_token(context.csrf_key, active.token_hash),
            )
            request.state.member_id = active.member.id
            member_id_var.set(active.member.id)
            if active.renewed:
                setattr(request.state, SET_SESSION, token)
    request.state.principal = principal
    return principal


MaybePrincipal = Annotated[Principal | None, Depends(optional_principal)]


GUESTS_KEEP_OUT = "That page is for household members. Your invitations are on Today."


async def require_anyone(principal: MaybePrincipal) -> Principal:
    """A signed-in member, admin, or guest. Only routes a guest may use depend on this directly:
    the guest's restricted view (#38). Each one still checks, in its service, that the event is
    one the guest is invited to."""
    if principal is None:
        raise NotSignedIn
    return principal


async def require_principal(principal: MaybePrincipal) -> Principal:
    """A signed-in member or admin. A guest is refused here, so every route is closed to guests
    unless it opts in with ``GuestOrMember`` (#38)."""
    if principal is None:
        raise NotSignedIn
    if principal.member.is_guest:
        raise AccessDenied(GUESTS_KEEP_OUT)
    return principal


SignedIn = Annotated[Principal, Depends(require_principal)]
GuestOrMember = Annotated[Principal, Depends(require_anyone)]


async def require_admin(principal: SignedIn) -> Principal:
    if not principal.member.is_admin:
        raise AccessDenied("Only household admins can open that page.")
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]


async def _supplied_csrf(request: Request) -> str | None:
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        value = (await request.form()).get(CSRF_FIELD)
        return value if isinstance(value, str) else None
    return None


async def require_csrf(request: Request, principal: SignedIn) -> Principal:
    """Every state-changing route depends on this (or ``csrf_if_signed_in``); a test enforces it."""
    if not tokens_match(principal.csrf_token, await _supplied_csrf(request)):
        raise CSRFRejected
    return principal


async def require_admin_csrf(request: Request, principal: AdminPrincipal) -> Principal:
    """For state-changing admin routes: an admin, and a valid CSRF token."""
    if not tokens_match(principal.csrf_token, await _supplied_csrf(request)):
        raise CSRFRejected
    return principal


async def require_csrf_anyone(request: Request, principal: GuestOrMember) -> Principal:
    """``require_csrf`` for the routes a guest may use too (#38)."""
    if not tokens_match(principal.csrf_token, await _supplied_csrf(request)):
        raise CSRFRejected
    return principal


async def csrf_if_signed_in(request: Request, principal: MaybePrincipal) -> Principal | None:
    if principal is not None and not tokens_match(principal.csrf_token, await _supplied_csrf(request)):
        raise CSRFRejected
    return principal


CSRFChecked = Annotated[Principal, Depends(require_csrf)]
GuestCSRFChecked = Annotated[Principal, Depends(require_csrf_anyone)]
AdminCSRFChecked = Annotated[Principal, Depends(require_admin_csrf)]
CSRFCheckedIfSignedIn = Annotated[Principal | None, Depends(csrf_if_signed_in)]


def cookie_attributes(settings: Settings) -> str:
    secure = "; Secure" if settings.cookie_secure else ""
    return f"Path=/; HttpOnly; SameSite=Lax{secure}"


def session_cookie_header(names: CookieNames, settings: Settings, token: str) -> str:
    max_age = int(SESSION_LIFETIME.total_seconds())
    return f"{names.session}={token}; Max-Age={max_age}; {cookie_attributes(settings)}"


def clear_cookie_header(name: str, settings: Settings) -> str:
    return f"{name}=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; {cookie_attributes(settings)}"


def session_cookie_writer(names: CookieNames, settings: Settings) -> Callable[[MutableHeaders, Scope], None]:
    def write(headers: MutableHeaders, scope: Scope) -> None:
        state = scope.get("state", {})
        if any(name.lower() == "set-cookie" and names.session in value for name, value in headers.items()):
            return  # the route already set or cleared the session cookie
        if token := state.get(SET_SESSION):
            headers.append("Set-Cookie", session_cookie_header(names, settings, token))
        elif state.get(CLEAR_SESSION):
            headers.append("Set-Cookie", clear_cookie_header(names.session, settings))

    return write
