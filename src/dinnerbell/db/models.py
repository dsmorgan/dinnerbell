# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORM models. M0: household, member, session, audit. M1: the catalog, preferences, and meal log.
M2: dinner calls (event, response, suggestion, proposal, vote, assignment) and the outbox.
M3: feedback on meals, offers (an event's ``capacity``), guests (a member's ``guest`` role
and the ``invitation`` table, #38), and the meals each catalog item is for (#72). M4: contributions and badge awards.
M7: ``stance_edit``, so a stance set on Preferences wins over earlier ratings (FR-55).

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
    false,
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
        CheckConstraint("role IN ('admin', 'member', 'guest')", name="role"),
        CheckConstraint(
            "(role = 'guest' AND email IS NULL) OR (role <> 'guest' AND email IS NOT NULL AND email <> '')",
            name="email",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"), index=True)
    display_name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(254))
    """How a member signs in (FR-41): required for admins and members, and never set for a guest."""
    slack_email: Mapped[str | None] = mapped_column(String(254))
    """The email Slack shared when a guest joined from the channel (#38). Sign-in never reads it;
    it only warns the admin who adds a member with it."""
    oidc_subject: Mapped[str | None] = mapped_column(String(255))
    slack_user_id: Mapped[str | None] = mapped_column(String(32))
    slack_link_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    role: Mapped[str] = mapped_column(String(16))
    can_cook: Mapped[bool] = mapped_column(Boolean, default=False)
    can_pickup: Mapped[bool] = mapped_column(Boolean, default=False)
    can_order: Mapped[bool] = mapped_column(Boolean, default=False)
    mute_dm_reminders: Mapped[bool] = mapped_column(Boolean, default=False)
    feedback_reminder: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    dietary_needs: Mapped[list[int]] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    """A guest's dietary tag IDs (#38). Empty for members, whose stances are preferences."""
    join_refused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    """An admin removed this guest, so joining from the channel is refused (#38)."""
    slack_guest_account: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    """The guest joined with a Slack guest account, not as a full member of the workspace (#38)."""
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
    """A restaurant or home meal. Items are never deleted, only deactivated, so history keeps them.

    ``meals`` is the meals it's for (#72), as bits: breakfast 1, lunch 2, dinner 4. At least one
    is set; the default is lunch and dinner (6).
    """

    __tablename__ = "catalog_item"
    __table_args__ = (
        UniqueConstraint("household_id", "kind", "name_key"),
        CheckConstraint("kind IN ('home', 'restaurant')", name="kind"),
        CheckConstraint("meals BETWEEN 1 AND 7", name="meals"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(100))
    name_key: Mapped[str] = mapped_column(String(100))
    modes: Mapped[list[str]] = mapped_column(JSON)
    meals: Mapped[int] = mapped_column(Integer, server_default=text("6"))
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


class StanceEditRow(Base):
    """When a member last set their stance on an item on Preferences (FR-55, ADR 0008). Ratings
    they saved before it no longer move their score: the stance they set wins. A row outlives a
    neutral stance, whose ``preference`` row is deleted, so setting an item back to 0 still
    discards earlier ratings. One per member per item."""

    __tablename__ = "stance_edit"
    __table_args__ = (UniqueConstraint("member_id", "item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(ForeignKey("catalog_item.id", ondelete="CASCADE"), index=True)
    edited_at: Mapped[datetime] = mapped_column(UTCDateTime())


class MealLogRow(Base):
    """What the household ate. ``cuisine_tags`` and ``participants`` are snapshots taken at log
    time, as JSON lists of ``{"id": ..., "name": ...}``.

    ``event_id`` links a meal to the call that decided it. It is unique, so marking a call done
    twice can never log the meal twice.

    AUTOINCREMENT, because deleting meals (``dinnerbell reset-history``, #56) would otherwise let
    SQLite give their IDs to the next meals, and a History or feedback page left open would post
    to a different meal.
    """

    __tablename__ = "meal_log"
    __table_args__ = (
        Index("ix_meal_log_household_date", "household_id", "date"),
        CheckConstraint("slot IN ('breakfast', 'lunch', 'dinner')", name="slot"),
        UniqueConstraint("event_id"),
        {"sqlite_autoincrement": True},
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
    """One coordination of one slot on one date (spec 4, 8): a call, or an offer (FR-4), which
    keeps its item, mode, and time in the ``decided_*`` and ``meal_time`` columns from the start
    and its spare servings in ``capacity``. ``run`` counts admin re-runs; each run posts fresh
    messages. ``version`` changes on every write, which is how use cases take the
    event's write lock first (see ``db.events.EventRepository.lock``).

    AUTOINCREMENT, because a test instance can discard a call (#60), and SQLite would otherwise
    give its ID to the next call. Outbox keys, Slack buttons, and links name events by ID.
    """

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
        CheckConstraint("slot IN ('breakfast', 'lunch', 'dinner')", name="slot"),
        CheckConstraint(
            "state IN ('scheduled', 'collecting', 'voting', 'decided', 'done', 'abandoned', 'cancelled')",
            name="state",
        ),
        CheckConstraint("decided_mode IS NULL OR decided_mode IN ('cook', 'pickup', 'delivery')", name="mode"),
        CheckConstraint("end_reason IS NULL OR end_reason IN ('too_few', 'no_coverage', 'missed')", name="end_reason"),
        CheckConstraint(
            "(kind = 'call' AND capacity IS NULL) OR "
            "(kind = 'offer' AND capacity IS NOT NULL AND capacity BETWEEN 1 AND 20)",
            name="capacity",
        ),
        {"sqlite_autoincrement": True},
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
    locked_by: Mapped[int | None] = mapped_column(ForeignKey("member.id", ondelete="SET NULL"))
    ended_by: Mapped[int | None] = mapped_column(ForeignKey("member.id", ondelete="SET NULL"))
    end_note: Mapped[str | None] = mapped_column(String(140))
    capacity: Mapped[int | None] = mapped_column(Integer)
    """An offer's spare servings (FR-4); NULL for a call."""
    shuffles: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """How many times the coordinator shuffled this run's picks (#87)."""
    shuffle_note: Mapped[str | None] = mapped_column(String(200))
    """The picks message's context line after a shuffle."""
    roles_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    """While set, the coordinator is choosing roles until then (#87)."""
    tie_how: Mapped[str | None] = mapped_column(String(24))
    """How a tie for the most votes was broken (FR-16), a ``TieHow`` value."""
    tie_by: Mapped[int | None] = mapped_column(Integer)
    """The member whose choice or vote broke it. Not a foreign key: it is only a name to show."""
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ResponseRow(Base):
    """A member's answer to a call (FR-7 to FR-9). ``guest_needs`` is a JSON list of dietary
    tag IDs; ``maybe_at_deadline`` marks a maybe the deadline turned into out. ``late`` marks
    someone who joined while voting (#76), and ``vote_counts`` whether the coordinator counts
    their votes."""

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
    late: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    vote_counts: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


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
    """Who does which role for a decided call. One member per role.

    AUTOINCREMENT, because a reassignment deletes a row and inserts one, and SQLite would
    otherwise reuse the deleted ID. The assignment's notification key includes its ID, so a
    reused ID would silently drop the new assignee's message.
    """

    __tablename__ = "assignment"
    __table_args__ = (
        UniqueConstraint("event_id", "role"),
        CheckConstraint("role IN ('cook', 'pickup', 'order', 'coordinator')", name="role"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class InvitationRow(Base):
    """A guest invited to one call (#38): they may answer it, and nothing else. ``invited_by``
    is the member who invited them, who answers for a guest without Slack and breaks a tie
    when the guest coordinates."""

    __tablename__ = "invitation"
    __table_args__ = (UniqueConstraint("event_id", "guest_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"))
    guest_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    invited_by: Mapped[int | None] = mapped_column(ForeignKey("member.id", ondelete="SET NULL"))
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
        Index("ix_outbox_event", "event_id", "id"),
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


class FeedbackRow(Base):
    """One member's feedback on one meal (FR-45, FR-46). ``rating`` is the one rating of the
    meal, ``place_issues`` a JSON list of issue chips, and ``issue_note`` what went wrong for the
    "Other" chip. ``item_id`` through ``participants`` snapshot
    the meal when the feedback was saved: the item, its kind and mode, the cook, and who ate.

    Deleting the meal deletes its feedback (``meal_id`` cascades), so a discarded call that
    takes its meal log with it leaves nothing behind.
    """

    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("meal_id", "member_id"),
        CheckConstraint("rating IS NULL OR rating IN ('hated', 'disliked', 'fine', 'liked', 'loved')", name="rating"),
        CheckConstraint("item_kind IN ('home', 'restaurant')", name="item_kind"),
        CheckConstraint("mode IS NULL OR mode IN ('cook', 'pickup', 'delivery')", name="mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    meal_id: Mapped[int] = mapped_column(ForeignKey("meal_log.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    rating: Mapped[str | None] = mapped_column(String(16))
    place_issues: Mapped[list[str]] = mapped_column(JSON, default=list)
    issue_note: Mapped[str | None] = mapped_column(String(200))
    cook_thanks: Mapped[bool] = mapped_column(Boolean, default=False)
    cook_note: Mapped[str | None] = mapped_column(String(200))
    item_id: Mapped[int] = mapped_column(ForeignKey("catalog_item.id", ondelete="RESTRICT"), index=True)
    item_name: Mapped[str] = mapped_column(String(100))
    item_kind: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str | None] = mapped_column(String(16))
    cook_member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id", ondelete="SET NULL"), index=True)
    cook_name: Mapped[str | None] = mapped_column(String(80))
    participants: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ContributionRow(Base):
    """One thing a member did for a meal (FR-38): a role on a done call, a place at its table,
    a pick that won, or feedback on a meal. Counts are rows.

    A row comes from a done call (``event_id``) or, for feedback, a meal (``meal_id``), never
    both, and ``(member_id, kind, event_id)`` and ``(member_id, kind, meal_id)`` are unique, so
    recording a call twice, or two at once, counts once. ``day`` is the meal's day in the
    household's zone, for On a Roll's weeks and the monthly recap. ``item_id`` is set only for
    ``added_item``, and ``(kind, item_id)`` is unique, so an item is credited once.

    Rows go when their call or meal is deleted (a discarded call, ``reset-history``); the
    badges they earned stay.
    """

    __tablename__ = "contribution"
    __table_args__ = (
        UniqueConstraint("member_id", "kind", "event_id"),
        UniqueConstraint("member_id", "kind", "meal_id"),
        UniqueConstraint("kind", "item_id"),
        Index("ix_contribution_household_day", "household_id", "day"),
        Index("ix_contribution_event", "event_id"),
        CheckConstraint(
            "kind IN ('cooked', 'picked_up', 'ordered', 'coordinated', 'participated', 'responded_on_time', "
            "'suggested', 'added_item', 'explored', 'hosted', 'gave_feedback', 'gambled')",
            name="kind",
        ),
        CheckConstraint("(event_id IS NULL) <> (meal_id IS NULL)", name="one_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    day: Mapped[date] = mapped_column(Date)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"))
    meal_id: Mapped[int | None] = mapped_column(ForeignKey("meal_log.id", ondelete="CASCADE"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_item.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class BadgeAwardRow(Base):
    """A badge tier a member earned, once (``member_id``, ``badge_key``, ``tier`` are unique).
    ``announcement`` says whether the channel has been told: ``pending`` until a post goes out
    (``announced_at``), or ``skipped`` for an award made quietly, such as the backfill. The call
    that earned it is ``event_id``, cleared if the call is deleted; the award stays."""

    __tablename__ = "badge_award"
    __table_args__ = (
        UniqueConstraint("member_id", "badge_key", "tier"),
        CheckConstraint("tier IN ('first', 'bronze', 'silver', 'gold', 'diamond')", name="tier"),
        CheckConstraint("announcement IN ('pending', 'announced', 'skipped')", name="announcement"),
        Index("ix_badge_award_household_announcement", "household_id", "announcement"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    badge_key: Mapped[str] = mapped_column(String(24))
    tier: Mapped[str] = mapped_column(String(16))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("event.id", ondelete="SET NULL"))
    awarded_at: Mapped[datetime] = mapped_column(UTCDateTime())
    announcement: Mapped[str] = mapped_column(String(16))
    announced_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SlackBadgeFileRow(Base):
    """The Slack file that holds one badge tier's PNG (#105), uploaded once and reused. A file is
    current while ``content_hash`` (SHA-256 of the PNG bytes) matches the art now: other art, from
    any source, uploads again and replaces the row."""

    __tablename__ = "slack_badge_file"
    __table_args__ = (UniqueConstraint("household_id", "badge_key", "tier"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    badge_key: Mapped[str] = mapped_column(String(24))
    tier: Mapped[str] = mapped_column(String(16))
    content_hash: Mapped[str] = mapped_column(String(64))
    file_id: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime())


class SlackBadgeCompositeRow(Base):
    """The Slack file that holds one member's composite badge picture for App Home (#114): one row
    per member, replaced when ``content_hash`` (SHA-256 of the PNG bytes, which covers the badges,
    their tiers, and the art) no longer matches what the member holds. ``uploaded_at`` says when
    Slack got it: Home waits for it to settle before showing it."""

    __tablename__ = "slack_badge_composite"
    __table_args__ = (UniqueConstraint("household_id", "member_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"))
    content_hash: Mapped[str] = mapped_column(String(64))
    file_id: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime())
