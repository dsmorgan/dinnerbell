# SPDX-License-Identifier: AGPL-3.0-or-later
"""``SlackChannel``: the ``Channel`` protocol (docs/channels.md) on top of the Slack Web API.

Channel posts go to the configured channel, threaded when the drainer passes a thread. Direct
messages go to the member's Slack user ID with ``chat.postMessage``, which opens the DM. Every
post returns a ``MessageRef`` of Slack's channel ID and message ``ts``.

While the setup is broken, deliveries are held (``HeldDelivery``) instead of reaching Slack,
where ``not_in_channel`` would close them for good. A held row doesn't use up an attempt; it
goes out once the admin fixes the setup. A bot that isn't in the channel, or an archived
channel, holds channel posts and updates only. A refused token or the wrong workspace holds
every delivery, direct messages too.

On a labeled instance, such as test (ADR 0006), ``Audience`` limits who the bot may reach to
the members of the configured channel: a direct message to anyone else is dropped, and a
mention of anyone else shows as their plain name. Until the members are known, direct messages
are held and every mention shows as a name. On an unlabeled instance it changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.channels.slack.api import TOKEN_REFUSED, SlackApi, SlackAuthError, refusal, refused_blocks
from dinnerbell.channels.slack.badge_text import ArtIds, wanted
from dinnerbell.channels.slack.blocks import Directory
from dinnerbell.channels.slack.composites import held_of_member
from dinnerbell.channels.slack.messages import RenderContext, render
from dinnerbell.domain import notifications as note
from dinnerbell.domain.notifications import (
    DeliveryResult,
    HeldDelivery,
    MessageRef,
    Notification,
    PermanentDeliveryError,
)
from dinnerbell.services import slack_members
from dinnerbell.services.badge_files import RETRY_PAUSES, BadgeFiles
from dinnerbell.services.health import SlackState

log = logging.getLogger(__name__)

HOLD_RETRY_SECONDS = 60.0
"""How soon a held delivery is tried again: when the supervisor checks the setup again."""


class HoldScope(StrEnum):
    NONE = "none"
    CHANNEL = "channel"
    """The bot can't post in the channel: hold channel posts and updates."""
    ALL = "all"
    """The token or the workspace is wrong: hold everything."""


@dataclass
class Hold:
    """What to hold back while the setup is broken. The connection sets it; the channel reads it."""

    scope: HoldScope = HoldScope.NONE

    def check(self, *, direct: bool) -> None:
        if self.scope is HoldScope.ALL or (self.scope is HoldScope.CHANNEL and not direct):
            raise HeldDelivery("Slack setup problem; see /healthz and the logs", retry_after=HOLD_RETRY_SECONDS)


@dataclass
class Audience:
    """Who the bot may direct-message or mention. The connection sets it; the channel reads it.

    Unlimited unless ``limited``, which is set on a labeled instance. While limited, only Slack
    users in ``members`` (the configured channel's members, read at each supervisor check) are
    allowed. ``members`` is None while they're unknown: before the first read, and after a read
    or check that says the old list can't be trusted. Unknown allows nobody, but isn't "not in
    the channel": direct messages wait for the members instead of being dropped.
    """

    limited: bool = False
    members: frozenset[str] | None = None

    @property
    def known(self) -> bool:
        return not self.limited or self.members is not None

    def allows(self, slack_user: str) -> bool:
        return not self.limited or (self.members is not None and slack_user in self.members)

    def check_direct(self, slack_user: str) -> bool:
        """Whether a direct message to ``slack_user`` may go. Raises HeldDelivery while the
        members are unknown, so the message waits instead of being lost."""
        if not self.known:
            raise HeldDelivery(
                "channel members not known yet; direct messages wait for them", retry_after=HOLD_RETRY_SECONDS
            )
        return self.allows(slack_user)

    def directory(self, directory: Directory) -> Directory:
        """``directory`` without the Slack users this audience doesn't allow, so they render as
        plain names instead of mentions."""
        if not self.limited:
            return directory
        return Directory({m: s for m, s in directory.slack_ids.items() if self.allows(s)}, directory.inactive)


def deterministic(exc: SlackAuthError) -> Exception:
    """What a delivery raises for a Slack auth error. A refused token stays one, since the
    adapter holds every delivery until it's fixed. Any other code, such as ``missing_scope``,
    concerns one method and will fail the same way on every retry, so it fails the row at once."""
    if exc.code in TOKEN_REFUSED:
        return exc
    return PermanentDeliveryError(str(exc), code=exc.code)


