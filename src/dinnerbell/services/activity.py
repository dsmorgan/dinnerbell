# SPDX-License-Identifier: AGPL-3.0-or-later
"""The story of a call in the logs (#75): one ``info`` line per action, transition, and refusal,
with the same field names everywhere.

- **Actions.** A use case calls ``action`` inside its transaction. The line is written once the
  transaction commits, and dropped if it rolls back, so a refused or failed action is never
  logged as done. Slack, the web, the command line, and the scheduler all get the same lines.
- **Refusals.** A use case notes what it's about (``about``; ``locked_event`` notes the call),
  and the entry point that turns a refusal into a reply logs it once with ``refused``: a stable
  ``reason`` code (``reason``) and the message the member saw.

Each line has the call (``event_id``, ``run``, ``slot``, ``day``), its state (``state_before`` and
``state_after``, or ``state`` on a refusal), who (``member_id``, and their display name as
``member``), and the place when there is one (``item_id``, ``item``, ``mode``). The channel
(``slack``, ``web``, ``cli``, or ``scheduler``) and ``request_id`` come from ``logs.start_unit``.

Logs carry display names and IDs, never emails, tokens, OIDC subjects, calendar URLs, or text
members wrote, such as notes and feedback comments, which are logged by length (SEC-5). A
place's name is fine as ``item``, even typed in as a new pick.
"""

from __future__ import annotations

import contextvars
import logging
import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from dinnerbell.domain.catalog import CatalogItem, Mode
from dinnerbell.domain.events import Event, EventState, Proposal
from dinnerbell.domain.members import Member
from dinnerbell.logs import subject_var

log = logging.getLogger(__name__)

_PENDING = "dinnerbell.activity"
"""``Session.info`` key: lines waiting for their transaction to commit."""

Fields = dict[str, Any]


def action(
    db: AsyncSession,
    name: str,
    event: Event,
    actor: Member | None,
    *,
    before: EventState | None = None,
    after: EventState | None = None,
    started: bool = False,
    item: CatalogItem | str | None = None,
    item_id: int | None = None,
    mode: Mode | None = None,
    **extra: Any,
) -> None:
    """Log ``name`` once ``db``'s transaction commits. ``event`` is the call as the action found
    it, so its state is ``state_before`` unless ``before`` says otherwise, or ``started`` says
    the action made it; ``after`` is the state it left, when it changed. ``item`` is the catalog
    item, or a new pick's name; ``extra`` adds fields of its own, never text a member wrote."""
    fields = _call(event) | {
        "state_before": None if started else (before or event.state).value,
        "state_after": (after or event.state).value,
        **_who(actor),
        **_place(item, item_id, mode),
        **extra,
    }
    pending: list[tuple[contextvars.Context, str, Fields]] = db.info.setdefault(_PENDING, [])
    pending.append((contextvars.copy_context(), name, _present(fields)))


def pick(items: Mapping[int, CatalogItem], proposal: Proposal) -> Fields:
    """``item``, ``item_id``, and ``mode`` for ``action`` or ``about``, for a pick on a call
    whose catalog is ``items``."""
    item = items.get(proposal.item_id) if proposal.item_id is not None else None
    return {"item": item or proposal.free_text, "item_id": proposal.item_id, "mode": proposal.mode}


def about(
    event: Event | None = None,
    *,
    item: CatalogItem | str | None = None,
    item_id: int | None = None,
    mode: Mode | None = None,
) -> None:
    """Note what the current action is about, for its refusal line if it's refused."""
    subject = dict(subject_var.get() or {})
    if event is not None:
        subject |= _call(event) | {"state": event.state.value}
    subject |= _present(_place(item, item_id, mode))
    subject_var.set(subject)


def refused(exc: BaseException, shown: str, actor: Member | None, **extra: Any) -> None:
    """Log a refusal once, where it's turned into a reply: the ``reason`` code, the message
    the member saw (``refusal``), and what the action was about (``about``). Refusals are
    normal, so it's ``info``."""
    fields = (subject_var.get() or {}) | _who(actor) | {"reason": reason(exc), "refusal": shown} | extra
    log.info("refused", extra=_present(fields))


def reason(exc: BaseException) -> str:
    """``exc``'s stable reason code: its ``code``, such as ``EventRuleViolation.code``, or else
    its class's name in snake case, such as ``not_found`` for ``NotFound``."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()


def _call(event: Event) -> Fields:
    return {"event_id": event.id, "run": event.run, "slot": event.slot.value, "day": event.day.isoformat()}


def _who(actor: Member | None) -> Fields:
    return {} if actor is None else {"member_id": actor.id, "member": actor.display_name}


def _place(item: CatalogItem | str | None, item_id: int | None, mode: Mode | None) -> Fields:
    if isinstance(item, CatalogItem):
        item_id, item = item.id, item.name
    return {"item_id": item_id, "item": item, "mode": mode.value if mode is not None else None}


def _present(fields: Fields) -> Fields:
    return {key: value for key, value in fields.items() if value is not None}


@sa_event.listens_for(Session, "after_commit")
def _write(session: Session) -> None:
    """Write the committed transaction's lines, each in the context it was noted in, so it keeps
    that unit's request ID, member, and channel."""
    for context, name, fields in session.info.pop(_PENDING, ()):
        context.run(log.info, name, extra=fields)


@sa_event.listens_for(Session, "after_transaction_end")
def _drop(session: Session, transaction: SessionTransaction) -> None:
    """Drop what a transaction that ended without committing noted: rolled back or closed."""
    if transaction.parent is None:
        session.info.pop(_PENDING, None)
