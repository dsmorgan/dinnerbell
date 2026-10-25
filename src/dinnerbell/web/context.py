# SPDX-License-Identifier: AGPL-3.0-or-later
"""Objects the app builds once at startup and routes reach through ``request.app.state``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.config import Settings
from dinnerbell.services.clock import Clock
from dinnerbell.services.guests import JoinLimiter, SlackPeople
from dinnerbell.services.health import SchedulerState, SlackState
from dinnerbell.web.oidc import OIDCClient
from dinnerbell.web.signing import SignedPayload

LOGIN_STATE_LIFETIME = timedelta(minutes=10)
"""How long a sign-in may take between /auth/login and /auth/callback."""

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
    slack_state: SlackState = field(default_factory=SlackState)
    slack_people: SlackPeople | None = None
    """Who may join from the Slack channel (#38): set when Slack is connected."""
    join_limiter: JoinLimiter = field(default_factory=JoinLimiter)
    """Joins from Sign in with Slack, per Slack user (#38)."""


def app_context(request: Request) -> AppContext:
    context: AppContext = request.app.state.dinnerbell
    return context