class SlackChannel:
    def __init__(
        self,
        api: SlackApi,
        *,
        channel_id: str,
        base_url: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        hold: Hold,
        instance_label: str = "",
        audience: Audience | None = None,
        state: SlackState | None = None,
        badge_files: BadgeFiles | None = None,
    ) -> None:
        self.api = api
        self.badge_files = badge_files
        self.hold = hold
        self.audience = audience or Audience()
        self.state = state
        self.channel_id = channel_id
        self.base_url = base_url
        self.sessionmaker = sessionmaker
        self.instance_label = instance_label

    async def _directory(self, household_id: int) -> Directory:
        async with self.sessionmaker() as db:
            return Directory(
                await slack_members.slack_directory(db, household_id),
                await slack_members.inactive_members(db, household_id),
            )

    def _context(self, directory: Directory, art: ArtIds | None = None) -> RenderContext:
        return RenderContext(self.base_url, self.audience.directory(directory), self.instance_label, art or {})

    async def _art(self, notification: Notification) -> tuple[ArtIds, set[str]]:
        """The uploaded art a badge post shows, and which of its files were uploaded just now, or
        none: art never holds up or fails a delivery."""
        fresh: set[str] = set()
        if self.badge_files is None or not isinstance(notification, note.BadgesEarned):
            return {}, fresh
        asked = {w for n in notification.news for b in (*n.badges, *n.rest) if (w := wanted(b.name, b.tier))}
        return await self.badge_files.file_ids(notification.household_id, asked, fresh), fresh

    async def deliver(self, notification: Notification, *, thread: MessageRef | None = None) -> DeliveryResult:
        self.hold.check(direct=notification.recipient_id is not None)
        directory = await self._directory(notification.household_id)
        recipient = notification.recipient_id
        slack_user = None
        if recipient is not None:
            if recipient in directory.inactive:
                # Queued before they were deactivated. Done, not failed: the row closes and is never retried.
                log.info(
                    "slack direct message dropped: recipient is deactivated",
                    extra={"kind": notification.kind, "event_id": notification.event_id, "recipient_id": recipient},
                )
                if isinstance(notification, note.AssignmentMade | note.RoleVacated):
                    # A role may have nobody on it now. The members who run the call see it on the event page.
                    log.warning(
                        "slack role message dropped: recipient is deactivated",
                        extra={"kind": notification.kind, "event_id": notification.event_id},
                    )
                if self.state is not None:
                    self.state.dms_dropped_inactive += 1
                return DeliveryResult(None)
            slack_user = directory.slack_ids.get(recipient)
            if slack_user is None:
                raise PermanentDeliveryError(f"member {recipient} has no Slack user")
            if not self.audience.check_direct(slack_user):
                # Done, not failed: the row closes as delivered and is never retried.
                log.info(
                    "slack direct message dropped: recipient isn't in the channel",
                    extra={"kind": notification.kind, "event_id": notification.event_id, "recipient_id": recipient},
                )
                if self.state is not None and self.state.dms_dropped is not None:
                    self.state.dms_dropped += 1
                return DeliveryResult(None)
        art, fresh = await self._art(notification)
        message = render(notification, self._context(directory, art)).payload()
        try:
            ref = await self._send(message, slack_user, thread)
        except PermanentDeliveryError as exc:
            if not art or self.badge_files is None or not refused_blocks(exc):
                raise  # not about the blocks, so not about the art: the cache stays
            # Slack may refuse a file ID, such as one deleted in Slack, or one it hasn't finished
            # processing. A file uploaded just now gets up to two more tries after a pause, with the
            # cache untouched. Otherwise send the text instead, so the post still goes out, and only
            # if that works forget the files, so the next post uploads again. If the text is refused
            # too, the art wasn't the cause (``invalid_arguments`` can be about anything), so the
            # cache stays.
            extra: dict[str, Any] = {"event_id": notification.event_id}
            log.warning(
                "slack refused badge art (%s: %s)%s",
                exc.code,
                exc.detail,
                "; retrying" if fresh else "",
                extra=extra | refusal(exc),
            )
            retried: MessageRef | None = None
            for attempt, pause in enumerate(RETRY_PAUSES if fresh else (), 1):
                await self.badge_files.pause(pause)
                try:
                    retried = await self._send(message, slack_user, thread)
                except PermanentDeliveryError as again:
                    if not refused_blocks(again):
                        raise
                    log.warning(
                        "slack refused badge art again (retry %d of %d: %s: %s)",
                        attempt,
                        len(RETRY_PAUSES),
                        again.code,
                        again.detail,
                        extra=extra | refusal(again),
                    )
                else:
                    break
            if retried is None:
                message = render(notification, self._context(directory)).payload()
                retried = await self._send(message, slack_user, thread)
                try:
                    await self.badge_files.forget(notification.household_id, art.values(), cool=bool(fresh))
                except Exception:
                    # The post went out. Raising would make the outbox retry and post the badge twice.
                    log.exception("badge art could not be forgotten", extra={"event_id": notification.event_id})
            ref = retried
        await self._pictures_soon(notification)
        log.debug(  # the outbox logs "notification delivered" at info, with the channel and ts
            "slack message posted",
            extra={"kind": notification.kind, "event_id": notification.event_id, "recipient_id": recipient},
        )
        return DeliveryResult(ref)

    async def _pictures_soon(self, notification: Notification) -> None:
        """After a badge post goes out, start making each mentioned member's composite picture for App
        Home (#114), so it is ready before they open it. It runs in the background and never raises,
        so it can't delay or fail the delivery."""
        if self.badge_files is None or not isinstance(notification, note.BadgesEarned):
            return
        try:
            household_id = notification.household_id
            members = [
                (n.person.id, partial(held_of_member, self.sessionmaker, household_id, n.person.id))
                for n in notification.news
            ]
            await self.badge_files.composites_soon(household_id, members)
        except Exception:
            log.exception("badge pictures could not be started", extra={"event_id": notification.event_id})

    async def _send(self, message: dict[str, Any], slack_user: str | None, thread: MessageRef | None) -> MessageRef:
        try:
            if slack_user is not None:
                return await self.api.post(slack_user, message)
            if thread is not None:
                return await self.api.post(thread.channel_id, message, thread_ts=thread.message_id)
            return await self.api.post(self.channel_id, message)
        except SlackAuthError as exc:
            raise deterministic(exc) from None

    async def update(self, ref: MessageRef, notification: Notification) -> None:
        self.hold.check(direct=False)
        ctx = self._context(await self._directory(notification.household_id))
        try:
            await self.api.update(ref, render(notification, ctx).payload())
        except SlackAuthError as exc:
            raise deterministic(exc) from None
