# SPDX-License-Identifier: AGPL-3.0-or-later
"""GET /healthz for Uptime Kuma and the container health check. No secrets, emails, or counts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from dinnerbell.services.health import database_reachable
from dinnerbell.web.auth import Context

router = APIRouter()


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


@router.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz(context: Context) -> JSONResponse:
    """503 when the database is unreachable or the scheduler has stopped ticking, so the
    container health check and Uptime Kuma both catch a dead scheduler."""
    database_ok = await database_reachable(context.sessionmaker)
    state = context.scheduler_state
    scheduler = state.scheduler_status(context.clock())
    healthy = database_ok and scheduler != "stopped"
    if not healthy:
        status = "error"
    elif scheduler == "ok":
        status = "ok"
    else:
        status = "degraded"
    body = {
        "status": status,
        "database": "ok" if database_ok else "unreachable",
        "scheduler": {"status": scheduler, "last_tick": _iso(state.last_tick)},
        "slack": "not_configured",
        "backup": {"last_success": _iso(state.last_backup)},
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
