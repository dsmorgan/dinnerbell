# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who may join from the channel (#38 addendum): ``services.guests.SlackPeople`` on the Slack Web
API. A person may join when they're a real user of the configured workspace, in the configured
channel right now: never a bot or app, a deactivated account, or someone from another
organization over Slack Connect. A Slack guest account may join, as a guest.

Every check fails closed: an unknown workspace, a missing flag that should be false, or Slack
not answering means no. Emails are only passed on for matching an invited member; never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from dinnerbell.channels.slack.api import SlackApi, SlackUnavailable
from dinnerbell.domain.members import SLACK_USER_ID_PATTERN
from dinnerbell.domain.notifications import PermanentDeliveryError
from dinnerbell.services.guests import NotJoinable, SlackPerson

log = logging.getLogger(__name__)

NOT_PEOPLE = ("deleted", "is_bot", "is_app_user", "is_stranger")
"""Flags that rule a Slack user out of joining. Slack may leave one out when it's false, so any
value but a missing or false one refuses. ``is_restricted`` and ``is_ultra_restricted`` mark Slack
guest accounts, which may join, as guests."""
SLACK_GUEST = ("is_restricted", "is_ultra_restricted")
MAX_PEOPLE = 200
"""People an import lists at most; each takes a ``users.info`` call."""
PAGE_SIZE = 200
MAX_PAGES = 10


class ChannelPeople:
    """``services.guests.SlackPeople`` for the household's workspace and channel. ``team_id``
    reads the workspace the bot token belongs to, which the connection learns at its first check;
    until then nobody may join."""

    def __init__(self, api: SlackApi, *, channel_id: str, team_id: Callable[[], str | None]) -> None:
        self.api = api
        self.channel_id = channel_id
        self.team_id = team_id

    def check(self, user: dict[str, Any] | None) -> SlackPerson | NotJoinable:
        """The person behind ``users.info``'s answer, or why they can't join."""
        team = self.team_id()
        if not user or team is None or user.get("team_id") != team:
            return NotJoinable.NOT_A_PERSON
        if any(user.get(flag, False) is not False for flag in NOT_PEOPLE):
            return NotJoinable.NOT_A_PERSON
        slack_id = user.get("id")
        if not isinstance(slack_id, str) or not SLACK_USER_ID_PATTERN.match(slack_id):
            return NotJoinable.NOT_A_PERSON
        raw = user.get("profile")
        profile: dict[str, Any] = raw if isinstance(raw, dict) else {}
        names = (profile.get("display_name"), profile.get("real_name"), user.get("real_name"), user.get("name"))
        name = next((n.strip() for n in names if isinstance(n, str) and n.strip()), "Guest")
        email = profile.get("email")
        full = not any(user.get(flag, False) is not False for flag in SLACK_GUEST)
        return SlackPerson(slack_id, name[:80], email if isinstance(email, str) else None, full_member=full)

    async def _members(self) -> frozenset[str] | NotJoinable:
        try:
            members, complete = await self.api.channel_members(
                self.channel_id, page_size=PAGE_SIZE, max_pages=MAX_PAGES
            )
        except (SlackUnavailable, PermanentDeliveryError) as exc:
            log.warning("reading the channel's members for a join failed", extra={"error": str(exc)})
            return NotJoinable.UNAVAILABLE
        if not complete:
            log.warning("the channel has more members than Dinnerbell reads; the rest can't join")
        return members

    async def person(self, slack_user_id: str) -> SlackPerson | NotJoinable:
        if not SLACK_USER_ID_PATTERN.match(slack_user_id):
            return NotJoinable.NOT_A_PERSON
        members = await self._members()
        if isinstance(members, NotJoinable):
            return members
        if slack_user_id not in members:
            return NotJoinable.NOT_IN_CHANNEL
        try:
            user = await self.api.user_info(slack_user_id)
        except (SlackUnavailable, PermanentDeliveryError) as exc:
            log.warning("looking up a Slack user for a join failed", extra={"error": str(exc)})
            return NotJoinable.UNAVAILABLE
        return self.check(user)

    async def channel_people(self) -> list[SlackPerson]:
        """Everyone in the channel who could join, for the admin's import (option C). Empty when
        Slack can't say."""
        members = await self._members()
        if isinstance(members, NotJoinable):
            return []
        found: list[SlackPerson] = []
        for slack_user_id in sorted(members)[:MAX_PEOPLE]:
            try:
                user = await self.api.user_info(slack_user_id)
            except (SlackUnavailable, PermanentDeliveryError):
                break
            person = self.check(user)
            if isinstance(person, SlackPerson):
                found.append(person)
        return found
