# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository rules that keep the codebase maintainable and safe (see CLAUDE.md)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute

from dinnerbell.web.auth import csrf_if_signed_in, require_csrf

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
    files = [*PACKAGE.rglob("*.py"), *PACKAGE.rglob("*.html"), *PACKAGE.rglob("*.css"), *(ROOT / "tests").rglob("*.py")]
    files.append(PACKAGE / "db" / "alembic" / "script.py.mako")
    return files


def test_every_source_file_has_spdx_header() -> None:
    missing = [str(p.relative_to(ROOT)) for p in source_files() if SPDX not in p.read_text().splitlines()[0]]
    assert missing == []


def api_routes(routes: list[Any]) -> list[APIRoute]:
    """Flatten included routers (FastAPI includes them lazily)."""
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            found.extend(api_routes(route.original_router.routes))
    return found


def test_every_state_changing_route_checks_csrf(client: object) -> None:
    from fastapi import FastAPI

    app: FastAPI = client.app  # type: ignore[attr-defined]
    checked = 0
    for route in api_routes(app.routes):
        if route.methods <= {"GET", "HEAD", "OPTIONS"}:
            continue
        calls = {dep.call for dep in route.dependant.dependencies}
        assert calls & {require_csrf, csrf_if_signed_in}, f"{route.path} has no CSRF check"
        checked += 1
    assert checked >= 5


def test_fake_provider_is_not_in_the_package() -> None:
    for path in PACKAGE.rglob("*.py"):
        assert "tests" not in {m.split(".")[0] for m in imports(path)}, path
    assert not list(PACKAGE.rglob("fake_oidc*"))


def test_containerignore_excludes_tests() -> None:
    ignore = (ROOT / ".containerignore").read_text().splitlines()
    assert "tests" in ignore or "tests/" in ignore
