# Set up Dinnerbell

This guide takes you from nothing to a working Dinnerbell install: a Slack workspace and app, a container on your host, a public HTTPS hostname, and your family signed in. When it works, harden it with [hardening.md](hardening.md).

## Status

Dinnerbell is under construction. Milestones M0 through M2 ship these parts, and this guide covers them fully:

- Sign in with Slack, and the members admin page
- `/healthz`
- The scheduler and nightly backups
- The catalog, catalog file import, `DINNERBELL_SEED_FILE`, preferences, and meal history
- Dinner calls in Slack: the call, reminders, proposals and votes, the decision, and assignments, plus the App Home and `/dinnerbell call`

As of M2, the Slack bot connects when `DINNERBELL_SLACK_BOT_TOKEN`, `DINNERBELL_SLACK_APP_TOKEN`, and `DINNERBELL_SLACK_CHANNEL` are all set. Offers, `/dinnerbell offer`, `log`, and `schedule` come in M3.

## Prerequisites

- A Linux host with podman and podman-compose 1.0.6 or later, or Docker with the compose plugin. The host pulls the image from `ghcr.io`. These docs are checked with podman-compose 1.0.6 (Ubuntu 24.04's), 1.1.0, and 1.5.0, and Docker Compose 2.40.
- A public HTTPS hostname that a reverse proxy forwards to the host's port 8013. The reference setup uses Pangolin.
- A Slack account. You use its email address as the admin email.

## 0. Clone the repository

The repository holds `compose.yaml`, the Slack app manifests, and the example catalog, so clone it even though you don't build anything. Then check out the newest release. `compose.yaml` at a release tag names that release's image, so checking out a tag is how you choose what production runs.

```sh
git clone https://github.com/dsmorgan/dinnerbell.git
cd dinnerbell
git checkout "$(git tag --list 'v*' --sort=-version:refname | head -n 1)"
```

Git reports a detached HEAD, which is expected: production follows release tags, never `main`.

Keep the directory name `dinnerbell`, and run every command in this guide as the same user, with or without `sudo`. Compose names the data volume after the directory, as `dinnerbell_dinnerbell-data`, and rootless podman keeps each user's volumes apart. Another directory name or another user, including root, gets a new, empty database. [hardening.md](hardening.md) recommends a dedicated user.

## 1. Create the Slack workspace and channel

1. Go to [slack.com/get-started](https://slack.com/get-started).
2. Create a new workspace for your household. The free plan is enough.
3. Create a channel named `#dinnerbell`.

The free plan limits history to 90 days and the number of apps to 10. Neither matters: Dinnerbell is the system of record, and it is the only app.

## 2. Create the Slack app

Don't edit files in your clone, including the manifest. Local edits block `git checkout` when you upgrade, and they put your hostname one `git add` away from a commit. You set the hostname in Slack instead.

1. Open `deploy/slack-app-manifest.yaml` in the repository you cloned, and copy its contents.
2. Go to [api.slack.com/apps](https://api.slack.com/apps) and select **Create New App**.
3. Select **From a manifest**.
4. Select your household workspace.
5. Paste the manifest as YAML.
6. In the pasted text, replace `dinnerbell.example.com` in `redirect_urls` with your public hostname. Keep the path `/auth/callback`.
7. Select **Next**, then **Create**.

To keep a copy of the manifest with your hostname, write it to a file ending in `.local.yaml`, which git ignores:

```
sed 's#dinnerbell.example.com#dinnerbell.your-domain.example#' \
  deploy/slack-app-manifest.yaml > deploy/slack-app-manifest.local.yaml
```

If you change your hostname later, edit the redirect URL under **OAuth & Permissions**. When an upgrade changes the manifest, paste the new version under **App Manifest** in the app settings and set your hostname there again.

## 3. Create the app-level token

Socket Mode lets the app connect out to Slack, so Slack never calls your host. The manifest already enables Socket Mode. You still create the token.

1. In the app settings, open **Basic Information** and scroll to **App-Level Tokens**.
2. Select **Generate Token and Scopes**.
3. Name the token `dinnerbell`, add the `connections:write` scope, and select **Generate**.
4. Copy the token. It starts with `xapp-`. This is `DINNERBELL_SLACK_APP_TOKEN`.

## 4. Install the app and copy the bot token

1. Open **Install App** and select **Install to Workspace**, then **Allow**.
2. Copy the **Bot User OAuth Token**. It starts with `xoxb-`. This is `DINNERBELL_SLACK_BOT_TOKEN`.

## 5. Copy the sign-in credentials

1. Open **Basic Information** and find **App Credentials**.
2. Copy **Client ID** into `DINNERBELL_OIDC_CLIENT_ID`.
3. Reveal and copy **Client Secret** into `DINNERBELL_OIDC_CLIENT_SECRET`.
4. Set `DINNERBELL_OIDC_ISSUER` to `https://slack.com`. Slack publishes its discovery document at `https://slack.com/.well-known/openid-configuration`.

## 6. Find the team ID and channel ID

1. Open your workspace in a browser. The address looks like `https://app.slack.com/client/T0123ABCDEF/...`.
2. Copy the part that starts with `T`. This is `DINNERBELL_SLACK_TEAM_ID`. It is required with Slack sign-in, and Dinnerbell refuses to start without it. It makes Dinnerbell refuse sign-ins from any other Slack workspace; without it, someone in another workspace whose Slack email matches an invitation could claim it.
3. In the `#dinnerbell` channel, select the channel name at the top to open the channel details.
4. Copy the channel ID at the bottom of the **About** tab. It starts with `C`. This is `DINNERBELL_SLACK_CHANNEL`. If you don't see it there, copy it from the address bar when you open the channel in a browser.

## 7. Invite the bot to the channel

The bot posts only in channels it belongs to.

1. In `#dinnerbell`, type `/invite @Dinnerbell` and send it.
2. Select **Add to Channel** if Slack asks you to confirm.

If you start Dinnerbell before you invite the bot, `/healthz` reports Slack as `error` until you do. Dinnerbell checks again every minute, so you don't need to restart it.

## 8. Expose Dinnerbell through your reverse proxy

Point your hostname at the host's port 8013. With Pangolin:

1. Create a public HTTP resource for your hostname.
2. Set the target to the host running Dinnerbell, port 8013.
3. Open the resource's authentication settings and turn off **Use Platform SSO**. Pangolin turns it on by default for public resources.
4. Leave password, PIN, email OTP, and header auth off too.

Dinnerbell does its own sign-in. A second login in front of it breaks the OIDC redirect back from Slack and makes every device sign in twice. Pangolin still provides TLS, the hostname, and connection-level protections.

With authentication off, `/healthz` is public. It shows only coarse status and the running version, such as `0.2.0`, which the page footer also shows.

`compose.yaml` publishes port 8013 on the address in `DINNERBELL_BIND`, which defaults to `127.0.0.1`. That default only works when Newt runs on the same host and not inside a container network. Otherwise, set `DINNERBELL_BIND` in `.env` to the host's address on the interface Newt reaches, for example its LAN address:

```sh
DINNERBELL_BIND=192.168.1.20
```

Avoid `0.0.0.0` unless a firewall limits who can reach the port.

## 9. Run Dinnerbell

1. Copy the example environment file.

   ```sh
   cp .env.example .env
   ```

2. Restrict access to it, because it holds secrets.

   ```sh
   chmod 600 .env
   ```

3. Generate a secret key.

   ```sh
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Or use `openssl rand -base64 48`.

4. Edit `.env` and fill in the values you collected.

   ```sh
   DINNERBELL_BASE_URL=https://dinnerbell.example.com
   DINNERBELL_SECRET_KEY=<generated key>
   DINNERBELL_ADMIN_EMAIL=you@example.com
   DINNERBELL_HOUSEHOLD_NAME=Home
   DINNERBELL_TIMEZONE=America/New_York

   DINNERBELL_OIDC_ISSUER=https://slack.com
   DINNERBELL_OIDC_CLIENT_ID=<client ID>
   DINNERBELL_OIDC_CLIENT_SECRET=<client secret>
   DINNERBELL_SLACK_TEAM_ID=T0123ABCDEF

   DINNERBELL_SLACK_BOT_TOKEN=xoxb-...
   DINNERBELL_SLACK_APP_TOKEN=xapp-...
   DINNERBELL_SLACK_CHANNEL=C0123ABCDEF

   # Set this in the trusted-proxy step below.
   # DINNERBELL_TRUSTED_PROXIES=
   ```

   `DINNERBELL_ADMIN_EMAIL` must match the email on your Slack account. If you change it later, Dinnerbell invites the new address as another admin on the next start.

   `DINNERBELL_BASE_URL` must exactly match the URL people type: the same host, `https://`, and no explicit `:443` or path. Dinnerbell derives the sign-in redirect URL from it, and checks every form submission against it. With any other host, a LAN IP, an explicit `:443`, or plain `http://`, every form fails with "Cross-origin request refused". Plain `http://` also breaks sign-in, because the browser never stores the `Secure` session cookie over http.

   `DINNERBELL_HOUSEHOLD_NAME` and `DINNERBELL_TIMEZONE` take effect on first start only. After that, Dinnerbell reads them from its database, so set them correctly before you start it the first time.

5. Optional: load a starter catalog on first start. Copy `seed/example.yaml` to `seed/my-catalog.yaml` and edit it. Git ignores every file in `seed/` except the example and the schema, so your catalog stays out of the repository. The format is in [catalog-format.md](catalog-format.md).

   ```sh
   cp seed/example.yaml seed/my-catalog.yaml
   ```

6. Optional: mount the catalog file read-only. Create `compose.override.yaml` next to `compose.yaml` with this content. Both compose tools read it automatically, and git ignores it.

   ```yaml
   services:
     dinnerbell:
       volumes:
         - ./seed/my-catalog.yaml:/seed/catalog.yaml:ro
   ```

7. Optional: point Dinnerbell at the mounted file by adding this line to `.env`. Dinnerbell imports the file on first start, while the catalog is still empty. You can also import a file later from the Catalog page.

   ```sh
   DINNERBELL_SEED_FILE=/seed/catalog.yaml
   ```

8. Pull the image.

   ```sh
   podman-compose pull
   ```

   With Docker: `docker compose pull`

   `compose.yaml` names the release image, such as `ghcr.io/dsmorgan/dinnerbell:0.2.0`. To check that it came from this repository's release workflow, see [Verify an image](#verify-an-image).

9. Start the service.

   ```sh
   podman-compose up -d
   ```

   With Docker: `docker compose up -d`

10. Check that it is healthy.

    ```sh
    curl https://dinnerbell.example.com/healthz
    ```

    The response reports database reachability, the scheduler's last tick, the Slack state, the outbox (`backlog`: notifications waiting to be sent; `failed`: notifications that gave up), and the last backup time. It returns HTTP 503 when the database is unreachable or the scheduler has not ticked for 5 minutes, so the container health check and Uptime Kuma catch both.

11. Confirm that Slack is connected. Within a few seconds of starting, the response includes `"slack": "connected"` and `"status": "ok"`.

    ```sh
    curl -s https://dinnerbell.example.com/healthz | grep -o '"slack":"[a-z_]*"'
    ```

    | `slack` | Meaning |
    |---|---|
    | `connected` | The startup checks passed and the Socket Mode connection is up. |
    | `disconnected` | Dinnerbell can't reach Slack right now, or is reconnecting after a drop. It retries on its own. |
    | `error` | A startup check failed. The logs say which one and how to fix it; see Troubleshooting. |
    | `not_configured` | One or more of the three Slack variables is missing. The bot posts nothing. |

    A Slack problem alone sets `"status": "degraded"` but keeps HTTP 200, so the container keeps running and you can still sign in to fix it. Notifications wait in the outbox until Slack is back. To have Uptime Kuma alert on Slack too, use an HTTP keyword monitor for `"slack":"connected"`.

    The logs show what happened:

    ```sh
    podman-compose logs dinnerbell | grep -i slack
    ```

### Set the trusted proxy

Rate limiting needs the real client IP. Dinnerbell reads `X-Forwarded-For` only from proxies you list in `DINNERBELL_TRUSTED_PROXIES`.

1. Add `DINNERBELL_LOG_LEVEL=DEBUG` to `.env`.
2. Recreate the service so it reads `.env`. A restart doesn't re-read it.

   ```sh
   podman-compose down && podman-compose up -d
   ```

   With Docker: `docker compose down && docker compose up -d`

3. Load your public URL in a browser.
4. Read the logs. At this level, the app logs the peer IP and the resolved client IP for each request.

   ```sh
   podman-compose logs dinnerbell
   ```

   With Docker: `docker compose logs dinnerbell`

5. Find the peer IP. It is the address the proxy connects from. With a Newt site, it is usually the address of the host or container that runs Newt. With a plain WireGuard site, it is the Pangolin server's tunnel address.

   Check the peer IP from a second client too, such as `curl` from another machine on your LAN. Under Docker Desktop, and with rootless podman's port forwarding, every connection to a published port can arrive from the same gateway address, such as `10.0.2.2` or the bridge gateway. If the second client shows the same peer IP as Newt, trusting that address lets any client that can reach the published port forge `X-Forwarded-For`. Trust it only if nothing but Newt can reach the port, for example because `DINNERBELL_BIND` and your firewall restrict it. Otherwise leave `DINNERBELL_TRUSTED_PROXIES` empty: rate limiting then treats all clients as one, which is safe but stricter.
6. Set the single peer address in `.env`, and remove the `DINNERBELL_LOG_LEVEL` line or set it back to `INFO`.

   ```sh
   DINNERBELL_TRUSTED_PROXIES=10.0.0.5
   ```

7. Recreate the service again.

   ```sh
   podman-compose down && podman-compose up -d
   ```

Set only the one address the proxy connects from. Never use `0.0.0.0/0`, or a LAN or podman subnet range. Anyone inside a trusted range can forge `X-Forwarded-For` and get around rate limiting.

## 10. Sign in as the admin

1. Open your public URL in a browser.
2. Select **Sign in with Slack** and approve the request.
3. Open **Members**.
4. Add each household member by the email address you will invite to Slack. Sign-in is invite-only, and the email must match the one on their Slack account.

## 11. Onboard family members

Invite each person to the Slack workspace from Slack (**Invite people**), using the same email address you added on **Members**. Then forward them the following message.

> **Set up Dinnerbell on your phone**
>
> 1. Install Slack from the App Store.
> 2. When Slack asks to send notifications, tap **Allow**.
> 3. Open the invitation email from Slack and accept the invite to our workspace.
> 4. In Slack, open the `#dinnerbell` channel.
> 5. Tap the channel name, then tap **Notifications**.
> 6. Choose **All new messages**.
> 7. Optional: open `https://dinnerbell.example.com` in Safari, tap Share, then tap **Add to Home Screen**. This lets you open Dinnerbell like an app.
> 8. Open Dinnerbell and tap **Sign in with Slack**.
> 9. Slack asks you to sign in again in Safari. Enter your email, type the code Slack emails you, then tap **Allow**.
> 10. If the Home Screen icon asks you to sign in, sign in once more.

### Link everyone's Slack account

Dinnerbell needs each member's Slack user to mention them, send them direct messages, and accept their button taps. It links a member in any of these ways:

- When they sign in to the web app with Slack.
- By email, at start and every hour, for active members whose Dinnerbell email matches their Slack email.
- By hand: on **Members**, select **Edit**, then enter their Slack member ID. In Slack, open their profile, select the three dots, then **Copy member ID**.

The email match links only full members of your workspace, never guests or people from other organizations. A link you set or clear by hand stays that way: the hourly match and sign-in leave it alone. To hand a member back to automatic linking, select **Back to automatic** on their edit page.

The **Members** page shows who is linked. Someone who isn't linked gets a private "I don't know your Slack account yet" message when they tap a button.

## 12. Load the catalog

The catalog is your list of restaurants and home meals. [catalog-format.md](catalog-format.md) describes the file format, and [seed/example.yaml](../seed/example.yaml) is a starter file.

- **On first start:** mount a catalog file and set `DINNERBELL_SEED_FILE`, as described in steps 5 to 7 of section 9.
- **After first start:** paste or upload the file on the Catalog page.

On first start, only the admin is a member yet, so the seed skips every other cook email. After you invite everyone (section 11), import the same file again from the Catalog page to fill in the cooks. Fields in the file replace what's stored, so do this before you edit those items in the app.

## Back up

Dinnerbell runs an online backup of its SQLite database every night at 03:30 household time into `/data/backups/`, and once at start if the newest backup is more than a day old. It keeps the 14 newest daily copies plus the newest copy from each of the last 8 weeks. In M0, only `/healthz` shows the last successful backup; the Admin page gains it later.

These backups live in the same volume as the database, so copy them off the host with your own backup routine. Copy `/data/backups/` only. Never copy the live `dinnerbell.db` or its WAL file, because a copy taken mid-write can be corrupt.

1. List the volumes. Compose prefixes the volume name with the project name, which is the directory name by default. For a clone in `dinnerbell`, the volume is `dinnerbell_dinnerbell-data`. The test instance's is `dinnerbell-test_dinnerbell-data`.

   ```sh
   podman volume ls
   ```

2. Find the volume's path on the host.

   ```sh
   podman volume inspect <volume> --format '{{.Mountpoint}}'
   ```

   With Docker, use `docker volume ls` and `docker volume inspect <volume> --format '{{.Mountpoint}}'`.

3. Point your backup routine at the `backups` directory inside that path.

The files are readable only by their owner, uid 10001 inside the container, because they contain member emails. Under rootful podman or Docker, run your backup routine as root. Under rootless podman, uid 10001 maps to one of your subordinate IDs, so your own user can't read the files directly. Copy them out through the user namespace instead:

```sh
mkdir -p ~/dinnerbell-backups
podman unshare cp -a "$(podman volume inspect <volume> --format '{{.Mountpoint}}')/backups/." ~/dinnerbell-backups/
podman unshare chown -R 0:0 ~/dinnerbell-backups
```

The trailing `/.` copies the directory's contents, so running the same commands again refreshes the copy instead of nesting a second `backups` directory inside it.

Inside `podman unshare`, your user is root, so the copy works, and the `chown` hands the copies back to your own user.

### Back up now

Before an upgrade, take a fresh backup instead of relying on last night's. This runs SQLite's online backup inside the running container, so you don't stop the service:

```sh
podman-compose exec dinnerbell python -c "import os, sqlite3, time; from dinnerbell import __version__ as v; os.umask(0o077); name = time.strftime(f'/data/backups/pre-upgrade-{v}-%Y%m%dT%H%M%SZ.db', time.gmtime()); src = sqlite3.connect('file:/data/dinnerbell.db?mode=ro', uri=True); dst = sqlite3.connect(name); src.backup(dst); dst.close(); src.close(); print(name)"
```

With Docker: `docker compose exec dinnerbell python -c "..."` with the same command.

The command prints the backup's name, such as `/data/backups/pre-upgrade-0.2.0-20261024T013000Z.db`: the version you're running and the UTC time. Write the name down; [Roll back](#roll-back) needs it. Nightly pruning never deletes these files, so delete old ones yourself once you no longer need them. Copy the backup off the host as described above.

## Upgrade

Releases are listed in [CHANGELOG.md](../CHANGELOG.md). Production runs a release tag, never `latest` or `main`, and your clone stays checked out at the same release, so `compose.yaml` always matches the image.

If you run a [test instance](#test-instance), follow the [promotion checklist](#promote-a-release) instead. It rehearses the release on a copy of your data first.

1. Read the **Upgrade notes** for every release between yours and the new one.
2. [Back up now](#back-up-now). Note the file name, and copy the backup off the host.
3. Check out the new release, replacing `vX.Y.Z`. Its `compose.yaml` names the matching image.

   ```sh
   git fetch --tags
   git checkout vX.Y.Z
   ```

4. Pull the image, and [verify it](#verify-an-image).

   ```sh
   podman-compose pull
   ```

5. Recreate the service, so it starts from the new image.

   ```sh
   podman-compose down && podman-compose up -d
   ```

   With Docker: `docker compose pull`, then `docker compose down && docker compose up -d`.

   `down` keeps the data volume. Never add `-v`, which deletes it.

6. Check that the page footer shows the new version and that `/healthz` returns 200 with `"slack":"connected"`.

Database migrations run on start. They only move forward: after an upgrade, the older version refuses to open the database. To go back, see [Roll back](#roll-back).

## Roll back

Roll back by running the previous release on the backup you took before the upgrade. Anything recorded since that backup is lost, so roll back soon after a bad upgrade.

1. Stop the service.

   ```sh
   podman-compose down
   ```

2. Check out the release you ran before, replacing `vX.Y.Z`.

   ```sh
   git checkout vX.Y.Z
   ```

3. Find the backup you took before the upgrade.

   ```sh
   podman-compose run --rm --entrypoint ls dinnerbell -l /data/backups
   ```

4. Replace the database with it, using the file name from [Back up now](#back-up-now). The command runs in a one-off container on the same volume.

   ```sh
   podman-compose run --rm --entrypoint sh dinnerbell -c 'umask 077 && rm -f /data/dinnerbell.db-wal /data/dinnerbell.db-shm && cp /data/backups/pre-upgrade-X.Y.Z-YYYYMMDDTHHMMSSZ.db /data/dinnerbell.db'
   ```

   With Docker: `docker compose run --rm --entrypoint sh dinnerbell -c '...'` with the same command.

   The command assumes the default `DINNERBELL_DATABASE_URL`. Removing the `-wal` and `-shm` files matters: SQLite would otherwise apply the newer version's changes to the restored file.

5. Pull the previous image and start the service.

   ```sh
   podman-compose pull
   podman-compose up -d
   ```

6. Check that the footer shows the previous version.

When a fixed release ships, [upgrade](#upgrade) to it as usual.

## Verify an image

Every release image carries build provenance and SBOM attestations signed through GitHub. To check that `ci.yml` in this repository built an image from its release tag, run this on any machine with the [GitHub CLI](https://cli.github.com/), replacing `X.Y.Z`:

```sh
gh attestation verify oci://ghcr.io/dsmorgan/dinnerbell:X.Y.Z \
  --repo dsmorgan/dinnerbell \
  --signer-workflow dsmorgan/dinnerbell/.github/workflows/ci.yml \
  --source-ref refs/tags/vX.Y.Z
```

The release notes on GitHub list each release's digest, which never changes. Compare it with the digest of the image your host pulled:

```sh
podman image inspect ghcr.io/dsmorgan/dinnerbell:X.Y.Z --format '{{.RepoDigests}}'
```

With Docker: `docker image inspect` with the same arguments.

Don't use `podman auto-update` or a tool such as Watchtower. They follow moving tags, which defeats pinning a release. [hardening.md](hardening.md) lists the other host and GitHub settings that protect your install.

## Build from source

To run your own changes, build the image from your checkout with the `compose.build.yaml` override:

```sh
podman-compose -f compose.yaml -f compose.build.yaml up -d --build
```

Pass both `-f` flags to every later compose command. The image is named `localhost/dinnerbell:dev`, has no attestations, and isn't a release, so use it for development rather than production.

## Switch a source-built install to the prebuilt image

Before 0.2.0, `compose.yaml` built the image on your host. This one-time switch keeps your data, because the new `compose.yaml` keeps the volume name, `dinnerbell-data`, which compose prefixes with the project name. Run every command as the same user, with or without `sudo`, as your install. Rootless podman keeps each user's volumes apart, so another user, including root, gets a new, empty volume.

1. From your clone, note the data volume's name and the commit you run now. The undo steps need both.

   ```sh
   podman volume ls
   git rev-parse --short HEAD
   ```

   For a clone in a directory named `dinnerbell`, the volume is `dinnerbell_dinnerbell-data`. Run every later command from the same directory, so compose finds the same volume.

2. [Back up now](#back-up-now). Note the file name, and copy the backup off the host.
3. If `git status` shows local changes to `compose.yaml`, move them to `compose.override.yaml`, which git ignores, then run `git checkout compose.yaml`.
4. Check out the release.

   ```sh
   git fetch --tags
   git checkout v0.2.0
   ```

5. Pull and [verify](#verify-an-image) the image, and recreate the service.

   ```sh
   podman-compose pull
   podman-compose down && podman-compose up -d
   ```

6. Run `podman volume ls` again. You should see no new Dinnerbell volume.
7. Sign in and check that your members, catalog, and history are there. The footer shows `0.2.0`.

Keep the image you built, `localhost/dinnerbell`, until 0.2.0 has run for a week. After that, remove it with `podman image rm localhost/dinnerbell`.

If Dinnerbell starts empty, compose created a new volume, usually because you ran it from a different directory or as a different user. Run `podman-compose down`, then start it again from the original directory as the original user. Your data is still in the old volume.

To undo the switch:

1. Stop the service: `podman-compose down`.
2. Check out the commit you noted in step 1: `git checkout <commit>`.
3. Restore the backup from step 2 with the command in [Roll back](#roll-back), step 4.
4. Rebuild and start the old version: `podman-compose up -d --build`.

## Test instance

A test instance is a second copy of Dinnerbell on the same host. It runs builds from `main`, or a release before you promote it, with its own database, Slack app, channel, and hostname. Production never runs untested code, and its channel setting never changes. [ADR 0006](adr/0006-test-instance.md) explains the design.

| | Production | Test |
|---|---|---|
| Image tag | The release in `compose.yaml` | `main`, or the release you're rehearsing |
| Compose project | `dinnerbell` | `dinnerbell-test` |
| Data volume | `dinnerbell_dinnerbell-data` (named by compose) | `dinnerbell-test_dinnerbell-data` (named in `deploy/compose.test.yaml`) |
| Env file | `.env` | `.env.test` |
| Host port | 8013 | 8014 |
| Slack app and command | Dinnerbell, `/dinnerbell` | Dinnerbell Test, `/dinnerbell-test` |
| Pangolin authentication | Off | On |

Setup takes about half an hour. Stop the test instance when you aren't using it. Run its commands from your production clone, as the same user.

### Create the test channel and Slack app

Use the same workspace as production.

1. Create a channel named `#dinnerbell-test`, and invite only the people who test with you. While the test instance runs, it mentions and messages only members of this channel. The channel can be private.
2. Create a second Slack app as in [section 2](#2-create-the-slack-app), but paste `deploy/slack-app-manifest.test.yaml`, and replace `dinnerbell-test.example.com` with your test hostname.
3. Create its app-level token, install it, and copy its bot token, client ID, and client secret, as in sections 3 through 5.
4. Copy the test channel's ID, as in section 6.
5. In `#dinnerbell-test`, invite the bot: `/invite @Dinnerbell Test`.

Never reuse production's tokens. Socket Mode delivers each event to only one connection, so two instances on one app would split button taps between them.

### Write .env.test

1. Copy the example next to `.env`, and restrict access to it.

   ```sh
   cp .env.test.example .env.test
   chmod 600 .env.test
   ```

2. Fill it in with the test app's values, a new secret key, the test hostname as `DINNERBELL_BASE_URL`, and the same `DINNERBELL_SLACK_TEAM_ID` as production. Keep `DINNERBELL_INSTANCE_LABEL=TEST` and `DINNERBELL_SLACK_COMMAND=/dinnerbell-test`.

Git ignores `.env.test`. Never commit it.

### Add the Pangolin resource

1. Create a public HTTP resource for the test hostname, with the target on the host's port 8014.
2. Leave **Use Platform SSO** on, and allow only your own Pangolin user.

Unlike production, test runs unreleased code and only you use it, so it stays behind Pangolin's login. Sign in to Pangolin first, then select **Sign in with Slack**. If Slack's redirect lands on a Pangolin login page, sign in to Pangolin and select **Sign in with Slack** again.

Pangolin's login protects only the hostname. Port 8014 is open to anything that can reach `DINNERBELL_BIND`, such as your LAN, where only Dinnerbell's own Slack sign-in stands in the way. Use your host firewall to allow port 8014 only from the address Newt connects from. With rootful Docker, published ports skip `ufw` rules, so add the rule to the `DOCKER-USER` chain instead.

`DINNERBELL_TRUSTED_PROXIES` in `.env.test` takes the same address as in `.env`.

### Start, stop, and refresh

`deploy/dinnerbell-test` runs `podman-compose` with the test project, volume, env file, tag, and port, and takes any compose command:

```sh
deploy/dinnerbell-test pull
deploy/dinnerbell-test up -d
deploy/dinnerbell-test logs dinnerbell
deploy/dinnerbell-test down
```

Each run first prints the project, volume, env file, tag, and port it uses. To pick up a newer `main`, run `pull`, then `down` and `up -d`. To check it, run `curl -s http://<DINNERBELL_BIND>:8014/healthz` and look for `"instance":"TEST"`, or look for the TEST banner in the browser. Test is usually stopped, so don't give it an always-on Uptime Kuma monitor.

Run every test command through `deploy/dinnerbell-test`, never through `podman-compose` directly. The script sets the project, the volume, the env file, the tag, and the port together. Setting only some of them by hand mixes the instances: for example, `.env.test` without `-p dinnerbell-test` runs test's label and Slack app on production's database, where `scrub-test` would then run. The script refuses to run when:

- `.env.test` is missing, sets `DINNERBELL_INSTANCE_LABEL` more than once, or leaves it empty.
- The env file is production's `.env`.
- `.env` sets `DINNERBELL_TAG`, `DINNERBELL_PORT`, or `DINNERBELL_ENV_FILE`. podman-compose 1.1.0 and older let `.env` override the script's values.
- Production's compose project is also named `dinnerbell-test`, from the clone's directory name or `COMPOSE_PROJECT_NAME`.
- The first argument is a compose option, such as `-p` or `-f`. Put options after the subcommand.
- You run it through a symlink.

Compose fills in `${...}` values in `compose.yaml` from the shell and from `.env`, which is production's file, and never from the env file, so the script sets the test values in the shell. A `compose.override.yaml` can't do the same job: podman-compose adds an override's `ports` to the base list instead of replacing it. Test doesn't read your `compose.override.yaml`. Test always publishes on production's `DINNERBELL_BIND`.

Set these in front of the command to change what it uses:

| Variable | Default | Use |
|---|---|---|
| `DINNERBELL_TAG` | `main` | Image tag, such as a release you're rehearsing |
| `DINNERBELL_ENV_FILE` | `.env.test` | Test configuration |
| `DINNERBELL_PORT` | `8014` | Host port |
| `DINNERBELL_COMPOSE` | `podman-compose` | Set to `docker compose` for Docker |

Set them per command, such as `DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test up -d`. Never `export` them: production's compose commands in the same shell read them too. An exported `DINNERBELL_ENV_FILE=.env.test`, for example, starts production with test's label and Slack app on production's database.

A new test instance starts with an empty database and invites `DINNERBELL_ADMIN_EMAIL`. Add test members on **Members**, or copy production's data.

### Copy production data into test

A copy of production's data tests migrations and new features against real history. It contains member emails, so keep it on the host, and scrub it before the test instance starts.

1. Stop the test instance. If it has never run, start and stop it once so its volume exists.

   ```sh
   deploy/dinnerbell-test down
   ```

2. Check that both volumes exist. If production's has another name, find it with `podman volume ls`, and use it in the next steps.

   ```sh
   podman volume exists dinnerbell_dinnerbell-data && podman volume exists dinnerbell-test_dinnerbell-data && echo ok
   ```

   With Docker: `docker volume inspect dinnerbell_dinnerbell-data dinnerbell-test_dinnerbell-data`.

3. Pick a production backup: the newest nightly `dinnerbell-YYYY-MM-DD.db`, or a `pre-upgrade-*.db` from [Back up now](#back-up-now). The commands use production's release image, which is already on the host; replace `X.Y.Z` with the release in production's `compose.yaml`. To list the backups:

   ```sh
   podman run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
     --entrypoint ls -v dinnerbell_dinnerbell-data:/prod:ro \
     ghcr.io/dsmorgan/dinnerbell:X.Y.Z /prod/backups
   ```

4. Copy it over the test database, replacing the file name.

   ```sh
   podman run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
     --entrypoint sh \
     -v dinnerbell_dinnerbell-data:/prod:ro \
     -v dinnerbell-test_dinnerbell-data:/data \
     ghcr.io/dsmorgan/dinnerbell:X.Y.Z \
     -c 'umask 077 && rm -f /data/dinnerbell.db-wal /data/dinnerbell.db-shm && cp /prod/backups/dinnerbell-YYYY-MM-DD.db /data/dinnerbell.db'
   ```

   With Docker, use `docker run` with the same arguments. Production's volume is mounted read-only, so the copy can't change it.

5. Scrub the copy, with the test instance still stopped. Keep at least one active admin, usually yourself, and repeat `--keep` for each person who tests with you.

   ```sh
   deploy/dinnerbell-test run --rm dinnerbell scrub-test --keep you@example.com
   ```

   Before it changes anything, `scrub-test` prints the database path, the household name, its last activity, and counts of active members, enabled slots, open events, and unsent notifications. Check that the household and counts match the copy you restored, and that the last activity is from around when the backup was taken. Then type `scrub` to confirm. If it refuses because there's no terminal to ask on, check the same numbers in its output, and run the command again with `--yes` added.

   `scrub-test` deactivates and unlinks from Slack every member you didn't keep, turns off every slot, ends open calls without notifying anyone, and empties the outbox. It leaves the meal log, catalog, and preferences alone. It refuses to run, and changes nothing, unless `DINNERBELL_INSTANCE_LABEL` is set. It migrates the database first, so run it with the same tag you start next: an older release refuses a database that a newer one migrated. Running it again changes nothing.

6. Start the test instance.

   ```sh
   deploy/dinnerbell-test up -d
   ```

Slots are off after a scrub. To rehearse a scheduled call, turn one on in **Admin**, or start one with `/dinnerbell-test call`.

### Promote a release

Use this checklist for every release. It runs the exact release image on a fresh copy of production's data before your family sees it. Replace `X.Y.Z` with the new release.

1. Read the release's **Upgrade notes** in [CHANGELOG.md](../CHANGELOG.md).
2. Pull the release and [verify it](#verify-an-image). Don't go on if verification fails.

   ```sh
   DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test pull
   gh attestation verify oci://ghcr.io/dsmorgan/dinnerbell:X.Y.Z \
     --repo dsmorgan/dinnerbell \
     --signer-workflow dsmorgan/dinnerbell/.github/workflows/ci.yml \
     --source-ref refs/tags/vX.Y.Z
   ```

3. In production, [back up now](#back-up-now). Note the file name, and copy the backup off the host.
4. Follow steps 1 to 4 of [Copy production data into test](#copy-production-data-into-test) with that backup.
5. Scrub the copy and start test with the release. Every command names the release, so the scrub migrates the copy exactly as production's upgrade will.

   ```sh
   DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test run --rm dinnerbell scrub-test --keep you@example.com
   DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test up -d
   ```

6. Check that the page shows the TEST banner and `X.Y.Z` in the footer, and that `/healthz` reports `"status":"ok"`.
7. Rehearse a call: run `/dinnerbell-test call` in `#dinnerbell-test`, respond, vote, and let it reach a decision. Check that nobody outside the test channel was notified. While the label is set, `/healthz` reports `slack_dms_dropped`, the number of direct messages withheld from people outside the channel.
8. [Upgrade](#upgrade) production, without a second backup: check out `vX.Y.Z`, run `podman-compose pull`, then `podman-compose down && podman-compose up -d`.
9. Check production's footer and `/healthz`.
10. Stop the test instance: `deploy/dinnerbell-test down`.

If production misbehaves after step 8, [roll back](#roll-back) with the backup from step 3.

## Use a different sign-in provider

The web app works with any OpenID Connect provider that publishes a discovery document. To use Google:

1. In the Google Cloud console, open **Google Auth Platform** and configure the OAuth consent screen.
2. Under **Audience**, add your household members as test users, or publish the app.
3. Create an OAuth client of type **Web application**.
4. Add `https://dinnerbell.example.com/auth/callback` as an authorized redirect URI.
5. In `.env`, set the following values and leave `DINNERBELL_SLACK_TEAM_ID` unset.

   ```sh
   DINNERBELL_OIDC_ISSUER=https://accounts.google.com
   DINNERBELL_OIDC_CLIENT_ID=<Google client ID>
   DINNERBELL_OIDC_CLIENT_SECRET=<Google client secret>
   ```

Invite members by the email on their Google account. In version 1, Slack is still required for notifications, so you still complete sections 1 through 4, the channel ID in section 6, and section 7.

### Switch providers on an existing install

Each member is linked to the account they first signed in with. After you switch providers, everyone, including you, sees "That email is already linked" until those links are cleared. To clear them:

1. Stop the service.

   ```sh
   podman-compose down
   ```

2. Clear every member's link. Members keep their emails, roles, and history.

   ```sh
   podman-compose run --rm --entrypoint python dinnerbell -c "import sqlite3; db = sqlite3.connect('/data/dinnerbell.db'); db.execute('UPDATE member SET oidc_subject = NULL'); db.commit()"
   ```

   With Docker: `docker compose run --rm --entrypoint python dinnerbell -c "..."` with the same command.

   The command assumes the default `DINNERBELL_DATABASE_URL`, which puts the database at `/data/dinnerbell.db`. If you changed it, use your database's path in the command instead.

3. Start the service again. Each member's next sign-in with a verified matching email links the new account.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Slack shows "redirect_uri did not match" or similar after you select Sign in | The redirect URL in the Slack app differs from `https://<host>/auth/callback`, for example because you pasted the manifest without editing the hostname. | Under **OAuth & Permissions > Redirect URLs**, set the exact URL and save. Also check that `DINNERBELL_BASE_URL` matches. |
| Sign-in returns to a Pangolin login page or loops | Pangolin resource authentication is still on. | Turn off **Use Platform SSO**, and leave password, PIN, email OTP, and header auth off. |
| Sign-in completes but Dinnerbell says you are not invited | The email on the Slack account differs from the invited email, or the email is not verified. | Compare the emails on **Members** and in your Slack profile. Fix the member's email, or use `DINNERBELL_ADMIN_EMAIL` for the admin. |
| Sign-in refused for a person you invited | They signed in with a different Slack workspace, and `DINNERBELL_SLACK_TEAM_ID` blocks it. | Ask them to sign in to your household workspace. |
| `/healthz` reports `"slack": "not_configured"` | One of `DINNERBELL_SLACK_BOT_TOKEN`, `DINNERBELL_SLACK_APP_TOKEN`, or `DINNERBELL_SLACK_CHANNEL` is missing. The logs say so at start. | Set all three, then recreate the service with `podman-compose down && podman-compose up -d`. |
| `/healthz` reports `"slack": "disconnected"` for more than a minute | The host can't reach Slack. | Check that the host can make outbound HTTPS and WebSocket connections to `slack.com` and `wss-primary.slack.com`. |
| `/healthz` reports `"slack": "error"`; the log says `Slack refused DINNERBELL_SLACK_BOT_TOKEN` | The bot token is wrong, revoked, or from another app. Dinnerbell notices at start, and as soon as Slack refuses it later, and holds every notification until it's fixed. | Copy the **Bot User OAuth Token** (`xoxb-`) again from **Install App**, then recreate the service. |
| The log says `Slack refused DINNERBELL_SLACK_APP_TOKEN` | The app-level token is wrong, lacks `connections:write`, or Socket Mode is off. | Create the token again as in step 3, check that **Socket Mode** is on, then recreate the service. |
| The log says `The bot token belongs to Slack workspace T…, but DINNERBELL_SLACK_TEAM_ID is T…` | The app is installed in a different workspace than the team ID names. | Install the app in your household workspace, or correct `DINNERBELL_SLACK_TEAM_ID`. |
| The log says `Slack can't find the channel` | `DINNERBELL_SLACK_CHANNEL` isn't a channel ID, or the channel is private. | Copy the ID that starts with `C` as in step 6. Use a public channel. |
| The log says `The bot isn't a member of the channel` | The bot hasn't been invited. | Run `/invite @Dinnerbell` in the channel. Dinnerbell notices within a minute. |
| The log says `The channel … is archived` | The channel was archived. | Unarchive it in Slack, or point `DINNERBELL_SLACK_CHANNEL` at another channel. |
| A member taps a button and Slack says "I don't know your Slack account yet" | Their Slack user isn't linked to a member, often because their emails differ. | Ask them to sign in to the web app with Slack once, or link them on **Members**. |
| `/healthz` shows `outbox.failed` above 0 | Slack refused some notifications for good, such as a direct message to someone with no Slack link. | Look for `notification delivery gave up` in the logs, then fix the link or the channel. Later notifications still go out. |
| Every form fails with "Cross-origin request refused", or sign-in loops back to the start page | `DINNERBELL_BASE_URL` doesn't exactly match the URL in the browser: another host, a LAN IP, an explicit `:443`, or `http://`. | Set `DINNERBELL_BASE_URL` to exactly the URL people type, `https://` and no port, then recreate the service. Open Dinnerbell only at that URL. |
| Sign-in says "That email is already linked" for everyone | You switched sign-in providers, so members are linked to accounts at the old provider. | Follow "Switch providers on an existing install". |
| Startup error: `DINNERBELL_SLACK_TEAM_ID is required` | The issuer is Slack and the team ID is empty. | Set `DINNERBELL_SLACK_TEAM_ID` as described in step 6, then recreate the service. |
| Startup error about a token prefix | The tokens are swapped or copied from the wrong page. | `DINNERBELL_SLACK_APP_TOKEN` starts with `xapp-` (Basic Information > App-Level Tokens). `DINNERBELL_SLACK_BOT_TOKEN` starts with `xoxb-` (Install App). |
| The test instance posts in its channel but sends no direct messages or mentions to someone | While `DINNERBELL_INSTANCE_LABEL` is set, Dinnerbell notifies only members of the test channel, and `/healthz` counts the withheld messages in `slack_dms_dropped`. If the log mentions a missing scope, it can't read the channel's members and notifies no one. | Invite the person to the test channel. For a missing scope, paste `deploy/slack-app-manifest.test.yaml` again under **App Manifest** and reinstall the app. |
| Production starts with an empty database after an upgrade | Compose created a new volume, because you ran it from a different directory, with a different project name, or as a different user. | See "Switch a source-built install to the prebuilt image". Your data is still in the old volume. |
| Rate limiting blocks everyone at once | Dinnerbell sees the proxy's address for every request. | Set `DINNERBELL_TRUSTED_PROXIES` to the proxy's single address, then recreate with `podman-compose down && podman-compose up -d`. A restart doesn't re-read `.env`. |
