# Contributing to Dinnerbell

Thanks for helping. Dinnerbell is licensed under [AGPL-3.0-or-later](LICENSE), and every contribution is accepted under the same license.

## Sign your commits (Developer Certificate of Origin)

Every commit must carry a `Signed-off-by` line. By adding it, you certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/): you wrote the change, or otherwise have the right to submit it under the project's license.

Add the line automatically with `-s`:

```sh
git commit -s -m "Explain what changed and why"
```

The line uses your configured `user.name` and `user.email`:

```
Signed-off-by: Your Name <you@example.com>
```

Merge commits need the line too. To sign off commits you already made on a branch, run `git rebase --signoff origin/main`, then push with `--force-with-lease`. The rebase also drops merge commits, which `main` doesn't accept anyway, because it requires linear history.

The `dco` check fails a pull request with any commit that lacks a `Signed-off-by` line for its author's email.

## Set up

You need Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
```

## Before you open a pull request

Run the same checks CI runs:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -n auto --cov
docker build -f Containerfile -t dinnerbell .
```

If you change `compose.yaml`, `compose.secrets.yaml`, `deploy/`, or an env example, also run `scripts/check-compose.sh` (it needs Docker and jq, and checks podman-compose too when it's installed) and `shellcheck deploy/dinnerbell-test deploy/check-secrets scripts/check-compose.sh`.

## Write tests that stay fast and parallel

CI runs the suite on every CPU with [pytest-xdist](https://pytest-xdist.readthedocs.io/) (`-n auto`), and the `check` job gates every merge, so keep each test quick and independent:

- **Never wait a fixed time.** Move time with the `clock` fixture (`FakeClock`) or a `now=` argument. When a test must wait for a background task, poll for the state it expects, and give up after a generous deadline, such as 10 seconds: a passing test returns as soon as the state holds, so only a failing test pays for the deadline.
- **Count work instead of timing it.** A test that asserts something finished within a time budget fails on a slow or busy runner. To show that code stops early, count the calls it makes instead, as `tests/test_catalog_file.py` does for the YAML loader.
- **Keep tests xdist-safe.** Each worker is a separate process with its own temporary directories. Write files only under `tmp_path`, never bind a fixed port, and never rely on another test's leftovers or on running in a particular order.
- **Take a database from the `migrated` fixture.** It copies a database that was migrated once per worker, which is much faster than migrating per test. Call `run_migrations` yourself only when the test is about migrations.

CI splits the suite into eight shards that run in parallel jobs, then merges their coverage in `check`. To run one shard yourself, run `uv run pytest $(uv run python -m tests.support.shard 2/8)` (shard 2 of 8); the coverage floor applies only to the merged total, so add `--cov-fail-under=0` to a single shard with `--cov`. To re-run CI, use "Re-run all jobs": re-running only a failed shard replaces that shard's coverage file, and `check` has to run again to merge it.

To debug a single test with `breakpoint()` or `--pdb`, run it without workers: `uv run pytest -n 0 tests/test_x.py::test_y`. To find slow tests, add `--durations=20`.

[CLAUDE.md](CLAUDE.md) lists the project conventions: layers and import rules, license headers, and the migration workflow. They apply to people and agents alike.

## Release

Releases follow [Semantic Versioning](https://semver.org/), and [CHANGELOG.md](CHANGELOG.md) lists every change. A merge to `main` publishes the `main` and short-SHA image tags. Only a plain `vX.Y.Z` tag publishes a release, as `X.Y.Z`, `X.Y`, and `latest`; there are no pre-release tags. Cut a patch release when the nightly image scan fails, and at least monthly otherwise.

1. On a branch, open a release pull request titled "Release X.Y.Z" that changes only these:
   - `__version__` in `src/dinnerbell/__init__.py`.
   - `version` in `pyproject.toml`, then run `uv lock` to update the version recorded in `uv.lock`.
   - The default tag in `compose.yaml`: `${DINNERBELL_TAG:-X.Y.Z}`. Production checks out the release tag, so its `compose.yaml` always names the matching image.
   - `CHANGELOG.md`: retitle `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, check that it has **Upgrade notes**, add a new empty `[Unreleased]` section above it, add the link `[X.Y.Z]: https://github.com/dsmorgan/dinnerbell/releases/tag/vX.Y.Z` at the bottom, and point `[Unreleased]` at `https://github.com/dsmorgan/dinnerbell/compare/vX.Y.Z...HEAD`.
2. Merge it once CI passes.
3. Tag the release commit on `main`, and push the tag. Check that `git log -1` shows the release commit before you tag. CI builds, scans, publishes, and attests the image.

   ```sh
   git switch main && git pull
   git tag -a vX.Y.Z -m "Dinnerbell X.Y.Z"
   git push origin vX.Y.Z
   gh run watch "$(gh run list --branch vX.Y.Z --limit 1 --json databaseId --jq '.[0].databaseId')"
   ```

4. Verify the published image. Copy its digest (`sha256:...`) from the `publish` job summary of the tag's CI run.

   ```sh
   gh attestation verify oci://ghcr.io/dsmorgan/dinnerbell:X.Y.Z \
     --repo dsmorgan/dinnerbell \
     --signer-workflow dsmorgan/dinnerbell/.github/workflows/ci.yml \
     --source-ref refs/tags/vX.Y.Z
   ```

   A release's digest never changes. Never re-run a release's workflow or move its tag; `publish` refuses to replace a published release. If the tag's run fails, or something is wrong with the release, cut X.Y.Z+1. The `scan-published` job is an alarm, not a gate: it scans the image after it's pushed, and a finding there also calls for X.Y.Z+1.

5. Download the install bundle (`dinnerbell-X.Y.Z.tar.gz` and its `.sha256`, see [docs/install.md](docs/install.md)) that the tag's `release-bundle` job attested, and verify it.

   ```sh
   bundle=$(mktemp -d)
   run=$(gh run list --repo dsmorgan/dinnerbell --workflow ci.yml --branch vX.Y.Z --event push --limit 1 \
     --json databaseId --jq '.[0].databaseId')
   gh run download "$run" --repo dsmorgan/dinnerbell --name dinnerbell-X.Y.Z --dir "$bundle"
   (cd "$bundle" && shasum -a 256 -c dinnerbell-X.Y.Z.tar.gz.sha256)
   gh attestation verify "$bundle/dinnerbell-X.Y.Z.tar.gz" \
     --repo dsmorgan/dinnerbell \
     --signer-workflow dsmorgan/dinnerbell/.github/workflows/ci.yml \
     --source-ref refs/tags/vX.Y.Z
   ```

6. Create the GitHub release from the changelog section, with the digest appended and the bundle attached. Releases are immutable, so attach the bundle now: nothing can be added after `gh release create` publishes it. If the bundle is missing or doesn't verify, don't publish the release; cut X.Y.Z+1.

   ```sh
   notes="${TMPDIR:-/tmp}/dinnerbell-X.Y.Z.md"
   awk '/^## \[X.Y.Z\]/{f=1; next} /^## /{f=0} f' CHANGELOG.md > "$notes"
   printf '\n### Image\n\n`ghcr.io/dsmorgan/dinnerbell:X.Y.Z@sha256:...`\n' >> "$notes"
   gh release create vX.Y.Z --title "X.Y.Z" --notes-file "$notes" \
     "$bundle/dinnerbell-X.Y.Z.tar.gz" "$bundle/dinnerbell-X.Y.Z.tar.gz.sha256"
   ```

7. Promote the release to production with the checklist in [docs/test-instance.md](docs/test-instance.md#promote-a-release).

## Update the vendored htmx

HTMX is vendored at `src/dinnerbell/web/static/htmx-X.Y.Z.min.js`, and `tests/test_vendored_assets.py` checks its SHA-256 against the npm release, so a changed file fails CI. To update it:

```sh
cd "$(mktemp -d)" && npm pack htmx.org@X.Y.Z && tar xzf htmx.org-X.Y.Z.tgz
shasum -a 256 package/dist/htmx.min.js
```

Copy `package/dist/htmx.min.js` to `src/dinnerbell/web/static/htmx-X.Y.Z.min.js` and delete the old file. Then change the version in `HTMX_FILE` (`src/dinnerbell/web/render.py`), the version and hash in `tests/test_vendored_assets.py`, the paths in `tests/test_csrf_and_headers.py`, and the line in CLAUDE.md. Never edit the file by hand.

## Report a security problem

Don't open a public issue. Follow [SECURITY.md](SECURITY.md).
