# Harden your install

Dinnerbell faces the internet from your home network, so treat the host and your GitHub account as part of the app. This checklist, for a working install from [setup.md](setup.md) or [install.md](install.md), lists what you set up yourself and what the repository's CI checks for you.

## Secure your GitHub account

If you maintain a fork or cut releases, an attacker who takes over your GitHub account can publish an image your host pulls.

1. Use a passkey or a hardware security key as your two-factor method, and remove SMS as a fallback.
2. Watch the repository with **Custom** > **Security alerts**, to hear about Dependabot alerts, code scanning alerts, and private vulnerability reports.
3. In **Settings** > **Notifications** > **System** > **Actions**, select **Email** and **Only notify for failed workflows**, so a failed nightly image scan reaches your inbox.

GitHub sends a scheduled workflow's failure notice to whoever last changed its `cron` line, so if someone else edits the schedule in `scan-release.yml`, `pip-audit.yml`, or `scorecard.yml`, the notices go to them.

## Harden the host

- **Run rootless podman under a dedicated user.** Create a user that owns nothing else, such as `dinnerbell`, give it subordinate UID and GID ranges in `/etc/subuid` and `/etc/subgid`, and run `loginctl enable-linger dinnerbell` so its containers keep running after you log out. Install and run Dinnerbell as that user.
- **Lock down `.env`.** It holds the secret key and tokens. `dinnerbell init` writes it with mode 600; check that it still is.

  ```sh
  chown dinnerbell: .env
  chmod 600 .env
  ```

