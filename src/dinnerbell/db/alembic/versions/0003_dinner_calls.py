# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 tables: event, response, suggestion, proposal, vote, assignment, outbox.

Also links ``meal_log.event_id`` to ``event`` with a foreign key and a unique constraint, so a
call logs at most one meal, and gives every household explicit slot schedules and ranking
weights (spec FR-1, section 7).

SQLite can't add a constraint to an existing table, so ``meal_log`` is rebuilt in batch mode.

Revision ID: 0003_dinner_calls
Revises: 0002_catalog
Create Date: 2026-10-22
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_dinner_calls"
down_revision: str | None = "0002_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A copy of dinnerbell.domain.schedule.default_settings() as of this revision. Migrations must
# not import app code, which keeps changing after the revision is written.
DEFAULT_SETTINGS = {
    "slots": {
        "dinner": {
            "enabled": True,
            "call_time": "15:00",
            "response_minutes": 90,
            "vote_minutes": 45,
            "reminder_minutes": 15,
            "modes": ["pickup", "delivery"],
            "meal_time": "18:30",
        },
        "lunch": {
            "enabled": False,
            "call_time": "10:30",
            "response_minutes": 60,
            "vote_minutes": 30,
            "reminder_minutes": 15,
            "modes": ["pickup"],
            "meal_time": "12:30",
        },
    },
    "ranking_weights": {
        "soft_preference": 1.0,
        "cuisine_variety": 3.0,
        "novelty": 1.0,
        "suggested": 2.0,
        "default_mode": 1.0,
        "volunteer_fit": 1.0,
    },
    "proposal_count": 3,
}


def _member_fk(column: str, table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["member.id"], name=op.f(f"fk_{table}_{column}_member"), ondelete="SET NULL"
    )


def _household_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["household_id"], ["household.id"], name=op.f(f"fk_{table}_household_id_household"), ondelete="CASCADE"
    )


def _event_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["event_id"], ["event.id"], name=op.f(f"fk_{table}_event_id_event"), ondelete="CASCADE"
    )


