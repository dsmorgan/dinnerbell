# Run a test instance

A test instance is a second Dinnerbell on the same host, with its own database, Slack app, channel, and hostname. Use it to run builds from `main`, and to rehearse each release on a copy of your data before your family sees it. Production never runs untested code. [ADR 0006](adr/0006-test-instance.md) explains the design.

The test instance is optional. Setup takes about half an hour, after a working production install ([setup.md](setup.md)). Stop the test instance when you aren't using it. The commands use podman; for Docker, see [Docker](#docker).

## One clone, two instances

Both instances run from your production clone or bundle, as the same user:

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
| Secret files (optional) | `secrets/`, with `-f compose.yaml -f compose.secrets.yaml` | `secrets-test/`, used when the files are there |
| Image tag | The release in `compose.yaml` | `main` in a clone, the bundle's release in a bundle, or the release you're rehearsing |
| Data volume | `dinnerbell_dinnerbell-data` | `dinnerbell-test_dinnerbell-data` |
| Host port | 8013 | 8014 |
| Compose project | `dinnerbell` | `dinnerbell-test` |
| Slack app and command | Dinnerbell, `/dinnerbell` | Dinnerbell Test, `/dinnerbell-test` |
| Pangolin authentication | Off | On |

The instances share `compose.yaml`, the host address in `DINNERBELL_BIND`, and the Slack workspace (`DINNERBELL_SLACK_TEAM_ID`). The secret key, both Slack tokens, and the client secret must differ: `deploy/dinnerbell-test` refuses to start test with any of production's.

## Create the test channel and Slack app

Use the same workspace as production.

