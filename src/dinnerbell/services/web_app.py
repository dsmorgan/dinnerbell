# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optional web app (FR-53, ADR 0010, spec 6.15): Admin's switch and each member's choice of
Slack, the web app, or both.

The switch is a household setting, off by default for every household, new or old. While it's
off, the manifest, the service worker, and the **Get the app** page answer 404, Preferences
hides the choice, and the outbox sends Slack direct messages exactly as before. While it's on,
the outbox closes a direct message to a member on **Web app** as skipped, not failed
(``slack_skips``).

Turning the switch on or off is audited (SEC-9). A member's own choice isn't, as their light or
dark isn't (``services.appearance``); an admin's choice for another member (#181) is.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.engine import write_transaction
from dinnerbell.db.meals import household_settings
from dinnerbell.db.push import WEB_APP_SETTING
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.members import Member, NotifyVia, check_notify_via
from dinnerbell.services.common import audit, require_admin, require_member
from dinnerbell.services.errors import NotFound
from dinnerbell.services.preferences import member_for_admin

ADMINS_ONLY = "Only the household admin can turn the web app on or off."
SWITCHED_OFF = "That page doesn't exist."
"""What a web app page or form answers while the switch is off: the same 404 as any page that
doesn't exist, so nothing says the feature is there."""


def web_app_on(settings: dict[str, Any]) -> bool:
    """Whether household ``settings`` have the web app on. Only ``true`` is on."""
    return settings.get(WEB_APP_SETTING) is True


async def web_app_enabled(db: AsyncSession, household_id: int) -> bool:
    """Whether the household has Admin's **Web app** switch on. The push, code sign-in, and
    device code reads this before doing anything."""
    return web_app_on(await household_settings(db, household_id))


async def web_app_enabled_for(db: AsyncSession, household_id: int | None) -> bool:
    """``web_app_enabled`` for a request: the signed-in member's household, or, for a visitor
    with no session (``household_id`` None), the install's household. A Dinnerbell install has
    one household, the one bootstrap made, as ``services.guests.joining_open`` and signed-out
    pages' household name assume. Without a household yet, it's off."""
    if household_id is None:
        household = await HouseholdRepository(db).first()
        if household is None:
            return False
        household_id = household.id
    return await web_app_enabled(db, household_id)


async def require_web_app(db: AsyncSession, household_id: int | None) -> None:
    """Raise NotFound unless the web app is on for ``household_id`` (see ``web_app_enabled_for``)."""
    if not await web_app_enabled_for(db, household_id):
        raise NotFound(SWITCHED_OFF)


async def set_web_app(db: AsyncSession, actor: Member, on: bool, *, now: datetime) -> bool:
    """Admin's switch. Audited as ``household.web_app_updated`` with ``on``. Turning it off also
    runs ``switched_off`` in the same transaction. Returns whether anything changed."""
    require_admin(actor, ADMINS_ONLY)
    async with write_transaction(db):
        settings = await household_settings(db, actor.household_id)
        if web_app_on(settings) == on:
            return False
        await HouseholdRepository(db).update_settings(actor.household_id, settings | {WEB_APP_SETTING: on})
        if not on:
            await switched_off(db, actor.household_id, now=now)
        await audit(
            db,
            actor.household_id,
            actor,
            "household.web_app_updated",
            f"household:{actor.household_id}",
            {"on": on},
            now,
        )
        await db.commit()
    return True


async def switched_off(db: AsyncSession, household_id: int, *, now: datetime) -> None:
    """What turning the switch off does beyond saving it, in the caller's transaction (spec 6.15):
    delete the household's push subscriptions and its queued push rows, and keep sessions and
    sign-in codes. Members' choices stay, for when it's on again. The Slack outbox is untouched."""
    from dinnerbell.services import push  # push reads the switch from this module

    await push.switched_off(db, household_id)


# Each member's choice ----------------------------------------------------------------------------


def slack_skips(member: Member) -> bool:
    """Whether, with the switch on, Slack skips a direct message to ``member``: an active member
    or admin on **Web app**. A deactivated member's direct messages are dropped by the Slack
    adapter as before, and a guest's go to Slack as before."""
    return member.is_member and member.delivery is NotifyVia.WEB


async def set_my_notify_via(db: AsyncSession, actor: Member, choice: NotifyVia, *, now: datetime) -> Member:
    """The actor's own choice on Preferences. NotFound while the switch is off, and refused for a
    choice ``check_notify_via`` refuses, such as Slack without a Slack user. Not audited."""
    require_member(actor)
    await require_web_app(db, actor.household_id)
    return await _set(db, actor, actor, choice, now=now)


async def set_member_notify_via(
    db: AsyncSession, actor: Member, member_id: int, choice: NotifyVia, *, now: datetime
) -> Member:
    """An admin's choice for another member (#181), with the same rules as the member's own.
    Audited as ``member.notify_via_set``, with the choice that counted before and the new one,
    whenever a choice is stored."""
    member = await member_for_admin(db, actor, member_id)
    await require_web_app(db, actor.household_id)
    return await _set(db, actor, member, choice, now=now)


async def _set(db: AsyncSession, actor: Member, member: Member, choice: NotifyVia, *, now: datetime) -> Member:
    check_notify_via(member, choice)
    # A choice is stored even when it matches the default, so a later change to the default never
    # moves someone who chose. Without Slack, Web app is the only choice, so there's nothing to keep.
    if member.slack_user_id is None or member.notify_via is choice:
        return member
    updated = await MemberRepository(db).update(member.household_id, member.id, now, notify_via=choice)
    if actor.id != member.id:
        payload = {"from": member.delivery.value, "to": choice.value}
        await audit(db, actor.household_id, actor, "member.notify_via_set", f"member:{member.id}", payload, now)
    await db.commit()
    return updated
