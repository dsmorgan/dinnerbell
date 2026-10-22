# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORM models for the M0 tables: household, member, session, audit.

Every table carries ``household_id`` (the household table's own key is that ID).
Changing a model requires a new Alembic revision; see CLAUDE.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, MetaData, String, UniqueConstraint
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
