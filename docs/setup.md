# Set up Dinnerbell

This guide installs Dinnerbell from a git clone: you create a Slack app, run the container behind HTTPS, and sign your household in. To install from a release bundle without git, follow [install.md](install.md) instead. Afterward, see [Back up and upgrade](backup-and-upgrade.md) and [hardening.md](hardening.md).

The commands use podman. With Docker, replace `podman-compose` with `docker compose` and `podman` with `docker`. The [configuration reference](configuration.md) lists every setting.

<a id="status"></a>

## Prerequisites

- A Linux host with podman and podman-compose 1.0.6 or later, or Docker with the compose plugin. The host pulls the image from `ghcr.io`.
- A public HTTPS hostname that a reverse proxy or tunnel, such as Pangolin, forwards to the host's port 8013.
- A Slack account. Its email address becomes the admin email.

## 0. Clone the repository

The clone holds `compose.yaml`, the Slack app manifests, and the example catalog; you build nothing. Clone it, and check out the newest release:

```sh
git clone https://github.com/dsmorgan/dinnerbell.git
cd dinnerbell
git checkout "$(git tag --list 'v*' --sort=-version:refname | head -n 1)"
```

Git reports a detached HEAD. That's expected: `compose.yaml` at a release tag names that release's image.

Keep the directory name `dinnerbell`, and run every command in this guide as the same user. Compose names the data volume after the directory, and rootless podman keeps each user's volumes apart, so another directory name or another user, including root, gets a new, empty database.

## 1. Create the Slack workspace and channel

