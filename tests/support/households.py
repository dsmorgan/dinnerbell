# SPDX-License-Identifier: AGPL-3.0-or-later
"""A second household in the same database, for testing that nothing crosses households."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from dinnerbell.config import Settings

STAMP = "2026-01-01 00:00:00"


def other_household(settings: Settings) -> dict[str, int]:
    """Create a household with one cuisine tag, one tagged item, and one meal. Returns their IDs."""
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO household (name, timezone, settings, created_at) VALUES ('Evil', 'UTC', '{}', :t)"),
                {"t": STAMP},
            )
            household = c.execute(text("SELECT max(id) FROM household")).scalar_one()
            c.execute(
                text(
                    "INSERT INTO tag (household_id, kind, name, name_key, created_at) "
                    "VALUES (:h, 'cuisine', 'EvilCuisine', 'evilcuisine', :t)"
                ),
                {"h": household, "t": STAMP},
            )
            tag = c.execute(text("SELECT max(id) FROM tag")).scalar_one()
            c.execute(
                text(
                    "INSERT INTO catalog_item "
                    "(household_id, kind, name, name_key, modes, active, created_at, updated_at) "
                    "VALUES (:h, 'restaurant', 'EvilItem', 'evilitem', '[\"pickup\"]', 1, :t, :t)"
                ),
                {"h": household, "t": STAMP},
            )
            item = c.execute(text("SELECT max(id) FROM catalog_item")).scalar_one()
            c.execute(
                text("INSERT INTO catalog_item_tag (household_id, item_id, tag_id) VALUES (:h, :i, :g)"),
                {"h": household, "i": item, "g": tag},
            )
            c.execute(
                text(
                    "INSERT INTO meal_log (household_id, date, slot, item_id, cuisine_tags, participants, "
                    "created_at, updated_at) VALUES (:h, '2026-10-01', 'dinner', :i, '[]', '[]', :t, :t)"
                ),
                {"h": household, "i": item, "t": STAMP},
            )
            meal = c.execute(text("SELECT max(id) FROM meal_log")).scalar_one()
    finally:
        engine.dispose()
    return {"household": household, "tag": tag, "item": item, "meal": meal}
