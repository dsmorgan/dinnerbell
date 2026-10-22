# SPDX-License-Identifier: AGPL-3.0-or-later
"""The composition root: wires the database, services, scheduler, and web app together.

It is the only module that imports both ``web`` and ``scheduler``; the layers never import
each other (spec 13.2).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

from dinnerbell.config import Settings
from dinnerbell.scheduler.runner import JobContext, build_scheduler, stop_scheduler
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.clock import Clock, utc_now
from dinnerbell.services.database import open_database
from dinnerbell.services.health import SchedulerState
from dinnerbell.web.app import build_app
from dinnerbell.web.context import LOGIN_STATE_LIFETIME, AppContext, CookieNames
from dinnerbell.web.oidc import OIDCClient
from dinnerbell.web.signing import SignedPayload, derive_key

log = logging.getLogger(__name__)
OIDC_TIMEOUT_SECONDS = 10.0


def create_app(
    settings: Settings,
    *,
    clock: Clock = utc_now,
    oidc_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app. ``oidc_transport`` lets tests route provider calls to a fake in-process."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = open_database(settings.async_database_url)
        async with database.sessionmaker() as db:
            household = await bootstrap(
                db,
                household_name=settings.household_name,
                timezone=settings.timezone,
                admin_email=settings.admin_email,
                now=clock(),
            )
            await catalog_service.seed_from_file(db, household, settings.seed_file, now=clock())
        http = httpx.AsyncClient(transport=oidc_transport, timeout=OIDC_TIMEOUT_SECONDS, follow_redirects=False)
        secret = settings.secret_key.get_secret_value()
        state = SchedulerState(started_at=clock())
        app.state.dinnerbell = AppContext(
            settings=settings,
            clock=clock,
            cookies=CookieNames.for_settings(settings),
            csrf_key=derive_key(secret, "csrf"),
            login_state=SignedPayload(derive_key(secret, "login-state"), LOGIN_STATE_LIFETIME, clock),
            sessionmaker=database.sessionmaker,
            oidc=_oidc_client(settings, http, clock),
            scheduler_state=state,
        )
        jobs = JobContext(
            state=state,
            clock=clock,
            zone=ZoneInfo(household.timezone),  # the household row, not the env var, is authoritative
            database_path=settings.database_path,
            backup_dir=settings.backup_dir,
            sessionmaker=database.sessionmaker,
        )
        app.state.jobs = jobs
        scheduler = build_scheduler(jobs)
        scheduler.start()
        log.info("dinnerbell started")
        try:
            yield
        finally:
            await stop_scheduler(scheduler, jobs)
            await http.aclose()
            await database.close()

    return build_app(settings, lifespan)


def _oidc_client(settings: Settings, http: httpx.AsyncClient, clock: Clock) -> OIDCClient:
    # Slack's `team` parameter skips the workspace picker. Other providers ignore unknown parameters.
    extra = {"team": settings.slack_team_id} if settings.slack_team_id else {}
    return OIDCClient(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret.get_secret_value(),
        scopes=settings.oidc_scopes,
        redirect_uri=f"{settings.base_url}/auth/callback",
        http=http,
        clock=clock,
        extra_auth_params=extra,
    )
