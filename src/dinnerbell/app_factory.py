# SPDX-License-Identifier: AGPL-3.0-or-later
"""The composition root: wires the database, services, scheduler, and web app together.

It is the only module that imports both ``web`` and ``scheduler``; the layers never import
each other (spec 13.2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.channels.base import Channel, NullChannel
from dinnerbell.channels.slack.connection import BoltSocket, SlackConnection, SocketFactory
from dinnerbell.channels.webpush.sender import WebPushSender, new_client
from dinnerbell.channels.webpush.vapid import VapidKeys
from dinnerbell.config import Settings
from dinnerbell.scheduler.runner import CANCEL_UNWIND_SECONDS, JobContext, build_scheduler, stop_scheduler
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services import contributions
from dinnerbell.services import push as push_service
from dinnerbell.services.assistant.anthropic_client import AnthropicModelClient
from dinnerbell.services.assistant.client import ModelClient
from dinnerbell.services.assistant.conversation import Assistant
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.clock import Clock, utc_now
from dinnerbell.services.database import open_database
from dinnerbell.services.health import SchedulerState, SlackState, schema_revision
from dinnerbell.services.open_meteo import OpenMeteo
from dinnerbell.web.app import build_app
from dinnerbell.web.assistant_stream import Answers
from dinnerbell.web.context import LOGIN_STATE_LIFETIME, AppContext, CookieNames
from dinnerbell.web.oidc import OIDCClient
from dinnerbell.web.routes.catalog import draft_import_url
from dinnerbell.web.signing import SignedPayload, derive_key

log = logging.getLogger(__name__)
OIDC_TIMEOUT_SECONDS = 10.0
SHUTDOWN_SECONDS = 8.0
"""How long shutdown waits for running work in all, under podman's 10-second stop timeout."""


