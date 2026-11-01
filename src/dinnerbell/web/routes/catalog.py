# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catalog: browse, add, edit, and deactivate items; manage tags; import and export catalog files."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.responses import Response

from dinnerbell.domain.catalog import (
    DEFAULT_MEALS,
    MEAL_ORDER,
    CatalogItem,
    CatalogRuleViolation,
    InvalidCatalogInput,
    ItemFilter,
    ItemInput,
    ItemKind,
    Meal,
    Mode,
    Status,
    TagKind,
    sorted_meals,
    sorted_modes,
    tags_by_kind,
)
from dinnerbell.domain.catalog_file import MAX_FILE_BYTES
from dinnerbell.domain.catalog_import import ImportPlan
from dinnerbell.domain.pages import page_of
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services import feedback as feedback_service
from dinnerbell.services.variety import household_today
from dinnerbell.web.auth import AdminCSRFChecked, Context, CSRFChecked, Database, Principal, SignedIn
from dinnerbell.web.context import AppContext, app_context
from dinnerbell.web.forms import BadFormValue, RowId, choice, optional_int, page_number, parse_id, split_names
from dinnerbell.web.redirects import local_path, local_redirect
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
ITEMS_PER_PAGE = 20
"""The Catalog page's page size: enough to scan, short enough to scroll on a phone."""
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
    meal: str = "",
    tag: str = "",
    status: str = "",
    notice: str = "",
    created: int = 0,
    updated: int = 0,
    tags_created: int = 0,
    page: str = "1",
) -> Response:
    """The catalog, ``ITEMS_PER_PAGE`` matches at a time. The page links keep the search, a page
    past either end shows the nearest one, and a page that isn't a number shows the first."""
    data = await catalog_service.catalog(db, principal.member)
    tag_id = parse_id(tag)
    search = ItemFilter(
        query=q[:100],
        kind=choice(ItemKind, kind),
        mode=choice(Mode, mode),
        meal=choice(Meal, meal),
        tag_id=tag_id,
        status=choice(Status, status) or Status.ACTIVE,
    )
    names = {t.id: t.name for t in data.tags}
    matches = [item for item in data.items if search.matches(item, names)]
    shown = page_of(page_number(page), len(matches), ITEMS_PER_PAGE)
    items = matches[shown.offset : shown.offset + shown.size]
    kept = {
        "q": search.query,
        "kind": search.kind.value if search.kind else "",
        "mode": search.mode.value if search.mode else "",
        "meal": search.meal.value if search.meal else "",
        "tag": str(search.tag_id or ""),
        "status": "" if search.status is Status.ACTIVE else search.status.value,
    }
    kept = {name: value for name, value in kept.items() if value}  # the search as understood, for page links

    def page_url(number: int) -> str:
        query = urlencode(kept | ({"page": number} if number > 1 else {}))
        return f"/catalog?{query}" if query else "/catalog"

    today = household_today(principal.household, context.clock())
    message = NOTICES.get(notice)
    if notice == "imported":
        message = f"Imported: {created} added, {updated} updated, {tags_created} new tags."
    return await render(
        request,
        "catalog.html",
        {
            "items": items,
            "matches": len(matches),
            "page": shown,
            "page_url": page_url,
            "total": len([i for i in data.items if i.active]),
            "search": search,
            "tag_groups": tags_by_kind(data.tags),
            "tags": {t.id: t for t in data.tags},
            "members": {m.id: m for m in data.members},
            "eaten": {i.id: last_eaten_label(data.last_eaten.get(i.id), today) for i in items},
            "hints": await feedback_service.item_hints(db, principal.member, today=today),
            "sorted_modes": sorted_modes,
            "sorted_meals": sorted_meals,
            "kinds": list(ItemKind),
            "modes": list(Mode),
            "meals": MEAL_ORDER,
            "statuses": list(Status),
            "notice": message,
        },
    )


