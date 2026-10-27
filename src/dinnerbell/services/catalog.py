# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catalog use cases: items, tags, and catalog file imports and exports.

Any member can add and edit items, add tags, and import. Only the admin renames and merges
tags. Every change writes an audit row.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository, ItemValues
from dinnerbell.db.engine import read_snapshot
from dinnerbell.db.events import ProposalRepository, ResponseRepository
from dinnerbell.db.meals import MealLogRepository
from dinnerbell.db.preferences import PreferenceRepository
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.catalog import (
    CatalogItem,
    CatalogRuleViolation,
    CleanItem,
    InvalidCatalogInput,
    ItemInput,
    Tag,
    TagKind,
    check_merge,
    clean_item,
    name_key,
    normalize_tag_name,
    sorted_meals,
)
from dinnerbell.domain.catalog_export import CatalogExport, export_catalog
from dinnerbell.domain.catalog_file import parse_catalog
from dinnerbell.domain.catalog_import import CatalogSnapshot, ImportPlan, ItemState, plan_import
from dinnerbell.domain.household import Household
from dinnerbell.domain.members import Member
from dinnerbell.domain.preferences import merged_stance
from dinnerbell.services.common import audit, is_unique_violation, require_admin, require_member
from dinnerbell.services.errors import NotFound

log = logging.getLogger(__name__)


class ImportRejected(Exception):
    """The file has errors, so nothing was saved. ``plan`` lists them."""

    def __init__(self, plan: ImportPlan) -> None:
        super().__init__("The file has errors; nothing was imported.")
        self.plan = plan


class ImportChanged(Exception):
    """The catalog changed after the preview, so the import would differ from what was shown."""

    def __init__(self, plan: ImportPlan) -> None:
        super().__init__("The catalog changed since your preview. Check the new preview, then apply again.")
        self.plan = plan


@dataclass(frozen=True, slots=True)
class CatalogData:
    items: list[CatalogItem]
    tags: list[Tag]
    members: list[Member]
    last_eaten: dict[int, date]


@asynccontextmanager
async def _unique(db: AsyncSession, message: str) -> AsyncIterator[None]:
    """Turn a lost race on a unique name into a friendly rule violation. Any other integrity
    error, such as a broken foreign key, is a bug and propagates unchanged."""
    try:
        yield
    except IntegrityError as exc:
        await db.rollback()
        if not is_unique_violation(exc):
            raise
        raise CatalogRuleViolation(message) from None


# Reading ------------------------------------------------------------------------------------------


async def catalog(db: AsyncSession, actor: Member) -> CatalogData:
    require_member(actor)
    repo = CatalogRepository(db)
    return CatalogData(
        items=await repo.items(actor.household_id),
        tags=await repo.tags(actor.household_id),
        members=[m for m in await MemberRepository(db).list(actor.household_id) if not m.is_guest],
        last_eaten=await MealLogRepository(db).last_eaten(actor.household_id),
    )


async def get_item(db: AsyncSession, actor: Member, item_id: int) -> CatalogItem:
    require_member(actor)
    item = await CatalogRepository(db).item(actor.household_id, item_id)
    if item is None:
        raise NotFound("No such catalog item.")
    return item


async def tags(db: AsyncSession, actor: Member) -> list[Tag]:
    require_member(actor)
    return await CatalogRepository(db).tags(actor.household_id)


async def tag_usage(db: AsyncSession, actor: Member) -> dict[int, int]:
    require_member(actor)
    return await CatalogRepository(db).tag_usage(actor.household_id)


# Items --------------------------------------------------------------------------------------------


async def _clean(
    db: AsyncSession, actor: Member, data: ItemInput, current: CatalogItem | None = None
) -> tuple[CleanItem, list[Tag]]:
    """Validate an item. Cooks must be active members, except that an edit may keep cooks the
    item already has, so deactivating someone doesn't make their dishes uneditable."""
    existing_tags = await CatalogRepository(db).tags(actor.household_id)
    cooks = {m.id for m in await MemberRepository(db).list(actor.household_id) if m.is_member}  # never a guest
    if current is not None:
        cooks |= current.cook_ids
    return clean_item(data, existing_tags, cooks), existing_tags


async def _create_tags(
    db: AsyncSession, household_id: int, wanted: tuple[tuple[TagKind, str], ...], now: datetime
) -> list[Tag]:
    repo = CatalogRepository(db)
    return [await repo.add_tag(household_id, kind, name, now) for kind, name in wanted]


