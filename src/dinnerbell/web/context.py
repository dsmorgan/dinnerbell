# SPDX-License-Identifier: AGPL-3.0-or-later
"""Objects the app builds once at startup and routes reach through ``request.app.state``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.config import Settings
from dinnerbell.scheduler.runner import SchedulerState
from dinnerbell.services.clock import Clock
from dinnerbell.web.oidc import OIDCClient
from dinnerbell.web.signing import SignedPayload

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


@dataclass(frozen=True)
class CookieNames:
    session: str
    login: str

    @classmethod
    def for_settings(cls, settings: Settings) -> CookieNames:
        # The __Host- prefix makes browsers insist on Secure, Path=/, and no Domain.
        prefix = "__Host-" if settings.cookie_secure else ""
        return cls(session=f"{prefix}dinnerbell_session", login=f"{prefix}dinnerbell_login")


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    clock: Clock
    cookies: CookieNames
    csrf_key: bytes
    login_state: SignedPayload
    sessionmaker: async_sessionmaker[AsyncSession]
    oidc: OIDCClient
    scheduler_state: SchedulerState


def app_context(request: Request) -> AppContext:
    context: AppContext = request.app.state.dinnerbell
    return context
