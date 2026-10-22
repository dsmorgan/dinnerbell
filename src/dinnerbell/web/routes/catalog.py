# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catalog: browse, add, edit, and deactivate items; manage tags; import catalog files."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.domain.catalog import (
    CatalogItem,
    CatalogRuleViolation,
    InvalidCatalogInput,
    ItemFilter,
    ItemInput,
    ItemKind,
    Mode,
    Status,
    TagKind,
    sorted_modes,
    tags_by_kind,
)
from dinnerbell.domain.catalog_file import MAX_FILE_BYTES
from dinnerbell.domain.catalog_import import ImportPlan
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.variety import household_today
from dinnerbell.web.auth import AdminCSRFChecked, Context, CSRFChecked, Database, Principal, SignedIn
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import BadFormValue, choice, optional_int, split_names
from dinnerbell.web.render import render
from dinnerbell.web.signing import SignedPayload, derive_key

router = APIRouter(prefix="/catalog")

IMPORT_PREVIEW_LIFETIME = timedelta(minutes=30)
NOTICES = {
    "created": "Saved to the catalog.",
    "updated": "Changes saved.",
    "deactivated": "Deactivated. It's hidden from proposals, and its history stays.",
    "reactivated": "Reactivated.",
    "tag_created": "Tag added.",
    "tag_renamed": "Tag renamed.",
    "tags_merged": "Tags merged.",
}
FormText = Annotated[str, Form(max_length=4000)]
FormList = Annotated[list[str] | None, Form()]


def last_eaten_label(day: date | None, today: date) -> str:
    if day is None:
        return "Never eaten"
    days = (today - day).days
    if days <= 0:
        return "Eaten today"
    if days == 1:
        return "Eaten yesterday"
    if days < 14:
        return f"Eaten {days} days ago"
    return f"Last eaten {day:%b} {day.day}, {day.year}" if day.year != today.year else f"Last eaten {day:%b} {day.day}"


# Listing ------------------------------------------------------------------------------------------


@router.get("")
async def catalog_page(
    request: Request,
    principal: SignedIn,
    context: Context,
    db: Database,
    q: str = "",
    kind: str = "",
    mode: str = "",
    tag: str = "",
    status: str = "",
    notice: str = "",
    created: int = 0,
    updated: int = 0,
    tags_created: int = 0,
) -> Response:
    data = await catalog_service.catalog(db, principal.member)
    tag_id = int(tag) if tag.isdigit() else None
    search = ItemFilter(
        query=q[:100],
        kind=choice(ItemKind, kind),
        mode=choice(Mode, mode),
        tag_id=tag_id,
        status=choice(Status, status) or Status.ACTIVE,
    )
    names = {t.id: t.name for t in data.tags}
    items = [item for item in data.items if search.matches(item, names)]
    today = household_today(principal.household, context.clock())
    message = NOTICES.get(notice)
    if notice == "imported":
        message = f"Imported: {created} added, {updated} updated, {tags_created} new tags."
    return await render(
        request,
        "catalog.html",
        {
            "items": items,
            "total": len([i for i in data.items if i.active]),
            "search": search,
            "tag_groups": tags_by_kind(data.tags),
            "tags": {t.id: t for t in data.tags},
            "members": {m.id: m for m in data.members},
            "eaten": {i.id: last_eaten_label(data.last_eaten.get(i.id), today) for i in items},
            "sorted_modes": sorted_modes,
            "kinds": list(ItemKind),
            "modes": list(Mode),
            "statuses": list(Status),
            "notice": message,
        },
    )


# Add and edit -------------------------------------------------------------------------------------


