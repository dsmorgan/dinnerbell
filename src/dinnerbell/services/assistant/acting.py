# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who the assistant acts as (spec 6.14, **Scope**), and the refusal its use cases send back to
the model.

``tool_actor`` is the one wrapper: every tool, and **Confirm** on a draft, runs as the member it
answers with a member's role, even when they're an admin, so no admin-only branch in a service
ever passes for the assistant. Rules that match on the member's ID still apply, such as being
the call's coordinator.
"""

from __future__ import annotations

from dataclasses import replace

from dinnerbell.domain.members import Member, Role


def tool_actor(member: Member) -> Member:
    """The actor every tool runs as: ``member`` with a member's role, never an admin's."""
    return replace(member, role=Role.MEMBER)


class ToolRefusal(ValueError):
    """What a tool can't do for the model's input, such as candidates before voting or a call
    the member didn't say. The message goes back to the model, so it's written for it, and never
    says anything about people's stances."""
