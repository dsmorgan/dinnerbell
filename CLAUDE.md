# Working on Dinnerbell

Read `docs/spec.md` (especially section 13) before changing anything. Decisions live in `docs/adr/`.

## Commands

```sh
uv sync                          # install, including dev tools
uv run pytest                    # tests
uv run pytest -n auto --cov      # tests on every CPU, with coverage (CI fails under 90%)
uv run ruff check . && uv run ruff format --check .
uv run mypy                      # strict, whole package
uv run --env-file .env dinnerbell serve   # run locally (needs a .env; see .env.example). Secrets in .env work, with a warning; with compose.secrets.yaml they are files in /run/secrets
docker build -f Containerfile -t dinnerbell .
```

## Releases and repository settings

- The release recipe is in [CONTRIBUTING.md](CONTRIBUTING.md#release). A release pull request bumps `__version__` in `src/dinnerbell/__init__.py`, `version` in `pyproject.toml` (then `uv lock`), and the default tag in `compose.yaml`, and retitles `[Unreleased]` in `CHANGELOG.md`. Every release has **Upgrade notes**. Push a `vX.Y.Z` tag only when the owner says to release. Verify with `gh attestation verify oci://ghcr.io/dsmorgan/dinnerbell:X.Y.Z --repo dsmorgan/dinnerbell --signer-workflow dsmorgan/dinnerbell/.github/workflows/ci.yml --source-ref refs/tags/vX.Y.Z`, and record the digest from the `publish` job summary in the release notes. Releases are immutable, so the install bundle goes in at creation: `gh run download` the tag run's `dinnerbell-X.Y.Z` artifact, check its `.sha256`, `gh attestation verify` the tarball with the same `--repo`, `--signer-workflow`, and `--source-ref`, and pass both files to `gh release create`. CI never writes to a release. Never re-run a release's workflow or move its tag; cut X.Y.Z+1 instead.
- Changes under `[Unreleased]` in `CHANGELOG.md` go in the same pull request as the change.
- Agents never pass `--admin` to `gh` (for example, `gh pr merge --admin`), and never change repository settings without the owner's approval: rulesets, branch protection, Actions policy, security settings, secrets, or package settings.

## Layers (spec 13.2)

`src/dinnerbell/` has one package per layer. `tests/test_architecture.py` enforces the import rules.

| Layer | May import | Notes |
|---|---|---|
| `domain` | nothing else in Dinnerbell | Pure Python. No I/O, no SQLAlchemy, no logging, no clock reads: pass `now` in. |
| `db` | `domain` | Models, Alembic, repositories. Repositories return domain entities. |
| `services` | `domain`, `db` | Use cases. The only layer that writes; each use case commits its own transaction. |
| `channels` | `domain`, `services` | `Channel` protocol now; Slack adapter in M2. |
| `scheduler` | `domain`, `services` | Jobs must be idempotent. |
| `web` | `domain`, `services` | FastAPI routes, templates, auth. Never imports `db` or `scheduler`. |

`config.py` and `logs.py` are shared by every layer. `app_factory.py` is the composition root: the only module that wires `web` and `scheduler` together (they share state through `services`, such as `services.health.SchedulerState`). `main.py` is the CLI. No layer imports `app_factory` or `main`.

## Conventions

- Every source file starts with `# SPDX-License-Identifier: AGPL-3.0-or-later` (`{#- ... -#}` in templates, `/* ... */` in CSS). A test checks this.
- Dependencies must be AGPL-compatible (MIT, BSD, Apache-2.0, LGPL, MPL-2.0, 0BSD). Prefer the standard library.
- Times are UTC-aware `datetime`s everywhere; `UTCDateTime` handles SQLite. Get "now" from the injected clock.
- Log member IDs, never emails, tokens, or secrets (SEC-5). Secrets are `SecretStr`.
- Pages: no inline `<script>`, `style=`, or `on*=` attributes; the CSP forbids them. HTMX is vendored at `web/static/htmx-2.0.11.min.js` (npm `htmx.org@2.0.11`, sha256 `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`).
- Every state-changing route depends on `require_csrf`, `require_admin_csrf` (admin routes), or `csrf_if_signed_in` (sign-out). Every `/admin` route checks for an admin in the web layer too, not only in the service. Add routes with routers, never `add_route` or `mount`; `tests/test_architecture.py` fails on anything it can't audit.
- Every admin action writes an audit row (SEC-9).

## Migrations

1. Change `src/dinnerbell/db/models.py`. Every table gets `household_id`.
2. Generate a revision against a scratch database:
   ```sh
   uv run alembic upgrade head
   uv run alembic revision --autogenerate -m "describe the change"
   rm migrations-scratch.db
   ```
3. Rename the file and its `revision` to the next number (`0002_...`), review it, and add the SPDX header.
4. `tests/test_migrations.py::test_models_match_migrations` fails if models and migrations drift.

The app runs migrations on `dinnerbell serve` and `dinnerbell migrate`, and refuses to start on a revision it doesn't know.

## Tests

- Tests run with production security: HTTPS base URL, `Secure` `__Host-` cookies, CSRF tokens, Origin checks. **Never weaken auth, CSRF, or headers to make a test pass**; send what a browser sends instead (see `tests/conftest.py`).
- The fake OIDC provider lives in `tests/support/fake_oidc.py`. It must never be imported from `src/` or shipped in the image.
- Use `FakeClock` for anything time-dependent. Never wait a fixed time; tests run in parallel with pytest-xdist, so keep them xdist-safe (see [CONTRIBUTING.md](CONTRIBUTING.md#write-tests-that-stay-fast-and-parallel)).
