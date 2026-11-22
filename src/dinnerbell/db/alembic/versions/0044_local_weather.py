# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dinnerbone's local weather (#319, ADR 0011): ``local_weather``, empty.

One row per household, written only once an admin saves **Dinnerbone's weather** on Admin > Web
app: the setting, the place its postal code found (rounded to 0.1°), the last answer from
Open-Meteo, and the failure state. It starts empty, so every household keeps the seasonal
weather until an admin turns local weather on. A downgrade drops it; the admin saves the setting
again after the next upgrade.

Revision ID: 0044_local_weather
Revises: 0043_web_push
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from dinnerbell.db.types import UTCDateTime

revision: str = "0044_local_weather"
down_revision: str | None = "0043_web_push"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_weather",
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("postal_code", sa.String(length=10), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("place", sa.String(length=120), nullable=True),
        sa.Column("code", sa.Integer(), nullable=True),
        sa.Column("temperature_c", sa.Float(), nullable=True),
        sa.Column("wind_kmh", sa.Float(), nullable=True),
        sa.Column("observed_at", UTCDateTime(), nullable=True),
        sa.Column("next_fetch_at", UTCDateTime(), nullable=True),
        sa.Column("failing_since", UTCDateTime(), nullable=True),
        sa.Column("failure", sa.String(length=16), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_local_weather_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("household_id", name=op.f("pk_local_weather")),
    )


def downgrade() -> None:
    op.drop_table("local_weather")
