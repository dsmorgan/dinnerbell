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
uv run pytest --cov
docker build -f Containerfile -t dinnerbell .
```

If you change `compose.yaml`, `deploy/dinnerbell-test`, or an env example, also run the checks in the `compose` CI job.

[CLAUDE.md](CLAUDE.md) lists the project conventions: layers and import rules, license headers, and the migration workflow. They apply to people and agents alike.

## Release

Releases follow [Semantic Versioning](https://semver.org/), and [CHANGELOG.md](CHANGELOG.md) lists every change. A merge to `main` publishes the `main` and short-SHA image tags. Only a plain `vX.Y.Z` tag publishes a release, as `X.Y.Z`, `X.Y`, and `latest`; there are no pre-release tags. Cut a patch release when the nightly image scan fails, and at least monthly otherwise.

1. On a branch, open a release pull request titled "Release X.Y.Z" that changes only these:
   - `__version__` in `src/dinnerbell/__init__.py`.
   - `version` in `pyproject.toml`, then run `uv lock` to update the version recorded in `uv.lock`.
   - The default tag in `compose.yaml`: `${DINNERBELL_TAG:-X.Y.Z}`. This moves production to the release on `git pull`.
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

   A release's digest never changes. Never re-run a release's workflow or move its tag; if something is wrong, cut X.Y.Z+1.

5. Create the GitHub release from the changelog section, with the digest appended.

   ```sh
   notes="${TMPDIR:-/tmp}/dinnerbell-X.Y.Z.md"
   awk '/^## \[X.Y.Z\]/{f=1; next} /^## /{f=0} f' CHANGELOG.md > "$notes"
   printf '\n### Image\n\n`ghcr.io/dsmorgan/dinnerbell:X.Y.Z@sha256:...`\n' >> "$notes"
   gh release create vX.Y.Z --title "X.Y.Z" --notes-file "$notes"
   ```

6. Promote the release to production with the checklist in [docs/setup.md](docs/setup.md#promote-a-release).

## Report a security problem

Don't open a public issue. Follow [SECURITY.md](SECURITY.md).
