# Harden your install

Dinnerbell faces the internet from your home network, so treat the host and your GitHub account as part of the app. This checklist assumes a working install from [setup.md](setup.md). It lists what you set up yourself, and what the repository's CI checks for you.

## Secure your GitHub account

If you maintain a fork or cut releases, an attacker who takes over your GitHub account can publish an image your host pulls.

1. Use a passkey or a hardware security key as your two-factor method, and remove SMS as a fallback.
2. Watch the repository with **Custom** > **Security alerts**, to hear about Dependabot alerts, code scanning alerts, and private vulnerability reports.
3. In **Settings** > **Notifications** > **System** > **Actions**, select **Email** and **Only notify for failed workflows**, so a failed nightly image scan reaches your inbox.

GitHub sends a scheduled workflow's failure notice to the person who last changed its `cron` line. If someone else edits the schedule in `scan-release.yml`, `pip-audit.yml`, or `scorecard.yml`, the notices go to them.

## Harden the host

- **Run rootless podman under a dedicated user.** Create a user that owns nothing else, such as `dinnerbell`, give it subordinate UID and GID ranges in `/etc/subuid` and `/etc/subgid`, and run `loginctl enable-linger dinnerbell` so that its containers keep running after you log out. Install and run Dinnerbell as that user.
- **Lock down `.env`.** It holds the secret key and the Slack tokens.

  ```sh
  chown dinnerbell: .env
  chmod 600 .env
  ```

- **Keep backups off the container volume.** The nightly backups live next to the database, so losing the volume loses both. Copy `/data/backups/` to another disk or host, as in [Back up](setup.md#back-up).
- **Pin a release.** Run a specific `X.Y.Z` image tag, never `latest` or `main`, and don't use `podman auto-update`, which follows moving tags. Upgrade on purpose, after you read the upgrade notes. After you pull, [verify the image](setup.md#verify-an-image) by the digest the host holds, and consider pinning `image:` to that digest in `compose.override.yaml`. The test instance follows the moving `main` tag, so it never runs a verified release.
- **Bind only to the interface Newt reaches.** Set `DINNERBELL_BIND` in `.env` to that interface's address, never `0.0.0.0`, so that your reverse proxy is the only way in. To confirm, run `ss -ltn | grep 8013` and check the listening address.
- **Keep the compose hardening flags.** `compose.yaml` runs the container with `read_only`, `cap_drop: ALL`, `no-new-privileges`, a size-capped `/tmp`, a process limit, a memory limit, and rotated logs. Don't remove them in `compose.override.yaml`.

## Monitor it

Add two monitors in Uptime Kuma, or any monitor that can match a keyword:

| Monitor | Target | Alerts when |
|---|---|---|
| HTTP | `https://<your hostname>/healthz` | The response isn't 200. Dinnerbell returns 503 when the database is unreachable or the scheduler hasn't ticked for 5 minutes. |
| HTTP keyword | `https://<your hostname>/healthz`, keyword `"status":"ok","version"` | Dinnerbell reports `degraded`, with HTTP 200: a late scheduler tick, a stuck call, a notification waiting over an hour, a failed backup, or a Slack problem. |

The keyword includes `"version"` because the scheduler's own `"status":"ok"` would otherwise match. In Uptime Kuma, an **HTTP(s) - Json Query** monitor with the query `status` and the expected value `ok` works too. [Check /healthz](troubleshooting.md#check-healthz) explains each key.

## What CI enforces

The `main` ruleset requires a pull request and these checks before a merge. An admin can bypass them only through a pull request, for emergencies. Agents never pass `--admin`.

| Check | Workflow | What it does | Required to merge |
|---|---|---|---|
| `check` | `ci.yml` | Ruff, strict mypy, pytest with at least 90% coverage, and `pip-audit` on the locked dependencies. | Yes |
| `image` | `ci.yml` | Builds the image, runs a smoke test against `/healthz`, and scans it with Grype. | Yes |
| `dependency-review` | `dependency-review.yml` | Fails a PR that adds a dependency with a known vulnerability of moderate severity or worse. | Yes |
| `dco` | `dco.yml` | Fails a PR with a commit, merge commits included, that lacks a `Signed-off-by` trailer for its author's email. Only Dependabot's own commits in its own PRs are exempt. See [CONTRIBUTING.md](../CONTRIBUTING.md). | Yes |
| CodeQL | Default setup | Scans Python, JavaScript, and the workflows. | Yes |
| Release scan | `scan-release.yml` | Scans the image tagged `latest` with Grype every night. A fixable HIGH or CRITICAL finding fails the run: cut a patch release. | No (scheduled) |
| `pip-audit` | `pip-audit.yml` | Audits the locked Python dependencies every Monday, so a new advisory surfaces without a push. | No (scheduled) |
| Scorecard | `scorecard.yml` | Rates the repository's supply-chain practices every Monday and on each push to `main`. Treat the score as a checklist, not a target. | No (scheduled) |
| Dependabot | `.github/dependabot.yml` | Opens grouped update PRs for Python, GitHub Actions, and base images every Monday, and separate PRs for security fixes to Python packages and actions. GitHub publishes no advisories for base images, so Grype covers those. Version updates wait 7 days after a release. Nothing auto-merges. | Its PRs pass the same checks |

Every workflow pins its actions by commit SHA, sets no or read-only token permissions at the top, and grants each job only what it needs. The repository allows only GitHub-owned actions plus `astral-sh`, `docker`, `anchore`, and `ossf` actions, and requires SHA pins.

A pull request runs the workflows from its own branch, so a PR can change what its checks do. The repository holds every outside contributor's workflow runs until you approve them, and you read any diff under `.github/` before you approve a run or merge.

GitHub disables a public repository's scheduled workflows after 60 days without activity, which stops the nightly scan without a failed run. `scripts/verify-github-settings.sh` compares the repository's GitHub settings with what the lockdown expects, including disabled workflows. It only reads settings, and exits non-zero if any is wrong. To turn a workflow back on, run `gh workflow enable scan-release.yml`, or select **Enable workflow** on the workflow's page in the **Actions** tab.
