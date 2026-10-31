# Set up Dinnerbell

This guide installs Dinnerbell from a git clone. You create a Slack app, run the container, put it behind HTTPS, and sign your household in. To install from a release bundle without git, follow [install.md](install.md) instead. Once Dinnerbell works, harden it with [hardening.md](hardening.md).

The commands use podman. With Docker, replace `podman-compose` with `docker compose` and `podman` with `docker`. The [configuration reference](configuration.md) lists every setting.

<a id="status"></a>

## Prerequisites

- A Linux host with podman and podman-compose 1.0.6 or later, or Docker with the compose plugin. The host pulls the image from `ghcr.io`. These docs are tested with podman-compose 1.0.6 (Ubuntu 24.04's), 1.1.0, 1.5.0, and 1.6.0, and Docker Compose 2.40.
- A public HTTPS hostname that a reverse proxy forwards to the host's port 8013. The reference setup uses Pangolin.
- A Slack account. Its email address becomes the admin email.

## 0. Clone the repository

The clone holds `compose.yaml`, the Slack app manifests, and the example catalog. You build nothing. Clone it, then check out the newest release:

```sh
git clone https://github.com/dsmorgan/dinnerbell.git
cd dinnerbell
git checkout "$(git tag --list 'v*' --sort=-version:refname | head -n 1)"
```

Git reports a detached HEAD. That's expected: `compose.yaml` at a release tag names that release's image, so production follows release tags, never `main`.

Keep the directory name `dinnerbell`, and run every command in this guide as the same user, with or without `sudo`. Compose names the data volume after the directory (`dinnerbell_dinnerbell-data`), and rootless podman keeps each user's volumes apart. Another directory name or another user, including root, gets a new, empty database. [hardening.md](hardening.md) recommends a dedicated user.

## 1. Create the Slack workspace and channel

1. At [slack.com/get-started](https://slack.com/get-started), create a workspace for your household. The free plan is enough: its 90-day history and 10-app limits don't matter, because Dinnerbell keeps its own records and is the only app.
2. Create a public channel named `#dinnerbell`. A private channel works too, but the Slack app then also needs the `groups:read` scope to read who's in the channel.

## 2. Create the Slack app

Don't edit files in your clone, including the manifest. Local edits block `git checkout` when you upgrade. You set your hostname in Slack instead.

1. Copy the contents of `deploy/slack-app-manifest.yaml`.
2. At [api.slack.com/apps](https://api.slack.com/apps), select **Create New App** > **From a manifest**, and select your household workspace.
3. Paste the manifest as YAML.
4. In `redirect_urls`, replace `dinnerbell.example.com` with your hostname. Keep the path `/auth/callback`.
5. Select **Next**, then **Create**.
6. Optional: under **Basic Information** > **Display Information**, upload `deploy/slack-app-icon-1024.png` as the app icon.

To keep a copy of the manifest with your hostname, write it to a file ending in `.local.yaml`, which git ignores:

```sh
sed 's#dinnerbell.example.com#dinnerbell.your-domain.example#' \
  deploy/slack-app-manifest.yaml > deploy/slack-app-manifest.local.yaml
```

If your hostname changes, edit the redirect URL under **OAuth & Permissions**.

## 3. Create the app-level token

The app connects out to Slack over Socket Mode, so Slack never calls your host. The manifest turns Socket Mode on; you create its token.

1. Under **Basic Information** > **App-Level Tokens**, select **Generate Token and Scopes**.
2. Name the token `dinnerbell`, add the `connections:write` scope, and select **Generate**.
3. Copy the token, which starts with `xapp-`. This is `DINNERBELL_SLACK_APP_TOKEN`.

## 4. Install the app and copy the bot token

1. Under **Install App**, select **Install to Workspace**, then **Allow**.
2. Copy the **Bot User OAuth Token**, which starts with `xoxb-`. This is `DINNERBELL_SLACK_BOT_TOKEN`.

## 5. Copy the sign-in credentials

Under **Basic Information** > **App Credentials**, copy these values:

- **Client ID**: `DINNERBELL_OIDC_CLIENT_ID`.
- **Client Secret**: `DINNERBELL_OIDC_CLIENT_SECRET`.

## 6. Find the team ID and channel ID

1. Open your workspace in a browser. The address looks like `https://app.slack.com/client/T0123ABCDEF/...`.
2. Copy the part that starts with `T`. This is `DINNERBELL_SLACK_TEAM_ID`. Dinnerbell requires it with Slack sign-in and refuses sign-ins from any other workspace, so nobody in another workspace can claim an invitation with a matching email.
3. In `#dinnerbell`, select the channel name, and copy the channel ID at the bottom of the **About** tab. It starts with `C`. This is `DINNERBELL_SLACK_CHANNEL`. You can also copy it from the browser's address bar.

## 7. Invite the bot to the channel

The bot posts only in channels it belongs to. In `#dinnerbell`, send `/invite @Dinnerbell`, and select **Add to Channel** if Slack asks.

If you start Dinnerbell before you invite the bot, `/healthz` reports Slack as `error` until you do. Dinnerbell checks again every minute, so you don't need to restart it.

## 8. Expose Dinnerbell through your reverse proxy

Point your hostname at the host's port 8013, with the proxy's own sign-in turned off. Dinnerbell signs people in itself: a second login in front of it breaks the redirect back from Slack and makes every device sign in twice. With Pangolin:

1. Create a public HTTP resource for your hostname, with the target on the Dinnerbell host, port 8013.
2. In the resource's authentication settings, turn off **Use Platform SSO**, which is on by default.
3. Leave password, PIN, email OTP, and header auth off.

Pangolin still provides TLS and connection-level protections. `/healthz` is public, and shows only coarse status and the running version.

`compose.yaml` publishes port 8013 on `DINNERBELL_BIND`, which defaults to `127.0.0.1`. That works only when Newt runs on the same host and outside a container network. Otherwise, set it in `.env` to the host's address on the interface Newt reaches, such as its LAN address. Avoid `0.0.0.0` unless a firewall limits who can reach the port.

```sh
DINNERBELL_BIND=192.168.1.20
```

## 9. Run Dinnerbell

1. Create `.env` from the example, readable only by you, because it holds secrets.

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

2. Generate a secret key, or use `openssl rand -base64 48`.

   ```sh
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

3. In `.env`, set these values:

   | Setting | Value |
   |---|---|
   | `DINNERBELL_BASE_URL` | The URL people type, such as `https://dinnerbell.example.com`: the same host and port, with `https://` and no path. |
   | `DINNERBELL_SECRET_KEY` | The key from step 2. |
   | `DINNERBELL_ADMIN_EMAIL` | The email on your Slack account. |
   | `DINNERBELL_HOUSEHOLD_NAME` | Your household's name, such as `Home`. |
   | `DINNERBELL_TIMEZONE` | Your time zone, such as `America/New_York`. |
   | `DINNERBELL_OIDC_ISSUER` | `https://slack.com` |
   | `DINNERBELL_OIDC_CLIENT_ID`, `DINNERBELL_OIDC_CLIENT_SECRET` | From section 5. |
   | `DINNERBELL_SLACK_TEAM_ID`, `DINNERBELL_SLACK_CHANNEL` | From section 6. |
   | `DINNERBELL_SLACK_BOT_TOKEN` | From section 4. |
   | `DINNERBELL_SLACK_APP_TOKEN` | From section 3. |

   Before you start Dinnerbell, check these rules:

   - If `DINNERBELL_BASE_URL` differs from the address in the browser, such as a LAN IP, every form fails with "Cross-origin request refused". Dinnerbell builds the sign-in redirect from it, too.
   - Dinnerbell refuses a secret key that is shorter than 32 characters, has fewer than 8 distinct characters, or contains placeholder text such as `REPLACE-ME`. A generated key always passes.
   - Dinnerbell reads the household name and time zone on first start only, then keeps them in its database.
   - If you change the admin email later, Dinnerbell invites the new address as another admin on its next start.

   Dinnerbell logs a warning for each of the four secrets in `.env`. To keep them in files instead, see [Keep secrets in files](#keep-secrets-in-files). It's optional.

4. Pull the image that `compose.yaml` names. To check where it came from, see [Verify an image](#verify-an-image).

   ```sh
   podman-compose pull
   ```

5. Start the service.

   ```sh
   podman-compose up -d
   ```

   With rootless podman, Dinnerbell stays down after a reboot until you start it again. To start it at boot, see [Start at boot](#start-at-boot) (optional).

6. Check that Slack is connected. Within a few seconds of the start, the response includes `"status":"ok"` and `"slack":"connected"`.

   ```sh
   curl -s https://dinnerbell.example.com/healthz
   ```

   If it doesn't, see [troubleshooting.md](troubleshooting.md#check-healthz), or read the log with `podman-compose logs dinnerbell | grep -i slack`.

## 10. Sign in as the admin

1. Open your URL, select **Sign in with Slack**, and approve the request.
2. Open **Admin**, which opens on its **Members** tab.
3. Add each household member by the email address you'll invite to Slack. Sign-in is invite-only, and the email must match the one on their Slack account.

## 11. Onboard family members

Invite each person to the Slack workspace (**Invite people** in Slack) with the email you added on **Admin** > **Members**. Then forward them this message:

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

Dinnerbell needs each member's Slack account to mention them, message them, and accept their button taps. **Admin** > **Members** shows who is linked. Dinnerbell links a member in any of these ways:

- When they sign in to the web app with Slack.
- At start and every hour, by email, for active members whose Dinnerbell email matches their Slack email. This links only full members of your workspace, never guests or people from other organizations.
- By hand: on **Admin** > **Members**, select **Edit**, and enter their Slack member ID. In Slack, open their profile, select the three dots, then **Copy member ID**.

A link you set or clear by hand stays as you left it. To hand a member back to automatic linking, select **Back to automatic** on their edit page.

### Guests and joining from the channel

**Join from the channel** is on by default: anyone in the channel can join as a guest, and you get a direct message for each join. To add only the people you choose, turn it off on **Admin** > **Members**; on an existing install, do it right after upgrading. [members.md](members.md) explains guests, joining, and linking a guest to a member.

## 12. Load the catalog

The catalog is your list of restaurants and home meals. Start from [seed/example.yaml](../seed/example.yaml); [catalog-format.md](catalog-format.md) describes the format.

1. Invite everyone first (section 11), so the import can match cooks to members.
2. On the **Catalog** page, paste or upload your file, check the preview, and apply it.

To import a file on first start instead, see [Load a catalog on first start](configuration.md#load-a-catalog-on-first-start).

## Going live

While you try Dinnerbell out, it records test calls, votes, meals, and feedback. Before the household relies on it, run `dinnerbell reset-history` once, on production, so History and stats start clean.

The reset deletes every call and everything in it (answers, suggestions, proposals, votes, decisions, and assignments), the meal log, feedback on meals, and unsent Slack notifications. It keeps the catalog, members and roles, Slack links, preferences and vetoes, the schedule and settings, sign-ins, and the audit log.

1. Call off every open call: on its card or in Slack, select **Call it off**. The reset refuses while a call is scheduled, taking answers, voting, or decided. Calling off a call someone else scheduled for a later day sends them a direct message, so tell them first.
2. [Back up now](#back-up-now), and copy the backup off the host.
3. Run the reset in the running container.

   ```sh
   podman-compose exec dinnerbell dinnerbell reset-history
   ```

   Before it changes anything, the command prints the database path, the household name, and how many rows it deletes and keeps in each table. Check these lines:

   - **Notifications not yet sent** should be 0. If you just called off a call, type anything but `reset`, wait a minute, and run the command again.
   - **Days off kept** lists today's and later days whose call was skipped or called off. They stay off. To have a call on one after all, start it from App Home, Today, or `/dinnerbell call`.
   - If it says the scheduler posts today's call again, today's call has finished but its answers are still open. To avoid a second call, run the reset after the answer deadline.

   Then type `reset`. Without a terminal, run the command again with `--yes`. The command first backs up the database to `/data/backups/pre-reset-VERSION-TIME.db`, which nightly pruning never deletes, then deletes the history in one transaction, and writes a `history.reset` audit row.

4. Check that History and Stats are empty, Today and App Home show no old calls, and the catalog, Members, Preferences, and Schedule are unchanged. The **Kept** counts the command printed match its preview's **Keeps** counts.

Old messages stay in Slack, and their buttons reply "No such call." Call IDs are never reused. The next scheduled call posts as usual.

To undo the reset, follow [Roll back](#roll-back) with the `pre-reset-*.db` file, skipping step 2. Anything recorded after the reset is lost.

## Back up

Every night at 03:30 household time, Dinnerbell backs up its database to `/data/backups/`, and also at start if the newest backup is more than a day old. It keeps the 14 newest daily copies and the newest copy from each of the last 8 weeks. It checks each copy with SQLite's `quick_check` and retries once. If both tries fail, it logs `database backup failed its check`, and `/healthz` reports `"backup": {"failing": true}` and `degraded` until a backup succeeds. It also deletes any unfinished `pre-*.partial` file after an hour.

<a id="what-the-audit-log-and-backups-hold"></a>Backups include the audit log, which holds member emails and Slack user IDs, so keep every copy as private as the database. Audit rows stay for good, except `auth.sign_in_refused` rows, which hold a non-member's email: an hourly job deletes them after 30 days, and Dinnerbell writes at most one per person and reason per hour.

The backups share a volume with the database, so copy them off the host with your own backup routine. Copy `/data/backups/` only, never the live `dinnerbell.db` or its WAL file.

1. Find the volume's path. Production's volume is `dinnerbell_dinnerbell-data`, and the test instance's is `dinnerbell-test_dinnerbell-data`; `podman volume ls` lists them.

   ```sh
   podman volume inspect dinnerbell_dinnerbell-data --format '{{.Mountpoint}}'
   ```

2. Point your backup routine at the `backups` directory in that path.

Only UID 10001 in the container can read the files. Under rootful podman or Docker, run your backup routine as root. Under rootless podman, copy them out through the user namespace. Running it again refreshes the copy.

```sh
mkdir -p ~/dinnerbell-backups
podman unshare cp -a "$(podman volume inspect dinnerbell_dinnerbell-data --format '{{.Mountpoint}}')/backups/." ~/dinnerbell-backups/
podman unshare chown -R 0:0 ~/dinnerbell-backups
```

### Back up now

Before an upgrade, take a fresh backup in the running container:

```sh
podman-compose exec dinnerbell python -c "import os, sqlite3, time; from dinnerbell import __version__ as v; os.umask(0o077); name = time.strftime(f'/data/backups/pre-upgrade-{v}-%Y%m%dT%H%M%SZ.db', time.gmtime()); src = sqlite3.connect('file:/data/dinnerbell.db?mode=ro', uri=True); dst = sqlite3.connect(name); src.backup(dst); dst.close(); src.close(); print(name)"
```

The command prints the backup's name, such as `/data/backups/pre-upgrade-0.2.0-20261024T013000Z.db`, with the running version and the UTC time. Write it down: [Roll back](#roll-back) needs it. Nightly pruning never deletes these files, so delete old ones yourself. Copy the backup off the host.

## Upgrade

Production runs a release tag, never `latest` or `main`, and your clone stays checked out at that release so that `compose.yaml` matches the image. Releases are listed in [CHANGELOG.md](../CHANGELOG.md). If you run a test instance, follow [Promote a release](test-instance.md#promote-a-release) instead, which rehearses on a copy of your data first.

1. Read the **Upgrade notes** of every release between yours and the new one.
2. [Back up now](#back-up-now). Note the file name, and copy the backup off the host.
3. Check out the new release, replacing `vX.Y.Z`.

   ```sh
   git fetch --tags
   git checkout vX.Y.Z
   ```

4. Pull the image, and [verify it](#verify-an-image).

   ```sh
   podman-compose pull
   ```

5. Recreate the service. `down` keeps the data volume; never add `-v`, which deletes it.

   ```sh
   podman-compose down && podman-compose up -d
   ```

6. Check that the page footer shows the new version, and that `/healthz` returns 200 with `"slack":"connected"`.

Migrations run on start and only move forward: an older version refuses to open the upgraded database. To go back, see [Roll back](#roll-back).

If the upgrade notes say the Slack manifest changed, paste the new manifest under **App Manifest** in the app settings, set your hostname in it again, then select **Install App** > **Reinstall to Workspace** and approve. The bot token stays the same. Until you reinstall, Dinnerbell keeps working and shows text where a feature needs a new scope, and a removed scope stays granted.

## Roll back

To roll back, run the previous release on the backup you took before the upgrade. Anything recorded since that backup is lost, so roll back soon after a bad upgrade.

1. Stop the service.

   ```sh
   podman-compose down
   ```

2. Check out the release you ran before, replacing `vX.Y.Z`.

   ```sh
   git checkout vX.Y.Z
   ```

3. List the backups, and find the one you took before the upgrade.

   ```sh
   podman-compose run --rm --entrypoint ls dinnerbell -l /data/backups
   ```

4. Replace the database with it, using its file name. The command assumes the default `DINNERBELL_DATABASE_URL`. It deletes the `-wal` and `-shm` files, which SQLite would otherwise apply to the restored file.

   ```sh
   podman-compose run --rm --entrypoint sh dinnerbell -c 'umask 077 && rm -f /data/dinnerbell.db-wal /data/dinnerbell.db-shm && cp /data/backups/pre-upgrade-X.Y.Z-YYYYMMDDTHHMMSSZ.db /data/dinnerbell.db'
   ```

5. Pull the previous image and start the service.

   ```sh
   podman-compose pull
   podman-compose up -d
   ```

6. Check that the footer shows the previous version.

When a fixed release ships, [upgrade](#upgrade) to it as usual.

## Verify an image

Every release image carries build provenance and SBOM attestations. To check that this repository's `ci.yml` built an image from its release tag, verify the digest your host pulled, on any machine with the [GitHub CLI](https://cli.github.com/). Replace `X.Y.Z` and the digest:

```sh
podman image inspect ghcr.io/dsmorgan/dinnerbell:X.Y.Z --format '{{.RepoDigests}}'
gh attestation verify oci://ghcr.io/dsmorgan/dinnerbell@sha256:... \
  --repo dsmorgan/dinnerbell \
  --signer-workflow dsmorgan/dinnerbell/.github/workflows/ci.yml \
  --source-ref refs/tags/vX.Y.Z
```

The release notes on GitHub list each release's digest; compare it with yours. A release tag is never re-pushed, but to run exactly what you verified, pin `image:` to `ghcr.io/dsmorgan/dinnerbell@sha256:...` in a `compose.override.yaml`. Don't use `podman auto-update` or Watchtower: they follow moving tags.

## Optional setup

### Keep secrets in files

By default, the four secrets (`DINNERBELL_SECRET_KEY`, `DINNERBELL_OIDC_CLIENT_SECRET`, `DINNERBELL_SLACK_BOT_TOKEN`, and `DINNERBELL_SLACK_APP_TOKEN`) are lines in `.env`, and Dinnerbell logs a warning for each. podman-compose passes every `.env` line to podman as `-e NAME=value`, so while podman creates the container, any local user can see the values in `ps`, and `podman-compose --dry-run` and `--verbose` print them. If other people can log in to the host, keep the secrets in files. Compose then mounts each file read-only at `/run/secrets/<name>`.

1. From the clone's directory, write the four files from `.env`. Each file is owner-only and holds the bare value with no trailing newline, and no value lands on a command line.

   ```sh
   ( umask 077
     mkdir -p secrets
     for name in SECRET_KEY OIDC_CLIENT_SECRET SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
         sed -n "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}DINNERBELL_$name[[:space:]]*=[[:space:]]*//p" .env | tail -n 1 | tr -d '\r' | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" | tr -d '\n' >"secrets/DINNERBELL_$name"
     done )
   ```

   The `sed` removes one pair of enclosing quotes and nothing else. If a value has a trailing `# comment` or an escape inside double quotes, write its file by hand with `printf '%s' '<value>' >secrets/<name>`, with a space before the command to keep it out of your shell history. Every file must exist; an empty one is fine for a Slack token you don't use.

2. Compare the files with `.env`. The check prints `match` or `differs` for each secret, never a value, and reports a missing file, a wrong mode, or a placeholder key. Every line must say `match`.

   ```sh
   deploy/check-secrets --env-file .env
   ```

3. Give the files to the container's user, UID 10001, with mode 600. Compose ignores `uid`, `gid`, and `mode` in the compose file. With rootful Docker, use `sudo chown 10001:10001` instead of `podman unshare chown`.

   ```sh
   for name in SECRET_KEY OIDC_CLIENT_SECRET SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
       chmod 600 "secrets/DINNERBELL_$name"
       podman unshare chown 10001:10001 "secrets/DINNERBELL_$name"
   done
   ```

4. Delete the four lines from `.env`, or leave them empty. If a line and its file both hold a value, they must match, or Dinnerbell refuses to start.
5. Recreate the service with `compose.secrets.yaml`, which mounts the files. The log no longer shows the `is set in the environment` warning.

   ```sh
   podman-compose down
   podman-compose -f compose.yaml -f compose.secrets.yaml up -d
   ```

From now on, pass the same two `-f` options to every `up`. Without them, Dinnerbell stops with `NAME is required but not set`.

To change a secret, delete its file, write it again, repeat step 3, and recreate the service. To go back to `.env`, put the lines back and run `up` without `compose.secrets.yaml`. For the test instance, see [Secrets in files for test](test-instance.md#secrets-in-files-for-test).

If Dinnerbell stops at start because of a secret file, the message names the file:

| Message | Cause | Fix |
|---|---|---|
| `Can't read the secret file /run/secrets/NAME (PermissionError)` | The file isn't owned by UID 10001 with mode 600. | Repeat step 3. With SELinux enforcing and podman-compose 1.6.0 or later, add `x-podman.relabel: z` to the secret in a `compose.override.yaml` (untested). |
| `/run/secrets/NAME is a directory, not a file` | The file didn't exist when the engine mounted it, so the engine created a directory. | Remove the directory, create the file, and recreate the service. |
| `NAME is required but not set` | The file is empty, or you started without `compose.secrets.yaml`. | Write the secret into the file, or add `-f compose.yaml -f compose.secrets.yaml`. |
| `NAME is set in the environment ... with different values` | A line in `.env` and the file disagree. | Delete the line from `.env`. |

### Start at boot

Rootless podman has no daemon, so after a reboot Dinnerbell stays down until you run `podman-compose up -d`. To start it at boot, install the systemd user unit `deploy/dinnerbell.service`. With rootful Docker, skip this: the daemon restarts Dinnerbell, because `compose.yaml` sets `restart: unless-stopped`. Rootless Docker installs its daemon as a user service of its own.

1. Let your user's services run without a login, and check that the second command shows `Linger=yes`.

   ```sh
   loginctl enable-linger "$USER"
   loginctl show-user "$USER" -p Linger
   ```

2. Install the unit.

   ```sh
   mkdir -p ~/.config/systemd/user && cp deploy/dinnerbell.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   ```

3. If any of the cases below applies, run `systemctl --user edit dinnerbell.service` and add only the lines for that case. The unit must start Dinnerbell exactly as you do; with the wrong compose files, the container starts without your secret files and stops.

   ```ini
   [Service]
   # Only if your install isn't in ~/dinnerbell:
   WorkingDirectory=/srv/dinnerbell
   # Only if you keep secrets in files:
   ExecStart=
   ExecStart=/usr/bin/env podman-compose -f compose.yaml -f compose.secrets.yaml up -d
   # Only if `systemctl --version` is older than 244, such as on RHEL 8, which refuses the unit otherwise:
   Restart=no
   ```

4. Enable the unit. `--now` also runs it once.

   ```sh
   systemctl --user enable --now dinnerbell.service
   ```

5. Check that `systemctl --user status dinnerbell.service` shows `active (exited)`, and that `podman-compose ps` shows the container `Up`. If either fails, `journalctl --user -u dinnerbell.service` and `podman-compose logs dinnerbell` show why.

At boot, the unit runs `podman-compose up -d`, found in `~/.local/bin`, `/usr/local/bin`, or `/usr/bin`. At shutdown, it runs `podman-compose stop`, which keeps the container and its logs. If `up` fails, the unit tries again 30 seconds later, and gives up after a few failures; to retry, run `systemctl --user reset-failed dinnerbell.service`, then `systemctl --user start dinnerbell.service`. Some podman-compose versions, such as 1.0.6, report success even when the container fails to start, so check step 5 after a reboot too.

- If you switch to the install's user with `sudo -iu`, first run `export XDG_RUNTIME_DIR=/run/user/$(id -u)`, or `systemctl --user` can't reach the user's service manager.
- To stop starting at boot, run `systemctl --user disable dinnerbell.service`. Add `--now` to stop Dinnerbell too.
- To start the test instance at boot, see [Start test at boot](test-instance.md#start-test-at-boot).

### Use a different sign-in provider

Dinnerbell signs people in with any OpenID Connect provider that publishes a discovery document. Slack still carries the calls, so you still do sections 1 to 4, the channel ID in section 6, and section 7. To use Google:

1. In the Google Cloud console, open **Google Auth Platform** and configure the OAuth consent screen.
2. Under **Audience**, add your household members as test users, or publish the app.
3. Create an OAuth client of type **Web application**, with `https://dinnerbell.example.com/auth/callback` as an authorized redirect URI.
4. In `.env`, set these values, and leave `DINNERBELL_SLACK_TEAM_ID` empty. If you keep secrets in files, put the client secret in `secrets/DINNERBELL_OIDC_CLIENT_SECRET` instead.

   ```sh
   DINNERBELL_OIDC_ISSUER=https://accounts.google.com
   DINNERBELL_OIDC_CLIENT_ID=<Google client ID>
   DINNERBELL_OIDC_CLIENT_SECRET=<Google client secret>
   ```

5. Invite members by the email on their Google account.

#### Switch providers on an existing install

Each member is linked to the account they first signed in with, so after a switch everyone sees "That email is already linked". To clear the links:

1. Stop the service: `podman-compose down`.
2. Clear every member's link. Members keep their emails, roles, and history. The command assumes the default `DINNERBELL_DATABASE_URL`, with the database at `/data/dinnerbell.db`; if you changed it, use your path.

   ```sh
   podman-compose run --rm --entrypoint python dinnerbell -c "import sqlite3; db = sqlite3.connect('/data/dinnerbell.db'); db.execute('UPDATE member SET oidc_subject = NULL'); db.commit()"
   ```

3. Start the service. Each member's next sign-in with a verified matching email links the new account.

## Troubleshooting

If something doesn't work, see [troubleshooting.md](troubleshooting.md). It lists common problems and their fixes, explains what `/healthz` reports, and shows how to read the logs.

## Moved sections

These sections moved to their own pages:

- <a id="test-instance"></a>[Test instance](test-instance.md)
- <a id="one-clone-two-instances"></a>[One clone, two instances](test-instance.md#one-clone-two-instances)
- <a id="create-the-test-channel-and-slack-app"></a>[Create the test channel and Slack app](test-instance.md#create-the-test-channel-and-slack-app)
- <a id="write-envtest"></a>[Write .env.test](test-instance.md#write-envtest)
- <a id="add-the-pangolin-resource"></a>[Add the Pangolin resource](test-instance.md#add-the-pangolin-resource)
- <a id="start-stop-and-refresh"></a>[Start, stop, and refresh](test-instance.md#start-stop-and-refresh)
- <a id="copy-production-data-into-test"></a>[Copy production data into test](test-instance.md#copy-production-data-into-test)
- <a id="discard-a-call-and-start-again"></a>[Discard a call and start again](test-instance.md#discard-a-call-and-start-again)
- <a id="promote-a-release"></a>[Promote a release](test-instance.md#promote-a-release)
- <a id="copy-the-catalog-between-production-and-test"></a>[Copy the catalog between production and test](catalog-format.md#copy-the-catalog-to-another-instance)
- <a id="verify-that-no-secret-is-on-a-command-line"></a>[Verify that no secret is on a command line](hardening.md#verify-that-no-secret-is-on-a-command-line)
- <a id="switch-between-a-clone-and-the-bundle"></a>[Switch between a clone and the bundle](switch-install.md#switch-between-a-clone-and-the-bundle)
- <a id="switch-a-source-built-install-to-the-prebuilt-image"></a>[Switch a source-built install to the prebuilt image](switch-install.md#switch-a-source-built-install-to-the-prebuilt-image)
- <a id="build-from-source"></a>[Build from source](switch-install.md#build-from-source)
- <a id="resource-and-log-limits"></a>[Resource and log limits](configuration.md#resource-and-log-limits)
- <a id="read-the-logs"></a>[Read the logs](troubleshooting.md#read-the-logs)