async def _listed_path(db: Database, principal: Principal, item_id: int, notice: str) -> str:
    """The catalog page that lists the item with no search, scrolled to it, with ``notice`` (a
    ``NOTICES`` key). An inactive item isn't listed there, so that's the first page."""
    data = await catalog_service.catalog(db, principal.member)
    names = {t.id: t.name for t in data.tags}
    listed = [item.id for item in data.items if ItemFilter().matches(item, names)]
    page = listed.index(item_id) // ITEMS_PER_PAGE + 1 if item_id in listed else 1
    query = f"page={page}&notice={notice}" if page > 1 else f"notice={notice}"
    return local_path(f"/catalog?{query}#item-{{}}", item_id)


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
    meals: set[str] = field(default_factory=lambda: {m.value for m in DEFAULT_MEALS})
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
            meals={m.value for m in item.meals},
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
        meals = {choice(Meal, m) for m in self.meals}
        if None in meals:
            raise BadFormValue("Pick breakfast, lunch, dinner, or a mix.")
        return ItemInput(
            kind=kind,
            name=self.name,
            modes=frozenset(m for m in modes if m is not None),
            meals=frozenset(m for m in meals if m is not None),
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
    return {i for v in values or [] if (i := parse_id(v)) is not None}


def posted_item(
    name: FormText = "",
    tag_ids: FormList = None,
    new_cuisines: FormText = "",
    new_tags: FormText = "",
    new_tags_kind: FormText = TagKind.OTHER.value,
    modes: FormList = None,
    meals: FormList = None,
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
        meals=set(meals or []),
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
    today = household_today(principal.household, app_context(request).clock())
    hints = await feedback_service.item_hints(db, principal.member, [item.id], today=today) if item else {}
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
            "meals": MEAL_ORDER,
            "hint": hints.get(item.id) if item else None,
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
    return local_redirect(await _listed_path(db, principal, item.id, "created"))


@router.get("/items/{item_id}")
async def edit_item_page(
    request: Request, principal: SignedIn, db: Database, item_id: RowId, notice: str = ""
) -> Response:
    item = await catalog_service.get_item(db, principal.member, item_id)
    form = ItemForm.from_item(item)
    return await _item_page(request, principal, db, kind=item.kind, form=form, item=item, notice=NOTICES.get(notice))


@router.post("/items/{item_id}")
async def update_item(
    request: Request, principal: CSRFChecked, context: Context, db: Database, item_id: RowId, form: PostedItem
) -> Response:
    item = await catalog_service.get_item(db, principal.member, item_id)
    try:
        await catalog_service.update_item(db, principal.member, item_id, form.to_input(item.kind), now=context.clock())
    except (InvalidCatalogInput, CatalogRuleViolation, BadFormValue) as exc:
        return await _item_page(
            request, principal, db, kind=item.kind, form=form, item=item, error=str(exc), status_code=400
        )
    return local_redirect(await _listed_path(db, principal, item_id, "updated"))


@router.post("/items/{item_id}/deactivate")
async def deactivate_item(principal: CSRFChecked, context: Context, db: Database, item_id: RowId) -> Response:
    await catalog_service.set_item_active(db, principal.member, item_id, False, now=context.clock())
    return local_redirect("/catalog/items/{}?notice=deactivated", item_id)


@router.post("/items/{item_id}/reactivate")
async def reactivate_item(principal: CSRFChecked, context: Context, db: Database, item_id: RowId) -> Response:
    await catalog_service.set_item_active(db, principal.member, item_id, True, now=context.clock())
    return local_redirect("/catalog/items/{}?notice=reactivated", item_id)


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
    request: Request, principal: AdminCSRFChecked, context: Context, db: Database, tag_id: RowId, name: FormText = ""
) -> Response:
    try:
        await catalog_service.rename_tag(db, principal.member, tag_id, name, now=context.clock())
    except (InvalidCatalogInput, CatalogRuleViolation) as exc:
        return await _tags_page(request, principal, db, error=str(exc), status_code=400)
    return RedirectResponse("/catalog/tags?notice=tag_renamed", status_code=303)


@router.post("/tags/{tag_id}/merge")
async def merge_tag(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    tag_id: RowId,
    into: FormText = "",
    confirm: FormText = "",
) -> Response:
    try:
        if not confirm:
            raise BadFormValue("Tick the box to confirm the merge. Merging can't be undone.")
        target = parse_id(into)
        if target is None:
            raise BadFormValue("Pick a tag to merge into.")
        await catalog_service.merge_tags(db, principal.member, tag_id, target, now=context.clock())
    except (CatalogRuleViolation, BadFormValue) as exc:
        # The merge form is boosted for its confirm prompt; HTMX swaps only 2xx responses.
        status = 200 if request.headers.get("HX-Boosted") == "true" else 400
        return await _tags_page(request, principal, db, error=str(exc), status_code=status)
    return RedirectResponse("/catalog/tags?notice=tags_merged", status_code=303)


# Export -------------------------------------------------------------------------------------------


@router.get("/export.yaml")
async def export_catalog(principal: SignedIn, context: Context, db: Database) -> Response:
    """Download the catalog as a catalog file. A GET that changes nothing, so it needs sign-in but
    no CSRF token. The file holds no personal data, but it's still never cached."""
    exported = await catalog_service.export(db, principal.household.id)
    day = household_today(principal.household, context.clock())
    return Response(
        exported.data,
        media_type="application/yaml; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="dinnerbell-catalog-{day.isoformat()}.yaml"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


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
            "max_file_bytes": MAX_FILE_BYTES,
        },
        status_code=status_code,
    )