@dataclass
class ItemForm:
    """The add/edit form's fields as posted, kept to redisplay the form after an error."""

    name: str = ""
    tag_ids: set[int] = field(default_factory=set)
    new_cuisines: str = ""
    new_tags: str = ""
    new_tags_kind: str = TagKind.OTHER.value
    modes: set[str] = field(default_factory=lambda: {Mode.PICKUP.value})
    cook_ids: set[int] = field(default_factory=set)
    url: str = ""
    order_url: str = ""
    notes: str = ""
    servings: str = ""

    @classmethod
    def from_item(cls, item: CatalogItem) -> ItemForm:
        return cls(
            name=item.name,
            tag_ids=set(item.tag_ids),
            modes={m.value for m in item.modes},
            cook_ids=set(item.cook_ids),
            url=item.url or "",
            order_url=item.order_url or "",
            notes=item.notes or "",
            servings="" if item.default_servings is None else str(item.default_servings),
        )

    def to_input(self, kind: ItemKind) -> ItemInput:
        modes = {choice(Mode, m) for m in self.modes}
        if None in modes:
            raise BadFormValue("Pick pickup, delivery, or both.")
        return ItemInput(
            kind=kind,
            name=self.name,
            modes=frozenset(m for m in modes if m is not None),
            tag_ids=frozenset(self.tag_ids),
            new_cuisines=split_names(self.new_cuisines),
            new_tags=split_names(self.new_tags),
            new_tags_kind=choice(TagKind, self.new_tags_kind) or TagKind.OTHER,
            cook_ids=frozenset(self.cook_ids),
            url=self.url,
            order_url=self.order_url,
            notes=self.notes,
            default_servings=optional_int(self.servings, "Servings must be a whole number."),
        )


def _ids(values: list[str] | None) -> set[int]:
    return {int(v) for v in values or [] if v.isdigit()}


def posted_item(
    name: FormText = "",
    tag_ids: FormList = None,
    new_cuisines: FormText = "",
    new_tags: FormText = "",
    new_tags_kind: FormText = TagKind.OTHER.value,
    modes: FormList = None,
    cook_ids: FormList = None,
    url: FormText = "",
    order_url: FormText = "",
    notes: FormText = "",
    servings: FormText = "",
) -> ItemForm:
    return ItemForm(
        name=name,
        tag_ids=_ids(tag_ids),
        new_cuisines=new_cuisines,
        new_tags=new_tags,
        new_tags_kind=new_tags_kind,
        modes=set(modes or []),
        cook_ids=_ids(cook_ids),
        url=url,
        order_url=order_url,
        notes=notes,
        servings=servings,
    )


PostedItem = Annotated[ItemForm, Depends(posted_item)]