- **Keep backups off the container volume.** The nightly backups live next to the database, so losing the volume loses both. Copy them to another disk or host ([Back up](backup-and-upgrade.md#back-up)), and keep `.env` and `secrets/` apart from those copies ([What else to back up](backup-and-upgrade.md#what-else-to-back-up)).
- **Pin a release.** Run a specific `X.Y.Z` image tag, never `latest` or `main`, and don't use `podman auto-update`, which follows moving tags. Upgrade on purpose, after you read the upgrade notes. After you pull, [verify the image](backup-and-upgrade.md#verify-an-image), and consider pinning `image:` to its digest in `compose.override.yaml` (with secrets in files, [name it with `-f`](setup.md#run-compose-with-secret-files)). In a clone, the test instance follows `main`, which is never a verified release.
- **Bind only to the interface your reverse proxy reaches.** Set [`DINNERBELL_BIND`](configuration.md#compose-settings), and never use `0.0.0.0`: the proxy must be the only way in. To confirm, run `ss -ltn | grep -E ':801[34]'` and check the listening addresses.
- **Keep the compose hardening flags.** Don't remove `compose.yaml`'s `read_only`, `cap_drop: ALL`, `no-new-privileges`, size-capped `/tmp`, or [resource and log limits](configuration.md#resource-and-log-limits) in `compose.override.yaml`.

## Send HSTS from your reverse proxy

Dinnerbell sends no `Strict-Transport-Security` (HSTS) header; your reverse proxy terminates TLS, so it must. Without HSTS, a browser that opens your hostname without `https://` makes its first request over plain HTTP, which someone on the same network can intercept.

With a self-hosted Pangolin, which runs Traefik, follow these steps. They follow Pangolin's Traefik hardening guide but haven't been tried on a real Pangolin, so keep a copy of each file you change, and check that Traefik starts again.

1. In Traefik's dynamic configuration, `config/traefik/dynamic_config.yml` in a default install, add a headers middleware under `http.middlewares`, beside the ones there:

   ```yaml
   http:
     middlewares:
       hsts:
         headers:
           stsSeconds: 31536000
   ```

2. In `config/traefik/traefik_config.yml`, attach it to the HTTPS entry point, `websecure`, beside the keys under `http`:

   ```yaml
   entryPoints:
     websecure:
       http:
         middlewares:
           - hsts@file
   ```

3. Restart Pangolin's Traefik container.
4. Check that the header arrives:

   ```sh
   curl -sI https://<your hostname>/healthz | grep -i strict-transport-security
   ```

The entry point applies the header to every resource on that Pangolin, so each must serve HTTPS. Browsers remember it for `stsSeconds`, one year here. Add `stsIncludeSubdomains` or `stsPreload` only if every subdomain of your domain serves HTTPS.

With any other reverse proxy, add `Strict-Transport-Security: max-age=31536000` to its HTTPS responses for Dinnerbell's hostname, and check it with the same `curl`.

## Allow only the outbound connections Dinnerbell needs

If your firewall limits outbound connections, Dinnerbell needs HTTPS (port 443) to these hosts, each only while its feature is on. Only Slack's API and Socket Mode can go through a proxy ([Outbound connections](configuration.md#outbound-connections)).

| Host | For | When |
|---|---|---|
| `slack.com`, `wss-primary.slack.com` | Slack's API and Socket Mode | Always, with Slack configured |
| Your sign-in provider, such as `slack.com` | Sign-in (OpenID Connect) | Always |
| `api.anthropic.com` | The assistant | With `DINNERBELL_ANTHROPIC_API_KEY` set and **Admin** > **Assistant** on ([The assistant](configuration.md#the-assistant)) |
| Any public website | A menu link a member asks the assistant to read: public addresses only, never a private one | With the assistant on |
| The push services | Web push | With **Admin** > **Web app** on ([Web push](configuration.md#web-push)) |
| `geocoding-api.open-meteo.com`, `api.open-meteo.com` | Dinnerbone's local weather | With **Dinnerbone's weather** on ([Local weather](configuration.md#local-weather)) |

## Monitor it

Add two monitors in Uptime Kuma, or any monitor that can match a keyword:

| Monitor | Target | Alerts when |
|---|---|---|
| HTTP | `https://<your hostname>/healthz` | The response isn't 200. Dinnerbell returns 503 when the database is unreachable or the scheduler hasn't ticked for 5 minutes. |
| HTTP keyword | `https://<your hostname>/healthz`, keyword `"status":"ok","version"` | Dinnerbell reports `degraded`, with HTTP 200: a late scheduler tick, a stuck call, a notification waiting over an hour, a push waiting over an hour while the web app is on, a failed backup, or a Slack problem. |

The keyword includes `"version"` because the scheduler's own `"status":"ok"` would otherwise match. An Uptime Kuma **HTTP(s) - Json Query** monitor with the query `status` and the expected value `ok` works too. [Check /healthz](troubleshooting.md#check-healthz) explains each key.

## Verify that no secret is on a command line

If you [keep secrets in files](setup.md#keep-secrets-in-files), you can check that no secret reaches a command line. Run it on a [test instance](test-instance.md) with secrets in files: it replaces test's secret key with a throwaway value to search for.

1. Write a canary of at least 32 characters into test's key file.

   ```sh
   rm -f secrets-test/DINNERBELL_SECRET_KEY
   ( umask 077; printf '%s' 'canary-0123456789-abcdefghijklmnopqrstuvwxyz' >secrets-test/DINNERBELL_SECRET_KEY )
   podman unshare chown 10001:10001 secrets-test/DINNERBELL_SECRET_KEY
   ```

2. In a second terminal, watch the process list while the service starts.

   ```sh
   while :; do ps -eww -o args= | grep -F canary-0123456789 | grep -v grep; sleep 0.05; done
   ```

3. In the first terminal, start test, then count the canary in a dry run's output. podman-compose 1.0.6 needs only `--dry-run`; later versions need `--verbose` too.

   ```sh
   deploy/dinnerbell-test up -d
   DINNERBELL_COMPOSE='podman-compose --verbose --dry-run' deploy/dinnerbell-test up -d 2>&1 | grep -c canary-0123456789
   ```

   The second terminal prints nothing, and the count is `0` (`grep -c` exits 1 then). Press Ctrl+C in the second terminal.

4. Replace the canary with a real key.

   ```sh
   rm -f secrets-test/DINNERBELL_SECRET_KEY
   ( umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" >secrets-test/DINNERBELL_SECRET_KEY )
   podman unshare chown 10001:10001 secrets-test/DINNERBELL_SECRET_KEY
   ```

## What CI enforces

The `main` ruleset requires a pull request and these checks before a merge. An admin can bypass them only through a pull request, for emergencies.

| Check | Workflow | What it does | Required to merge |
|---|---|---|---|
| `check` | `ci.yml` | Lint, type checks, tests with at least 90% coverage, and `pip-audit`. | Yes |
| `image` | `ci.yml` | Builds the image, runs a smoke test against `/healthz`, and scans it with Grype. | Yes |
| `dependency-review` | `dependency-review.yml` | Fails a PR that adds a dependency with a known moderate or worse vulnerability. | Yes |
| `dco` | `dco.yml` | Fails a PR with any commit, merges included, that lacks a `Signed-off-by` for its author's email. Only Dependabot's own commits in its own PRs are exempt. See [CONTRIBUTING.md](../CONTRIBUTING.md). | Yes |
| CodeQL | Default setup | Scans Python, JavaScript, and the workflows. | Yes |
| Release scan | `scan-release.yml` | Scans the image tagged `latest` with Grype every night. A fixable HIGH or CRITICAL finding fails the run: cut a patch release. | No (scheduled) |
| `pip-audit` | `pip-audit.yml` | Audits the locked Python dependencies every Monday. | No (scheduled) |
| Scorecard | `scorecard.yml` | Rates the repository's supply-chain practices every Monday and on each push to `main`. Treat the score as a checklist, not a target. | No (scheduled) |
| Dependabot | `.github/dependabot.yml` | Opens grouped update PRs for Python, Actions, and base images every Monday, waiting 7 days after a release, plus security fixes for Python and Actions; Grype covers base images. Nothing auto-merges. | Its PRs pass the same checks |

Every workflow pins its actions by commit SHA and grants each job only the token permissions it needs. The repository allows only GitHub-owned actions plus `astral-sh`, `docker`, `anchore`, and `ossf` actions.

A pull request runs the workflows from its own branch, so a PR can change what its checks do. Outside contributors' runs wait for your approval: read any diff under `.github/` before you approve a run or merge.

GitHub disables a public repository's scheduled workflows after 60 days without activity, which stops the nightly scan without a failed run. `scripts/verify-github-settings.sh` reads the repository's settings, disabled workflows included, and exits non-zero if any differs from what the lockdown expects. To turn a workflow back on, run `gh workflow enable scan-release.yml`, or select **Enable workflow** on its page in the **Actions** tab.
