# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository rules that keep the codebase maintainable and safe (see CLAUDE.md)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route

from dinnerbell.web.auth import csrf_if_signed_in, require_admin, require_admin_csrf, require_csrf

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "dinnerbell"
SPDX = "SPDX-License-Identifier: AGPL-3.0-or-later"

# layer -> layers it may import (spec section 13.2). config and logs are shared by all layers;
# app_factory (the composition root) and main sit above every layer and nothing imports them.
ALLOWED = {
    "domain": set(),
    "db": {"domain"},
    "services": {"domain", "db"},
    "channels": {"domain", "services"},
    "web": {"domain", "services"},
    "scheduler": {"domain", "services"},
}
TOP_LEVEL = {"app_factory", "main"}
LAYERS = set(ALLOWED)


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("layer", sorted(LAYERS))
def test_layer_imports(layer: str) -> None:
    for path in (PACKAGE / layer).rglob("*.py"):
        for module in imports(path):
            parts = module.split(".")
            assert not (len(parts) > 1 and parts[0] == "dinnerbell" and parts[1] in TOP_LEVEL), (
                f"{path.relative_to(ROOT)} imports the composition root {module}"
            )
            if parts[0] != "dinnerbell" or len(parts) < 2 or parts[1] not in LAYERS or parts[1] == layer:
                continue
            assert parts[1] in ALLOWED[layer], f"{path.relative_to(ROOT)} imports {module}"


def test_domain_has_no_io_dependencies() -> None:
    forbidden = {"sqlalchemy", "fastapi", "starlette", "httpx", "apscheduler", "sqlite3", "os", "pathlib", "logging"}
    for path in (PACKAGE / "domain").rglob("*.py"):
        for module in imports(path):
            assert module.split(".")[0] not in forbidden, f"{path.name} imports {module}"
    for path in (PACKAGE / "domain").rglob("*.py"):
        assert "dinnerbell.config" not in imports(path)


def test_only_services_and_db_touch_the_orm() -> None:
    for layer in ("web", "scheduler", "channels"):
        for path in (PACKAGE / layer).rglob("*.py"):
            for module in imports(path):
                assert not module.startswith("dinnerbell.db.models"), f"{path.name} imports {module}"
                assert not module.startswith("dinnerbell.db.repositories"), f"{path.name} imports {module}"


def source_files() -> list[Path]:
    files = [
        *PACKAGE.rglob("*.py"),
        *PACKAGE.rglob("*.html"),
        *PACKAGE.rglob("*.css"),
        *PACKAGE.rglob("*.svg"),
        *(ROOT / "tests").rglob("*.py"),
    ]
    files.append(PACKAGE / "db" / "alembic" / "script.py.mako")
    return files


def test_every_source_file_has_spdx_header() -> None:
    missing = [str(p.relative_to(ROOT)) for p in source_files() if SPDX not in p.read_text().splitlines()[0]]
    assert missing == []


SAFE = {"GET", "HEAD", "OPTIONS"}
CSRF_CHECKS = {require_csrf, require_admin_csrf, csrf_if_signed_in}
ADMIN_CHECKS = {require_admin, require_admin_csrf}


def audit_routes(routes: list[Any]) -> tuple[list[APIRoute], list[str]]:
    """Flatten the app's routes. Anything the CSRF audit can't see into is reported as a problem:
    routes added with add_route, unknown mounted sub-apps, and websockets."""
    api: list[APIRoute] = []
    problems: list[str] = []
    for route in routes:
        if isinstance(route, APIRoute):
            api.append(route)
        elif hasattr(route, "original_router"):  # FastAPI includes routers lazily
            nested, nested_problems = audit_routes(route.original_router.routes)
            api += nested
            problems += nested_problems
        elif isinstance(route, Mount) and route.path == "/static" and isinstance(route.app, StaticFiles):
            continue  # serves files only; StaticFiles answers GET and HEAD
        elif isinstance(route, Route) and route.methods is not None and route.methods <= SAFE:
            if route.path.startswith("/admin"):
                # Even a GET-only admin page must be an APIRoute, or the admin check can't be seen.
                problems.append(f"Route {route.path} is an admin route the audit can't inspect")
        else:
            problems.append(f"{type(route).__name__} {getattr(route, 'path', '?')} can't be audited for CSRF")
    return api, problems


def test_every_state_changing_route_checks_csrf(client: TestClient) -> None:
    api, problems = audit_routes(client.app.routes)  # type: ignore[attr-defined]
    assert problems == []
    unsafe = [route for route in api if not route.methods <= SAFE]
    for route in unsafe:
        calls = {dep.call for dep in route.dependant.dependencies}
        assert calls & CSRF_CHECKS, f"{route.path} has no CSRF check"
    assert len(unsafe) >= 5


def test_every_admin_route_requires_an_admin_in_the_web_layer(client: TestClient) -> None:
    api, problems = audit_routes(client.app.routes)  # type: ignore[attr-defined]
    assert problems == []
    # Admin actions outside /admin, such as /events/{id}/admin/cancel, count too.
    admin = [route for route in api if route.path.startswith("/admin") or "/admin/" in route.path]
    assert admin
    for route in admin:
        calls = {dep.call for dep in route.dependant.dependencies}
        assert calls & ADMIN_CHECKS, f"{route.path} relies on the service layer alone"


def test_route_audit_flags_what_it_cannot_see(client: TestClient) -> None:
    from starlette.applications import Starlette

    async def raw(_request: Any) -> Any:
        return None

    app = client.app  # type: ignore[attr-defined]
    app.add_route("/raw-post", raw, methods=["POST"])
    app.add_route("/admin/raw-page", raw, methods=["GET"])
    app.mount("/sub", Starlette())
    _, problems = audit_routes(app.routes)
    assert any("/raw-post" in problem for problem in problems)
    assert any("/admin/raw-page" in problem for problem in problems)
    assert any("/sub" in problem for problem in problems)


def test_fake_provider_is_not_in_the_package() -> None:
    for path in PACKAGE.rglob("*.py"):
        assert "tests" not in {m.split(".")[0] for m in imports(path)}, path
    assert not list(PACKAGE.rglob("fake_oidc*"))


def test_containerignore_excludes_tests() -> None:
    ignore = (ROOT / ".containerignore").read_text().splitlines()
    assert "tests" in ignore or "tests/" in ignore


def test_tag_rename_and_merge_require_an_admin_in_the_web_layer(client: TestClient) -> None:
    """Admin-only actions outside /admin need the same web-layer check (spec 3, FR-25)."""
    api, _ = audit_routes(client.app.routes)  # type: ignore[attr-defined]
    admin_only = {"/catalog/tags/{tag_id}/rename", "/catalog/tags/{tag_id}/merge"}
    found = {route.path: route for route in api if route.path in admin_only}
    assert set(found) == admin_only
    for route in found.values():
        assert require_admin_csrf in {dep.call for dep in route.dependant.dependencies}, route.path
