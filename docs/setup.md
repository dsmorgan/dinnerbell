# Set up Dinnerbell

This guide takes you from nothing to a working Dinnerbell install: a Slack workspace and app, a container on your host, a public HTTPS hostname, and your family signed in. When it works, harden it with [hardening.md](hardening.md).

## Status

Dinnerbell is under construction. Milestones M0 through M2 ship these parts, and this guide covers them fully:

- Sign in with Slack, and the members admin page
- `/healthz`
- The scheduler and nightly backups
- The catalog, catalog file import, `DINNERBELL_SEED_FILE`, preferences, and meal history
- Dinner calls in Slack: the call, reminders, proposals and votes, the decision, and assignments, plus the App Home, `/dinnerbell call`, and calling off a call or closing a window early from Slack (`/dinnerbell cancel`)

As of M2, the Slack bot connects when `DINNERBELL_SLACK_BOT_TOKEN`, `DINNERBELL_SLACK_APP_TOKEN`, and `DINNERBELL_SLACK_CHANNEL` are all set. Offers, `/dinnerbell offer`, and `log` come in M3.

## Prerequisites

- A Linux host with podman and podman-compose 1.0.6 or later, or Docker with the compose plugin. The host pulls the image from `ghcr.io`. These docs are checked with podman-compose 1.0.6 (Ubuntu 24.04's), 1.1.0, 1.5.0, and 1.6.0, and Docker Compose 2.40.
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
3. Create a channel named `#dinnerbell`. Make it public: a private channel works too, but the Slack app then also needs the `groups:read` scope, which Dinnerbell uses to read who's in the channel.

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
8. Optional: open **Basic Information**, and under **Display Information**, upload `deploy/slack-app-icon-1024.png` as the app icon.

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

   Dinnerbell refuses to start with a key that is shorter than 32 characters, has fewer than 8 distinct characters, or contains a placeholder word (`replace`, `changeme`, `placeholder`, `example`, `yoursecret`, `secretkeyhere`; case, `-`, and `_` ignored), such as the text in `secrets/DINNERBELL_SECRET_KEY.example`. A generated key always passes, and an existing long, random key keeps working.

4. Edit `.env` and fill in the values you collected. To keep the four secrets out of `.env`, see [Keep secrets in files](#keep-secrets-in-files) (optional).

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
   ```

   `DINNERBELL_BASE_URL` must match the URL people type: the same host, with `https://` and no path. Dinnerbell derives the sign-in redirect URL from it and checks every form submission against it, so with any other host, such as a LAN IP, every form fails with "Cross-origin request refused".

   `DINNERBELL_ADMIN_EMAIL` must match the email on your Slack account. If you change it later, Dinnerbell invites the new address as another admin on the next start.

   `DINNERBELL_HOUSEHOLD_NAME` and `DINNERBELL_TIMEZONE` take effect on first start only. After that, Dinnerbell reads them from its database, so set them before you start it the first time.

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

    The response reports database reachability, the scheduler's last tick, the Slack state, the outbox (`backlog`: notifications waiting to be sent; `failed`: notifications that gave up; `oldest_pending_age`: seconds the oldest waiting notification has waited, `null` if none waits), and the last backup time. It returns HTTP 503 when the database is unreachable or the scheduler has not ticked for 5 minutes, so the container health check and Uptime Kuma catch both.

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

    A notification waiting more than an hour (`oldest_pending_age` above 3600) also sets `"status": "degraded"`: Slack or the network has been down that long, and a notification that waits 24 hours is lost. A Slack problem alone sets `"status": "degraded"` but keeps HTTP 200, so the container keeps running and you can still sign in to fix it. Notifications wait in the outbox until Slack is back. To have Uptime Kuma alert on Slack too, use an HTTP keyword monitor for `"slack":"connected"`.

    The logs show what happened:

    ```sh
    podman-compose logs dinnerbell | grep -i slack
    ```

### Keep secrets in files

This is optional. By default the four secrets, `DINNERBELL_SECRET_KEY`, `DINNERBELL_OIDC_CLIENT_SECRET`, `DINNERBELL_SLACK_BOT_TOKEN`, and `DINNERBELL_SLACK_APP_TOKEN`, are lines in `.env` like every other setting, and Dinnerbell logs a warning for each at start. The warning is about exposure: podman-compose passes every `.env` line to podman as `-e NAME=value`, so while podman creates the container, any local user can read the values in `ps`, and `podman-compose --dry-run` and `--verbose` print them. If other people can log in to the host, keep the secrets in files instead. Compose then mounts each file read-only at `/run/secrets/<name>`, so only its path is on a command line.

1. From the clone's directory, write the four files from `.env`. The subshell keeps `umask 077` from changing your shell, so the files are owner-only, and each holds the bare value with no trailing newline. No value lands on a command line.

   ```sh
   ( umask 077
     mkdir -p secrets
     for name in SECRET_KEY OIDC_CLIENT_SECRET SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
         sed -n "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}DINNERBELL_$name[[:space:]]*=[[:space:]]*//p" .env | tail -n 1 | tr -d '\r' | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" | tr -d '\n' >"secrets/DINNERBELL_$name"
     done )
   ```

   The `sed` removes one pair of enclosing quotes and nothing else. For a value with a trailing `# comment` or an escape inside double quotes, write the file by hand with `printf '%s' '<value>' >secrets/<name>` (put a space before the command to keep it out of your shell history). Every file must exist; an empty one is fine for a Slack token you don't use.

2. Compare the files with `.env`. It prints `match` or `differs` for each secret, never a value, and also reports a missing file, a wrong mode, or a placeholder key. Every line must say `match`.

   ```sh
   deploy/check-secrets --env-file .env
   ```

3. Give the files to the container's user, UID 10001, with mode 600. Compose mounts each file as it is and ignores `uid`, `gid`, and `mode` in the compose file, so a file owned by your account is unreadable inside the container. With rootful Docker, use `sudo chown 10001:10001`.

   ```sh
   for name in SECRET_KEY OIDC_CLIENT_SECRET SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
       chmod 600 "secrets/DINNERBELL_$name"
       podman unshare chown 10001:10001 "secrets/DINNERBELL_$name"
   done
   ```

4. Delete the four lines from `.env`, or leave them empty. If a line and its file both give a value, they must match, or Dinnerbell refuses to start.

5. Recreate the service with `compose.secrets.yaml`, which mounts the files. Add the same `-f` pair every time you run `up`; without it, Dinnerbell stops with `NAME is required but not set`.

   ```sh
   podman-compose down
   podman-compose -f compose.yaml -f compose.secrets.yaml up -d
   ```

   With Docker: `docker compose -f compose.yaml -f compose.secrets.yaml up -d`. The log shows no `is set in the environment` warning.

To change a secret later, delete its file, write it again, run step 3 again, and recreate the service. To go back to `.env`, put the lines back and run `up` without `compose.secrets.yaml`.

**Test instance:** do the same in `secrets-test/` from `.env.test` (`deploy/check-secrets --env-file .env.test secrets-test`). `deploy/dinnerbell-test` needs no `-f`: when any of the four files exists in `secrets-test/`, it adds the override itself and requires all four. Without them, test reads its secrets from `.env.test`.

If Dinnerbell stops at start because of a secret file, the message names the file:

| Message | Cause | Fix |
|---|---|---|
| `Can't read the secret file /run/secrets/NAME (PermissionError)` | The file isn't owned by UID 10001, or its mode isn't 600. | Run step 3 again. On a host with SELinux enforcing, a correct owner can still be refused: with podman-compose 1.6.0 or later, add `x-podman.relabel: z` to the secret in a `compose.override.yaml` (untested here). |
| `/run/secrets/NAME is a directory, not a file` | The file didn't exist when the engine mounted it, and the engine created a directory in its place. | Remove the directory, create the file, and recreate the service. |
| `NAME is required but not set` | The file is empty, or you started without `compose.secrets.yaml` after deleting the line from `.env`. | Write the secret into the file, or add `-f compose.yaml -f compose.secrets.yaml`. |
| `NAME is set in the environment ... with different values` | A line in `.env` and the file disagree. | Delete the line from `.env`. |

#### Verify that no secret is on a command line

Use a throwaway value that you can search for, on a test instance that keeps its secrets in files, because it replaces the secret.

1. Write a canary into the test instance's key file. Keep it at least 32 characters.

   ```sh
   rm -f secrets-test/DINNERBELL_SECRET_KEY
   ( umask 077; printf '%s' 'canary-0123456789-abcdefghijklmnopqrstuvwxyz' >secrets-test/DINNERBELL_SECRET_KEY )
   podman unshare chown 10001:10001 secrets-test/DINNERBELL_SECRET_KEY
   ```

2. In a second terminal, record the process list while the service starts.

   ```sh
   while :; do ps -eww -o args= | grep -F canary-0123456789 | grep -v grep; sleep 0.05; done
   ```

3. In the first terminal, start the service, then run the dry run and count the canary in its output. podman-compose 1.0.6 prints the commands with `--dry-run` alone; later versions need `--verbose` too.

   ```sh
   deploy/dinnerbell-test up -d
   DINNERBELL_COMPOSE='podman-compose --verbose --dry-run' deploy/dinnerbell-test up -d 2>&1 | grep -c canary-0123456789
   ```

   The second terminal prints nothing, and the count is `0`. (`grep -c` also exits 1 when the count is `0`.) Press Ctrl+C in the second terminal, then replace the canary with a real key:

   ```sh
   rm -f secrets-test/DINNERBELL_SECRET_KEY
   ( umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" >secrets-test/DINNERBELL_SECRET_KEY )
   podman unshare chown 10001:10001 secrets-test/DINNERBELL_SECRET_KEY
   ```

### Resource and log limits

`compose.yaml` and `deploy/compose.test.yaml` (through `compose.yaml`) cap the container, so a runaway process or a noisy log can't take the host:

| Key | Value | Why |
|---|---|---|
| `pids_limit` | `256` | Measured on the image: 6 processes and threads at idle, 47 after 300 concurrent requests. The cap is five times that. |
| `mem_limit` | `512m` | Measured: about 100 MiB at idle and 130 MiB peak after 300 concurrent requests. The cap leaves room for badge rendering and large catalog imports. |
| `logging` | `json-file`, `max-size: 10m`, `max-file: "3"` | Keeps at most 30 MB of logs per container. |

If you see the container killed for memory (`podman inspect` shows `OOMKilled`), raise `mem_limit` in a `compose.override.yaml`.

What each tool does with the keys:

| Tool | `mem_limit` | `pids_limit` | `logging` |
|---|---|---|---|
| Docker Compose | Applied | Applied | Applied |
| podman-compose 1.6.0 | Applied (`-m`) | Applied (`--pids-limit`) | Applied (`--log-driver`, `--log-opt`) |
| podman-compose 1.0.6 | Applied (`-m`) | Ignored, with no warning | Applied |

podman-compose 1.0.6 never reads `pids_limit` (confirmed in its source). On that version, the process cap is absent; upgrade podman-compose to get it. podman passes `max-size` to its log driver in every version, but older podman releases ignore `max-file`. `scripts/check-compose.sh` fails if any of these keys is missing from either rendering.

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

The **Members** page shows who is linked. Someone in the channel who isn't linked gets **Join Dinnerbell** when they tap a button (see below), or, with joining off, a private "I don't know your Slack account yet" message.

### Guests and joining from the channel

A guest eats with the household when a member invites them to a call: they answer In or Out and can pitch in, but never vote or suggest. See spec sections 3 and 5.5.

**Join from the channel** is on by default. Anyone in the Dinnerbell channel who isn't in Dinnerbell can then join as a guest: from **Join Dinnerbell** in Slack, or by signing in on the web with Slack. Dinnerbell checks with Slack first that they're a real person of your workspace who's in the channel; bots, deactivated accounts, and people from other organizations can't join. You get a direct message for each join, with **Make member** and **Remove**. Joined guests appear in every member's **Invite a guest** list.

- To add only the people you choose, turn **Join from the channel** off on **Members**. Do it right after upgrading if you don't want anyone to join on their own.
- **Add people from the channel** on **Members** lists everyone in the channel who isn't in Dinnerbell, to add each as a guest or a member.
- If someone you invited by email joins as a guest because their Slack email differs, select **Link to member** on their guest row: their Slack account moves to their membership. Anyone can take any name in Slack, so check the Slack account and the email Slack shares, shown in the join message and on the guest row. When that email isn't the member's, or they use a Slack guest account, the page shows both emails and asks you to type the member's name to link anyway. Admins aren't offered: an admin sets their own Slack member ID on their row.
- **Remove** signs a guest out and stops them joining again until you select **Add back**.

Joining needs no extra Slack scopes for a public channel: `channels:read` reads who's in it and `users:read` checks the person. A private channel needs `groups:read`.

## 12. Load the catalog

The catalog is your list of restaurants and home meals. [catalog-format.md](catalog-format.md) describes the file format, and [seed/example.yaml](../seed/example.yaml) is a starter file.

- **On first start:** mount a catalog file and set `DINNERBELL_SEED_FILE`, as described in steps 5 to 7 of section 9.
- **After first start:** paste or upload the file on the Catalog page.

On first start, only the admin is a member yet, so the seed skips every other cook email. After you invite everyone (section 11), import the same file again from the Catalog page to fill in the cooks. Fields in the file replace what's stored, so do this before you edit those items in the app.

### Copy the catalog between production and test

To give the [test instance](#test-instance) production's catalog, or to bring a catalog you tried out in test back to production, export it from one and import it into the other. An export holds the tags and items, never members, cooks, or preferences, so it's safe to move between instances. It's still your household's list of places and notes, so keep it out of public repositories.

1. Export the catalog from the instance you copy from. Either:

   - In the browser, open the Catalog page and select **Download catalog**.
   - On the host, run the export in a one-off container. For production:

     ```sh
     podman-compose run --rm -T dinnerbell catalog export > catalog.yaml
     ```

     For the test instance:

     ```sh
     deploy/dinnerbell-test run --rm -T dinnerbell catalog export > catalog.yaml
     ```

     With Docker: `docker compose run --rm -T dinnerbell catalog export > catalog.yaml`, and `DINNERBELL_COMPOSE="docker compose"` in front of `deploy/dinnerbell-test`.

     The image's entry point is `dinnerbell`, so `catalog export` after the service name is the command. Keep `-T`: without it, compose runs the command in a terminal, which changes line endings and mixes messages into the file. The export opens the database read-only, so it works whether the instance is running or stopped. It runs in the container as the app's user, so the empty `-wal` and `-shm` files SQLite creates next to a stopped instance's database belong to the app. It prints what it exported, such as `Exported 4 restaurants, 4 home meals, 25 tags.`, as a message rather than into the file.

2. In the instance you copy to, open the Catalog page, select **Import a catalog file**, upload `catalog.yaml`, and select **Preview**.

3. Check the preview. It lists the tags and items it will add and, for each item that matches by name, the fields it will change. If the catalogs already match, it says there's nothing to import. Then select **Apply**.

Items and tags match by name, ignoring case, and a match keeps the spelling the instance you copy to already has. A tag of a different kind in each instance, such as `vegetarian` as a protein in one and as other in the other, is an error, and nothing is imported until you merge or rename it on one side.

The import never deletes: items that exist only in the instance you copy to stay, as do each item's cooks. To retire an item in both, set it inactive in one and copy again.

## Going live

While you try Dinnerbell out, it records test calls, votes, meals, and feedback. Before the household relies on it, delete them, so History, "eaten lately" in proposals, and later stats start from a clean record. `dinnerbell reset-history` deletes every call and everything in it (answers, suggestions, proposals, votes, decisions, and assignments), the meal log, feedback on meals, and the outbox of Slack notifications. It keeps the catalog, members and their roles, Slack links, preferences and vetoes, the schedule and settings, sign-ins, and the audit log. Run it once, on production.

1. Call off every call that's still open: on its card or in Slack, select **Call it off**. The reset refuses while a call is scheduled, taking answers, voting, or decided, and lists those calls. Calling off a call that someone else scheduled for a later day sends them the usual direct message saying you called it off, so tell them first.
2. [Back up now](#back-up-now), and copy the backup off the host.
3. Reset the history in the running container:

   ```sh
   podman-compose exec dinnerbell dinnerbell reset-history
   ```

   With Docker: `docker compose exec dinnerbell dinnerbell reset-history`.

   Before it changes anything, the command prints the database path, the household name, and, for each table, how many rows it deletes and how many it keeps. Check the numbers:

   - **Notifications not yet sent** should be 0. If you just called a call off, Slack may not show it yet; type anything but `reset`, wait a minute, and run the command again.
   - **Days off kept** lists the days, today or later, whose call was skipped or called off. They stay off: the reset replaces each with a fresh called-off call, so the scheduler doesn't post that day's call. To have one after all, start it from App Home, Today, or `/dinnerbell call`.
   - If it says the scheduler posts today's call again, that call already finished while its answers are still open, so the next tick posts a fresh one. To avoid it, run the reset after the call's answer deadline.

   Then type `reset` to confirm. Without a terminal, run the command again with `--yes` added, after checking the same numbers. The command backs up the database to `/data/backups/pre-reset-VERSION-TIME.db`, such as `pre-reset-0.3.0-20261024T013000Z.db`, then deletes the history in one transaction, so it either finishes or changes nothing. The audit log records a `history.reset` row with the counts and the backup's name. Nightly pruning never deletes the backup.

4. Check the result:

   - History has no meals, and Stats, in versions that have it, is empty.
   - Today and App Home show no old calls.
   - The catalog, Members, Preferences, and Schedule look as they did. The **Kept** counts the command printed match the **Keeps** counts in its preview.

Messages from earlier calls stay in the Slack channel as they were. Dinnerbell never edits them again, and their buttons reply "No such call." Call IDs are never reused, so an old button can't reach a new call. The next scheduled call posts as usual.

To undo the reset, restore its `pre-reset-*.db` backup the way you'd restore a pre-upgrade one: follow steps 1, 3, and 4 of [Roll back](#roll-back) with the `pre-reset-*.db` file name, skip step 2 because you stay on the same release, then start the service with `podman-compose up -d`. Anything recorded after the reset is lost.

## Back up

Dinnerbell runs an online backup of its SQLite database every night at 03:30 household time into `/data/backups/`, and once at start if the newest backup is more than a day old. It keeps the 14 newest daily copies plus the newest copy from each of the last 8 weeks. Each nightly copy is checked with SQLite's `quick_check`; one that fails is deleted and made once more, and one that fails twice is deleted, logged as an error (`database backup failed its check`), and shown by `/healthz` as `"backup": {"failing": true}` with status `degraded` until a backup succeeds. Dinnerbell also deletes a `pre-*.partial` file (an unfinished one-off backup) once it is more than an hour old. In M0, only `/healthz` shows the last successful backup; the Admin page gains it later.

### What the audit log and backups hold

Audit rows record who did what, and their payloads hold member emails (for example `member.invited` and `member.updated`) and Slack user IDs. Nothing deletes them except one rule: an hourly job deletes `auth.sign_in_refused` rows older than 30 days, because they hold the email of someone who isn't a member. Dinnerbell writes at most one such row per person and reason per hour, however often they retry. Every other audit row stays for good. The database and every backup copy include the audit log, so a backup holds those emails too: keep `/data/backups/` and any copy of it as private as the database.

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

If the release's upgrade notes say the Slack app has a changed manifest (a new scope, or a scope removed), paste the new manifest under **App Manifest** in the app settings (set your hostname in it again), then select **Install App** > **Reinstall to Workspace** and approve the scopes. A removed scope only takes effect after the reinstall, and nothing breaks before it. Slack keeps the same bot token, so `.env` doesn't change. Until you reinstall, Dinnerbell keeps working and shows text where a new feature needs the scope: for example, 0.8.0 adds `files:write`, and badges show as text tags until the app has it.

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

The tag can move, so verify the exact image your host pulled, by its digest. Read the digest from the pulled image, then verify that digest:

```sh
podman image inspect ghcr.io/dsmorgan/dinnerbell:X.Y.Z --format '{{.RepoDigests}}'
gh attestation verify oci://ghcr.io/dsmorgan/dinnerbell@sha256:... \
  --repo dsmorgan/dinnerbell \
  --signer-workflow dsmorgan/dinnerbell/.github/workflows/ci.yml \
  --source-ref refs/tags/vX.Y.Z
```

With Docker: `docker image inspect` with the same arguments. The release notes on GitHub list each release's digest, which never changes. Compare it with the digest you read. To make production run exactly what you verified, pin `image:` in a `compose.override.yaml` to `ghcr.io/dsmorgan/dinnerbell@sha256:...`, or keep the version tag: a release tag is never re-pushed.

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

Setup takes about half an hour. Stop the test instance when you aren't using it.

### One clone, two instances

Both instances run from your production clone, as the same user. There's no second clone:

```text
~/dinnerbell/
├── compose.yaml              both instances
├── compose.secrets.yaml      optional: mounts the secret files
├── .env                      production's settings, and DINNERBELL_BIND for both
├── .env.test                 test's settings
├── secrets/                  optional: production's four secret files
├── secrets-test/             optional: test's four secret files
└── deploy/
    ├── compose.test.yaml     test's volume name
    ├── compose.test-secrets.yaml  test's secret files, when it has them
    └── dinnerbell-test       runs compose for test
```

| | Production | Test |
|---|---|---|
| Command | `podman-compose ...` | `deploy/dinnerbell-test ...` |
| Env file | `.env` | `.env.test` |
| Secret files (optional) | `secrets/`, with `-f compose.yaml -f compose.secrets.yaml` | `secrets-test/`, used when the files are there (named in `deploy/compose.test-secrets.yaml`) |
| Image tag | The release in `compose.yaml` | `main`, or the release you're rehearsing |
| Data volume | `dinnerbell_dinnerbell-data` (named by compose) | `dinnerbell-test_dinnerbell-data` (named in `deploy/compose.test.yaml`) |
| Host port | 8013 | 8014 |
| Compose project | `dinnerbell` | `dinnerbell-test` |
| Slack app and command | Dinnerbell, `/dinnerbell` | Dinnerbell Test, `/dinnerbell-test` |
| Pangolin authentication | Off | On |

The instances share the clone and its `compose.yaml`, the host address in `DINNERBELL_BIND`, and the Slack workspace (`DINNERBELL_SLACK_TEAM_ID`). Everything else is separate. The secret key, both Slack tokens, and the sign-in client secret must differ: `deploy/dinnerbell-test` refuses to start test when test shares one with production, whether each instance keeps it in its env file or in a secret file.

### Create the test channel and Slack app

Use the same workspace as production.

1. Create a channel named `#dinnerbell-test`, and invite only the people who test with you. While the test instance runs, it mentions and messages only members of this channel. The channel can be private.
2. Create a second Slack app as in [section 2](#2-create-the-slack-app), but paste `deploy/slack-app-manifest.test.yaml`, and replace `dinnerbell-test.example.com` with your test hostname. For its icon, upload `deploy/slack-app-icon-test-1024.png`, which shows Dinnerbot, the test mascot.
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

2. Fill it in with the test app's values, a new secret key, the test hostname as `DINNERBELL_BASE_URL`, and the same `DINNERBELL_SLACK_TEAM_ID` as production. Keep `DINNERBELL_INSTANCE_LABEL=TEST` and `DINNERBELL_SLACK_COMMAND=/dinnerbell-test`. To keep test's secrets in files instead, see [Keep secrets in files](#keep-secrets-in-files).

To start from your `.env` instead, for example to keep your time zone and proxy settings, don't copy the whole file: it would hold production's secrets, and any value you forget to replace would stay production's. Copy it without the lines test must set itself, add those lines back empty, and fill in every blank:

```sh
(umask 077 && grep -Ev '^[[:space:]]*(export[[:space:]]+)?DINNERBELL_(SECRET_KEY|OIDC_CLIENT_ID|OIDC_CLIENT_SECRET|SLACK_BOT_TOKEN|SLACK_APP_TOKEN|SLACK_CHANNEL|SLACK_COMMAND|BASE_URL|INSTANCE_LABEL|HOUSEHOLD_NAME)[[:space:]]*=' .env >.env.test)
cat >>.env.test <<'EOF'
DINNERBELL_INSTANCE_LABEL=TEST
DINNERBELL_SLACK_COMMAND=/dinnerbell-test
DINNERBELL_HOUSEHOLD_NAME=Test
DINNERBELL_BASE_URL=
DINNERBELL_SECRET_KEY=
DINNERBELL_OIDC_CLIENT_ID=
DINNERBELL_OIDC_CLIENT_SECRET=
DINNERBELL_SLACK_BOT_TOKEN=
DINNERBELL_SLACK_APP_TOKEN=
DINNERBELL_SLACK_CHANNEL=
EOF
chmod 600 .env.test
```

Before every start, `deploy/dinnerbell-test` checks `.env.test` (and test's secret files, if any) and names each setting that's missing or invalid, so a required blank you missed stops it with a message instead of a container that restarts forever.

Git ignores `.env.test`. Never commit it.

### Add the Pangolin resource

1. Create a public HTTP resource for the test hostname, with the target on the host's port 8014.
2. Leave **Use Platform SSO** on, and allow only your own Pangolin user.

Unlike production, test runs unreleased code and only you use it, so it stays behind Pangolin's login. Sign in to Pangolin first, then select **Sign in with Slack**. If Slack's redirect lands on a Pangolin login page, sign in to Pangolin and select **Sign in with Slack** again.

Pangolin's login protects only the hostname. Port 8014 is open to anything that can reach `DINNERBELL_BIND`, such as your LAN, where only Dinnerbell's own Slack sign-in stands in the way. Use your host firewall to allow port 8014 only from the address Newt connects from. With rootful Docker, published ports skip `ufw` rules, so add the rule to the `DOCKER-USER` chain instead.

### Start, stop, and refresh

`deploy/dinnerbell-test` runs `podman-compose` with the test project, volume, env file, tag, and port, and takes any compose command:

```sh
deploy/dinnerbell-test pull
deploy/dinnerbell-test up -d
deploy/dinnerbell-test logs dinnerbell
deploy/dinnerbell-test down
```

Each run first prints the project, volume, env file, tag, and port it uses. To pick up a newer `main`, run `pull`, then `down` and `up -d`. The `main` tag moves with every merge to `main` and isn't pinned or attested per version, so treat the test instance as running unreleased code: give it no production data except the scrubbed copy, and don't verify it as a release. To check it, run `curl -s http://<DINNERBELL_BIND>:8014/healthz` and look for `"instance":"TEST"`, or look for the TEST banner in the browser. Test is usually stopped, so don't give it an always-on Uptime Kuma monitor.

Before any command that can start a container, such as `up`, `run`, `start`, or `restart`, the script checks the env file and, if test keeps its secrets in files, the four secret files. Only commands that stop or inspect test, such as `down`, `stop`, `logs`, `ps`, and `pull`, skip the check. It runs `dinnerbell config check` in a one-off container of the same image, with no network, no published port, no logging, and a read-only file system. It passes the env file on standard input and, if test keeps its secrets in files, mounts them read-only at `/run/secrets`, as the service gets them; only their paths are on the command line. The image declares `/data` a volume, so the engine gives the container an empty anonymous volume, and removes it with the container. If a setting is missing or invalid, the check prints the setting's name and the reason, never the value, and the script starts nothing. The script pulls the image first if it isn't on the host. An image from before the check, such as 0.2.0, starts with a warning instead.

Run every test command through `deploy/dinnerbell-test`, never through `podman-compose` directly. The script sets the project, the volume, the env file, the tag, and the port together. Setting only some of them by hand mixes the instances: for example, `.env.test` without `-p dinnerbell-test` runs test's label and Slack app on production's database, where `scrub-test` would then run. The script refuses to run when:

- `.env.test` is missing, sets `DINNERBELL_INSTANCE_LABEL` more than once, or leaves it empty.
- `secrets-test/` holds some of test's four secret files but not all, or one is a directory. An empty file is allowed for a Slack token. With none of them, test reads its secrets from `.env.test`.
- It can't read `.env.test` or `.env`. Run it as the user who owns them.
- Test has the same `DINNERBELL_SECRET_KEY`, `DINNERBELL_SLACK_BOT_TOKEN`, `DINNERBELL_SLACK_APP_TOKEN`, or `DINNERBELL_OIDC_CLIENT_SECRET` as production, whether each holds it on a line in `.env` and `.env.test` or in a file in `secrets/` and `secrets-test/`, or has production's value of one of them anywhere in a line of `.env.test`, such as a commented-out copy. Shared tokens split Slack's events between the instances. The script reads the env files as compose does and compares SHA-256 hashes of the files' contents, so it never prints a value or puts one on a command line. Your account can't read files owned by UID 10001, so with podman it reads them through `podman unshare`; with Docker, run the script with `sudo`, or it refuses because it can't compare. It skips these checks for commands that only stop or inspect test.
- The configuration check fails, as described above.
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
| `DINNERBELL_ENGINE` | `docker` if `DINNERBELL_COMPOSE` starts with `docker`, else `podman` | Container engine for the configuration check |

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

3. Pick a production backup: the newest nightly `dinnerbell-YYYY-MM-DD.db`, or a `pre-upgrade-*.db` from [Back up now](#back-up-now). The commands use production's release image, which is already on the host; replace `X.Y.Z` with the release production runs, shown in its footer. To list the backups:

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

   Before it changes anything, `scrub-test` prints the database path, the household name, its last activity, and counts of active members, enabled slots, open events, unsent notifications, and feedback on meals. Check that the household and counts match the copy you restored, and that the last activity is from around when the backup was taken. Then type `scrub` to confirm. If it refuses because there's no terminal to ask on, check the same numbers in its output, and run the command again with `--yes` added.

   `scrub-test` deactivates and unlinks from Slack every member you didn't keep, turns off every slot, ends open calls without notifying anyone, empties the outbox, marks badges not yet announced as quiet and the last monthly recap as posted, and deletes all feedback on meals, which says how each member rated them. It leaves the meal log, catalog, and preferences alone. It refuses to run, and changes nothing, unless `DINNERBELL_INSTANCE_LABEL` is set. It migrates the database first, so run it with the same tag you start next: an older release refuses a database that a newer one migrated. Running it again changes nothing.

6. Start the test instance.

   ```sh
   deploy/dinnerbell-test up -d
   ```

Slots are off after a scrub. To rehearse a scheduled call, turn one on on the **Schedule** page, or start one with `/dinnerbell-test call`.

### Discard a call and start again

On production, a call that's decided, done, or past its meal time stays: history matters there, and "There's already a dinner call for that day" stops a second one. On test, you can throw a call away to test the same slot and day again. These controls work only while `DINNERBELL_INSTANCE_LABEL` is set, the same check `scrub-test` uses; on production they don't appear, the pages are a 404, and the command refuses.

- **Discard a call.** As an admin, select **Discard (test)** on the call's card, check what the confirmation page says it deletes, and select **Discard (test)** again. In Slack, run `/dinnerbell-test discard`, which discards today's dinner call; add `breakfast` or `lunch`, `tomorrow`, or a date such as `2026-10-24`. From the host, run:

  ```sh
  deploy/dinnerbell-test run --rm dinnerbell discard-call 12
  ```

  `12` is the call's ID, from its page's address, `/events/12`. The command prints the call and how many answers, suggestions, proposals, votes, assignments, meal log entries, feedback on that meal, and unsent notifications it has, then asks you to type `discard`. Add `--yes` to skip the question. It works while test is running; the running instance sends the Slack edits.

  Discarding deletes the call and everything in it, in one step: answers (with guests and role offers), suggestions, proposals, votes, the decision, assignments, the meal it logged and any feedback on it, and its notifications that weren't sent yet. The call's messages in Slack change to "Discarded (test)", with no buttons. The audit log keeps an `event.discarded` row with the call's ID, slot, date, and counts. You can't undo a discard. Discarded IDs are never reused, so an old Slack button or link can't reach the next call.

- **Start a called-off call again at any time.** On production, **Start dinner again** after a call-off works only until the usual meal time plus two hours. On test, it works at any time that day, on the web, in App Home, and with `/dinnerbell-test call`.

### Promote a release

Use this checklist for every release. It runs the exact release image and compose files on a fresh copy of production's data before your family sees it. Replace `X.Y.Z` with the new release, and note the release production runs now.

1. Read the release's **Upgrade notes** in [CHANGELOG.md](../CHANGELOG.md).
2. Check out the new release, so the rehearsal uses its `compose.yaml` and `deploy/dinnerbell-test`. Production keeps running its current container until you recreate it in step 10.

   ```sh
   git fetch --tags
   git checkout vX.Y.Z
   ```

3. Pull the release and [verify it](#verify-an-image) against the digest your host pulled, not just the tag. Don't go on if verification fails.

   ```sh
   DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test pull
   podman image inspect ghcr.io/dsmorgan/dinnerbell:X.Y.Z --format '{{.RepoDigests}}'
   gh attestation verify oci://ghcr.io/dsmorgan/dinnerbell@sha256:... \
     --repo dsmorgan/dinnerbell \
     --signer-workflow dsmorgan/dinnerbell/.github/workflows/ci.yml \
     --source-ref refs/tags/vX.Y.Z
   ```

   Production pulls the same tag in step 10. Check that its digest equals the one you verified (or pin the digest in a `compose.override.yaml`).

4. In production, [back up now](#back-up-now). Note the file name, and copy the backup off the host.
5. Follow steps 1 to 4 of [Copy production data into test](#copy-production-data-into-test) with that backup.
6. Scrub the copy and start test with the release. Every command names the release, so the scrub migrates the copy exactly as production's upgrade will.

   ```sh
   DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test run --rm dinnerbell scrub-test --keep you@example.com
   DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test up -d
   ```

7. Check that the page shows the TEST banner and `X.Y.Z` in the footer, and that `/healthz` reports `"status":"ok"`.
8. Rehearse a call: run `/dinnerbell-test call` in `#dinnerbell-test`, respond, vote, and let it reach a decision. Check that nobody outside the test channel was notified. While the label is set, `/healthz` reports `slack_dms_dropped`, the number of direct messages withheld from people outside the channel.
9. Optional: if the rehearsal took long enough for production to record something new, such as a call or a meal, [back up now](#back-up-now) again, and use that backup if you roll back.
10. Upgrade production. The clone is already on `vX.Y.Z`.

    ```sh
    podman-compose pull
    podman-compose down && podman-compose up -d
    ```

11. Check production's footer and `/healthz`.
12. Stop the test instance: `deploy/dinnerbell-test down`.

If the rehearsal fails, stop the test instance, and check out the release production runs, so the clone matches production again. Production hasn't changed.

If production misbehaves after step 10, [roll back](#roll-back) with the newest pre-upgrade backup.

## Use a different sign-in provider

The web app works with any OpenID Connect provider that publishes a discovery document. To use Google:

1. In the Google Cloud console, open **Google Auth Platform** and configure the OAuth consent screen.
2. Under **Audience**, add your household members as test users, or publish the app.
3. Create an OAuth client of type **Web application**.
4. Add `https://dinnerbell.example.com/auth/callback` as an authorized redirect URI.
5. In `.env`, set the following values and leave `DINNERBELL_SLACK_TEAM_ID` unset. If you keep secrets in files, write the client secret to `secrets/DINNERBELL_OIDC_CLIENT_SECRET` instead of `.env`.

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
| `/healthz` reports `"slack": "not_configured"` | One of `DINNERBELL_SLACK_BOT_TOKEN`, `DINNERBELL_SLACK_APP_TOKEN` (in `.env` or its secret file), or `DINNERBELL_SLACK_CHANNEL` is missing. The logs say so at start. | Set all three, then recreate the service with `podman-compose down && podman-compose up -d`. |
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
| The log warns `ignoring environment variables that match no setting` | A `DINNERBELL_` variable in `.env` has a name Dinnerbell doesn't know, most likely a typo such as `DINNERBELL_TRUSTED_PROXY`. Dinnerbell ignores it, so the setting keeps its default. The warning names the variable and never its value. | Fix the name in `.env` (see `.env.example`), then recreate the service. |
| Startup error: `DINNERBELL_SECRET_KEY: is still a placeholder` | The key still holds placeholder text, such as the text from `secrets/DINNERBELL_SECRET_KEY.example`, or a repetitive value. | Generate a key as in step 3 of section 9 and put it in `.env` (or its secret file). For secret files, `deploy/check-secrets` reports the same problem before you start. |
| `migrate` or `serve` says `The database is locked: another Dinnerbell process is probably migrating it` | A second process (a `migrate` or `reset-history` run while the service starts) held the database's write lock for about 15 seconds. Nothing changed. | Wait for the other process to finish, then run it again. |
| Startup error: `DINNERBELL_SLACK_TEAM_ID is required` | The issuer is Slack and the team ID is empty. | Set `DINNERBELL_SLACK_TEAM_ID` as described in step 6, then recreate the service. |
| Startup error: `Can't read the secret file` or `is a directory` | You keep secrets in files, and Dinnerbell can't read one: it isn't owned by UID 10001 with mode 600, or it didn't exist when the engine mounted it. | See the table in [Keep secrets in files](#keep-secrets-in-files). |
| Startup error about a token prefix | The tokens are swapped or copied from the wrong page. | `DINNERBELL_SLACK_APP_TOKEN` starts with `xapp-` (Basic Information > App-Level Tokens). `DINNERBELL_SLACK_BOT_TOKEN` starts with `xoxb-` (Install App). |
| The test instance posts in its channel but sends no direct messages or mentions to someone | While `DINNERBELL_INSTANCE_LABEL` is set, Dinnerbell notifies only members of the test channel, and `/healthz` counts the withheld messages in `slack_dms_dropped`. If the log mentions a missing scope, it can't read the channel's members and notifies no one. | Invite the person to the test channel. For a missing scope, paste `deploy/slack-app-manifest.test.yaml` again under **App Manifest** and reinstall the app. |
| Badges show as text tags (🥇 Chef · Gold) instead of art, and the log says `badge art not available` with `missing_scope` | The Slack app doesn't have the `files:write` scope, which uploads the badge art. Dinnerbell tries again every 15 minutes. | Paste the current manifest under **App Manifest**, then reinstall the app (see "Upgrade"). Art appears on the next badge post or App Home view after the retry. |
| Badges show as text tags, and the log says `slack refused badge art` | Slack refused the art in a post or App Home view. The line carries Slack's error code (`slack_error`) and its explanation (`slack_messages`). Dinnerbell retries a post with a new file up to twice; each refused try logs a line. | Send the line to the Dinnerbell issue tracker (#110). Dinnerbell has already posted text badges, so nothing is lost. |
| Production starts with an empty database after an upgrade | Compose created a new volume, because you ran it from a different directory, with a different project name, or as a different user. | See "Switch a source-built install to the prebuilt image". Your data is still in the old volume. |
| The test port, 8014, accepts connections but never answers | The test container is in a restart loop: Dinnerbell stops at start, usually on a missing or invalid setting in `.env.test`, and compose restarts it. Port forwarding keeps the port open in between. | Run `podman ps -a --filter name=dinnerbell-test`, and look for `Restarting` or a recent `Exited`. Read why with `deploy/dinnerbell-test logs dinnerbell`. Fix `.env.test`, then run `deploy/dinnerbell-test down` and `deploy/dinnerbell-test up -d`; the configuration check names any setting that's still wrong. |

### Read the logs

Dinnerbell writes one JSON line per log entry to standard output. Every state-changing action on a call or offer writes one `info` line, wherever it came from, and so does every refusal. To see what happened to a call, and why something was refused, follow its lines.

Each line has these fields:

| Field | What it holds |
|---|---|
| `msg` | What happened: an action such as `answer`, `offer_role`, `propose`, `vote`, `lock`, `assign`, `call_off`, `offer_posted`, `join`, `join_late`, or `late_switch`; a scheduler step such as `opened`, `voting`, `decided`, `abandoned`, or `stale_closed`; or `refused`. Deliveries to Slack are `notification delivered`, `notification delivery failed` (it retries), `notification delivery gave up`, and `notification closed` (superseded, such as by a call-off). |
| `event_id`, `run`, `slot`, `day` | The call or offer. `run` goes up each time a call starts again. |
| `state_before`, `state_after` | The call's state before and after the action. A refusal has `state` instead. |
| `member_id`, `member` | Who acted, by ID and display name. The scheduler's lines have neither. |
| `item_id`, `item`, `mode` | The place, when there is one, such as the pick proposed, voted for, or decided. |
| `channel` | Where the action came from: `slack`, `web`, `cli`, or `scheduler`. |
| `request_id` | One tap, request, or scheduler run. Every line it writes has the same ID, and the web returns it in the `X-Request-ID` header. |
| `reason`, `refusal` | On a refusal: a short code, such as `not_in`, `responses_closed`, `cook_list`, or `stale_run`, and the message the member saw. |
| `kind`, `channel_id`, `message_ts` | On a delivery: the notification, and where Slack posted it. `message_ts` is the message's Slack timestamp. |
| `end_reason` | Why a call ended on its own: `too_few`, `no_coverage`, or `missed`. |

Logs never contain emails, tokens, sign-in subjects, or calendar URLs. Notes and call-off reasons that members write appear only as their length, `note_length`.

To follow call 12, filter on its ID:

```sh
podman-compose logs dinnerbell 2>&1 | grep '"event_id": 12,'
```

With `jq`, show just the story. `grep -o` drops anything compose puts before each line's JSON:

```sh
podman-compose logs dinnerbell 2>&1 | grep -o '{.*}' \
  | jq -c 'select(.event_id == 12) | {ts, msg, member, item, state_after, reason, refusal}'
```

To see everything one tap did, filter on its `request_id` instead: `jq -c 'select(.request_id == "…")'`.
