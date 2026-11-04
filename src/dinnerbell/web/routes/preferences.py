# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preferences: a member's vetoes, scores, capabilities, reminders, dietary needs, and fun mode.

``/preferences`` accepts no member ID. Every read and write there is for the signed-in member,
admins included (FR-29).

``/admin/members/{member_id}/preferences`` is the one way to reach someone else's (#181): an
admin opens a member's page from Admin > Members and changes it, with the same template and
forms. Every route there checks for an admin here too, and the services look the member up in
the admin's own household and audit each change. The member isn't messaged, and nothing about
their sign-in or session changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from dinnerbell.domain import labels
from dinnerbell.domain.catalog import ItemKind, tags_by_kind
from dinnerbell.domain.events import EventRuleViolation
from dinnerbell.domain.members import Capabilities, InvalidMemberInput, Member
from dinnerbell.domain.preferences import LEVEL_LABELS, LEVELS, NEUTRAL, InvalidPreference, Stance, TargetType
from dinnerbell.services import activity, away, guests
from dinnerbell.services import preferences as preference_service
from dinnerbell.services.errors import NotFound
from dinnerbell.services.fun import set_member_fun_mode
from dinnerbell.services.variety import household_today
from dinnerbell.web.auth import AdminCSRFChecked, AdminPrincipal, Context, CSRFChecked, Database, Principal, SignedIn
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import BadFormValue, RowId, checked, choice, iso_date, parse_id
from dinnerbell.web.redirects import local_path, local_redirect
from dinnerbell.web.render import is_htmx, render

router = APIRouter(prefix="/preferences")
admin_router = APIRouter(prefix="/admin/members/{member_id}/preferences")

Checkbox = Annotated[str | None, Form()]
Needs = Annotated[list[str] | None, Form()]
Day = Annotated[str, Form(max_length=10)]
NOTICES = {
    "settings": "Settings saved.",
    "saved": "Preference saved.",
    "away": "Away days saved.",
    "back": labels.AWAY_CLEARED,
}
FUN_CHOICES = {"on": True, "off": False}


def _levels() -> list[tuple[str, str]]:
    return [(level, LEVEL_LABELS[level]) for level in LEVELS]


@dataclass(frozen=True, slots=True)
class Whose:
    """Whose preferences a request reads and writes: the signed-in member's own, or, for an
    admin on Admin > Members, another member's (#181)."""

    principal: Principal
    member: Member
    """The member whose preferences these are."""
    admin: bool
    """True on the admin's routes, where every read and write goes through the ``member_``
    services, which check for an admin and audit each change."""

    @property
    def base(self) -> str:
        """Where the page's forms post, and where they come back to."""
        if self.admin:
            return local_path("/admin/members/{}/preferences", self.member.id)
        return "/preferences"

    async def preferences(self, db: AsyncSession, today: date) -> preference_service.MyPreferences:
        actor = self.principal.member
        if self.admin:
            return await preference_service.member_preferences(db, actor, self.member.id, today=today)
        return await preference_service.my_preferences(db, actor, today=today)

    async def stance(self, db: AsyncSession, target_type: TargetType, target_id: int) -> tuple[str, Stance] | None:
        actor = self.principal.member
        if self.admin:
            return await preference_service.member_stance(db, actor, self.member.id, target_type, target_id)
        return await preference_service.my_stance(db, actor, target_type, target_id)

    async def set_stance(
        self, db: AsyncSession, context: AppContext, target_type: TargetType, target_id: int, stance: Stance
    ) -> str:
        actor, now = self.principal.member, context.clock()
        if self.admin:
            return await preference_service.set_member_preference(
                db, actor, self.member.id, target_type, target_id, stance, now=now
            )
        return await preference_service.set_my_preference(db, actor, target_type, target_id, stance, now=now)

    async def set_away(self, db: AsyncSession, context: AppContext, first: date, last: date) -> None:
        actor, now = self.principal.member, context.clock()
        if self.admin:
            await away.set_member_away(db, actor, self.member.id, first, last, now=now)
        else:
            await away.set_my_away(db, actor, first, last, now=now)

    async def clear_away(self, db: AsyncSession, context: AppContext) -> None:
        actor, now = self.principal.member, context.clock()
        if self.admin:
            await away.clear_member_away(db, actor, self.member.id, now=now)
        else:
            await away.clear_my_away(db, actor, now=now)

    async def save_settings(
        self, db: AsyncSession, context: AppContext, settings: preference_service.MySettings
    ) -> None:
        actor, now = self.principal.member, context.clock()
        if self.admin:
            await preference_service.update_member_settings(db, actor, self.member.id, settings, now=now)
        else:
            await preference_service.update_my_settings(db, actor, settings, now=now)


def _own(principal: Principal) -> Whose:
    return Whose(principal, principal.member, admin=False)


async def _theirs(principal: Principal, db: AsyncSession, member_id: int) -> Whose:
    member = await preference_service.member_for_admin(db, principal.member, member_id)
    return Whose(principal, member, admin=True)


