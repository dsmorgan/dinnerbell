# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake Slack: a Web API client that records calls and answers like Slack, a fake Socket
Mode socket, and Socket Mode request payloads.

``FakeSlack`` subclasses the SDK's ``AsyncWebClient`` and replaces only the HTTP request, so
the SDK's own argument handling and ``SlackApiError`` raising run for real. It checks every
message and view it receives against Block Kit's limits. Queue failures with ``fail``. It never
touches the network. Never import this from ``src/``.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from slack_bolt.adapter.socket_mode.async_internals import run_async_bolt_app
from slack_bolt.async_app import AsyncApp
from slack_bolt.response import BoltResponse
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.web.async_client import AsyncWebClient

from tests.support.blockkit import check_message, check_view

TEAM = "T0HOUSE"
OTHER_TEAM = "T0EVIL"
BOT_USER = "U0BOT"
CHANNEL = "C0DINNER"
BOT_TOKEN = "xoxb-0000000000-fake-bot-token-do-not-log"
APP_TOKEN = "xapp-1-A000-fake-app-token-do-not-log"


@dataclass
class Call:
    method: str
    args: dict[str, Any]


@dataclass
class Failure:
    """An error response (``error`` with ``status``) or a raised exception."""

    error: str | None = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    raises: BaseException | None = None


class FakeSlack(AsyncWebClient):
    def __init__(self, *, team_id: str = TEAM) -> None:
        super().__init__(token=BOT_TOKEN, base_url="https://slack.invalid/api/")
        self.team_id = team_id
        self.calls: list[Call] = []
        self.failures: dict[str, list[Failure]] = {}
        self.users_by_email: dict[str, dict[str, Any]] = {}
        self.channel: dict[str, Any] = {"id": CHANNEL, "is_member": True, "is_archived": False}
        self._ts = itertools.count(1)
        self._ephemeral_ts = itertools.count(1)

    # Setup ---------------------------------------------------------------------------------------

    def fail(self, method: str, *failures: Failure) -> None:
        self.failures.setdefault(method, []).extend(failures)

    def add_user(self, email: str, user_id: str, **extra: Any) -> None:
        self.users_by_email[email] = {
            "id": user_id,
            "team_id": self.team_id,
            "deleted": False,
            "is_bot": False,
            **extra,
        }

    # Reading -------------------------------------------------------------------------------------

    def of(self, *methods: str) -> list[Call]:
        return [c for c in self.calls if c.method in methods]

    @property
    def methods(self) -> list[str]:
        return [c.method for c in self.calls]

    def clear(self) -> None:
        self.calls.clear()

    # The fake HTTP layer -------------------------------------------------------------------------

    async def _request(self, *, http_verb: str, api_url: str, req_args: dict[str, Any]) -> dict[str, Any]:
        method = api_url.rsplit("/", 1)[-1]
        args: dict[str, Any] = {}
        for part in ("params", "data", "json"):
            if isinstance(req_args.get(part), dict):
                args.update(req_args[part])
        args.pop("token", None)
        self.calls.append(Call(method, args))
        await asyncio.sleep(0)
        queued = self.failures.get(method)
        if queued:
            failure = queued.pop(0)
            if failure.raises is not None:
                raise failure.raises
            return _error(failure.error, failure.status, failure.headers)
        if method == "users.lookupByEmail" and args["email"] not in self.users_by_email:
            return _error("users_not_found")
        return {"data": {"ok": True, **self._answer(method, args)}, "headers": {}, "status_code": 200}

    def _answer(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        match method:
            case "auth.test":
                return {"team_id": self.team_id, "user_id": BOT_USER, "bot_id": "B0BOT", "team": "Home"}
            case "apps.connections.open":
                return {"url": "wss://wss.slack.invalid/link"}
            case "conversations.info":
                return {"channel": dict(self.channel)}
            case "chat.postMessage":
                check_message(args)
                channel = args["channel"]
                posted_in = "D" + channel[1:] if channel.startswith(("U", "W")) else channel
                return {"channel": posted_in, "ts": f"{next(self._ts)}.000100"}
            case "conversations.open":
                user = args["users"]
                return {"channel": {"id": "D" + user[1:]}}
            case "chat.update":
                check_message(args)
                return {"channel": args["channel"], "ts": args["ts"]}
            case "chat.postEphemeral":
                return {"message_ts": f"{next(self._ephemeral_ts)}.000200"}
            case "views.open" | "views.publish":
                check_view(args["view"] if isinstance(args["view"], dict) else {})
                return {"view": {"id": "V1"}}
            case "users.lookupByEmail":
                return {"user": self.users_by_email[args["email"]]}
        raise AssertionError(f"FakeSlack doesn't know {method}")


def _error(code: str | None, status: int = 200, headers: dict[str, str] | None = None) -> dict[str, Any]:
    return {"data": {"ok": False, "error": code}, "headers": headers or {}, "status_code": status}


class FakeSocket:
    """Stands in for Bolt's Socket Mode handler."""

    instances: ClassVar[list[FakeSocket]] = []

    def __init__(self, app: AsyncApp, app_token: str, on_drop: Callable[[], None]) -> None:
        self.app = app
        self.on_drop = on_drop
        self.connected = False
        self.closed = False
        FakeSocket.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True
        self.connected = False

    async def is_connected(self) -> bool:
        return self.connected

    def drop(self) -> None:
        """The connection closes, as when Slack restarts the socket."""
        self.connected = False
        self.on_drop()


# Socket Mode requests ----------------------------------------------------------------------------

_envelopes = itertools.count(1)


async def send(app: AsyncApp, kind: str, payload: dict[str, Any]) -> BoltResponse:
    """Deliver ``payload`` to the Bolt app the way the Socket Mode client does."""
    request = SocketModeRequest(type=kind, envelope_id=f"env-{next(_envelopes)}", payload=payload)
    response: BoltResponse = await run_async_bolt_app(app, request)
    return response


def block_action(
    user: str,
    action_id: str,
    value: str | None,
    *,
    channel: str | None = CHANNEL,
    team: str | None = TEAM,
    home: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "block_actions",
        "team": {"id": team, "domain": "home"} if team else None,
        "user": {"id": user, "username": "someone", "team_id": team},
        "api_app_id": "A0DINNER",
        "trigger_id": "1234.5678.trigger",
        "actions": [
            {
                "action_id": action_id,
                "block_id": "b",
                "type": "button",
                "action_ts": "1.2",
                "text": {"type": "plain_text", "text": "x"},
            }
        ],
    }
    if value is not None:
        payload["actions"][0]["value"] = value
    if home:
        payload["container"] = {"type": "view", "view_id": "V0HOME"}
        payload["view"] = {"id": "V0HOME", "type": "home"}
    else:
        payload["container"] = {
            "type": "message",
            "message_ts": "1.000100",
            "channel_id": channel,
            "is_ephemeral": False,
        }
        payload["channel"] = {"id": channel, "name": "dinnerbell"}
    return payload


def view_submission(
    user: str, callback_id: str, values: dict[str, Any], metadata: str, *, team: str | None = TEAM
) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "team": {"id": team, "domain": "home"} if team else None,
        "user": {"id": user, "username": "someone", "team_id": team},
        "api_app_id": "A0DINNER",
        "view": {
            "id": "V0MODAL",
            "type": "modal",
            "callback_id": callback_id,
            "private_metadata": metadata,
            "state": {"values": values},
            "hash": "1.abc",
        },
    }