def _values(clean: CleanItem, tag_ids: frozenset[int], active: bool) -> ItemValues:
    return ItemValues(
        kind=clean.kind,
        name=clean.name,
        modes=clean.modes,
        meals=clean.meals,
        tag_ids=tag_ids,
        cook_ids=clean.cook_ids,
        url=clean.url,
        order_url=clean.order_url,
        notes=clean.notes,
        default_servings=clean.default_servings,
        active=active,
    )


def _describe(item: CatalogItem, tag_names: dict[int, str]) -> dict[str, object]:
    return {
        "kind": item.kind.value,
        "name": item.name,
        "modes": sorted(m.value for m in item.modes),
        "meals": [m.value for m in sorted_meals(item.meals)],
        "tags": sorted(tag_names.get(i, str(i)) for i in item.tag_ids),
        "cooks": sorted(item.cook_ids),
        "url": item.url,
        "order_url": item.order_url,
        "notes": item.notes,
        "default_servings": item.default_servings,
        "active": item.active,
    }


def _log_fields(item: CatalogItem) -> dict[str, object]:
    """An item's log line: its ID, its name (not sensitive, SEC-5), and the meals it's for (#72)."""
    return {"item_id": item.id, "item": item.name, "meals": [m.value for m in sorted_meals(item.meals)]}


async def create_item(db: AsyncSession, actor: Member, data: ItemInput, *, now: datetime) -> CatalogItem:
    require_member(actor)
    clean, _ = await _clean(db, actor, data)
    repo = CatalogRepository(db)
    duplicate = f"A {clean.kind.label.lower()} named {clean.name} already exists."
    if await repo.item_by_name(actor.household_id, clean.kind, clean.name) is not None:
        raise CatalogRuleViolation(duplicate)
    async with _unique(db, duplicate):
        created = await _create_tags(db, actor.household_id, clean.tags_to_create, now)
        tag_ids = clean.tag_ids | {t.id for t in created}
        item = await repo.add_item(actor.household_id, _values(clean, tag_ids, True), created_by=actor.id, now=now)
        names = {t.id: t.name for t in await repo.tags(actor.household_id)}
        payload = _describe(item, names) | {"tags_created": [t.name for t in created]}
        await audit(db, actor.household_id, actor, "catalog.item_created", f"item:{item.id}", payload, now)
        await db.commit()
    log.info("catalog item created", extra=_log_fields(item))
    return item


async def update_item(db: AsyncSession, actor: Member, item_id: int, data: ItemInput, *, now: datetime) -> CatalogItem:
    require_member(actor)
    before = await get_item(db, actor, item_id)
    if data.kind is not before.kind:
        raise InvalidCatalogInput("An item's kind can't change. Add a new item instead.")
    clean, _ = await _clean(db, actor, data, current=before)
    repo = CatalogRepository(db)
    duplicate = f"A {clean.kind.label.lower()} named {clean.name} already exists."
    same_name = await repo.item_by_name(actor.household_id, clean.kind, clean.name)
    if same_name is not None and same_name.id != item_id:
        raise CatalogRuleViolation(duplicate)
    async with _unique(db, duplicate):
        created = await _create_tags(db, actor.household_id, clean.tags_to_create, now)
        tag_ids = clean.tag_ids | {t.id for t in created}
        item = await repo.update_item(actor.household_id, item_id, _values(clean, tag_ids, before.active), now)
        names = {t.id: t.name for t in await repo.tags(actor.household_id)}
        old, new = _describe(before, names), _describe(item, names)
        changes = {key: value for key, value in new.items() if old[key] != value}
        payload = changes | {"tags_created": [t.name for t in created]}
        await audit(db, actor.household_id, actor, "catalog.item_updated", f"item:{item.id}", payload, now)
        await db.commit()
    log.info("catalog item updated", extra=_log_fields(item))
    return item


async def set_item_active(db: AsyncSession, actor: Member, item_id: int, active: bool, *, now: datetime) -> CatalogItem:
    require_member(actor)
    item = await get_item(db, actor, item_id)
    if item.active == active:
        return item
    await CatalogRepository(db).set_active(actor.household_id, item_id, active, now)
    action = "catalog.item_reactivated" if active else "catalog.item_deactivated"
    await audit(db, actor.household_id, actor, action, f"item:{item_id}", {"name": item.name}, now)
    await db.commit()
    return await get_item(db, actor, item_id)


# Tags ---------------------------------------------------------------------------------------------