@router.get("/import")
async def import_page(request: Request, principal: SignedIn, context: Context) -> Response:
    return await _import_page(request, context, principal)


IMPORT_PART_BYTES = 2_100_000
"""A pasted 1 MB file can grow when the browser turns each line break into CRLF."""


@dataclass(frozen=True, slots=True)
class ImportPost:
    content: str
    upload: bytes | None
    token: str


async def import_post(request: Request, principal: SignedIn) -> AsyncIterator[ImportPost]:
    """Read the import form, but only for a signed-in member.

    The import routes declare no ``Form`` parameters, because FastAPI would parse those before
    any dependency ran. Depending on ``SignedIn`` first means an anonymous body is never read,
    and the larger part limit lets a pasted file grow by CRLF line breaks and still reach the
    domain's friendly size check instead of a bare 400.
    """
    form = await request.form(max_files=1, max_fields=8, max_part_size=IMPORT_PART_BYTES)
    try:
        content, token, upload = form.get("content"), form.get("token"), form.get("upload")
        data = None
        if isinstance(upload, StarletteUploadFile) and upload.filename:
            data = await upload.read(MAX_FILE_BYTES + 1)
        yield ImportPost(
            content=_normalized(content) if isinstance(content, str) else "",
            upload=data,
            token=token if isinstance(token, str) and len(token) <= 1000 else "",
        )
    finally:
        await form.close()


ImportForm = Annotated[ImportPost, Depends(import_post)]
IMPORT_FORM_FIRST = Depends(import_post)
"""Declared on the route, so FastAPI runs it before every parameter dependency. The CSRF check
also reads the form (browsers send the token as a field), and Starlette caches whichever read
comes first; this makes sure that read is the bounded one, whatever the parameter order. The
``form`` parameter then reuses the cached result."""


@router.post("/import/preview", dependencies=[IMPORT_FORM_FIRST])
async def preview_import(
    request: Request, principal: CSRFChecked, form: ImportForm, context: Context, db: Database
) -> Response:
    source: str | bytes = form.content
    content = form.content
    if form.upload is not None:
        source = form.upload
        try:
            content = _normalized(form.upload.decode("utf-8-sig")) if len(form.upload) <= MAX_FILE_BYTES else ""
        except UnicodeDecodeError:
            content = ""
    elif not content.strip():
        return await _import_page(request, context, principal, notice="Paste a catalog file or choose one to upload.")
    plan = await catalog_service.preview_import(db, principal.household.id, source)
    return await _import_page(
        request, context, principal, content=content, plan=plan, status_code=200 if plan.ok else 400
    )


@router.post("/import/apply", dependencies=[IMPORT_FORM_FIRST])
async def apply_import(
    request: Request, principal: CSRFChecked, form: ImportForm, context: Context, db: Database
) -> Response:
    """Apply what the person previewed. The browser re-sends the file; the signed token proves
    which file and plan they saw. The import is re-planned against the current catalog and
    refused if the outcome would differ from the preview."""
    content = form.content
    signed = _preview_signer(context).loads(form.token)
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
    return local_redirect(
        "/catalog?notice=imported&created={}&updated={}&tags_created={}",
        summary["items_created"],
        summary["items_updated"],
        summary["tags_created"],
    )
