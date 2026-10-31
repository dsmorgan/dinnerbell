# Run a test instance

A test instance is an optional second Dinnerbell on the same host, with its own database, Slack app, channel, and hostname. Use it to run builds from `main`, and to rehearse each release on a copy of your data before your family sees it. [ADR 0006](adr/0006-test-instance.md) explains the design.

Setup takes about half an hour, after a working production install ([setup.md](setup.md)). Stop test when you aren't using it. The commands use podman; for Docker, see [Docker](#docker).

## One clone, two instances

Both instances run from your production clone or bundle, as the same user:

| | Production | Test |
|---|---|---|
| Command | `podman-compose ...` | `deploy/dinnerbell-test ...` |
| Env file | `.env` | `.env.test` |
| Secret files (optional) | `secrets/`, with `-f compose.yaml -f compose.secrets.yaml` | `secrets-test/`, used when the files are there |
| Image tag | The release in `compose.yaml` | `main` in a clone, the bundle's release in a bundle, or the release you're rehearsing |
| Data volume | `dinnerbell_dinnerbell-data` | `dinnerbell-test_dinnerbell-data` |
| Host port | 8013 | 8014 |
| Slack app and command | Dinnerbell, `/dinnerbell` | Dinnerbell Test, `/dinnerbell-test` |
| Pangolin authentication | Off | On |

The instances share `compose.yaml`, the Slack workspace, and `DINNERBELL_BIND`. Test doesn't read `compose.override.yaml`. The secret key, both Slack tokens, and the client secret must differ.

## Create the test channel and Slack app

Use the same workspace as production.