def upgrade() -> None:
    op.create_table(
        "event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("slot", sa.String(length=16), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("run", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("coordinator_id", sa.Integer(), nullable=True),
        sa.Column("opens_at", sa.DateTime(), nullable=False),
        sa.Column("respond_deadline", sa.DateTime(), nullable=False),
        sa.Column("vote_deadline", sa.DateTime(), nullable=True),
        sa.Column("reminder_minutes", sa.Integer(), nullable=False),
        sa.Column("vote_minutes", sa.Integer(), nullable=False),
        sa.Column("reminder_sent_at", sa.DateTime(), nullable=True),
        sa.Column("decided_item_id", sa.Integer(), nullable=True),
        sa.Column("decided_mode", sa.String(length=16), nullable=True),
        sa.Column("meal_time", sa.DateTime(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("done_at", sa.DateTime(), nullable=True),
        sa.Column("end_reason", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "decided_mode IS NULL OR decided_mode IN ('cook', 'pickup', 'delivery')", name=op.f("ck_event_mode")
        ),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('too_few', 'no_coverage')", name=op.f("ck_event_end_reason")
        ),
        sa.CheckConstraint("kind IN ('call', 'offer')", name=op.f("ck_event_kind")),
        sa.CheckConstraint("slot IN ('lunch', 'dinner')", name=op.f("ck_event_slot")),
        sa.CheckConstraint(
            "state IN ('scheduled', 'collecting', 'voting', 'decided', 'done', 'abandoned', 'cancelled')",
            name=op.f("ck_event_state"),
        ),
        _member_fk("coordinator_id", "event"),
        _member_fk("created_by", "event"),
        sa.ForeignKeyConstraint(
            ["decided_item_id"],
            ["catalog_item.id"],
            name=op.f("fk_event_decided_item_id_catalog_item"),
            ondelete="RESTRICT",
        ),
        _household_fk("event"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event")),
    )
    op.create_index("ix_event_household_state", "event", ["household_id", "state"], unique=False)
    op.create_index(
        "uq_event_call_per_slot",
        "event",
        ["household_id", "slot", "date"],
        unique=True,
        sqlite_where=sa.text("kind = 'call'"),
    )
    op.create_table(
        "response",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("guests", sa.Integer(), nullable=False),
        sa.Column("guest_needs", sa.JSON(), nullable=False),
        sa.Column("eat_from", sa.Time(), nullable=True),
        sa.Column("eat_until", sa.Time(), nullable=True),
        sa.Column("offers_cook", sa.Boolean(), nullable=False),
        sa.Column("offers_pickup", sa.Boolean(), nullable=False),
        sa.Column("offers_order", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("maybe_at_deadline", sa.Boolean(), nullable=False),
        sa.Column("responded_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('in', 'out', 'maybe')", name=op.f("ck_response_status")),
        sa.CheckConstraint("guests BETWEEN 0 AND 20", name=op.f("ck_response_guests")),
        _event_fk("response"),
        _household_fk("response"),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_response_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_response")),
        sa.UniqueConstraint("event_id", "member_id", name=op.f("uq_response_event_id_member_id")),
    )
    op.create_index(op.f("ix_response_member_id"), "response", ["member_id"], unique=False)
    op.create_table(
        "suggestion",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("free_text", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("(item_id IS NULL) <> (free_text IS NULL)", name=op.f("ck_suggestion_one_target")),
        _event_fk("suggestion"),
        _household_fk("suggestion"),
        sa.ForeignKeyConstraint(
            ["item_id"], ["catalog_item.id"], name=op.f("fk_suggestion_item_id_catalog_item"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_suggestion_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suggestion")),
    )
    op.create_index(op.f("ix_suggestion_event_id"), "suggestion", ["event_id"], unique=False)
    op.create_table(
        "proposal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("free_text", sa.String(length=100), nullable=True),
        sa.Column("free_cuisine_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("proposed_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("mode IN ('cook', 'pickup', 'delivery')", name=op.f("ck_proposal_mode")),
        sa.CheckConstraint("source IN ('system', 'coordinator', 'member')", name=op.f("ck_proposal_source")),
        sa.CheckConstraint("(item_id IS NULL) <> (free_text IS NULL)", name=op.f("ck_proposal_one_target")),
        _event_fk("proposal"),
        sa.ForeignKeyConstraint(
            ["free_cuisine_id"], ["tag.id"], name=op.f("fk_proposal_free_cuisine_id_tag"), ondelete="SET NULL"
        ),
        _household_fk("proposal"),
        sa.ForeignKeyConstraint(
            ["item_id"], ["catalog_item.id"], name=op.f("fk_proposal_item_id_catalog_item"), ondelete="RESTRICT"
        ),
        _member_fk("proposed_by", "proposal"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposal")),
        sa.UniqueConstraint("event_id", "item_id", "mode", name=op.f("uq_proposal_event_id_item_id_mode")),
    )
    op.create_index(op.f("ix_proposal_event_id"), "proposal", ["event_id"], unique=False)
    op.create_table(
        "vote",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _event_fk("vote"),
        _household_fk("vote"),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_vote_member_id_member"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposal.id"], name=op.f("fk_vote_proposal_id_proposal"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vote")),
        sa.UniqueConstraint("proposal_id", "member_id", name=op.f("uq_vote_proposal_id_member_id")),
    )
    op.create_index(op.f("ix_vote_event_id"), "vote", ["event_id"], unique=False)
    op.create_table(
        "assignment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("role IN ('cook', 'pickup', 'order', 'coordinator')", name=op.f("ck_assignment_role")),
        _event_fk("assignment"),
        _household_fk("assignment"),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_assignment_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assignment")),
        sa.UniqueConstraint("event_id", "role", name=op.f("uq_assignment_event_id_role")),
        sqlite_autoincrement=True,  # assignment IDs key notifications; never reuse one
    )
    op.create_index(op.f("ix_assignment_member_id"), "assignment", ["member_id"], unique=False)
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("message_key", sa.String(length=200), nullable=True),
        sa.Column("thread_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("claim_token", sa.String(length=32), nullable=True),
        sa.Column("claimed_until", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.Column("ref_channel", sa.String(length=64), nullable=True),
        sa.Column("ref_message", sa.String(length=64), nullable=True),
        _event_fk("outbox"),
        _household_fk("outbox"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.UniqueConstraint("household_id", "idempotency_key", name=op.f("uq_outbox_household_id_idempotency_key")),
    )
    op.create_index("ix_outbox_message_key", "outbox", ["household_id", "message_key"], unique=False)
    op.create_index("ix_outbox_pending", "outbox", ["delivered_at", "failed_at", "id"], unique=False)
    with op.batch_alter_table("meal_log") as batch_op:
        batch_op.create_unique_constraint(batch_op.f("uq_meal_log_event_id"), ["event_id"])
        batch_op.create_foreign_key(
            batch_op.f("fk_meal_log_event_id_event"), "event", ["event_id"], ["id"], ondelete="SET NULL"
        )
    for key, value in DEFAULT_SETTINGS.items():
        op.execute(
            sa.text(
                "UPDATE household SET settings = json_set(settings, :path, json(:value)) "
                "WHERE json_extract(settings, :path) IS NULL"
            ).bindparams(path=f"$.{key}", value=json.dumps(value))
        )


def downgrade() -> None:
    # Before 0003 nothing wrote these keys, and every logged meal had event_id NULL.
    for key in DEFAULT_SETTINGS:
        op.execute(sa.text("UPDATE household SET settings = json_remove(settings, :path)").bindparams(path=f"$.{key}"))
    op.execute(sa.text("UPDATE meal_log SET event_id = NULL"))
    with op.batch_alter_table("meal_log") as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_meal_log_event_id_event"), type_="foreignkey")
        batch_op.drop_constraint(batch_op.f("uq_meal_log_event_id"), type_="unique")
    op.drop_index("ix_outbox_pending", table_name="outbox")
    op.drop_index("ix_outbox_message_key", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index(op.f("ix_assignment_member_id"), table_name="assignment")
    op.drop_table("assignment")
    op.drop_index(op.f("ix_vote_event_id"), table_name="vote")
    op.drop_table("vote")
    op.drop_index(op.f("ix_proposal_event_id"), table_name="proposal")
    op.drop_table("proposal")
    op.drop_index(op.f("ix_suggestion_event_id"), table_name="suggestion")
    op.drop_table("suggestion")
    op.drop_index(op.f("ix_response_member_id"), table_name="response")
    op.drop_table("response")
    op.drop_index("uq_event_call_per_slot", table_name="event", sqlite_where=sa.text("kind = 'call'"))
    op.drop_index("ix_event_household_state", table_name="event")
    op.drop_table("event")