def create_app(
    settings: Settings,
    *,
    clock: Clock = utc_now,
    oidc_transport: httpx.AsyncBaseTransport | None = None,
    slack_client: AsyncWebClient | None = None,
    slack_socket: SocketFactory = BoltSocket,
    prewarm_badge_art: bool = False,
    model_client: ModelClient | None = None,
    push_transport: httpx.AsyncBaseTransport | None = None,
    weather_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app. ``oidc_transport`` lets tests route provider calls to a fake in-process;
    ``slack_client`` and ``slack_socket`` do the same for Slack, ``model_client`` for the
    assistant's model, in place of Anthropic, ``push_transport`` for the web push services, and
    ``weather_transport`` for Open-Meteo (ADR 0011).
    ``prewarm_badge_art`` (the CLI sets it) uploads the badge art to Slack in the background
    once connected (#112)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Each resource joins the stack as it opens, so a failure anywhere, at startup or at
        # shutdown, closes everything opened so far, newest first, and one close failing
        # doesn't stop the rest.
        async with AsyncExitStack() as stack:
            database = open_database(settings.async_database_url)
            stack.push_async_callback(database.close)
            async with database.sessionmaker() as db:
                household = await bootstrap(
                    db,
                    household_name=settings.household_name,
                    timezone=settings.timezone,
                    admin_email=settings.admin_email,
                    now=clock(),
                )
                await catalog_service.seed_from_file(db, household, settings.seed_file, now=clock())
                await contributions.ensure_backfilled(db, household.id, now=clock())
                await push_service.check_base_url(db, household.id, settings.base_url)
            http = oidc_http_client(oidc_transport)
            stack.push_async_callback(http.aclose)
            secret = settings.secret_key.get_secret_value()
            state = SchedulerState(started_at=clock())
            slack_state = SlackState()
            anthropic = _anthropic(settings) if model_client is None else None
            if anthropic is not None:
                stack.push_async_callback(anthropic.close)
            assistant = _assistant(settings, model_client or anthropic, clock)
            app.state.assistant = assistant
            slack = _slack(settings, database.sessionmaker, clock, slack_state, slack_client, slack_socket, assistant)
            channel: Channel = slack.channel if slack else NullChannel()
            vapid = VapidKeys.from_secret(secret)
            push_http = new_client(push_transport)
            weather = OpenMeteo(weather_transport)
            stack.push_async_callback(push_http.aclose)
            deadline: list[float] = []  # set when shutdown begins: one for the scheduler, Slack, and the web
            answers = Answers()
            app.state.web_answers = answers

            async def settle_answers() -> None:
                # After the scheduler and Slack, before the model client and the database close:
                # cancel the web's answers still running (#370), within the time that's left.
                left = deadline[0] - asyncio.get_running_loop().time() if deadline else CANCEL_UNWIND_SECONDS
                await answers.settle(max(left, CANCEL_UNWIND_SECONDS))

            stack.push_async_callback(settle_answers)
            app.state.dinnerbell = AppContext(
                settings=settings,
                clock=clock,
                cookies=CookieNames.for_settings(settings),
                csrf_key=derive_key(secret, "csrf"),
                code_key=derive_key(secret, "sign-in-code"),
                login_state=SignedPayload(derive_key(secret, "login-state"), LOGIN_STATE_LIFETIME, clock),
                sessionmaker=database.sessionmaker,
                oidc=_oidc_client(settings, http, clock),
                scheduler_state=state,
                slack_state=slack_state,
                slack_people=slack.people if slack else None,
                vapid_public_key=vapid.public_key,
                weather=weather,
            )
            jobs = JobContext(
                state=state,
                clock=clock,
                zone=ZoneInfo(household.timezone),  # the household row, not the env var, is authoritative
                database_path=settings.database_path,
                backup_dir=settings.backup_dir,
                sessionmaker=database.sessionmaker,
                channel=channel,
                push_sender=WebPushSender(vapid, settings.base_url, push_http, clock),
                base_url=settings.base_url,
                weather=weather,
                assistant=assistant,
            )
            app.state.jobs = jobs
            app.state.slack = slack
            if slack is not None:
                connection = slack

                async def stop_slack() -> None:
                    await connection.stop(deadline[0] if deadline else None)

                stack.push_async_callback(stop_slack)  # before start: a half-started Slack stops too
                await slack.start(prewarm_household=household.id if prewarm_badge_art else None)
            scheduler = build_scheduler(jobs)
            scheduler.start()

            async def stop_jobs() -> None:
                # One deadline for all, inside podman's 10-second stop timeout: the scheduler's
                # jobs first (a drain may be mid-delivery), then Slack, then the web's answers, with
                # whatever time is left.
                deadline.append(asyncio.get_running_loop().time() + SHUTDOWN_SECONDS)
                await stop_scheduler(scheduler, jobs, grace=SHUTDOWN_SECONDS - CANCEL_UNWIND_SECONDS)

            stack.push_async_callback(stop_jobs)
            log.info("dinnerbell started")
            yield

    return build_app(settings, lifespan)


def _slack(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    clock: Clock,
    state: SlackState,
    client: AsyncWebClient | None,
    socket: SocketFactory,
    assistant: Assistant | None = None,
) -> SlackConnection | None:
    """The Slack connection when the bot token, app token, and channel are all set."""
    bot, app_token, channel = settings.slack_bot_token, settings.slack_app_token, settings.slack_channel
    if not (bot and app_token and channel):
        if bot or app_token or channel:
            log.warning(
                "Slack is not connected: set DINNERBELL_SLACK_BOT_TOKEN, DINNERBELL_SLACK_APP_TOKEN, "
                "and DINNERBELL_SLACK_CHANNEL together"
            )
        return None
    return SlackConnection(
        bot_token=bot.get_secret_value(),
        app_token=app_token.get_secret_value(),
        channel_id=channel,
        team_id=settings.slack_team_id,
        base_url=settings.base_url,
        sessionmaker=sessionmaker,
        clock=clock,
        state=state,
        web_client=client,
        socket_factory=socket,
        command=settings.slack_command,
        instance_label=settings.instance_label,
        version=settings.version_label,
        schema_revision=schema_revision(),
        assistant=assistant,
        import_link=lambda draft_id, member_id: draft_import_url(settings, clock, draft_id, member_id),
        # Only Sign in with Slack needs the admin's email in Slack: with another provider, their
        # sign-in email may well differ from their Slack one (#360).
        admin_email=settings.admin_email if settings.slack_sign_in else None,
    )


def _anthropic(settings: Settings) -> AnthropicModelClient | None:
    """Anthropic's client, only when the operator set ``DINNERBELL_ANTHROPIC_API_KEY`` (ADR 0009)."""
    key = settings.anthropic_api_key
    return AnthropicModelClient(key) if key is not None else None


def _assistant(settings: Settings, client: ModelClient | None, clock: Clock) -> Assistant | None:
    """What ``converse`` needs, when there's a key and so a client. Without one, nothing about
    the assistant is built, and Slack never listens for DMs or mentions."""
    if client is None or settings.anthropic_api_key is None:
        return None
    return Assistant(AssistantConfig.from_settings(settings), client, clock)


def oidc_http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The sign-in provider's client: no redirects, and a 10-second timeout. ``trust_env=False``
    ignores proxy and CA bundle variables, such as ``HTTPS_PROXY`` and ``SSL_CERT_FILE``, and
    verifies TLS against the bundled certificate authorities, as pushes, menu links, the
    weather, and the assistant do (#328)."""
    return httpx.AsyncClient(transport=transport, timeout=OIDC_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False)


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
