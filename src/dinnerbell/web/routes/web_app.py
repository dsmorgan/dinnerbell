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
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.responses import Response

from dinnerbell.domain.push import InvalidSubscription
from dinnerbell.services import push, web_app
from dinnerbell.web.auth import AdminCSRFChecked, AdminPrincipal, Context, CSRFChecked, Database, MaybePrincipal
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


WORKER_ICON = 'var ICON = "/static/icon-192.png";'


@cache
def worker(instance_label: str) -> str:
    """The service worker. A labeled instance's notifications show its test mascot, as its
    pages and manifest do."""
    script = WORKER_FILE.read_text()
    if instance_label:
        script = script.replace(WORKER_ICON, WORKER_ICON.replace("icon-", "icon-test-"), 1)
    return script


def app_name(instance_label: str) -> str:
    """What the installed app is called: "Dinnerbell", or "Dinnerbell Test" on an instance
    labeled Test, so a phone with both tells them apart."""
    return f"Dinnerbell {instance_label}" if instance_label else "Dinnerbell"


def manifest(version: str, instance_label: str) -> dict[str, object]:
    """The web app manifest (spec 6.15): Dinnerbell, standalone, starting at ``/``, with the
    icons from /static. A labeled instance, such as test, gets its label in the name and the test
    mascot, as its pages do."""
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
        "name": app_name(instance_label),
        "short_name": app_name(instance_label),
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
async def service_worker(principal: MaybePrincipal, context: Context, db: Database) -> Response:
    """Served from the root, not /static, so its scope is the whole site, with its own policy."""
    await web_app.require_web_app(db, _household_id(principal))
    headers = {"Content-Security-Policy": WORKER_CONTENT_SECURITY_POLICY}
    return Response(worker(context.settings.instance_label), media_type=WORKER_TYPE, headers=headers)


@router.get("/app")
async def get_the_app(request: Request, principal: MaybePrincipal, db: Database) -> Response:
    """**Get the app**: the Add to Home Screen steps for iPhone and Android. Open signed out
    too, since a member without Slack installs the app before they can sign in."""
    await web_app.require_web_app(db, _household_id(principal))
    return await render(request, "get_app.html")


# Turn on notifications (spec 6.15) ---------------------------------------------------------------

# No max_length: FastAPI would answer a long field with the HTML error page, and push.js shows
# the 400's sentence instead. The service checks every length; the body limit caps the request.
Field = Annotated[str, Form()]


@router.post("/app/notifications")
async def turn_on(
    principal: CSRFChecked, context: Context, db: Database, endpoint: Field = "", p256dh: Field = "", auth: Field = ""
) -> Response:
    """Save this device's push subscription, from push.js. 204, or 400 with what to do."""
    try:
        await push.subscribe(db, principal.member, principal.session_hash, endpoint, p256dh, auth, now=context.clock())
    except InvalidSubscription as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return Response(status_code=204)


@router.post("/app/notifications/off")
async def turn_off(principal: CSRFChecked, db: Database, endpoint: Field = "") -> Response:
    """Forget this device's push subscription: 204 whether or not Dinnerbell had it."""
    await push.unsubscribe(db, principal.member, endpoint)
    return Response(status_code=204)


# Admin > Web app ---------------------------------------------------------------------------------


@admin_router.get("/web-app")
async def admin_page(
    request: Request, principal: AdminPrincipal, context: Context, db: Database, notice: str = ""
) -> Response:
    on = await web_app.web_app_enabled(db, principal.member.household_id)
    # Over 200 wrong sign-in codes in the last hour, from every address (spec 6.15).
    guessing = context.wrong_codes.alarming(context.clock())
    pushes = await push.status(db, principal.member.household_id, context.clock())
    page = {"on": on, "notice": NOTICES.get(notice), "wrong_codes": guessing, "push": pushes}
    return await render(request, "admin_web_app.html", page)


@admin_router.post("/web-app")
async def save_switch(
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    on: Annotated[str, Form(max_length=8)] = "",
) -> Response:
    """The switch is a checkbox: the browser sends ``on=on`` when it's checked and nothing when
    not. Only that value turns it on; anything else is off."""
    turned_on = on == "on"
    await web_app.set_web_app(db, principal.member, turned_on, now=context.clock())
    if turned_on:  # Apple refuses pushes without an https VAPID subject: say so now, not only at startup
        await push.check_base_url(db, principal.member.household_id, context.settings.base_url)
    return RedirectResponse(f"/admin/web-app?notice={'on' if turned_on else 'off'}", status_code=303)
