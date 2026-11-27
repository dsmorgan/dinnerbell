# Back up and upgrade

This page covers backups, upgrades, rollbacks, and moving Dinnerbell to a new host. The commands use podman and a git clone. With Docker, replace `podman-compose` with `docker compose` and `podman` with `docker`. With the release bundle, upgrade as [install.md](install.md#upgrade) shows.

If you keep secrets in files, add `-f compose.yaml -f compose.secrets.yaml` to every `podman-compose` command below, and `-f compose.override.yaml` last if you have one, as [Run compose with secret files](setup.md#run-compose-with-secret-files) shows. Without the first two, Dinnerbell starts without its secrets and restarts in a loop; without the last, compose skips your override file.

## Back up

Every night at 03:30 household time, Dinnerbell backs up its database to `/data/backups/dinnerbell-YYYY-MM-DD.db`, and at start if the newest backup is more than a day old. If the host sleeps through 03:30, or the clocks skip it, Dinnerbell backs up as soon as it can. It checks each copy with SQLite's `quick_check` and retries once. If both tries fail, it logs `database backup failed its check`, and `/healthz` reports `"backup": {"failing": true}` until a backup succeeds. **Admin** > **Health** shows the newest backup's time and file, and warns when it failed or is more than 36 hours old.

Dinnerbell also takes a one-off copy before anything hard to undo, named for what it came before, with the UTC time:

- `pre-upgrade-VERSION-TIME.db`: [`dinnerbell backup`](#back-up-now), which you run before an upgrade.
- `pre-migrate-FROM-TO-TIME.db`, such as `pre-migrate-0040-to-0045-20261122T013000.123456Z.db`: `serve` or `migrate`, before migrating the database.
- `pre-reset-VERSION-TIME.db`: `reset-history`, as in [Going live](setup.md#going-live).

### What pruning keeps

After each nightly backup, Dinnerbell keeps the 14 newest nightly copies, the newest nightly copy from each of the last 8 weeks, and the five newest `pre-migrate-*` copies, and deletes the other nightly and `pre-migrate-*` copies. It never deletes `pre-upgrade-*` or `pre-reset-*` copies, or any file it didn't name, so delete old ones yourself. To keep a `pre-migrate-*` copy for good, copy it off the host, or rename it without the time.

### What a backup holds

<a id="what-the-audit-log-and-backups-hold"></a>Each copy holds the whole database as it was then, so keep every copy as private as the database. That includes the audit log, with member emails and Slack user IDs; assistant conversations, drafts, and memory notes; devices that get notifications; and members' preferences and dietary needs.

Retention settings, such as the assistant's **Conversation hours** and the 30 days for refused sign-ins, and **Clear memory**, delete rows from the live database only. A copy keeps what it held until the copy is deleted.

### What Dinnerbell deletes

Most of the database grows with the household's history: calls, meals, feedback, and the audit log, which Dinnerbell keeps for good. The notification queue would grow too, by a row for every Slack message and edit, so every night at 04:15 household time, after the backup, Dinnerbell deletes old notifications:

- A call's sent and failed notifications, 90 days after the call's last change once it ended: done, abandoned, or called off. The posts that made the call's channel messages, about 3 per run, stay for good, so Dinnerbell can still find and edit them. A direct message, such as a reminder, goes, so an assistant thread under it no longer knows which call it's about.
- Other notifications, such as schedule changes, badge posts, and monthly recaps, 90 days after they were sent or failed. A sent message asking an admin to link a Slack account stays, so Dinnerbell doesn't ask again.

`/healthz`'s `outbox.failed` count falls as old failures go. Pushes to the web app go 7 days after they're sent or closed. The log line `old notifications removed` gives the count.

### What else to back up

To run Dinnerbell on another host, also back up these files from your clone or bundle directory:

- `.env`
- `secrets/`, if you [keep secrets in files](setup.md#keep-secrets-in-files)
- `compose.override.yaml`, if you have one

`DINNERBELL_SECRET_KEY` matters most: Dinnerbell derives the web push key and the sign-in code key from it, so without it, every device's notifications and every unused sign-in code stop working. Store these files apart from the database copies, such as in a password manager, so one lost copy never holds both the household's data and its keys. Copy them again whenever you change them.

### Copy backups off the host

The backups share a volume with the database, so copy `/data/backups/` off the host with your own backup routine. Never copy the live `dinnerbell.db` or its WAL file.

Only UID 10001 in the container can read the files. Under rootful podman or Docker, run your routine as root, from the path that `podman volume inspect dinnerbell_dinnerbell-data --format '{{.Mountpoint}}'` prints. Under rootless podman, copy them out through the user namespace:

```sh
mkdir -p ~/dinnerbell-backups
podman unshare cp -a "$(podman volume inspect dinnerbell_dinnerbell-data --format '{{.Mountpoint}}')/backups/." ~/dinnerbell-backups/
podman unshare chown -R 0:0 ~/dinnerbell-backups
```

The test instance's volume is `dinnerbell-test_dinnerbell-data`.

### Back up now

Before an upgrade, or any change you might want to undo, back up in the running container:

```sh
podman-compose exec dinnerbell dinnerbell backup
```

The command copies the database to `/data/backups/pre-upgrade-VERSION-TIME.db`, checks the copy, and prints its name, such as `Backed up the database to /data/backups/pre-upgrade-0.16.0-20261112T013000Z.db`. Write the name down: [Roll back](#roll-back) needs it. If the copy fails its check, the command deletes it and exits with status 1. Copy the backup off the host.

## Upgrade

Production runs a release tag, never `latest` or `main`, and your clone stays checked out at that release so that `compose.yaml` matches the image. If you run a test instance, follow [Promote a release](test-instance.md#promote-a-release) instead, which rehearses on a copy of your data first.

1. Read the **Upgrade notes** in [CHANGELOG.md](../CHANGELOG.md) for every release between yours and the new one.
2. [Back up now](#back-up-now), and copy the backup off the host.
3. Check out the new release, replacing `vX.Y.Z`.

   ```sh
   git fetch --tags
   git checkout vX.Y.Z
   ```

4. Pull the image. Optional: [verify it](#verify-an-image). If you pinned a digest in `compose.override.yaml`, update or remove the pin first, or you keep the old image.

   ```sh
   podman-compose pull
   ```

5. Recreate the service. `down` keeps the data volume; never add `-v`, which deletes it. With secrets in files, name the compose files, as at the top of this page.

   ```sh
   podman-compose down && podman-compose up -d
   ```

6. Check that the page footer shows the new version, and that `/healthz` returns 200 with `"slack":"connected"`.

Before it migrates, Dinnerbell backs up the database to a `pre-migrate-*` copy and logs `database backed up before migrating`. If that backup fails, it logs `Can't back up the database before migrating it`, changes nothing, and exits, and the container restarts until you fix the cause, such as a full disk. Step 2 still matters: that copy stays on the host, and a release without a migration takes none.

Migrations only move forward: an older version refuses to open the upgraded database. To go back, [roll back](#roll-back).

If the upgrade notes say the Slack manifest changed, paste the new manifest under **App Manifest** in the app settings, set your hostname in it again, then select **Install App** > **Reinstall to Workspace**. The bot token stays the same. Until you reinstall, Dinnerbell keeps working without the new feature.

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

3. List the backups, and find the one from before the upgrade: your `pre-upgrade-*` file, or the `pre-migrate-*` file the upgrade took, which is newer, so it loses less.

   ```sh
   podman-compose run --rm --entrypoint ls dinnerbell -l /data/backups
   ```

4. Replace the database with it, using its file name. The command assumes the default `DINNERBELL_DATABASE_URL`, and deletes the `-wal` and `-shm` files, which SQLite would otherwise apply to the restored file.

   ```sh
   podman-compose run --rm --entrypoint sh dinnerbell -c 'umask 077 && rm -f /data/dinnerbell.db-wal /data/dinnerbell.db-shm && cp /data/backups/pre-upgrade-X.Y.Z-YYYYMMDDTHHMMSSZ.db /data/dinnerbell.db'
   ```

5. Pull the previous image and start the service, with the `-f` options from the top of this page.

   ```sh
   podman-compose pull
   podman-compose up -d
   ```

6. Check that the footer shows the previous version.

When a fixed release ships, [upgrade](#upgrade) to it as usual.

## Restore on a new host

To move Dinnerbell to a new host, or rebuild it after losing one, restore a database copy and the files from [What else to back up](#what-else-to-back-up). Anything recorded after the copy is lost.

1. Stop Dinnerbell on the old host if it still runs. Two instances on one Slack app both answer.
2. On the new host, [clone the repository](setup.md#0-clone-the-repository), or extract the release bundle as in the [quick install](install.md). Keep the directory name `dinnerbell`. Check out the release that wrote the copy, replacing `vX.Y.Z`: the version in a `pre-upgrade-*` or `pre-reset-*` name, the release before the upgrade for a `pre-migrate-*` copy, or the release the old host ran for a nightly copy.

   ```sh
   git checkout vX.Y.Z
   ```

3. Put `.env`, `secrets/`, and `compose.override.yaml` back, with mode 600 on `.env` and on the files in `secrets/`.
4. Pull the image, and create the data volume without starting Dinnerbell.

   ```sh
   podman-compose pull
   podman-compose run --rm --entrypoint true dinnerbell
   ```

5. Copy the database copy into the volume as `dinnerbell.db`, owned by UID 10001, replacing the file name. Under rootful podman or Docker, run the same `cp` and `chown` as root, without `podman unshare`.

   ```sh
   data="$(podman volume inspect dinnerbell_dinnerbell-data --format '{{.Mountpoint}}')"
   podman unshare sh -c 'umask 077 && cp "$1" "$2/dinnerbell.db" && chown 10001:10001 "$2/dinnerbell.db"' \
     sh ~/dinnerbell-backups/pre-upgrade-X.Y.Z-YYYYMMDDTHHMMSSZ.db "$data"
   ```

6. Start the service with the compose files you used on the old host, and point your reverse proxy at the new host.
7. Check that the footer shows the release you checked out, that `/healthz` returns 200 with `"slack":"connected"`, and that History shows the calls you expect.

If the restored `.env` lacks the old secret key, every member turns notifications on again on each device, and unused sign-in codes stop working. To move to a newer release too, restore first, then [upgrade](#upgrade).

## Verify an image

Every release image carries build provenance and SBOM attestations. To check that this repository's CI built your image from its release tag, verify the digest your host pulled, on any machine with the [GitHub CLI](https://cli.github.com/). Replace `X.Y.Z` and the digest:

```sh
podman image inspect ghcr.io/dsmorgan/dinnerbell:X.Y.Z --format '{{.RepoDigests}}'
gh attestation verify oci://ghcr.io/dsmorgan/dinnerbell@sha256:... \
  --repo dsmorgan/dinnerbell \
  --signer-workflow dsmorgan/dinnerbell/.github/workflows/ci.yml \
  --source-ref refs/tags/vX.Y.Z
```

The release notes on GitHub list each release's digest. To run exactly what you verified, pin `image:` to `ghcr.io/dsmorgan/dinnerbell@sha256:...` in a `compose.override.yaml`. Don't use `podman auto-update` or Watchtower: they follow moving tags.