async def _tag(db: AsyncSession, actor: Member, tag_id: int) -> Tag:
    tag = await CatalogRepository(db).tag(actor.household_id, tag_id)
    if tag is None:
        raise NotFound("No such tag.")
    return tag


async def create_tag(db: AsyncSession, actor: Member, kind: TagKind, name: str, *, now: datetime) -> Tag:
    require_member(actor)
    clean = normalize_tag_name(name)
    repo = CatalogRepository(db)
    existing = next((t for t in await repo.tags(actor.household_id) if t.key == name_key(clean)), None)
    if existing is not None:
        raise CatalogRuleViolation(f'There\'s already {existing.kind.a} tag named "{existing.name}".')
    async with _unique(db, f'There\'s already a tag named "{clean}".'):
        tag = await repo.add_tag(actor.household_id, kind, clean, now)
        payload = {"kind": kind.value, "name": clean}
        await audit(db, actor.household_id, actor, "catalog.tag_created", f"tag:{tag.id}", payload, now)
        await db.commit()
    return tag


async def rename_tag(db: AsyncSession, actor: Member, tag_id: int, name: str, *, now: datetime) -> Tag:
    require_member(actor)
    require_admin(actor, "Only the household admin can rename tags.")
    tag = await _tag(db, actor, tag_id)
    clean = normalize_tag_name(name)
    if clean == tag.name:
        return tag
    repo = CatalogRepository(db)
    clash = next((t for t in await repo.tags(actor.household_id) if t.key == name_key(clean) and t.id != tag.id), None)
    if clash is not None:
        raise CatalogRuleViolation(f'There\'s already a tag named "{clash.name}". Merge the two tags instead.')
    async with _unique(db, f'There\'s already a tag named "{clean}".'):
        renamed = await repo.rename_tag(actor.household_id, tag_id, clean)
        payload = {"from": tag.name, "to": clean}
        await audit(db, actor.household_id, actor, "catalog.tag_renamed", f"tag:{tag_id}", payload, now)
        await db.commit()
    return renamed


async def merge_tags(db: AsyncSession, actor: Member, source_id: int, target_id: int, *, now: datetime) -> Tag:
    """Merge ``source`` into ``target``: items, preferences, meal-log snapshots, guest needs, and
    free-text proposals that named the source now name the target, and the source tag is removed."""
    require_member(actor)
    require_admin(actor, "Only the household admin can merge tags.")
    source = await _tag(db, actor, source_id)
    target = await _tag(db, actor, target_id)
    check_merge(source, target)
    household = actor.household_id
    items = await CatalogRepository(db).move_tag_links(household, source.id, target.id)
    # Not counted in the audit row: even a count would tell the admin whether anyone rated the tag (FR-29).
    await PreferenceRepository(db).move_tag(household, source.id, target.id, merged_stance, now)
    meals = await MealLogRepository(db).repoint_cuisine(household, source.id, target.id)
    # Guest needs and free-text proposals name tags too; without this the delete would drop them.
    await ResponseRepository(db).repoint_guest_need(household, source.id, target.id)
    await ProposalRepository(db).repoint_cuisine(household, source.id, target.id)
    await CatalogRepository(db).delete_tag(household, source.id)
    payload = {
        "source": {"id": source.id, "kind": source.kind.value, "name": source.name},
        "target": {"id": target.id, "kind": target.kind.value, "name": target.name},
        "items_retagged": items,
        "meals_repointed": meals,
    }
    await audit(db, household, actor, "catalog.tags_merged", f"tag:{target.id}", payload, now)
    await db.commit()
    log.info("tags merged", extra={"source_tag_id": source.id, "target_tag_id": target.id})
    return target


# Export -------------------------------------------------------------------------------------------


async def export(db: AsyncSession, household_id: int) -> CatalogExport:
    """The household's catalog as a catalog file. It reads only tags and items, never members,
    preferences, or history, so it holds no personal data. Any member may export.

    Tags and items are read in one snapshot, so a tag renamed or merged meanwhile can't leave an
    item pointing at a tag the export doesn't list.
    """
    repo = CatalogRepository(db)
    async with read_snapshot(db):
        tags, items = await repo.tags(household_id), await repo.items(household_id)
    exported = export_catalog(tags, items)
    counts = {"restaurants": exported.restaurants, "home_meals": exported.home_meals, "tags": exported.tags}
    log.info("catalog exported", extra=counts)
    if exported.too_big_to_import:
        log.warning("catalog export is over the import limits", extra={"bytes": len(exported.data), **counts})
    return exported


