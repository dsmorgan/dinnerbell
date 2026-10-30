# Install Dinnerbell

This guide installs Dinnerbell from a release bundle, without cloning the repository. The [setup guide](https://github.com/dsmorgan/dinnerbell/blob/main/docs/setup.md) is the full reference: every step in detail, backups, upgrades, troubleshooting, and the optional test instance.

You need:

- A Linux host with podman and podman-compose 1.0.6 or later, or Docker with the compose plugin. Run every command as the same user.
- A public HTTPS hostname that a reverse proxy or tunnel, such as Pangolin, forwards to the host.
- A Slack account. Its email address becomes the admin email.

## Install

1. Download the bundle and its checksum, replacing `X.Y.Z` with the newest version on the [releases page](https://github.com/dsmorgan/dinnerbell/releases), and check it.

   ```sh
   VERSION=X.Y.Z
   base="https://github.com/dsmorgan/dinnerbell/releases/download/v$VERSION"
   curl -fL -O "$base/dinnerbell-$VERSION.tar.gz" -O "$base/dinnerbell-$VERSION.tar.gz.sha256"
   sha256sum -c "dinnerbell-$VERSION.tar.gz.sha256"
   ```

2. Optional: with the [GitHub CLI](https://cli.github.com/), check that this repository's CI built the bundle from the release tag.

   ```sh
   gh attestation verify "dinnerbell-$VERSION.tar.gz" --repo dsmorgan/dinnerbell \
     --signer-workflow dsmorgan/dinnerbell/.github/workflows/ci.yml --source-ref "refs/tags/v$VERSION"
   ```

3. Extract it, and keep the directory name `dinnerbell`: compose names the data volume after it.

   ```sh
   tar xzf "dinnerbell-$VERSION.tar.gz" && cd dinnerbell
   ```

4. Create the Slack app. In a Slack workspace for your household, create a channel such as `#dinnerbell`. At [api.slack.com/apps](https://api.slack.com/apps), select **Create New App** > **From a manifest**, paste `deploy/slack-app-manifest.yaml`, and replace `dinnerbell.example.com` with your hostname. Create an app-level token with the `connections:write` scope, install the app, and invite it to the channel with `/invite @Dinnerbell`. Sections 1 to 7 of the [setup guide](https://github.com/dsmorgan/dinnerbell/blob/main/docs/setup.md#1-create-the-slack-workspace-and-channel) show each screen.

5. Write `.env`. `dinnerbell init` asks for your hostname, admin email, time zone, household name, and the Slack app's values, with a hint for each. It generates the secret key, writes `.env` with mode 600, and checks it. Secrets don't show as you type or paste them.

   ```sh
   podman run --rm -it --userns keep-id --user "$(id -u):$(id -g)" -v "$PWD:/out:z" \
     "ghcr.io/dsmorgan/dinnerbell:$VERSION" init --output /out/.env
   ```

   With Docker: `docker run --rm -it --user "$(id -u):$(id -g)" -v "$PWD:/out:z" "ghcr.io/dsmorgan/dinnerbell:$VERSION" init --output /out/.env`

   To skip a Slack value, press Enter, and fill it in `.env` later.

6. Point your reverse proxy or tunnel at port 8013 on this host, with its own sign-in turned off: Dinnerbell signs people in itself. Compose publishes the port on `127.0.0.1`. If the proxy runs on another host or in a container network, set `DINNERBELL_BIND` in `.env` to this host's LAN address. For Pangolin, see [section 8](https://github.com/dsmorgan/dinnerbell/blob/main/docs/setup.md#8-expose-dinnerbell-through-your-reverse-proxy).

7. Start Dinnerbell.

   ```sh
   podman-compose up -d
   ```

   With Docker: `docker compose up -d`

8. Sign in. Open your URL, select **Sign in with Slack**, and add your household on **Members**. `https://<your hostname>/healthz` shows `"slack":"connected"` once the bot is connected. Next, [onboard your family](https://github.com/dsmorgan/dinnerbell/blob/main/docs/setup.md#11-onboard-family-members) and [load the catalog](https://github.com/dsmorgan/dinnerbell/blob/main/docs/setup.md#12-load-the-catalog).

## Upgrade

1. Read the **Upgrade notes** of every release between yours and the new one. They say when `.env` needs a new setting.
2. [Back up now](https://github.com/dsmorgan/dinnerbell/blob/main/docs/setup.md#back-up-now).
3. In the directory that holds `dinnerbell`, download and check the new bundle as in steps 1 and 2, then extract it over the old one. It replaces the bundle's files, including `compose.yaml`, which names the new image, and keeps `.env`, `secrets/`, and `compose.override.yaml`. Keep your own changes in `compose.override.yaml`, never in the bundle's files.
4. Pull the new image and recreate the service.

   ```sh
   cd dinnerbell && podman-compose pull && podman-compose down && podman-compose up -d
   ```

   With Docker: `cd dinnerbell && docker compose pull && docker compose down && docker compose up -d`

## Options

- **Secrets in files:** to keep the four secrets out of `.env`, use `compose.secrets.yaml`. See [Keep secrets in files](https://github.com/dsmorgan/dinnerbell/blob/main/docs/setup.md#keep-secrets-in-files).
- **Test instance:** run step 5 with `init --test --output /out/.env.test`. It writes the test settings and refuses production's secrets from `.env`. Start it with `deploy/dinnerbell-test up -d`, on port 8014. The bundle's `deploy/dinnerbell-test` runs the bundle's version. See [Test instance](https://github.com/dsmorgan/dinnerbell/blob/main/docs/setup.md#test-instance).
- **Start at boot:** with podman, Dinnerbell stays down after a reboot until you start it; the bundle's `deploy/dinnerbell.service` starts it at boot. Docker needs nothing. See [Start at boot](https://github.com/dsmorgan/dinnerbell/blob/main/docs/setup.md#start-at-boot).
- **Switch to a git clone, or from one:** see [Switch between a clone and the bundle](https://github.com/dsmorgan/dinnerbell/blob/main/docs/setup.md#switch-between-a-clone-and-the-bundle).