def slash(user: str, text: str, *, team: str = TEAM, channel: str = CHANNEL) -> dict[str, Any]:
    return {
        "token": "legacy-verification-token",
        "team_id": team,
        "team_domain": "home",
        "channel_id": channel,
        "channel_name": "dinnerbell",
        "user_id": user,
        "user_name": "someone",
        "command": "/dinnerbell",
        "text": text,
        "api_app_id": "A0DINNER",
        "response_url": "https://hooks.slack.invalid/commands/1",
        "trigger_id": "1234.5678.trigger",
    }


def home_opened(user: str, *, team: str = TEAM, tab: str = "home") -> dict[str, Any]:
    return {
        "token": "legacy-verification-token",
        "team_id": team,
        "api_app_id": "A0DINNER",
        "event": {"type": "app_home_opened", "user": user, "channel": "D0HOME", "tab": tab, "event_ts": "1.2"},
        "type": "event_callback",
        "event_id": f"Ev{next(_envelopes)}",
        "event_time": 1,
        "authorizations": [{"team_id": team, "user_id": BOT_USER, "is_bot": True}],
    }


def state(**fields: dict[str, Any]) -> dict[str, Any]:
    """view.state.values with each block's single input under action ID "value"."""
    return {block_id: {"value": entry} for block_id, entry in fields.items()}