async def _page(
    request: Request,
    whose: Whose,
    context: AppContext,
    db: AsyncSession,
    *,
    notice: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    principal = whose.principal
    today = household_today(principal.household, context.clock())
    mine = await whose.preferences(db, today)
    base = whose.base
    window = whose.member.away_now(today)
    page = {
        "mine": mine,
        "tag_groups": tags_by_kind(mine.tags),
        "restaurants": [i for i in mine.items if i.kind is ItemKind.RESTAURANT],
        "home_meals": [i for i in mine.items if i.kind is ItemKind.HOME],
        "levels": _levels(),
        "TargetType": TargetType,
        "me": whose.member,
        "editing": whose.member if whose.admin else None,
        "prefs_base": base,
        "needs_action": f"{base}/needs" if whose.admin else "/me",
        "fun_action": f"{base}/fun" if whose.admin else "/me/fun",
        "fun_member": whose.member,
        "notice": NOTICES.get(notice),
        "fun_saved": notice == "fun",
        "error": error,
        "dietary_tags": await guests.dietary_tags(db, principal.member),
        "today": today,
        "away": window,
        "away_line": labels.away_status(window, today) if window else None,
        "away_end": labels.away_end(window, today) if window else None,
    }
    return await render(request, "preferences.html", page, status_code=status_code)


async def _failed_row(
    request: Request, db: AsyncSession, whose: Whose, target_type: TargetType, target_id: int, error: str
) -> Response:
    current = await whose.stance(db, target_type, target_id)
    name, stance = current if current is not None else ("Removed", NEUTRAL)
    row = {
        "target": f"{target_type.value}:{target_id}",
        "name": name,
        "stance": stance,
        "levels": _levels(),
        "error": error,
        "prefs_base": whose.base,
    }
    return await render(request, "_preference_row.html", row)


def _settings(
    can_cook: str | None,
    can_pickup: str | None,
    can_order: str | None,
    mute_dm_reminders: str | None,
    feedback_reminder: str | None,
) -> preference_service.MySettings:
    return preference_service.MySettings(
        capabilities=Capabilities(checked(can_cook), checked(can_pickup), checked(can_order)),
        mute_dm_reminders=checked(mute_dm_reminders),
        feedback_reminder=checked(feedback_reminder),
    )


async def _save_stance(
    request: Request, whose: Whose, context: AppContext, db: AsyncSession, target: str, level: str
) -> Response:
    """Set one stance. ``target`` reads ``tag:12`` or ``item:7``.

    HTMX gets the row back. When saving fails, it still gets the row, with status 200 so HTMX
    swaps it in, showing the stance that is actually stored and why the new one wasn't saved.
    A failure HTMX can't swap, such as an expired session, shows the page's save-error banner.
    """
    kind, _, raw_id = target.partition(":")
    target_type = choice(TargetType, kind)
    target_id = parse_id(raw_id)
    if target_type is None or target_id is None:
        message = "Reload the page and try again."
        if is_htmx(request):
            return Response(message, status_code=400)
        return await render(request, "error.html", {"title": "Not saved", "message": message}, status_code=400)
    try:
        stance = Stance.parse(level)
        name = await whose.set_stance(db, context, target_type, target_id, stance)
    except InvalidPreference as exc:
        if not is_htmx(request):
            return await render(request, "error.html", {"title": "Not saved", "message": str(exc)}, status_code=400)
        return await _failed_row(request, db, whose, target_type, target_id, f"Not saved. {exc}")
    except NotFound:
        if not is_htmx(request):
            raise
        return await _failed_row(
            request, db, whose, target_type, target_id, "Not saved. It was removed from the catalog; reload the page."
        )
    if is_htmx(request):
        row = {
            "target": target,
            "name": name,
            "stance": stance,
            "levels": _levels(),
            "saved": True,
            "prefs_base": whose.base,
        }
        return await render(request, "_preference_row.html", row)
    if whose.admin:
        return local_redirect(
            "/admin/members/{}/preferences?notice=saved#pref-{}-{}", whose.member.id, target_type, target_id
        )
    return local_redirect("/preferences?notice=saved#pref-{}-{}", target_type, target_id)


async def _save_away(
    request: Request, whose: Whose, context: AppContext, db: AsyncSession, first: str, last: str
) -> Response:
    """Set the window (FR-63), or show the page again with why not."""
    try:
        start = iso_date(first, "Pick the first day away.")
        end = iso_date(last, "Pick the last day away.")
        await whose.set_away(db, context, start, end)
    except (BadFormValue, EventRuleViolation) as exc:
        activity.refused(exc, str(exc), whose.principal.member, path=request.url.path)
        return await _page(request, whose, context, db, error=str(exc), status_code=400)
    if whose.admin:
        return local_redirect("/admin/members/{}/preferences?notice=away#away", whose.member.id)
    return local_redirect("/preferences?notice=away#away")


# The signed-in member's own -------------------------------------------------------------------


@router.get("")
async def preferences_page(
    request: Request, principal: SignedIn, context: Context, db: Database, notice: str = ""
) -> Response:
    return await _page(request, _own(principal), context, db, notice=notice)


@router.post("/settings")
async def save_settings(
    principal: CSRFChecked,
    context: Context,
    db: Database,
    can_cook: Checkbox = None,
    can_pickup: Checkbox = None,
    can_order: Checkbox = None,
    mute_dm_reminders: Checkbox = None,
    feedback_reminder: Checkbox = None,
) -> Response:
    settings = _settings(can_cook, can_pickup, can_order, mute_dm_reminders, feedback_reminder)
    await _own(principal).save_settings(db, context, settings)
    return RedirectResponse("/preferences?notice=settings", status_code=303)


@router.post("/away")
async def save_away(
    request: Request, principal: CSRFChecked, context: Context, db: Database, first: Day = "", last: Day = ""
) -> Response:
    return await _save_away(request, _own(principal), context, db, first, last)


@router.post("/away/clear")
async def clear_away(principal: CSRFChecked, context: Context, db: Database) -> Response:
    await _own(principal).clear_away(db, context)
    return RedirectResponse("/preferences?notice=back#away", status_code=303)


@router.post("/stance")
async def save_stance(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    target: Annotated[str, Form(max_length=40)] = "",
    level: Annotated[str, Form(max_length=8)] = "",
) -> Response:
    return await _save_stance(request, _own(principal), context, db, target, level)


# An admin, for another member (#181) ----------------------------------------------------------


@admin_router.get("")
async def member_preferences_page(
    request: Request, principal: AdminPrincipal, context: Context, db: Database, member_id: RowId, notice: str = ""
) -> Response:
    if member_id == principal.member.id:
        return RedirectResponse("/preferences", status_code=303)
    whose = await _theirs(principal, db, member_id)
    return await _page(request, whose, context, db, notice=notice)


@admin_router.post("/settings")
async def save_member_settings(
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    member_id: RowId,
    can_cook: Checkbox = None,
    can_pickup: Checkbox = None,
    can_order: Checkbox = None,
    mute_dm_reminders: Checkbox = None,
    feedback_reminder: Checkbox = None,
) -> Response:
    settings = _settings(can_cook, can_pickup, can_order, mute_dm_reminders, feedback_reminder)
    whose = await _theirs(principal, db, member_id)
    await whose.save_settings(db, context, settings)
    return local_redirect("/admin/members/{}/preferences?notice=settings", whose.member.id)


@admin_router.post("/away")
async def save_member_away(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    member_id: RowId,
    first: Day = "",
    last: Day = "",
) -> Response:
    whose = await _theirs(principal, db, member_id)
    return await _save_away(request, whose, context, db, first, last)


@admin_router.post("/away/clear")
async def clear_member_away(principal: AdminCSRFChecked, context: Context, db: Database, member_id: RowId) -> Response:
    whose = await _theirs(principal, db, member_id)
    await whose.clear_away(db, context)
    return local_redirect("/admin/members/{}/preferences?notice=back#away", whose.member.id)


@admin_router.post("/stance")
async def save_member_stance(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    member_id: RowId,
    target: Annotated[str, Form(max_length=40)] = "",
    level: Annotated[str, Form(max_length=8)] = "",
) -> Response:
    whose = await _theirs(principal, db, member_id)
    return await _save_stance(request, whose, context, db, target, level)


@admin_router.post("/needs")
async def save_member_needs(
    request: Request, principal: AdminCSRFChecked, context: Context, db: Database, member_id: RowId, needs: Needs = None
) -> Response:
    whose = await _theirs(principal, db, member_id)
    chosen = frozenset(i for v in needs or [] if (i := parse_id(v)) is not None)
    try:
        await guests.set_member_needs(db, principal.member, whose.member.id, chosen, now=context.clock())
    except (InvalidMemberInput, EventRuleViolation) as exc:
        activity.refused(exc, str(exc), principal.member, path=request.url.path)
        return await _page(request, whose, context, db, error=str(exc), status_code=400)
    return local_redirect("/admin/members/{}/preferences?notice=settings", whose.member.id)


@admin_router.post("/fun")
async def save_member_fun_mode(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    member_id: RowId,
    fun: Annotated[str, Form(max_length=3)] = "off",
) -> Response:
    """The form's checkbox sends ``on`` when checked and nothing when not, which means off."""
    on = FUN_CHOICES.get(fun)
    if on is None:
        message = "Not a fun mode setting."
        return await render(request, "error.html", {"title": "Not saved", "message": message}, status_code=400)
    whose = await _theirs(principal, db, member_id)
    await set_member_fun_mode(db, principal.member, whose.member.id, on, now=context.clock())
    return local_redirect("/admin/members/{}/preferences?notice=fun#fun-mode", whose.member.id)
