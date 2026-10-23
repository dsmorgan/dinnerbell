# SPDX-License-Identifier: AGPL-3.0-or-later
"""GET /healthz for Uptime Kuma and the container health check. No secrets, emails, or member
data; the only counts are the outbox's and, on a labeled instance, dropped direct messages. A
labeled instance adds its label, never a channel ID."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from dinnerbell.services.health import database_reachable, outbox_backlog
from dinnerbell.web.auth import Context

router = APIRouter()


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


@router.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz(context: Context) -> JSONResponse:
    """503 when the database is unreachable or the scheduler has stopped ticking, so the
    container health check and Uptime Kuma both catch a dead scheduler.

    Slack problems alone report "degraded" with a 200. Restarting the container can't invite
    the bot or fix a token, the outbox keeps every notification until Slack is back, and the
    web app must stay up so the admin can sign in and fix things."""
    database_ok = await database_reachable(context.sessionmaker)
    backlog = await outbox_backlog(context.sessionmaker) if database_ok else None
    state = context.scheduler_state
    scheduler = state.scheduler_status(context.clock())
    slack = await context.slack_state.status()
    healthy = database_ok and scheduler != "stopped"
    if not healthy:
        status = "error"
    elif scheduler == "ok" and slack in {"connected", "not_configured"}:
        status = "ok"
    else:
        status = "degraded"
    body: dict[str, Any] = {
        "status": status,
        "database": "ok" if database_ok else "unreachable",
        "scheduler": {"status": scheduler, "last_tick": _iso(state.last_tick)},
        "slack": slack,
        "outbox": None if backlog is None else {"backlog": backlog[0], "failed": backlog[1]},
        "backup": {"last_success": _iso(state.last_backup)},
    }
    if context.settings.instance_label:
        body["instance"] = context.settings.instance_label
    dropped = context.slack_state.dms_dropped
    if dropped is not None:  # only while the notification guard is on, so production's body is unchanged
        body["slack_dms_dropped"] = dropped
    return JSONResponse(body, status_code=200 if healthy else 503)