1. Create a channel named `#dinnerbell-test`, which can be private, and invite only the people who test with you. Test mentions and messages only members of this channel.
2. Create a second Slack app as in [section 2 of setup.md](setup.md#2-create-the-slack-app), but paste `deploy/slack-app-manifest.test.yaml`, and replace `dinnerbell-test.example.com` with your test hostname. For its icon, upload `deploy/slack-app-icon-test-1024.png`, which shows Dinnerbot, the test mascot.
3. Create its app-level token, install it, and copy its bot token, client ID, and client secret, as in sections 3 to 5.
4. Copy the test channel's ID, as in section 6.
5. In `#dinnerbell-test`, invite the bot: `/invite @Dinnerbell Test`.

Never reuse production's tokens: Socket Mode delivers each event to one connection, so two instances on one app split button taps between them.

## Write .env.test

`dinnerbell init --test` writes `.env.test` from your answers: the test app's values, the test hostname, and production's team ID. It generates a new secret key, sets the `TEST` label, the `/dinnerbell-test` command, and the household name `Test`, and refuses production's secrets, client ID, and URL from the `.env` next to it. From your install's directory, replacing `X.Y.Z` with the release production runs (0.10.0 or later), run:

```sh
podman run --rm -it --userns keep-id --user "$(id -u):$(id -g)" -v "$PWD:/out:z" \
  ghcr.io/dsmorgan/dinnerbell:X.Y.Z init --test --output /out/.env.test
```

To write it by hand instead, copy `.env.test.example` to `.env.test`, run `chmod 600 .env.test`, and fill it in. Git ignores `.env.test`; never commit it.

### Secrets in files for test

To keep test's secrets in files, follow [Keep secrets in files](setup.md#keep-secrets-in-files) in `secrets-test/`, from `.env.test`, and check them with `deploy/check-secrets --env-file .env.test secrets-test`. Test needs no `-f` options: when any of the four files exists, `deploy/dinnerbell-test` mounts them and requires all four.

## Add the Pangolin resource

1. Create a public HTTP resource for the test hostname, with the target on the host's port 8014.
2. Leave **Use Platform SSO** on, and allow only your own Pangolin user.

Test runs unreleased code that only you use, so it stays behind Pangolin's login. Sign in to Pangolin first, then select **Sign in with Slack**; if Slack's redirect lands on a Pangolin login page, do both again.

Pangolin protects only the hostname, so use your host firewall to allow port 8014 only from the address Newt connects from. With rootful Docker, published ports skip `ufw` rules, so add the rule to the `DOCKER-USER` chain.

## Start, stop, and refresh

`deploy/dinnerbell-test` runs `podman-compose` for test, and takes any compose command:

```sh
deploy/dinnerbell-test pull
deploy/dinnerbell-test up -d
deploy/dinnerbell-test logs dinnerbell
deploy/dinnerbell-test down
```

- To pick up a newer `main`, run `pull`, then `down` and `up -d`. The `main` tag moves with every merge and isn't attested, so give test no production data except a scrubbed copy.
- To check test, look for the TEST banner in the browser, or `"instance":"TEST"` from `curl -s http://<DINNERBELL_BIND>:8014/healthz`. Test is usually stopped, so don't give it an always-on monitor.
- A new test instance starts with an empty database and invites `DINNERBELL_ADMIN_EMAIL`. Add test members on **Admin** > **Members**, or [copy production's data](#copy-production-data-into-test).

Always run test through `deploy/dinnerbell-test`, never through `podman-compose` directly. The script sets the project, volume, env file, tag, and port together; setting only some of them by hand mixes the instances, such as test's label and Slack app on production's database. Before any command that can start test, the script checks `.env.test` and test's secret files with `dinnerbell config check`, and refuses anything that would mix test and production, naming the reason. `deploy/dinnerbell-test --help` lists every check and variable.

To change what the script uses, set a variable in front of a command, such as `DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test up -d` to rehearse a release. Never `export` these variables: production's compose commands in the same shell would read them too.

### Start test at boot

To start test at boot with podman, install `deploy/dinnerbell-test.service` as in [Start at boot](setup.md#start-at-boot), with `dinnerbell-test` in place of `dinnerbell`. It starts test on the script's default tag, so a reboot ends a rehearsal on another tag.

## Copy production data into test

A copy of production's data tests migrations and features against real history. It holds member emails, so keep it on the host, and scrub it before test starts.

1. Stop test. If it has never run, start and stop it once so that its volume exists.

   ```sh
   deploy/dinnerbell-test down
   ```

2. Check that both volumes exist. If production's has another name, use that name below.

   ```sh
   podman volume exists dinnerbell_dinnerbell-data && podman volume exists dinnerbell-test_dinnerbell-data && echo ok
   ```

3. List production's backups, and pick the newest nightly `dinnerbell-YYYY-MM-DD.db` or a `pre-upgrade-*.db`. Replace `X.Y.Z` with the release production runs; its image is already on the host.

   ```sh
   podman run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
     --entrypoint ls -v dinnerbell_dinnerbell-data:/prod:ro \
     ghcr.io/dsmorgan/dinnerbell:X.Y.Z /prod/backups
   ```

4. Copy it over test's database, replacing the file name. Production's volume is mounted read-only.

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

   Before it changes anything, `scrub-test` prints the household, its last activity, and counts of members, slots, open events, unsent notifications, and feedback. Check that they match the backup, and that the last activity is from around when the backup was taken. Then type `scrub`, or, without a terminal, run the command again with `--yes`.

6. Start test.

   ```sh
   deploy/dinnerbell-test up -d
   ```

`scrub-test` runs only while `DINNERBELL_INSTANCE_LABEL` is set. It deactivates and unlinks every member you didn't keep, turns off every slot, ends open calls silently, empties the outbox, marks unannounced badges and the last monthly recap as done, and deletes all feedback on meals. It keeps the meal log, catalog, and preferences. It migrates the database first, so run it with the tag you start next: an older release refuses a database that a newer one migrated.

To rehearse a scheduled call after a scrub, turn a slot on on the **Schedule** page, or start a call with `/dinnerbell-test call`.

## Discard a call and start again

On test, you can throw a call away to test the same slot and day again. These controls work only while `DINNERBELL_INSTANCE_LABEL` is set; production keeps every call.

- **On the web:** as an admin, select **Discard (test)** on the call's card, check what the confirmation page says it deletes, and select **Discard (test)** again.
- **In Slack:** as an admin, `/dinnerbell-test discard` discards today's dinner call. Add `breakfast` or `lunch`, and `tomorrow` or a date such as `2026-10-24`.
- **On the host:** run `deploy/dinnerbell-test run --rm dinnerbell discard-call 12`, where `12` is the ID in the call's address, `/events/12`. It shows what it deletes and asks you to type `discard`; `--yes` skips the question.

Discarding deletes the call and everything in it, including the meal it logged, its feedback, and its unsent notifications. Its Slack messages change to "Discarded (test)", with no buttons, and the audit log keeps an `event.discarded` row. You can't undo a discard, and IDs are never reused.

On test, you can also start a called-off call again at any time that day, on the web, in App Home, or with `/dinnerbell-test call`. On production, **Start dinner again** works only until the usual meal time plus two hours.

## Promote a release

Use this checklist for every release: it runs the exact release on a fresh copy of production's data. Replace `X.Y.Z` with the new release, and note the release production runs now.

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
6. Scrub the copy and start test on the release, so the scrub migrates the copy exactly as production's upgrade will.

   ```sh
   DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test run --rm dinnerbell scrub-test --keep you@example.com
   DINNERBELL_TAG=X.Y.Z deploy/dinnerbell-test up -d
   ```

7. Check that the page shows the TEST banner and `X.Y.Z` in the footer, and that `/healthz` reports `"status":"ok"`.
8. Rehearse a call: run `/dinnerbell-test call`, answer, vote, and let it reach a decision. Check that nobody outside the test channel was notified.
9. Optional: if production recorded something new during the rehearsal, such as a call or a meal, [back up now](setup.md#back-up-now) again, and use that backup if you roll back.
10. Upgrade production. Check that the digest it pulls equals the one you verified, or pin it in a `compose.override.yaml`.

    ```sh
    podman-compose pull
    podman-compose down && podman-compose up -d
    ```

11. Check production's footer and `/healthz`.
12. Stop test: `deploy/dinnerbell-test down`.

If the rehearsal fails, stop test, and check out the release production runs; production hasn't changed. If production misbehaves after step 10, [roll back](setup.md#roll-back) with the newest pre-upgrade backup. If the host reboots during the rehearsal with `dinnerbell-test.service` enabled, test restarts on its default tag, so start again from step 5.

## Troubleshoot the test instance

| Symptom | Cause | Fix |
|---|---|---|
| Test doesn't mention or message someone | Test notifies only members of the test channel, and counts withheld messages in `slack_dms_dropped`. If the log mentions a missing scope, it notifies no one. | Invite the person to the test channel. For a missing scope, paste `deploy/slack-app-manifest.test.yaml` again under **App Manifest**, and reinstall the app. |
| Port 8014 accepts connections but never answers | Dinnerbell stops at start, usually on a setting in `.env.test`, and compose restarts it in a loop. | Read why with `deploy/dinnerbell-test logs dinnerbell`, fix `.env.test`, then run `deploy/dinnerbell-test down` and `deploy/dinnerbell-test up -d`. |

## Docker

With Docker, put `DINNERBELL_COMPOSE="docker compose"` in front of each `deploy/dinnerbell-test` command, and use `docker` for `podman`: `docker volume inspect` checks a volume, and `init` runs without `--userns keep-id`. If production keeps its secrets in files (`secrets/`), run `deploy/dinnerbell-test` with `sudo`, so it can read them to check that test doesn't share them.
