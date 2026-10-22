# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORM models. M0: household, member, session, audit. M1: the catalog, preferences, and meal log.

Every table carries ``household_id`` (the household table's own key is that ID).
Changing a model requires a new Alembic revision; see CLAUDE.md.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    UniqueConstraint,
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

    ``event_id`` has no foreign key until M2 creates the event table.
    """

    __tablename__ = "meal_log"
    __table_args__ = (
        Index("ix_meal_log_household_date", "household_id", "date"),
        CheckConstraint("slot IN ('lunch', 'dinner')", name="slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    slot: Mapped[str] = mapped_column(String(16))
    item_id: Mapped[int] = mapped_column(ForeignKey("catalog_item.id", ondelete="RESTRICT"), index=True)
    cuisine_tags: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    participants: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    event_id: Mapped[int | None] = mapped_column(Integer)
    logged_by: Mapped[int | None] = mapped_column(ForeignKey("member.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
