# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORM models. M0: household, member, session, audit. M1: the catalog, preferences, and meal log.
M2: dinner calls (event, response, suggestion, proposal, vote, assignment) and the outbox.

Every table carries ``household_id`` (the household table's own key is that ID).
Changing a model requires a new Alembic revision; see CLAUDE.md.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from dinnerbell.db.types import UTCDateTime

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class HouseholdRow(Base):
    __tablename__ = "household"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    timezone: Mapped[str] = mapped_column(String(64))
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class MemberRow(Base):
    __tablename__ = "member"
    __table_args__ = (
        UniqueConstraint("household_id", "email"),
        UniqueConstraint("household_id", "oidc_subject"),
        UniqueConstraint("household_id", "slack_user_id"),
        CheckConstraint("role IN ('admin', 'member')", name="role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"), index=True)
    display_name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(254))
    oidc_subject: Mapped[str | None] = mapped_column(String(255))
    slack_user_id: Mapped[str | None] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(16))
    can_cook: Mapped[bool] = mapped_column(Boolean, default=False)
    can_pickup: Mapped[bool] = mapped_column(Boolean, default=False)
    can_order: Mapped[bool] = mapped_column(Boolean, default=False)
    mute_dm_reminders: Mapped[bool] = mapped_column(Boolean, default=False)
    feedback_reminder: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class SessionRow(Base):
    """A signed-in browser. ``id`` is the SHA-256 hex digest of the cookie token, never the token."""

    __tablename__ = "session"
    __table_args__ = (Index("ix_session_expires_at", "expires_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime())


class AuditRow(Base):
    __tablename__ = "audit"
    __table_args__ = (Index("ix_audit_household_created", "household_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    actor_member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class TagRow(Base):
    """A tag. ``name_key`` is the case-folded name, so names are unique per household ignoring
    case and across kinds."""

    __tablename__ = "tag"
    __table_args__ = (
        UniqueConstraint("household_id", "name_key"),
        CheckConstraint("kind IN ('cuisine', 'protein', 'dietary', 'other')", name="kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(40))
    name_key: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class CatalogItemRow(Base):
    """A restaurant or home meal. Items are never deleted, only deactivated, so history keeps them."""

    __tablename__ = "catalog_item"
    __table_args__ = (
        UniqueConstraint("household_id", "kind", "name_key"),
        CheckConstraint("kind IN ('home', 'restaurant')", name="kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(100))
    name_key: Mapped[str] = mapped_column(String(100))
    modes: Mapped[list[str]] = mapped_column(JSON)
    url: Mapped[str | None] = mapped_column(String(2000))
    order_url: Mapped[str | None] = mapped_column(String(2000))
    notes: Mapped[str | None] = mapped_column(String(500))
    default_servings: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("member.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class CatalogItemTagRow(Base):
    __tablename__ = "catalog_item_tag"

    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(ForeignKey("catalog_item.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True, index=True)


class CatalogItemCookRow(Base):
    __tablename__ = "catalog_item_cook"

    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(ForeignKey("catalog_item.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), primary_key=True, index=True)


class PreferenceRow(Base):
    """One member's stance on one tag or one item. Exactly one of ``tag_id`` and ``item_id`` is
    set; separate foreign keys keep referential integrity that a polymorphic target_id can't.
    Neutral stances are deleted rather than stored."""

    __tablename__ = "preference"
    __table_args__ = (
        UniqueConstraint("member_id", "tag_id"),
        UniqueConstraint("member_id", "item_id"),
        CheckConstraint("(tag_id IS NULL) <> (item_id IS NULL)", name="one_target"),
        CheckConstraint("score BETWEEN -2 AND 2", name="score_range"),
        CheckConstraint("NOT (veto AND score <> 0)", name="veto_has_no_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    tag_id: Mapped[int | None] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_item.id", ondelete="CASCADE"), index=True)
    veto: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[int] = mapped_column(SmallInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class MealLogRow(Base):
    """What the household ate. ``cuisine_tags`` and ``participants`` are snapshots taken at log
    time, as JSON lists of ``{"id": ..., "name": ...}``.

    ``event_id`` links a meal to the call that decided it. It is unique, so marking a call done
    twice can never log the meal twice.
    """

    __tablename__ = "meal_log"
    __table_args__ = (
        Index("ix_meal_log_household_date", "household_id", "date"),
        CheckConstraint("slot IN ('lunch', 'dinner')", name="slot"),
        UniqueConstraint("event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    slot: Mapped[str] = mapped_column(String(16))
    item_id: Mapped[int] = mapped_column(ForeignKey("catalog_item.id", ondelete="RESTRICT"), index=True)
    cuisine_tags: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    participants: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("event.id", ondelete="SET NULL"))
    logged_by: Mapped[int | None] = mapped_column(ForeignKey("member.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class EventRow(Base):
    """One coordination of one slot on one date (spec 4, 8). ``run`` counts admin re-runs; each
    run posts fresh messages. ``version`` changes on every write, which is how use cases take the
    event's write lock first (see ``db.events.EventRepository.lock``)."""

    __tablename__ = "event"
    __table_args__ = (
        # FR-2: one call per household, slot, and date, so a restart can never double-post.
        Index(
            "uq_event_call_per_slot",
            "household_id",
            "slot",
            "date",
            unique=True,
            sqlite_where=text("kind = 'call'"),
        ),
        Index("ix_event_household_state", "household_id", "state"),
        CheckConstraint("kind IN ('call', 'offer')", name="kind"),
        CheckConstraint("slot IN ('lunch', 'dinner')", name="slot"),
        CheckConstraint(
            "state IN ('scheduled', 'collecting', 'voting', 'decided', 'done', 'abandoned', 'cancelled')",
            name="state",
        ),
        CheckConstraint("decided_mode IS NULL OR decided_mode IN ('cook', 'pickup', 'delivery')", name="mode"),
        CheckConstraint("end_reason IS NULL OR end_reason IN ('too_few', 'no_coverage')", name="end_reason"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    slot: Mapped[str] = mapped_column(String(16))
    date: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(16))
    run: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("member.id", ondelete="SET NULL"))
    coordinator_id: Mapped[int | None] = mapped_column(ForeignKey("member.id", ondelete="SET NULL"))
    opens_at: Mapped[datetime] = mapped_column(UTCDateTime())
    respond_deadline: Mapped[datetime] = mapped_column(UTCDateTime())
    vote_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reminder_minutes: Mapped[int] = mapped_column(Integer)
    vote_minutes: Mapped[int] = mapped_column(Integer)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decided_item_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_item.id", ondelete="RESTRICT"))
    decided_mode: Mapped[str | None] = mapped_column(String(16))
    meal_time: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    end_reason: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ResponseRow(Base):
    """A member's answer to a call (FR-7 to FR-9). ``guest_needs`` is a JSON list of dietary
    tag IDs; ``maybe_at_deadline`` marks a maybe the deadline turned into out."""

    __tablename__ = "response"
    __table_args__ = (
        UniqueConstraint("event_id", "member_id"),
        CheckConstraint("status IN ('in', 'out', 'maybe')", name="status"),
        CheckConstraint("guests BETWEEN 0 AND 20", name="guests"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(8))
    guests: Mapped[int] = mapped_column(Integer, default=0)
    guest_needs: Mapped[list[int]] = mapped_column(JSON, default=list)
    eat_from: Mapped[time | None] = mapped_column(Time)
    eat_until: Mapped[time | None] = mapped_column(Time)
    offers_cook: Mapped[bool] = mapped_column(Boolean, default=False)
    offers_pickup: Mapped[bool] = mapped_column(Boolean, default=False)
    offers_order: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(String(200))
    maybe_at_deadline: Mapped[bool] = mapped_column(Boolean, default=False)
    responded_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class SuggestionRow(Base):
    """Something a member suggested in a call: a catalog item or free text, never both."""

    __tablename__ = "suggestion"
    __table_args__ = (CheckConstraint("(item_id IS NULL) <> (free_text IS NULL)", name="one_target"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_item.id", ondelete="CASCADE"))
    free_text: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ProposalRow(Base):
    """A candidate for a call: a catalog item, or free text with a cuisine that becomes a
    catalog item only if it wins (FR-13). ``reasons`` is a JSON list of sentences."""

    __tablename__ = "proposal"
    __table_args__ = (
        UniqueConstraint("event_id", "item_id", "mode"),
        CheckConstraint("(item_id IS NULL) <> (free_text IS NULL)", name="one_target"),
        CheckConstraint("mode IN ('cook', 'pickup', 'delivery')", name="mode"),
        CheckConstraint("source IN ('system', 'coordinator', 'member')", name="source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_item.id", ondelete="RESTRICT"))
    free_text: Mapped[str | None] = mapped_column(String(100))
    free_cuisine_id: Mapped[int | None] = mapped_column(ForeignKey("tag.id", ondelete="SET NULL"))
    mode: Mapped[str] = mapped_column(String(16))
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(Float)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(16))
    proposed_by: Mapped[int | None] = mapped_column(ForeignKey("member.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class VoteRow(Base):
    """One member's vote for one proposal (FR-14). A member may vote for several proposals."""

    __tablename__ = "vote"
    __table_args__ = (UniqueConstraint("proposal_id", "member_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposal.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class AssignmentRow(Base):
    """Who does which role for a decided call. One member per role."""

    __tablename__ = "assignment"
    __table_args__ = (
        UniqueConstraint("event_id", "role"),
        CheckConstraint("role IN ('cook', 'pickup', 'order', 'coordinator')", name="role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class OutboxRow(Base):
    """Notifications waiting for a channel (spec 8, FR-34). See docs/channels.md.

    A drainer claims a row by setting ``claim_token`` and ``claimed_until`` in one conditional
    UPDATE, so two drainers never deliver the same row. ``ref_channel`` and ``ref_message``
    record where a delivered post landed, for updates and threads.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("household_id", "idempotency_key"),
        Index("ix_outbox_pending", "delivered_at", "failed_at", "id"),
        Index("ix_outbox_message_key", "household_id", "message_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    message_key: Mapped[str | None] = mapped_column(String(200))
    thread_key: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    available_at: Mapped[datetime] = mapped_column(UTCDateTime())
    claim_token: Mapped[str | None] = mapped_column(String(32))
    claimed_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ref_channel: Mapped[str | None] = mapped_column(String(64))
    ref_message: Mapped[str | None] = mapped_column(String(64))