1. At [slack.com/get-started](https://slack.com/get-started), create a workspace for your household. The free plan is enough.
2. Create a public channel named `#dinnerbell-channel`, or any name that isn't the bot's, so that `#dinnerbell` and `@Dinnerbell` don't look alike. A private channel also needs the `groups:read` bot scope, which you add in the manifest.

## 2. Create the Slack app

1. Copy the contents of `deploy/slack-app-manifest.yaml`. Don't edit it in your clone: local edits block `git checkout` when you upgrade.
2. At [api.slack.com/apps](https://api.slack.com/apps), select **Create New App** > **From a manifest**, and select your household workspace.
3. Paste the manifest as YAML.
4. In `redirect_urls`, replace `dinnerbell.example.com` with your hostname. Keep the path `/auth/callback`.
5. Select **Next**, then **Create**.
6. Optional: under **Basic Information** > **Display Information**, upload `deploy/slack-app-icon-1024.png` as the app icon.

If you plan to turn on the optional assistant, paste `deploy/slack-app-manifest.assistant.yaml` instead, or switch later as [Talk to it in Slack](configuration.md#talk-to-it-in-slack) describes.

## 3. Create the app-level token

The app connects out to Slack over Socket Mode, so Slack never calls your host.

1. Under **Basic Information** > **App-Level Tokens**, select **Generate Token and Scopes**.
2. Name the token `dinnerbell`, add the `connections:write` scope, and select **Generate**.
3. Copy the token, which starts with `xapp-`.

## 4. Install the app and copy the bot token

1. Under **Install App**, select **Install to Workspace**, then **Allow**.
2. Copy the **Bot User OAuth Token**, which starts with `xoxb-`.

## 5. Copy the sign-in credentials

Under **Basic Information** > **App Credentials**, copy the **Client ID** and **Client Secret**.

## 6. Find the team ID and channel ID

1. Open your workspace in a browser. The address looks like `https://app.slack.com/client/T0123ABCDEF/...`. Copy the part that starts with `T`: the team ID. Dinnerbell refuses Slack sign-ins from any other workspace.
2. In `#dinnerbell-channel`, select the channel name, and copy the channel ID at the bottom of the **About** tab. It starts with `C`.

## 7. Invite the bot to the channel

In `#dinnerbell-channel`, send `/invite @Dinnerbell`, and select **Add to Channel** if Slack asks. The bot posts only in channels it belongs to.

## 8. Expose Dinnerbell through your reverse proxy

Point your hostname at the host's port 8013, with the proxy's own sign-in turned off. Dinnerbell signs people in itself: a second login in front of it breaks the redirect back from Slack. With Pangolin:

1. Create a public HTTP resource for your hostname, with the target on the Dinnerbell host, port 8013.
2. In the resource's authentication settings, turn off **Use Platform SSO**, which is on by default.
3. Leave password, PIN, email OTP, and header auth off.

`compose.yaml` publishes port 8013 on `DINNERBELL_BIND`, which defaults to `127.0.0.1`. If the proxy runs on another host or in a container network, set `DINNERBELL_BIND` in `.env` to the host's address on the interface the proxy reaches, such as `192.168.1.20`. Avoid `0.0.0.0` unless a firewall limits who can reach the port.

## 9. Run Dinnerbell

1. Write `.env` with `dinnerbell init`. It asks for your hostname, admin email, time zone, household name, and the Slack values from sections 3 to 6, and hides secrets as you type. It generates the secret key, writes `.env` with mode 600, and checks it.

   ```sh
   VERSION="$(git describe --tags --exact-match | sed 's/^v//')"
   podman run --rm -it --userns keep-id --user "$(id -u):$(id -g)" -v "$PWD:/out:z" \
     "ghcr.io/dsmorgan/dinnerbell:$VERSION" init --output /out/.env
   ```

   With Docker, leave out `--userns keep-id`. To write `.env` by hand instead, copy `.env.example`, which explains each setting.

   Check these before you start:

   - The hostname must be the one people type. If the browser's address differs, such as a LAN IP, every form fails with "Cross-origin request refused".
   - Dinnerbell reads the household name and time zone on first start only, and ignores later changes, with a warning in the log ([#334](https://github.com/dsmorgan/dinnerbell/issues/334)). The time zone sets when calls go out.

   Dinnerbell logs a warning for each secret in `.env`. To keep them in files instead, see [Keep secrets in files](#keep-secrets-in-files).

2. Pull the image that `compose.yaml` names, and start the service.

   ```sh
   podman-compose pull
   podman-compose up -d
   ```

   With rootless podman, Dinnerbell stays down after a reboot until you start it again; see [Start at boot](#start-at-boot).

3. Check that Slack is connected. Within a few seconds, the response includes `"status":"ok"` and `"slack":"connected"`. If you started before inviting the bot, Slack shows `error` until you do; Dinnerbell checks again every minute.

   ```sh
   curl -s https://dinnerbell.example.com/healthz
   ```

   If it doesn't connect, see [troubleshooting.md](troubleshooting.md#check-healthz).

## 10. Sign in as the admin

1. Open your URL, select **Sign in with Slack**, and approve the request.
2. Open **Admin** > **Members**.
3. Add each household member by email. Sign-in is invite-only. For a member who uses Slack, the email must match their Slack account's.

## 11. Onboard family members

For each member who uses Slack, invite them to the workspace (**Invite people** in Slack) with the email you added. Then forward them this message:

> **Set up Dinnerbell on your phone**
>
> 1. Install Slack, and allow its notifications.
> 2. Accept the invitation Slack emailed you.
> 3. In Slack, open `#dinnerbell-channel`, tap the channel name, tap **Notifications**, and choose **All new messages**.
> 4. Open `https://dinnerbell.example.com` in your browser and tap **Sign in with Slack**. Slack asks you to sign in again: enter your email, type the code Slack emails you, and tap **Allow**.

Dinnerbell links each member's Slack account when they sign in, or by matching email; [members.md](members.md#link-slack-accounts) has the details.

For a member who doesn't use Slack, or to let members install Dinnerbell on their phone with push notifications, turn on **Admin** > **Web app**. [Add a member without Slack](members.md#add-a-member-without-slack) shows the steps.

**Join from the channel** is on by default: anyone in the channel can join as a guest, and you get a direct message for each join. To add only the people you choose, turn it off on **Admin** > **Members**. See [members.md](members.md).

## 12. Load the catalog

The catalog is your list of restaurants and home meals. [catalog-format.md](catalog-format.md) describes the file, [seed/example.yaml](../seed/example.yaml) is an example, and [seed/catalog.schema.json](../seed/catalog.schema.json) is its JSON Schema.

To write your first catalog quickly, give an AI assistant the schema, the example, and a source, such as delivery receipts in your email or a list of places you write yourself, and ask for a file that validates against the schema. Whatever you give the assistant goes to its provider.

1. Add everyone on **Admin** > **Members** first. The import matches `cooks` emails only to members already added.
2. On the **Catalog** page, select **Import a catalog file**, paste or upload the file, and select **Preview**. Check the preview, then select **Apply**.

To import a file on first start instead, see [Load a catalog on first start](configuration.md#load-a-catalog-on-first-start).

## Going live

While you try Dinnerbell out, it records test calls, votes, meals, and feedback. Before the household relies on it, run `dinnerbell reset-history` once, on production, so History and stats start clean. It deletes calls and everything in them, the meal log, feedback, contributions and badges, assistant conversations and drafts, and the Slack notification queue. It keeps the catalog, members, Slack links, preferences, sign-ins, the audit log, and every setting.

1. Call off every open call: on its card or in Slack, select **Call it off**. The reset refuses while any call is scheduled or still going. Calling off someone else's call for a later day sends them a direct message, so tell them first.
2. [Back up now](backup-and-upgrade.md#back-up-now), and copy the backup off the host.
3. Run the reset in the running container.

   ```sh
   podman-compose exec dinnerbell dinnerbell reset-history
   ```

   The command prints the database path, the household name, and what it deletes and keeps in each table. Check that **Notifications not yet sent** is 0; if you just called off a call, type anything but `reset`, wait a minute, and run it again. If it says the scheduler posts today's call again, run the reset after today's answer deadline.

   Then type `reset`, or without a terminal, run it again with `--yes`. The command backs up the database to `/data/backups/pre-reset-VERSION-TIME.db` first.

4. Check that History and Stats are empty, and that the catalog, Members, Preferences, and Schedule are unchanged.

Old messages stay in Slack, and their buttons reply "No such call." Days you skipped stay skipped. The next scheduled call posts as usual. To undo the reset, [roll back](backup-and-upgrade.md#roll-back) to the `pre-reset-*.db` file, skipping step 2.

## Optional setup

### Keep secrets in files

By default, the four secrets (`DINNERBELL_SECRET_KEY`, `DINNERBELL_OIDC_CLIENT_SECRET`, `DINNERBELL_SLACK_BOT_TOKEN`, and `DINNERBELL_SLACK_APP_TOKEN`) are lines in `.env`. podman-compose passes them to podman as `-e NAME=value`, where any local user can see them in `ps` while podman creates the container. If other people can log in to the host, keep the secrets in files, which compose mounts read-only at `/run/secrets/<name>`. The assistant's Anthropic API key works the same way, with one more step: see [The assistant](configuration.md#the-assistant).

1. From the clone's directory, write the four files from `.env`. Each file is owner-only and holds the bare value with no trailing newline, and no value lands on a command line.

   ```sh
   ( umask 077
     mkdir -p secrets
     for name in SECRET_KEY OIDC_CLIENT_SECRET SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
         sed -n "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}DINNERBELL_$name[[:space:]]*=[[:space:]]*//p" .env | tail -n 1 | tr -d '\r' | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" | tr -d '\n' >"secrets/DINNERBELL_$name"
     done )
   ```

   The `sed` removes one pair of enclosing quotes and nothing else. If a value has a trailing `# comment` or an escape inside double quotes, write its file by hand with `printf '%s' '<value>' >secrets/<name>`, after `unset HISTFILE`, so the value stays out of your shell history. Every file must exist; an empty one is fine for a Slack token you don't use.

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

From now on, pass the same two `-f` options to every production compose command. If you have a `compose.override.yaml`, add `-f compose.override.yaml` after them, in step 5 too. [Run compose with secret files](#run-compose-with-secret-files) explains why.

To change a secret, delete its file, write it again, repeat step 3, and recreate the service. To go back to `.env`, put the lines back and run `up` without `compose.secrets.yaml`. For the test instance, see [Secrets in files for test](test-instance.md#secrets-in-files-for-test).

If Dinnerbell stops at start because of a secret file, the message names the file:

| Message | Cause | Fix |
|---|---|---|
| `Can't read the secret file /run/secrets/NAME (PermissionError)` | The file isn't owned by UID 10001 with mode 600. | Repeat step 3. With SELinux enforcing and podman-compose 1.6.0 or later, add `x-podman.relabel: z` to the secret in a `compose.override.yaml`, and add `-f compose.override.yaml` last (untested). |
| `/run/secrets/NAME is a directory, not a file` | The file didn't exist when the engine mounted it, so the engine created a directory. | Remove the directory, create the file, and recreate the service. |
| `NAME is required but not set` | The file is empty, or you started without `compose.secrets.yaml`. | Write the secret into the file, or add `-f compose.yaml -f compose.secrets.yaml`. |
| `NAME is set in the environment ... with different values` | A line in `.env` and the file disagree. | Delete the line from `.env`. |

#### Run compose with secret files

After you move the secrets to files, pass `-f compose.yaml -f compose.secrets.yaml` to every production compose command, not only `up`: `pull`, `down`, `exec`, `run`, `logs`, and the rest. Without them, `up` starts Dinnerbell without its secrets, so it stops with `NAME is required but not set`, and compose restarts it in a loop. `run` starts a one-off container without them, so commands such as `catalog export` fail the same way.

When you pass `-f`, compose no longer reads `compose.override.yaml` on its own. If you have one, add it last:

```sh
podman-compose -f compose.yaml -f compose.secrets.yaml -f compose.override.yaml up -d
```

The [boot unit](#start-at-boot) needs the same `-f` options if you use it. The test instance needs none of this: `deploy/dinnerbell-test` picks its files itself.

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
   # Only if you keep secrets in files (with a compose.override.yaml, add -f compose.override.yaml after them):
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

See [troubleshooting.md](troubleshooting.md) for common problems and their fixes, what `/healthz` reports, and how to read the logs.

## Moved sections

These sections moved to their own pages:

- <a id="link-everyones-slack-account"></a>[Link everyone's Slack account](members.md#link-slack-accounts)
- <a id="guests-and-joining-from-the-channel"></a>[Guests and joining from the channel](members.md#join-from-the-channel)
- <a id="back-up"></a>[Back up](backup-and-upgrade.md#back-up)
- <a id="what-pruning-keeps"></a>[What pruning keeps](backup-and-upgrade.md#what-pruning-keeps)
- <a id="what-a-backup-holds"></a><a id="what-the-audit-log-and-backups-hold"></a>[What a backup holds](backup-and-upgrade.md#what-a-backup-holds)
- <a id="what-else-to-back-up"></a>[What else to back up](backup-and-upgrade.md#what-else-to-back-up)
- <a id="copy-backups-off-the-host"></a>[Copy backups off the host](backup-and-upgrade.md#copy-backups-off-the-host)
- <a id="back-up-now"></a>[Back up now](backup-and-upgrade.md#back-up-now)
- <a id="restore-on-a-new-host"></a>[Restore on a new host](backup-and-upgrade.md#restore-on-a-new-host)
- <a id="upgrade"></a>[Upgrade](backup-and-upgrade.md#upgrade)
- <a id="roll-back"></a>[Roll back](backup-and-upgrade.md#roll-back)
- <a id="verify-an-image"></a>[Verify an image](backup-and-upgrade.md#verify-an-image)
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
- <a id="switch-a-source-built-install-to-the-prebuilt-image"></a>[Switch a source-built install to the prebuilt image](https://github.com/dsmorgan/dinnerbell/blob/v0.16.0/docs/switch-install.md#switch-a-source-built-install-to-the-prebuilt-image) (before 0.2.0 only; kept in 0.16.0's docs)
- <a id="build-from-source"></a>[Build from source](switch-install.md#build-from-source)
- <a id="resource-and-log-limits"></a>[Resource and log limits](configuration.md#resource-and-log-limits)
- <a id="read-the-logs"></a>[Read the logs](troubleshooting.md#read-the-logs)
