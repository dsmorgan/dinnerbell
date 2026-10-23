# SPDX-License-Identifier: AGPL-3.0-or-later
"""A household wired to a ``SlackConnection`` over ``FakeSlack``: drive the scheduler, drain
the outbox through ``SlackChannel``, and press buttons as Slack users."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from slack_bolt.response import BoltResponse

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.connection import SlackConnection
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.services import outbox
from dinnerbell.services.health import SlackState
from tests.support.calls import House
from tests.support.fake_slack import (
    APP_TOKEN,
    BOT_TOKEN,
    CHANNEL,
    TEAM,
    FakeSlack,
    FakeSocket,
    block_action,
    home_opened,
    send,
    slash,
    view_submission,
)

BASE_URL = "https://dinnerbell.test"
SLACK_IDS = {"Alex": "U01ALEX", "Sam": "U02SAM", "Kim": "U03KIM", "Lee": "U04LEE"}


async def link(house: House, name: str, slack_id: str | None) -> None:
    async with house.sessionmaker() as db:
        await MemberRepository(db).update(house.household.id, house[name].id, house.clock(), slack_user_id=slack_id)
        await db.commit()
    await house.refresh(name)


def connection(
    house: House, fake: FakeSlack, *, team_id: str | None = TEAM, state: SlackState | None = None, **options: Any
) -> SlackConnection:
    """``options`` go to ``SlackConnection``, such as ``command`` and ``instance_label``."""
    return SlackConnection(
        bot_token=BOT_TOKEN,
        app_token=APP_TOKEN,
        channel_id=CHANNEL,
        team_id=team_id,
        base_url=BASE_URL,
        sessionmaker=house.sessionmaker,
        clock=house.clock,
        state=state or SlackState(),
        web_client=fake,
        socket_factory=FakeSocket,
        **options,
    )


@dataclass
class Slack:
    house: House
    fake: FakeSlack
    conn: SlackConnection

    async def drain(self) -> outbox.DrainResult:
        return await outbox.drain(self.house.sessionmaker, self.conn.channel, self.house.clock)

    async def _send(self, kind: str, payload: dict[str, Any]) -> BoltResponse:
        response = await send(self.conn.app, kind, payload)
        for _ in range(3):  # Bolt runs auto-acknowledged event listeners in its own future
            await asyncio.sleep(0)
        await self.conn.interactions.settle()
        return response

    async def click(self, who: str, action_id: str, value: str | None, **kwargs: Any) -> BoltResponse:
        return await self._send("interactive", block_action(SLACK_IDS.get(who, who), action_id, value, **kwargs))

    async def submit(
        self, who: str, callback_id: str, values: dict[str, Any], event_id: int, **kwargs: Any
    ) -> BoltResponse:
        metadata = ids.ViewOrigin(event_id, CHANNEL).encode()
        payload = view_submission(SLACK_IDS.get(who, who), callback_id, values, metadata, **kwargs)
        return await self._send("interactive", payload)

    async def command(self, who: str, text: str, **kwargs: Any) -> BoltResponse:
        return await self._send("slash_commands", slash(SLACK_IDS.get(who, who), text, **kwargs))

    async def open_home(self, who: str, **kwargs: Any) -> BoltResponse:
        return await self._send("events_api", home_opened(SLACK_IDS.get(who, who), **kwargs))

    def ephemerals(self, who: str) -> list[str]:
        user = SLACK_IDS.get(who, who)
        return [c.args["text"] for c in self.fake.of("chat.postEphemeral") if c.args["user"] == user]

    def last_home(self, who: str) -> dict[str, Any]:
        user = SLACK_IDS.get(who, who)
        views = [c.args["view"] for c in self.fake.of("views.publish") if c.args["user_id"] == user]
        assert views, f"no Home published for {who}"
        view: dict[str, Any] = views[-1]
        return view


async def wire(house: House, *, linked: tuple[str, ...] = ("Alex", "Sam", "Kim", "Lee"), **options: Any) -> Slack:
    for name in linked:
        await link(house, name, SLACK_IDS[name])
    fake = FakeSlack()
    conn = connection(house, fake, **options)
    assert await conn.run_once()
    fake.clear()
    return Slack(house, fake, conn)