async def _item_page(
    request: Request,
    principal: Principal,
    db: Database,
    *,
    kind: ItemKind,
    form: ItemForm,
    item: CatalogItem | None = None,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    data = await catalog_service.catalog(db, principal.member)
    cooks = [m for m in data.members if m.active or m.id in form.cook_ids]
    return await render(
        request,
        "catalog_item.html",
        {
            "kind": kind,
            "form": form,
            "item": item,
            "error": error,
            "notice": notice,
            "tag_groups": tags_by_kind(data.tags),
            "tag_kinds": list(TagKind),
            "cooks": cooks,
            "restaurant_modes": [Mode.PICKUP, Mode.DELIVERY],
        },
        status_code=status_code,
    )


@router.get("/new")
async def new_item_page(request: Request, principal: SignedIn, db: Database, kind: str = "restaurant") -> Response:
    chosen = choice(ItemKind, kind) or ItemKind.RESTAURANT
    return await _item_page(request, principal, db, kind=chosen, form=ItemForm())


@router.post("/items")
async def create_item(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    form: PostedItem,
    kind: FormText = ItemKind.RESTAURANT.value,
) -> Response:
    chosen = choice(ItemKind, kind) or ItemKind.RESTAURANT
    try:
        item = await catalog_service.create_item(db, principal.member, form.to_input(chosen), now=context.clock())
    except (InvalidCatalogInput, CatalogRuleViolation, BadFormValue) as exc:
        return await _item_page(request, principal, db, kind=chosen, form=form, error=str(exc), status_code=400)
    return RedirectResponse(f"/catalog?notice=created#item-{item.id}", status_code=303)


@router.get("/items/{item_id}")
async def edit_item_page(
    request: Request, principal: SignedIn, db: Database, item_id: int, notice: str = ""
) -> Response:
    item = await catalog_service.get_item(db, principal.member, item_id)
    form = ItemForm.from_item(item)
    return await _item_page(request, principal, db, kind=item.kind, form=form, item=item, notice=NOTICES.get(notice))


@router.post("/items/{item_id}")
async def update_item(
    request: Request, principal: CSRFChecked, context: Context, db: Database, item_id: int, form: PostedItem
) -> Response:
    item = await catalog_service.get_item(db, principal.member, item_id)
    try:
        await catalog_service.update_item(db, principal.member, item_id, form.to_input(item.kind), now=context.clock())
    except (InvalidCatalogInput, CatalogRuleViolation, BadFormValue) as exc:
        return await _item_page(
            request, principal, db, kind=item.kind, form=form, item=item, error=str(exc), status_code=400
        )
    return RedirectResponse(f"/catalog?notice=updated#item-{item_id}", status_code=303)


@router.post("/items/{item_id}/deactivate")
async def deactivate_item(principal: CSRFChecked, context: Context, db: Database, item_id: int) -> Response:
    await catalog_service.set_item_active(db, principal.member, item_id, False, now=context.clock())
    return RedirectResponse(f"/catalog/items/{item_id}?notice=deactivated", status_code=303)


@router.post("/items/{item_id}/reactivate")
async def reactivate_item(principal: CSRFChecked, context: Context, db: Database, item_id: int) -> Response:
    await catalog_service.set_item_active(db, principal.member, item_id, True, now=context.clock())
    return RedirectResponse(f"/catalog/items/{item_id}?notice=reactivated", status_code=303)


# Tags ---------------------------------------------------------------------------------------------


async def _tags_page(
    request: Request,
    principal: Principal,
    db: Database,
    *,
    error: str | None = None,
    status_code: int = 200,
    **extra: Any,
) -> Response:
    tags = await catalog_service.tags(db, principal.member)
    return await render(
        request,
        "catalog_tags.html",
        {
            "tag_groups": tags_by_kind(tags),
            "all_tags": tags,
            "usage": await catalog_service.tag_usage(db, principal.member),
            "tag_kinds": list(TagKind),
            "error": error,
        }
        | extra,
        status_code=status_code,
    )


@router.get("/tags")
async def tags_page(request: Request, principal: SignedIn, db: Database, notice: str = "") -> Response:
    return await _tags_page(request, principal, db, notice=NOTICES.get(notice))


@router.post("/tags")
async def create_tag(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    name: FormText = "",
    kind: FormText = TagKind.OTHER.value,
) -> Response:
    try:
        tag_kind = choice(TagKind, kind)
        if tag_kind is None:
            raise BadFormValue("Pick a kind for the tag.")
        await catalog_service.create_tag(db, principal.member, tag_kind, name, now=context.clock())
    except (InvalidCatalogInput, CatalogRuleViolation, BadFormValue) as exc:
        return await _tags_page(request, principal, db, error=str(exc), draft_name=name, status_code=400)
    return RedirectResponse("/catalog/tags?notice=tag_created", status_code=303)


@router.post("/tags/{tag_id}/rename")
async def rename_tag(
    request: Request, principal: AdminCSRFChecked, context: Context, db: Database, tag_id: int, name: FormText = ""
) -> Response:
    try:
        await catalog_service.rename_tag(db, principal.member, tag_id, name, now=context.clock())
    except (InvalidCatalogInput, CatalogRuleViolation) as exc:
        return await _tags_page(request, principal, db, error=str(exc), status_code=400)
    return RedirectResponse("/catalog/tags?notice=tag_renamed", status_code=303)


@router.post("/tags/{tag_id}/merge")
async def merge_tag(
    request: Request, principal: AdminCSRFChecked, context: Context, db: Database, tag_id: int, into: FormText = ""
) -> Response:
    try:
        if not into.isdigit():
            raise BadFormValue("Pick a tag to merge into.")
        await catalog_service.merge_tags(db, principal.member, tag_id, int(into), now=context.clock())
    except (CatalogRuleViolation, BadFormValue) as exc:
        return await _tags_page(request, principal, db, error=str(exc), status_code=400)
    return RedirectResponse("/catalog/tags?notice=tags_merged", status_code=303)


# Import -------------------------------------------------------------------------------------------


def _preview_signer(context: AppContext) -> SignedPayload:
    key = derive_key(context.settings.secret_key.get_secret_value(), "catalog-import")
    return SignedPayload(key, IMPORT_PREVIEW_LIFETIME, context.clock)


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


async def _import_page(
    request: Request,
    context: AppContext,
    principal: Principal,
    *,
    content: str = "",
    plan: ImportPlan | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    token = None
    if plan is not None and plan.ok and plan.has_changes:
        token = _preview_signer(context).dumps(
            {
                "sha": catalog_service.content_digest(content),
                "plan": plan.fingerprint(),
                "member": str(principal.member.id),
            }
        )
    return await render(
        request,
        "catalog_import.html",
        {
            "content": content,
            "plan": plan,
            "token": token,
            "notice": notice,
            "sorted_modes": sorted_modes,
            "max_megabytes": MAX_FILE_BYTES // 1_000_000,
        },
        status_code=status_code,
    )


@router.get("/import")
async def import_page(request: Request, principal: SignedIn, context: Context) -> Response:
    return await _import_page(request, context, principal)


async def _uploaded(upload: UploadFile | None) -> bytes | None:
    if upload is None or not upload.filename:
        return None
    return await upload.read(MAX_FILE_BYTES + 1)


@router.post("/import/preview")
async def preview_import(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    content: Annotated[str, Form()] = "",
    upload: UploadFile | None = None,
) -> Response:
    data = await _uploaded(upload)
    source: str | bytes = _normalized(content)
    if data is not None:
        source = data
        try:
            content = _normalized(data.decode("utf-8-sig")) if len(data) <= MAX_FILE_BYTES else ""
        except UnicodeDecodeError:
            content = ""
    else:
        content = _normalized(content)
    if data is None and not content.strip():
        return await _import_page(request, context, principal, notice="Paste a catalog file or choose one to upload.")
    plan = await catalog_service.preview_import(db, principal.household.id, source)
    return await _import_page(
        request, context, principal, content=content, plan=plan, status_code=200 if plan.ok else 400
    )


@router.post("/import/apply")
async def apply_import(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    content: Annotated[str, Form()] = "",
    token: Annotated[str, Form(max_length=1000)] = "",
) -> Response:
    """Apply what the person previewed. The browser re-sends the file; the signed token proves
    which file and plan they saw. The import is re-planned against the current catalog and
    refused if the outcome would differ from the preview."""
    content = _normalized(content)
    signed = _preview_signer(context).loads(token)
    seen = (
        signed is not None
        and signed.get("member") == str(principal.member.id)
        and signed.get("sha") == catalog_service.content_digest(content)
    )
    if signed is None or not seen:
        plan = await catalog_service.preview_import(db, principal.household.id, content)
        notice = "The file changed or the preview expired. Check this preview, then apply again."
        return await _import_page(
            request, context, principal, content=content, plan=plan, notice=notice, status_code=409
        )
    try:
        plan = await catalog_service.apply_import(
            db,
            principal.household.id,
            principal.member,
            content,
            now=context.clock(),
            expected_fingerprint=signed.get("plan"),
        )
    except catalog_service.ImportRejected as exc:
        return await _import_page(request, context, principal, content=content, plan=exc.plan, status_code=400)
    except catalog_service.ImportChanged as exc:
        return await _import_page(
            request, context, principal, content=content, plan=exc.plan, notice=str(exc), status_code=409
        )
    summary = plan.summary()
    location = (
        f"/catalog?notice=imported&created={summary['items_created']}"
        f"&updated={summary['items_updated']}&tags_created={summary['tags_created']}"
    )
    return RedirectResponse(location, status_code=303)
