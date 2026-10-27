# SPDX-License-Identifier: AGPL-3.0-or-later
"""Picks a member adds to a call: a proposal while voting (FR-13), or an offer that joins the
call (FR-5). Two rules hold for both.

- **Whoever adds a pick does it** (#74): they offer the role its mode needs, so it's covered
  (FR-17), and the roster shows it.
- **One pick per place** (#77): a place is a catalog item, or free text, by its kind and name,
  whatever the mode. ``ProposalRepository.add`` is the backstop for every pick, ranked ones too.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.events import ResponseRepository
from dinnerbell.domain.catalog import ItemKind, Mode, kind_for, name_key
from dinnerbell.domain.events import DuplicatePick, Event, EventState, Proposal, with_pick_role
from dinnerbell.domain.members import Member
from dinnerbell.services import call_context as cc
from dinnerbell.services.call_context import CallContext, load, publish


async def volunteer(
    db: AsyncSession, ctx: CallContext, actor: Member, mode: Mode, *, join: bool, now: datetime
) -> CallContext:
    """Save ``actor``'s answer offering the role a pick in ``mode`` needs (``with_pick_role``),
    and return the call read again. The caller holds the event's lock and checks the pick
    afterward (``lifecycle.misfit``), so a refusal rolls the role back with everything else."""
    mine = ctx.response_of(actor.id)
    answer = with_pick_role(mine, actor.id, mode, now, join=join)
    if answer == mine:
        return ctx
    await ResponseRepository(db).save(ctx.event.household_id, ctx.event.id, answer, now)
    return await load(db, ctx.event)


def place(ctx: CallContext, proposal: Proposal) -> tuple[ItemKind, str]:
    """Where a pick is from: its catalog item's kind and name key, or its free text's."""
    item = ctx.items.get(proposal.item_id) if proposal.item_id is not None else None
    if item is not None:
        return item.kind, item.key
    return kind_for(proposal.mode), name_key(proposal.free_text or "")


def picked(ctx: CallContext, candidate: Proposal) -> Proposal | None:
    """The call's pick for the same place as ``candidate``, in any mode, if it has one."""
    where = place(ctx, candidate)
    return next((p for p in ctx.proposals if place(ctx, p) == where), None)


def already_a_pick(ctx: CallContext, existing: Proposal) -> DuplicatePick:
    """The refusal for a second pick of ``existing``'s place, naming it and its mode."""
    name = cc.item_info(ctx, existing).name
    return DuplicatePick(f"{name} is already a pick ({existing.mode.label.lower()}). Vote for it instead.")


async def announce(db: AsyncSession, event: Event, *, now: datetime) -> CallContext:
    """Publish a new or changed pick: the call message, whose roster shows the role its adder
    took, and while voting the proposals. Returns the call read again."""
    ctx = await load(db, event)
    voting = [cc.proposals_updated(ctx)] if ctx.event.state is EventState.VOTING else []
    await publish(db, ctx, cc.call_updated(ctx), *voting, now=now)
    return ctx
