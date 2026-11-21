# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optional web app (FR-53, ADR 0010, spec 6.15): the manifest, the service worker, the
**Get the app** page, and Admin > **Web app**.

``/manifest.webmanifest``, ``/sw.js``, and ``/app`` answer 404 while Admin's switch is off, the
same 404 as a page that doesn't exist. A visitor with no session gets the install's household
(``services.web_app.web_app_enabled_for``): the browser fetches the manifest without cookies.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.services import web_app
from dinnerbell.web.auth import AdminCSRFChecked, AdminPrincipal, Context, Database, MaybePrincipal
from dinnerbell.web.render import render

router = APIRouter()
admin_router = APIRouter(prefix="/admin")

WORKER_FILE = Path(__file__).resolve().parents[1] / "worker" / "sw.js"
MANIFEST_TYPE = "application/manifest+json"
WORKER_TYPE = "text/javascript; charset=utf-8"
WORKER_CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'none'",
        "connect-src 'self'",
        "img-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)
"""The service worker's own policy (spec 6.15): it may reach this origin, for fetches and the
notification icon, and nothing outside it. No ``sandbox``, which would give the worker an
opaque origin.
``SecurityHeadersMiddleware`` keeps a route's own policy outside /static."""
THEME_COLOR = "#faf6f0"
"""The light page background, as base.html's ``theme-color``."""
ICON_SIZES = (192, 512)
NOTICES = {"on": "The web app is on.", "off": "The web app is off."}


def _household_id(principal: MaybePrincipal) -> int | None:
    return principal.household.id if principal is not None else None


@cache
def _worker() -> bytes:
    return WORKER_FILE.read_bytes()


def manifest(version: str, instance_label: str) -> dict[str, object]:
    """The web app manifest (spec 6.15): Dinnerbell, standalone, starting at ``/``, with the
    icons from /static. A labeled instance, such as test, gets the test mascot, as its pages do."""
    variant = "-test" if instance_label else ""
    icons = [
        {
            "src": f"/static/icon{variant}-{size}.png?v={version}",
            "sizes": f"{size}x{size}",
            "type": "image/png",
            "purpose": "any",
        }
        for size in ICON_SIZES
    ]
    return {
        "id": "/",
        "name": "Dinnerbell",
        "short_name": "Dinnerbell",
        "lang": "en",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "icons": icons,
    }


@router.get("/manifest.webmanifest")
async def manifest_file(principal: MaybePrincipal, context: Context, db: Database) -> Response:
    await web_app.require_web_app(db, _household_id(principal))
    settings = context.settings
    body = json.dumps(manifest(settings.version_label, settings.instance_label), separators=(",", ":"))
    return Response(body, media_type=MANIFEST_TYPE)


@router.get("/sw.js")
async def service_worker(principal: MaybePrincipal, db: Database) -> Response:
    """Served from the root, not /static, so its scope is the whole site, with its own policy."""
    await web_app.require_web_app(db, _household_id(principal))
    headers = {"Content-Security-Policy": WORKER_CONTENT_SECURITY_POLICY}
    return Response(_worker(), media_type=WORKER_TYPE, headers=headers)


@router.get("/app")
async def get_the_app(request: Request, principal: MaybePrincipal, db: Database) -> Response:
    """**Get the app**: the Add to Home Screen steps for iPhone and Android. Open signed out
    too, since a member without Slack installs the app before they can sign in."""
    await web_app.require_web_app(db, _household_id(principal))
    return await render(request, "get_app.html")


# Admin > Web app ---------------------------------------------------------------------------------


@admin_router.get("/web-app")
async def admin_page(request: Request, principal: AdminPrincipal, db: Database, notice: str = "") -> Response:
    on = await web_app.web_app_enabled(db, principal.member.household_id)
    return await render(request, "admin_web_app.html", {"on": on, "notice": NOTICES.get(notice)})


@admin_router.post("/web-app")
async def save_switch(
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    on: Annotated[str | None, Form(max_length=8)] = None,
) -> Response:
    """The switch is a checkbox: the browser sends ``on`` when it's checked and nothing when not."""
    turned_on = on is not None
    await web_app.set_web_app(db, principal.member, turned_on, now=context.clock())
    return RedirectResponse(f"/admin/web-app?notice={'on' if turned_on else 'off'}", status_code=303)
