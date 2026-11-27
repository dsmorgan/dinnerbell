# SPDX-License-Identifier: AGPL-3.0-or-later
"""GET /healthz for Uptime Kuma and the container health check. No secrets, emails, or member
data; the only counts are the outbox's, the push queue's while the web app is on, stuck calls
(never which ones), and, on a labeled instance, dropped direct messages. A labeled instance
adds its label, never a channel ID.

Admin > Health (#339) shows the same report in words, with the failed notifications and the
audit log, which name people and so stay behind the admin check."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from dinnerbell.services.admin_health import health_details
from dinnerbell.services.health import BACKUP_STALE_AFTER, check_health
from dinnerbell.services.outbox import KEEP_FOR
from dinnerbell.services.push import PURGE_AFTER
from dinnerbell.web.auth import AdminPrincipal, Context, Database
from dinnerbell.web.render import render

router = APIRouter()
admin_router = APIRouter(prefix="/admin")


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


@router.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz(context: Context) -> JSONResponse:
    """503 when the database is unreachable or the scheduler has stopped ticking; "degraded"
    with a 200 when something needs attention (``services.health.check_health`` says what).
    Admin > Health shows the same report in words."""
    report = await check_health(context.sessionmaker, context.scheduler_state, context.slack_state, context.clock())
    outbox = report.outbox
    body: dict[str, Any] = {
        "status": report.status,
        "version": context.settings.version_label,
        "database": "ok" if report.database_ok else "unreachable",
        "scheduler": {"status": report.scheduler, "last_tick": _iso(report.last_tick)},
        "slack": report.slack,
        "outbox": None
        if outbox is None
        else {
            "backlog": outbox.waiting,
            "failed": outbox.failed,
            "oldest_pending_age": None if outbox.oldest is None else int(outbox.oldest.total_seconds()),
        },
        "backup": {"last_success": _iso(report.last_backup), "failing": report.backup_failing},
        "stuck_calls": report.stuck_calls,
    }
    pushes = report.push
    if pushes is not None:  # only while the web app is on, so a Slack-only install's body is unchanged
        age = None if pushes.oldest is None else int(pushes.oldest.total_seconds())
        body["push"] = {"backlog": pushes.waiting, "oldest_pending_age": age}
    if context.settings.instance_label:
        body["instance"] = context.settings.instance_label
    dropped = context.slack_state.dms_dropped
    if dropped is not None:  # only while the notification guard is on, so production's body is unchanged
        body["slack_dms_dropped"] = dropped
    return JSONResponse(body, status_code=200 if report.healthy else 503)


@admin_router.get("/health")
async def admin_health(request: Request, principal: AdminPrincipal, context: Context, db: Database) -> Response:
    """Admin > Health: read only, so no form and no audit row."""
    now = context.clock()
    report = await check_health(context.sessionmaker, context.scheduler_state, context.slack_state, now)
    details = await health_details(db, principal.member)
    page = {
        "report": report,
        "now": now,
        "version": context.settings.version_label,
        "backup_stale": report.backup_stale(now),
        "backup_stale_hours": int(BACKUP_STALE_AFTER.total_seconds() // 3600),
        "dms_dropped": context.slack_state.dms_dropped,
        "failed": details.failed,
        "slack_keep_days": KEEP_FOR.days,  # the nightly prune (#340)
        "push_keep_days": PURGE_AFTER.days,
        "audit": details.audit,
    }
    return await render(request, "admin_health.html", page)
