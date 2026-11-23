# Switch install methods

You can run Dinnerbell from a git clone or from the release bundle, and move between them without losing data, or build the image yourself. The commands use podman; with Docker, replace `podman-compose` with `docker compose` and `podman` with `docker`. With secrets in files, add `-f compose.yaml -f compose.secrets.yaml` to each `podman-compose` command ([Run compose with secret files](setup.md#run-compose-with-secret-files)).

Keep the install in a directory named `dinnerbell`, and run every command as the same user. Compose names the data volume after the directory (`dinnerbell_dinnerbell-data`), and rootless podman keeps each user's volumes apart, so another directory name or another user, including root, gets a new, empty volume.

## Switch between a clone and the bundle

A git clone and the [release bundle](install.md) run the same release the same way. If the clone runs `main` or an image you built, first [upgrade](backup-and-upgrade.md#upgrade) it to a release at least as new: an older image refuses a database that a newer one migrated.

1. [Back up now](backup-and-upgrade.md#back-up-now).
2. In the old install, stop production and, if it runs, the test instance.

   ```sh
   podman-compose down
   deploy/dinnerbell-test down
   ```

3. From the directory that holds `dinnerbell`, move the old install aside.

   ```sh
   mv dinnerbell dinnerbell-old
   ```

4. Create the new `dinnerbell` directory at the release you run, which the `image:` line in `compose.yaml` names.

   - **Clone to bundle:** download, check, and extract that release's bundle, as in steps 1 to 3 of [install.md](install.md#install).
   - **Bundle to clone:** clone as in [section 0 of setup.md](setup.md#0-clone-the-repository), then run `git checkout vX.Y.Z`.

5. From the directory that holds both (run `cd ..` if step 4 left you in `dinnerbell`), move your files into the new directory. Move rather than copy: moving keeps the owner and mode the secret files need, and your account can't read them to copy them. A clone already has a `secrets/` directory, so the secret files move one by one.

   ```sh
   ( [ -d dinnerbell-old ] || { echo "Run this from the directory that holds dinnerbell-old" >&2; exit 1; }
     for file in .env .env.test compose.override.yaml; do
         [ ! -e "dinnerbell-old/$file" ] || mv "dinnerbell-old/$file" dinnerbell/
     done
     for dir in secrets secrets-test; do
         for name in SECRET_KEY OIDC_CLIENT_SECRET SLACK_BOT_TOKEN SLACK_APP_TOKEN ANTHROPIC_API_KEY; do
             file="$dir/DINNERBELL_$name"
             [ ! -e "dinnerbell-old/$file" ] || { mkdir -p -m 700 "dinnerbell/$dir" && mv "dinnerbell-old/$file" "dinnerbell/$file"; }
         done
     done )
   ```

   Also move anything else your `compose.override.yaml` mounts, such as the catalog: `mkdir -p dinnerbell/seed && mv dinnerbell-old/seed/my-catalog.yaml dinnerbell/seed/`.

6. Check that production's volume exists under the name the new directory gives it: `podman volume inspect dinnerbell_dinnerbell-data` must succeed. If `.env` sets `COMPOSE_PROJECT_NAME`, the volume starts with that name instead of `dinnerbell`.
7. From `dinnerbell`, start production as before, with the same `-f` options. Sign in, and check that your members and history are there. Then start test if you run it.
8. Check that `dinnerbell-old` holds nothing of yours, then delete it. In a clone, `git -C dinnerbell-old status --short --ignored` lists the files git doesn't track. In a bundle, `ls -A dinnerbell-old dinnerbell-old/secrets*` lists what's left; anything beyond the bundle's own files is yours.

If Dinnerbell starts empty, compose created a new volume because the directory name or the user differs. Run `podman-compose down`, fix the name or user, and start it again. Your data is still in the old volume.

A clone's `deploy/dinnerbell-test` runs `main` by default, and a bundle's runs the bundle's release. If a newer `main` migrated test's database, the bundle's older image refuses it. To keep test on `main` after you move to the bundle, put `DINNERBELL_TAG=main` in front of each test command, and if you enabled the test boot unit, add `Environment=DINNERBELL_TAG=main` under `[Service]` with `systemctl --user edit dinnerbell-test.service`. The [boot units](setup.md#start-at-boot) need no other change, because the path stays the same.

With Docker, stop test in step 2 with `DINNERBELL_COMPOSE="docker compose" deploy/dinnerbell-test down`.

## Build from source

To run your own changes, build the image from your checkout with the `compose.build.yaml` override:

```sh
podman-compose -f compose.yaml -f compose.build.yaml up -d --build
```

Pass both `-f` options to every later compose command, plus `-f compose.secrets.yaml` with secrets in files and `-f compose.override.yaml` last if you have one. The image, `localhost/dinnerbell:dev`, has no attestations and isn't a release, so use it for development, not production.