1. Create a channel named `#dinnerbell-test`, and invite only the people who test with you. While test runs, it mentions and messages only members of this channel. The channel can be private.
2. Create a second Slack app as in [section 2 of setup.md](setup.md#2-create-the-slack-app), but paste `deploy/slack-app-manifest.test.yaml`, and replace `dinnerbell-test.example.com` with your test hostname. For its icon, upload `deploy/slack-app-icon-test-1024.png`, which shows Dinnerbot, the test mascot.
3. Create its app-level token, install it, and copy its bot token, client ID, and client secret, as in sections 3 to 5.
4. Copy the test channel's ID, as in section 6.
5. In `#dinnerbell-test`, invite the bot: `/invite @Dinnerbell Test`.

Never reuse production's tokens. Socket Mode delivers each event to one connection only, so two instances on one app split button taps between them.

## Write .env.test

In a bundle, run `dinnerbell init` with `--test --output /out/.env.test` (see [install.md](install.md#options)). It writes the test settings, and refuses production's secrets from `.env`. In a clone:

1. Copy the example, and restrict access to it. Git ignores `.env.test`; never commit it.

   ```sh
   cp .env.test.example .env.test
   chmod 600 .env.test
   ```

2. Fill it in with the test app's values, a new secret key, the test hostname as `DINNERBELL_BASE_URL`, and production's `DINNERBELL_SLACK_TEAM_ID`. Keep `DINNERBELL_INSTANCE_LABEL=TEST` and `DINNERBELL_SLACK_COMMAND=/dinnerbell-test`.

To start from your `.env` instead, for example to keep your time zone and proxy settings, don't copy the whole file: any production secret you forget to replace would stay. These commands copy it without the lines test must set itself, and add those back empty. Fill in every blank.

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

`deploy/dinnerbell-test` checks `.env.test` before every start, and names each setting that's missing or invalid.

### Secrets in files for test

To keep test's secrets in files, follow [Keep secrets in files](setup.md#keep-secrets-in-files) in `secrets-test/`, from `.env.test`, and check them with `deploy/check-secrets --env-file .env.test secrets-test`. Test needs no `-f` options: when any of the four files exists in `secrets-test/`, `deploy/dinnerbell-test` mounts them and requires all four. Without them, test reads its secrets from `.env.test`.

## Add the Pangolin resource

1. Create a public HTTP resource for the test hostname, with the target on the host's port 8014.
2. Leave **Use Platform SSO** on, and allow only your own Pangolin user.

Test runs unreleased code that only you use, so it stays behind Pangolin's login. Sign in to Pangolin first, then select **Sign in with Slack**. If Slack's redirect lands on a Pangolin login page, sign in to Pangolin and select **Sign in with Slack** again.

Pangolin protects only the hostname. Port 8014 is open to anything that can reach `DINNERBELL_BIND`, such as your LAN. Use your host firewall to allow port 8014 only from the address Newt connects from. With rootful Docker, published ports skip `ufw` rules, so add the rule to the `DOCKER-USER` chain.

## Start, stop, and refresh

`deploy/dinnerbell-test` runs `podman-compose` with test's project, volume, env file, tag, and port, and takes any compose command. Each run first prints what it uses.

```sh
deploy/dinnerbell-test pull
deploy/dinnerbell-test up -d
deploy/dinnerbell-test logs dinnerbell
deploy/dinnerbell-test down
```

- To pick up a newer `main`, run `pull`, then `down` and `up -d`. The `main` tag moves with every merge and isn't attested per version, so give test no production data except a scrubbed copy, and don't verify it as a release.
- To check test, run `curl -s http://<DINNERBELL_BIND>:8014/healthz` and look for `"instance":"TEST"`, or look for the TEST banner in the browser. Test is usually stopped, so don't give it an always-on monitor.
- A new test instance starts with an empty database and invites `DINNERBELL_ADMIN_EMAIL`. Add test members on **Members**, or [copy production's data](#copy-production-data-into-test).

Always run test through `deploy/dinnerbell-test`, never through `podman-compose` directly. The script sets the project, volume, env file, tag, and port together; setting only some of them by hand mixes the instances. For example, `.env.test` without `-p dinnerbell-test` runs test's label and Slack app on production's database. Compose reads `${...}` values in `compose.yaml` from the shell and from production's `.env`, never from the env file, so the script sets test's values in the shell. Test doesn't read your `compose.override.yaml`, and always publishes on production's `DINNERBELL_BIND`.

To change what the script uses, set these in front of a command, such as `DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test up -d`. Never `export` them: production's compose commands in the same shell would read them too, and an exported `DINNERBELL_ENV_FILE=.env.test`, for example, starts production with test's label and Slack app.

| Variable | Default | Use |
|---|---|---|
| `DINNERBELL_TAG` | `main` in a clone, the bundle's release in a bundle | Image tag, such as a release you're rehearsing |
| `DINNERBELL_ENV_FILE` | `.env.test` | Test configuration |
| `DINNERBELL_PORT` | `8014` | Host port |
| `DINNERBELL_COMPOSE` | `podman-compose` | Set to `docker compose` for Docker |
| `DINNERBELL_ENGINE` | `docker` if `DINNERBELL_COMPOSE` starts with `docker`, else `podman` | Container engine for the configuration check |

### What the script checks

Before any command that can start a container, such as `up`, `run`, `start`, or `restart`, the script runs `dinnerbell config check` in a one-off container of the same image, pulling the image first if it's missing. That container has no network, no published port, no logging, a read-only file system, and an anonymous `/data` volume that's removed with it. It reads the env file on standard input and any secret files read-only at `/run/secrets`. If a setting is missing or invalid, it prints the setting's name and the reason, never the value, and the script starts nothing. Images older than the check, such as 0.2.0, start with a warning instead. Commands that only stop or inspect test, such as `down`, `stop`, `logs`, `ps`, and `pull`, skip the checks.

The script also refuses to run when:

- `.env.test` is missing, sets `DINNERBELL_INSTANCE_LABEL` more than once, or leaves it empty.
- `secrets-test/` holds some of the four secret files but not all, or one is a directory. An empty file is fine for a Slack token.
- It can't read `.env.test` or `.env`. Run it as the user who owns them.
- Test shares `DINNERBELL_SECRET_KEY`, `DINNERBELL_SLACK_BOT_TOKEN`, `DINNERBELL_SLACK_APP_TOKEN`, or `DINNERBELL_OIDC_CLIENT_SECRET` with production, in env files or secret files, or `.env.test` contains one of production's values anywhere in a line, such as a commented-out copy. The script compares SHA-256 hashes, so it never prints a value or puts one on a command line. It reads secret files through `podman unshare`; with Docker, run it with `sudo`, or it refuses because it can't compare.
- The env file is production's `.env`.
- `.env` sets `DINNERBELL_TAG`, `DINNERBELL_PORT`, or `DINNERBELL_ENV_FILE`, which podman-compose 1.1.0 and older would apply to test.
- Production's compose project is also named `dinnerbell-test`, from the directory name or `COMPOSE_PROJECT_NAME`.
- The first argument is a compose option, such as `-p` or `-f`. Put options after the subcommand.
- You run it through a symlink.

### Start test at boot

To start test at boot with podman, install `deploy/dinnerbell-test.service` as in [Start at boot](setup.md#start-at-boot), with `dinnerbell-test.service` in place of `dinnerbell.service`. It runs `deploy/dinnerbell-test up -d`. Leave it off if you start test only when you need it. It starts test on the script's default tag, so a reboot ends a rehearsal on another tag.

## Copy production data into test

A copy of production's data tests migrations and new features against real history. It contains member emails, so keep it on the host, and scrub it before test starts.

1. Stop test. If it has never run, start and stop it once so that its volume exists.

   ```sh
   deploy/dinnerbell-test down
   ```

2. Check that both volumes exist. If production's has another name, find it with `podman volume ls`, and use it below.

   ```sh
   podman volume exists dinnerbell_dinnerbell-data && podman volume exists dinnerbell-test_dinnerbell-data && echo ok
   ```

3. Pick a production backup: the newest nightly `dinnerbell-YYYY-MM-DD.db`, or a `pre-upgrade-*.db` from [Back up now](setup.md#back-up-now). The commands use production's image, which is already on the host; replace `X.Y.Z` with the release production runs. To list the backups:

   ```sh
   podman run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
     --entrypoint ls -v dinnerbell_dinnerbell-data:/prod:ro \
     ghcr.io/dsmorgan/dinnerbell:X.Y.Z /prod/backups
   ```

4. Copy it over test's database, replacing the file name. Production's volume is mounted read-only, so the copy can't change it.

   ```sh
   podman run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
     --entrypoint sh \
     -v dinnerbell_dinnerbell-data:/prod:ro \
     -v dinnerbell-test_dinnerbell-data:/data \
     ghcr.io/dsmorgan/dinnerbell:X.Y.Z \
     -c 'umask 077 && rm -f /data/dinnerbell.db-wal /data/dinnerbell.db-shm && cp /prod/backups/dinnerbell-YYYY-MM-DD.db /data/dinnerbell.db'
   ```

5. Scrub the copy, with test still stopped. Keep at least one active admin, usually yourself, and repeat `--keep` for each person who tests with you.

   ```sh
   deploy/dinnerbell-test run --rm dinnerbell scrub-test --keep you@example.com
   ```

   Before it changes anything, `scrub-test` prints the database path, the household name, its last activity, and counts of active members, enabled slots, open events, unsent notifications, and feedback on meals. Check that they match the backup you restored, then type `scrub`. Without a terminal, check the same output and run the command again with `--yes`.

6. Start test.

   ```sh
   deploy/dinnerbell-test up -d
   ```

`scrub-test` deactivates and unlinks from Slack every member you didn't keep, turns off every slot, ends open calls without notifying anyone, empties the outbox, marks unannounced badges as quiet and the last monthly recap as posted, and deletes all feedback on meals. It keeps the meal log, catalog, and preferences. It refuses to run unless `DINNERBELL_INSTANCE_LABEL` is set. It migrates the database first, so run it with the tag you start next: an older release refuses a database that a newer one migrated. Running it again changes nothing.

Slots are off after a scrub. To rehearse a scheduled call, turn one on on the **Schedule** page, or start one with `/dinnerbell-test call`.

## Copy the catalog between production and test

To copy a catalog between instances, export it from one and import it into the other. An export holds tags and items, never members, cooks, or preferences. It's still your household's list of places and notes, so keep it out of public repositories.

1. Export the catalog from the instance you copy from: on the **Catalog** page, select **Download catalog**. Or, on the host, run the export in a one-off container:

   ```sh
   podman-compose run --rm -T dinnerbell catalog export > catalog.yaml           # production
   deploy/dinnerbell-test run --rm -T dinnerbell catalog export > catalog.yaml   # test
   ```

   Keep `-T`: without it, compose runs the command in a terminal, which changes line endings and mixes messages into the file. The export opens the database read-only, so it works whether the instance runs or not, and it prints a summary such as `Exported 4 restaurants, 4 home meals, 25 tags.` outside the file.

2. In the other instance, on the **Catalog** page, select **Import a catalog file**, upload `catalog.yaml`, and select **Preview**.
3. Check the tags and items it adds and the fields it changes, then select **Apply**.

Items and tags match by name, ignoring case, and keep the target's spelling. A tag with a different kind in each instance, such as `vegetarian` as a protein in one and as other in the other, is an error, and nothing is imported until you rename or merge it. The import never deletes: items only in the target stay, with their cooks. To retire an item in both, set it inactive in one and copy again. [catalog-format.md](catalog-format.md#export-the-catalog) has the export's rules.

## Discard a call and start again

On production, a call that's decided, done, or past its meal time stays. On test, you can throw a call away to test the same slot and day again. These controls work only while `DINNERBELL_INSTANCE_LABEL` is set: on production they don't appear, their pages return 404, and the command refuses.

- **On the web:** as an admin, select **Discard (test)** on the call's card, check what the confirmation page says it deletes, and select **Discard (test)** again.
- **In Slack:** `/dinnerbell-test discard` discards today's dinner call. Add `breakfast` or `lunch`, and `tomorrow` or a date such as `2026-10-24`.
- **On the host:** run `deploy/dinnerbell-test run --rm dinnerbell discard-call 12`, where `12` is the call's ID from its address, `/events/12`. The command prints the call and what it holds, then asks you to type `discard`; `--yes` skips the question. It works while test runs, and the running instance updates Slack.

Discarding deletes the call and everything in it in one step: answers (with guests and role offers), suggestions, proposals, votes, the decision, assignments, the meal it logged and its feedback, and its unsent notifications. Its Slack messages change to "Discarded (test)", with no buttons. The audit log keeps an `event.discarded` row. You can't undo a discard, and IDs are never reused.

On test, you can also start a called-off call again at any time that day, on the web, in App Home, or with `/dinnerbell-test call`. On production, **Start dinner again** works only until the usual meal time plus two hours.

## Promote a release

Use this checklist for every release. It runs the exact release image and compose files on a fresh copy of production's data. Replace `X.Y.Z` with the new release, and note the release production runs now.

1. Read the release's **Upgrade notes** in [CHANGELOG.md](../CHANGELOG.md).
2. Check out the new release, so the rehearsal uses its `compose.yaml` and `deploy/dinnerbell-test`. Production keeps running its current container until step 10.

   ```sh
   git fetch --tags
   git checkout vX.Y.Z
   ```

3. Pull the release and [verify its digest](setup.md#verify-an-image). Don't go on if verification fails.

   ```sh
   DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test pull
   ```

4. In production, [back up now](setup.md#back-up-now). Note the file name, and copy the backup off the host.
5. Follow steps 1 to 4 of [Copy production data into test](#copy-production-data-into-test) with that backup.
6. Scrub the copy and start test on the release. Every command names the release, so the scrub migrates the copy exactly as production's upgrade will.

   ```sh
   DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test run --rm dinnerbell scrub-test --keep you@example.com
   DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test up -d
   ```

7. Check that the page shows the TEST banner and `X.Y.Z` in the footer, and that `/healthz` reports `"status":"ok"`.
8. Rehearse a call: run `/dinnerbell-test call` in `#dinnerbell-test`, answer, vote, and let it reach a decision. Check that nobody outside the test channel was notified; `/healthz` counts withheld direct messages in `slack_dms_dropped`.
9. Optional: if production recorded something new during the rehearsal, such as a call or a meal, [back up now](setup.md#back-up-now) again, and use that backup if you roll back.
10. Upgrade production. Check that the digest it pulls equals the one you verified, or pin it in a `compose.override.yaml`.

    ```sh
    podman-compose pull
    podman-compose down && podman-compose up -d
    ```

11. Check production's footer and `/healthz`.
12. Stop test: `deploy/dinnerbell-test down`.

If the rehearsal fails, stop test, and check out the release production runs, so the clone matches production again. Production hasn't changed. If production misbehaves after step 10, [roll back](setup.md#roll-back) with the newest pre-upgrade backup. If you enabled `dinnerbell-test.service` and the host reboots during the rehearsal, test restarts on its default tag, so start again from step 5.

## Verify that no secret is on a command line

If test keeps its secrets in files, you can check that no secret reaches a command line. The check replaces test's secret key with a throwaway value that you search for.

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

3. In the first terminal, start test, then count the canary in a dry run's output. podman-compose 1.0.6 prints the commands with `--dry-run` alone; later versions need `--verbose` too.

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

## Troubleshoot the test instance

| Symptom | Cause | Fix |
|---|---|---|
| Test posts in its channel but doesn't mention or message someone | While `DINNERBELL_INSTANCE_LABEL` is set, Dinnerbell notifies only members of the test channel, and counts withheld messages in `slack_dms_dropped`. If the log mentions a missing scope, it can't read the channel's members and notifies no one. | Invite the person to the test channel. For a missing scope, paste `deploy/slack-app-manifest.test.yaml` again under **App Manifest**, and reinstall the app. |
| Port 8014 accepts connections but never answers | The container is in a restart loop: Dinnerbell stops at start, usually on a setting in `.env.test`, and compose restarts it. | Run `podman ps -a --filter name=dinnerbell-test`, and look for `Restarting` or a recent `Exited`. Read why with `deploy/dinnerbell-test logs dinnerbell`, fix `.env.test`, then run `deploy/dinnerbell-test down` and `deploy/dinnerbell-test up -d`. |

## Docker

With Docker, put `DINNERBELL_COMPOSE="docker compose"` in front of each `deploy/dinnerbell-test` command, use `docker` for `podman` commands, and use `docker volume inspect dinnerbell_dinnerbell-data dinnerbell-test_dinnerbell-data` to check the volumes. With rootful Docker, use `sudo chown 10001:10001` for `podman unshare chown`, and run `deploy/dinnerbell-test` with `sudo` when test keeps secrets in files.
