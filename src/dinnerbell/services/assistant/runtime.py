# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether the operator configured the assistant, and whether it answers a member."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.config import Settings
from dinnerbell.db.meals import household_settings
from dinnerbell.domain import assistant, flags
from dinnerbell.domain.members import Member


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    """What the operator set: the key (only whether there is one) and the flag, and the model."""

    key_set: bool
    flag_on: bool
    model: str = assistant.DEFAULT_MODEL

    @property
    def configured(self) -> bool:
        """The key and the flag are both set, so Admin shows **Assistant** (ADR 0009)."""
        return self.key_set and self.flag_on

    @classmethod
    def from_settings(cls, settings: Settings) -> AssistantConfig:
        return cls(
            key_set=settings.anthropic_api_key is not None,
            flag_on=flags.enabled(settings, flags.ASSISTANT),
            model=settings.assistant_model,
        )


async def available(db: AsyncSession, config: AssistantConfig, member: Member) -> bool:
    """Whether the assistant answers ``member``: the key, the flag, Admin's switch, and an
    active member or admin who isn't a guest. Everyone else gets no reply (spec 6.14, **Who**)."""
    if not config.configured or not member.is_member:
        return False
    stored = assistant.assistant_settings(await household_settings(db, member.household_id))
    return assistant.available(configured=config.configured, switch_on=stored.enabled, member=member)
