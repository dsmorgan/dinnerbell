# SPDX-License-Identifier: AGPL-3.0-or-later
"""A menu link's draft without a message: ``StubLinks`` stands in for the ``LinkCalls`` that
``converse`` hands the tools, with a fixed page and a fixed drafting answer, and reserves
nothing. It never reaches the network. For tests of what happens to a link draft afterward,
such as Confirm on the web or the import link Slack gives."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.domain.assistant import web_key
from dinnerbell.domain.catalog import ItemKind
from dinnerbell.domain.members import Member
from dinnerbell.services.assistant.client import ModelReply
from dinnerbell.services.assistant.menu_links import LinkDraft, draft_from_link
from dinnerbell.services.safe_fetch import FetchedPage
from tests.support.fake_model import MODEL, answer

MENU_LINK = "https://menu.example.com/basil-house"
MENU_PAGE = "Basil House. Thai kitchen. Pad thai and green curry, for pickup."
MENU_DRAFT = """version: 1
restaurants:
  - name: Basil House
    cuisine: [Thai]
    order_url: https://pay.example.net/basil
    notes: Pay ahead at pay.example.net.
"""


@dataclass
class StubLinks:
    page: FetchedPage = field(default_factory=lambda: FetchedPage(MENU_LINK, "Basil House", MENU_PAGE, False))
    reply: ModelReply = field(default_factory=lambda: answer(MENU_DRAFT))
    requests: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def model(self) -> str:
        return MODEL

    async def fetch(self, url: str) -> FetchedPage:
        return self.page

    async def check_room(self, request: Mapping[str, Any]) -> None:
        return None

    async def call(self, request: Mapping[str, Any]) -> ModelReply:
        self.requests.append(request)
        return self.reply


async def link_draft(db: AsyncSession, member: Member, *, now: datetime, links: StubLinks | None = None) -> LinkDraft:
    """``member``'s catalog draft from ``MENU_LINK``, in their web conversation."""
    return await draft_from_link(
        db, member, web_key(member.id), links or StubLinks(), MENU_LINK, ItemKind.RESTAURANT, label="Member 1", now=now
    )
