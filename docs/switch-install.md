# Switch install methods

You can run Dinnerbell from a git clone or from the release bundle, and you can move between them without losing data. This page also covers building the image yourself. The commands use podman; with Docker, replace `podman-compose` with `docker compose` and `podman` with `docker`.

In every case, run each command as the same user as your install, from a directory named `dinnerbell`. Compose names the data volume after the directory (`dinnerbell_dinnerbell-data`, and `dinnerbell-test_dinnerbell-data` for the test instance), and rootless podman keeps each user's volumes apart. Another directory name or another user, including root, gets a new, empty volume.

## Switch between a clone and the bundle

A git clone and the [release bundle](install.md) run the same release the same way. Bundles start at 0.10.0, so to move a clone to the bundle, first [upgrade](setup.md#upgrade) it to 0.10.0 or later. If the clone runs `main` or an image you built, first [switch to the prebuilt image](#switch-a-source-built-install-to-the-prebuilt-image).

1. [Back up now](setup.md#back-up-now).
2. In the old install, stop production and, if it runs, the test instance.

   ```sh
   podman-compose down
   deploy/dinnerbell-test down
   ```

3. From the directory that holds `dinnerbell`, move the old install aside.

   ```sh
   mv dinnerbell dinnerbell-old
   ```

4. Create the new `dinnerbell` directory at the release you run, which the `image:` line in `compose.yaml` names:

   - **Clone to bundle:** download, check, and extract that release's bundle, as in steps 1 to 3 of [install.md](install.md#install).
   - **Bundle to clone:** clone as in [section 0 of setup.md](setup.md#0-clone-the-repository), then run `git checkout vX.Y.Z`.

5. Move your files into the new directory. Moving keeps each file's owner and mode, which the secret files need: your account can't read them to copy them. A clone already has a `secrets/` directory, so the secret files move one by one.

   ```sh
   for file in .env .env.test compose.override.yaml; do
       [ ! -e "dinnerbell-old/$file" ] || mv "dinnerbell-old/$file" dinnerbell/
   done
   for dir in secrets secrets-test; do
       for name in SECRET_KEY OIDC_CLIENT_SECRET SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
           file="$dir/DINNERBELL_$name"
           [ ! -e "dinnerbell-old/$file" ] || { mkdir -p -m 700 "dinnerbell/$dir" && mv "dinnerbell-old/$file" "dinnerbell/$file"; }
       done
   done
   ```

   Also move anything else your `compose.override.yaml` mounts, such as the catalog: `mkdir -p dinnerbell/seed && mv dinnerbell-old/seed/my-catalog.yaml dinnerbell/seed/`.

6. Check that production's volume is there under the name the new directory gives it. `podman volume inspect dinnerbell_dinnerbell-data` must succeed. If `.env` sets `COMPOSE_PROJECT_NAME`, the volume starts with that name instead of `dinnerbell`.
7. From `dinnerbell`, start production as before, with `-f compose.yaml -f compose.secrets.yaml` if you keep secrets in files. Sign in, and check that your members and history are there. Then start test if you run it.
8. Check that `dinnerbell-old` holds nothing of yours, then delete it. In a clone, `git -C dinnerbell-old status --short --ignored` lists the files git doesn't track. In a bundle, `ls -A dinnerbell-old dinnerbell-old/secrets*` lists what's left; anything beyond the bundle's own files is yours.

If Dinnerbell starts empty, compose created a new volume, because the directory name or the user differs. Run `podman-compose down`, fix the name or user, and start it again. Your data is still in the old volume.

A clone's `deploy/dinnerbell-test` runs `main` by default, and a bundle's runs the bundle's release. An older image refuses a database that a newer one migrated, so to keep test on `main` after you move to the bundle, put `DINNERBELL_TAG=main` in front of each test command. If you enabled the test boot unit, also add `Environment=DINNERBELL_TAG=main` under `[Service]` with `systemctl --user edit dinnerbell-test.service`. The [boot units](setup.md#start-at-boot) need no other change, because the path stays the same.

With Docker, stop test in step 2 with `DINNERBELL_COMPOSE="docker compose" deploy/dinnerbell-test down`.

## Build from source

To run your own changes, build the image from your checkout with the `compose.build.yaml` override:

```sh
podman-compose -f compose.yaml -f compose.build.yaml up -d --build
```

Pass both `-f` options to every later compose command. The image, `localhost/dinnerbell:dev`, has no attestations and isn't a release, so use it for development, not production.

## Switch a source-built install to the prebuilt image

Before 0.2.0, `compose.yaml` built the image on your host. This one-time switch keeps your data, because the volume name stays `dinnerbell-data`.

1. From your clone, note the data volume's name and the commit you run. The undo steps need both.

   ```sh
   podman volume ls
   git rev-parse --short HEAD
   ```

2. [Back up now](setup.md#back-up-now). Note the file name, and copy the backup off the host.
3. If `git status` shows local changes to `compose.yaml`, move them to `compose.override.yaml`, which git ignores, then run `git checkout compose.yaml`.
4. Check out the release.

   ```sh
   git fetch --tags
   git checkout v0.2.0
   ```

5. Pull and [verify](setup.md#verify-an-image) the image, and recreate the service.

   ```sh
   podman-compose pull
   podman-compose down && podman-compose up -d
   ```

6. Run `podman volume ls` again, and check that there's no new Dinnerbell volume.
7. Sign in, and check that your members, catalog, and history are there, and that the footer shows `0.2.0`.

Keep the image you built, `localhost/dinnerbell`, until 0.2.0 has run for a week. Then remove it with `podman image rm localhost/dinnerbell`.

If Dinnerbell starts empty, run `podman-compose down`, and start it again from the original directory as the original user. Your data is still in the old volume.

To undo the switch:

1. Stop the service: `podman-compose down`.
2. Check out the commit you noted in step 1: `git checkout <commit>`.
3. Restore the backup from step 2 with the command in step 4 of [Roll back](setup.md#roll-back).
4. Rebuild and start the old version: `podman-compose up -d --build`.
