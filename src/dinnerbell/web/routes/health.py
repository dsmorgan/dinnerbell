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


@router.get("/healthz")
async def healthz(context: Context) -> JSONResponse:
    database_ok = await database_reachable(context.sessionmaker)
    state = context.scheduler_state
    scheduler_ok = state.tick_is_fresh(context.clock())
    status = "ok" if database_ok and scheduler_ok else ("degraded" if database_ok else "error")
    body = {
        "status": status,
        "database": "ok" if database_ok else "unreachable",
        "scheduler": {"status": "ok" if scheduler_ok else "stale", "last_tick": _iso(state.last_tick)},
        "slack": "not_configured",
        "backup": {"last_success": _iso(state.last_backup)},
    }
    return JSONResponse(body, status_code=200 if database_ok else 503)