# Import -------------------------------------------------------------------------------------------


async def snapshot(db: AsyncSession, household_id: int) -> CatalogSnapshot:
    repo = CatalogRepository(db)
    return CatalogSnapshot(
        tags=tuple(await repo.tags(household_id)),
        items=tuple(await repo.items(household_id)),
        members=tuple(m for m in await MemberRepository(db).list(household_id) if not m.is_guest),  # cooks
    )


def content_digest(source: str | bytes) -> str:
    data = source.encode() if isinstance(source, str) else source
    return hashlib.sha256(data).hexdigest()


async def preview_import(db: AsyncSession, household_id: int, source: str | bytes) -> ImportPlan:
    """Plan an import without saving anything."""
    return plan_import(parse_catalog(source), await snapshot(db, household_id))


async def apply_import(
    db: AsyncSession,
    household_id: int,
    actor: Member | None,
    source: str | bytes,
    *,
    now: datetime,
    expected_fingerprint: str | None = None,
    action: str = "catalog.imported",
) -> ImportPlan:
    """Re-plan ``source`` against the current catalog and apply it in one transaction.

    Raises ImportRejected if the file has errors and ImportChanged if the plan no longer
    matches ``expected_fingerprint`` (the preview the person confirmed). Either way, and on any
    failure while saving, nothing is written.
    """
    plan = await preview_import(db, household_id, source)
    if not plan.ok:
        raise ImportRejected(plan)
    if expected_fingerprint is not None and plan.fingerprint() != expected_fingerprint:
        raise ImportChanged(plan)
    repo = CatalogRepository(db)
    try:
        tag_ids = {tag.key: tag.id for tag in await repo.tags(household_id)}
        for new in plan.tags_to_create:
            tag_ids[new.key] = (await repo.add_tag(household_id, new.kind, new.name, now)).id
        for create in plan.items_to_create:
            await repo.add_item(
                household_id, _state_values(create.state, tag_ids), created_by=actor.id if actor else None, now=now
            )
        for update in plan.items_to_update:
            await repo.update_item(household_id, update.item_id, _state_values(update.state, tag_ids), now)
        payload: dict[str, Any] = plan.summary() | {
            "sha256": content_digest(source),
            "tags": [t.name for t in plan.tags_to_create],
            "created": [c.state.name for c in plan.items_to_create],
            "updated": [u.state.name for u in plan.items_to_update],
        }
        await audit(db, household_id, actor, action, f"household:{household_id}", payload, now)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if not is_unique_violation(exc):
            raise
        raise ImportChanged(await preview_import(db, household_id, source)) from None
    except BaseException:
        await db.rollback()
        raise
    log.info("catalog imported", extra=plan.summary())
    return plan


def _state_values(state: ItemState, tag_ids: dict[str, int]) -> ItemValues:
    return ItemValues(
        kind=state.kind,
        name=state.name,
        modes=state.modes,
        meals=state.meals,
        tag_ids=frozenset(tag_ids[key] for key in state.tag_keys),
        cook_ids=state.cook_ids,
        url=state.url,
        order_url=state.order_url,
        notes=state.notes,
        default_servings=state.default_servings,
        active=state.active,
    )


async def seed_from_file(
    db: AsyncSession, household: Household, path: Path | None, *, now: datetime
) -> ImportPlan | None:
    """Import ``DINNERBELL_SEED_FILE`` on first start, only while the catalog is empty.

    A missing or invalid file is logged and skipped so the app still starts; the catalog can be
    imported from the web page instead.
    """
    if path is None:
        return None
    if not await CatalogRepository(db).is_empty(household.id):
        log.info("catalog already has entries; seed file skipped")
        return None
    try:
        source = await asyncio.to_thread(path.read_bytes)
    except OSError as exc:
        log.error("could not read the seed file", extra={"path": str(path), "error": exc.strerror})
        return None
    try:
        plan = await apply_import(db, household.id, None, source, now=now, action="catalog.seeded")
    except (ImportRejected, ImportChanged) as exc:
        log.error(
            "seed file has errors; nothing was imported",
            extra={"path": str(path), "errors": [str(e) for e in exc.plan.errors]},
        )
        return None
    except Exception:
        # A seed is a convenience; a bug in it must never keep the app from starting.
        log.exception("seed import failed; nothing was imported", extra={"path": str(path)})
        return None
    log.info(
        "catalog seeded",
        extra={"path": str(path), **plan.summary(), "unknown_cooks": [u.location for u in plan.unknown_cooks]},
    )
    return plan
