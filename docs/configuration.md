# Configuration reference

Dinnerbell reads its settings from environment variables, which compose loads from `.env`. [setup.md](setup.md#9-run-dinnerbell) covers the values a first install needs; the defaults suit most households. `.env.example` lists every setting with a comment. An empty value counts as unset.

After you change `.env`, recreate the service so that the container picks up the change:

```sh
podman-compose down && podman-compose up -d
```

## Settings

These settings are required:

| Setting | Meaning |
|---|---|
| `DINNERBELL_BASE_URL` | The public URL people open, exactly as they type it: the same scheme and host, no explicit `:443`, and no path. It must use `https`, except `http://localhost` or `http://127.0.0.1` for local testing. Any mismatch makes every form fail with "Cross-origin request refused". The sign-in redirect URL is this value plus `/auth/callback`. |
| `DINNERBELL_SECRET_KEY` | Signs cookies and CSRF tokens. Dinnerbell refuses a key that is shorter than 32 characters, has fewer than 8 distinct characters, or contains placeholder text such as `REPLACE-ME`. Changing it invalidates open sign-in pages and import previews, not sessions. A secret. |
| `DINNERBELL_ADMIN_EMAIL` | The first admin. On every start, if no member has this email, Dinnerbell invites it as an admin, so changing it later adds another admin. |
| `DINNERBELL_OIDC_ISSUER` | The sign-in provider, such as `https://slack.com`. It must publish `/.well-known/openid-configuration`. |
| `DINNERBELL_OIDC_CLIENT_ID` | The provider's client ID. |
| `DINNERBELL_OIDC_CLIENT_SECRET` | The provider's client secret. A secret. |

These settings connect Slack. The bot connects when both tokens and the channel are set:

| Setting | Default | Meaning |
|---|---|---|
| `DINNERBELL_SLACK_TEAM_ID` | None | The workspace ID, such as `T0123ABCD`. Required when the issuer is `https://slack.com`; anyone signing in from another workspace is refused. Leave it empty with other providers. |
| `DINNERBELL_SLACK_BOT_TOKEN` | None | The bot token, starting with `xoxb-`. A secret. |
| `DINNERBELL_SLACK_APP_TOKEN` | None | The Socket Mode app-level token, starting with `xapp-`. A secret. |
| `DINNERBELL_SLACK_CHANNEL` | None | The household channel's ID, such as `C0123ABCD`. Invite the bot to it. |
| `DINNERBELL_SLACK_COMMAND` | `/dinnerbell` | The slash command your Slack app defines. It must match the app's manifest. |

These settings are optional:

| Setting | Default | Meaning |
|---|---|---|
| `DINNERBELL_HOUSEHOLD_NAME` | `Home` | The household name shown in the app. Read on first start only; after that, the database keeps it. |
| `DINNERBELL_TIMEZONE` | `America/New_York` | The household time zone, as an IANA name. It sets when calls go out and when the nightly backup runs. Read on first start only. |
| `DINNERBELL_TRUSTED_PROXIES` | None | Comma-separated proxy IP addresses whose `X-Forwarded-For` header Dinnerbell trusts for client addresses. Without it, the rate limiter treats all clients as one, which is safe but stricter. Set one proxy address, never a range, and never an address that other clients share, such as a container gateway. |
| `DINNERBELL_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. At `DEBUG`, each request logs the direct peer address and the resolved client address, which helps you find your proxy's address. |
| `DINNERBELL_SEED_FILE` | None | A catalog file to import on first start, while the catalog is empty. See [Load a catalog on first start](#load-a-catalog-on-first-start). |
| `DINNERBELL_DATABASE_URL` | `sqlite:////data/dinnerbell.db` | The SQLite database. Keep it under `/data`, the container's only writable volume. Backups go to `backups/` next to the database file. |
| `DINNERBELL_OIDC_SCOPES` | `openid email profile` | Scopes requested at sign-in. They must include `openid` and `email`. |
| `DINNERBELL_INSTANCE_LABEL` | None | Marks every page, Slack message, App Home, `/healthz`, and the logs, such as `TEST` on a [test instance](test-instance.md). Up to 16 letters, digits, spaces, or hyphens. Leave it empty in production. |
| `DINNERBELL_FEATURES` | None | Comma-separated feature flags to turn on. Dinnerbell refuses to start on an unknown name; the list is in `src/dinnerbell/domain/flags.py`. |

The container image sets `DINNERBELL_BUILD` on builds from `main`. Don't add it to `.env`: even an empty line replaces the image's value.

Dinnerbell warns at start about a `DINNERBELL_` variable it doesn't know, which is most likely a typo. The warning names the variable, never its value.

To keep the four secrets out of `.env`, see [Keep secrets in files](setup.md#keep-secrets-in-files).

## Compose settings

Compose, not Dinnerbell, reads this setting from `.env`:

| Setting | Default | Meaning |
|---|---|---|
| `DINNERBELL_BIND` | `127.0.0.1` | The host address to publish port 8013 on, for production and the test instance. The default works only when Newt runs on the same host, outside a container network. Otherwise, set the host's address on the interface Newt reaches, such as `192.168.1.20`. Avoid `0.0.0.0` unless a firewall limits who can reach the port. |

Don't set `DINNERBELL_TAG`, `DINNERBELL_PORT`, or `DINNERBELL_ENV_FILE` in `.env`. The defaults in `compose.yaml` are right for production, and `deploy/dinnerbell-test` refuses to run if `.env` sets them.

## Load a catalog on first start

To start with a catalog already loaded, import a file on first start, before you start Dinnerbell for the first time:

1. Copy the example. Git ignores every file in `seed/` except the example and the schema.

   ```sh
   cp seed/example.yaml seed/my-catalog.yaml
   ```

2. Edit `seed/my-catalog.yaml`. The format is in [catalog-format.md](catalog-format.md).
3. Create `compose.override.yaml` next to `compose.yaml` with this content, to mount the file read-only. Both compose tools read it automatically, and git ignores it.

   ```yaml
   services:
     dinnerbell:
       volumes:
         - ./seed/my-catalog.yaml:/seed/catalog.yaml:ro
   ```

4. In `.env`, add this line:

   ```sh
   DINNERBELL_SEED_FILE=/seed/catalog.yaml
   ```

On first start, only the admin is a member, so the import skips every other cook's email. After you invite everyone, import the same file again on the **Catalog** page to fill in the cooks. The file's fields replace what's stored, so do that before you edit those items in the app.

## Resource and log limits

`compose.yaml` caps the container, so a runaway process or a noisy log can't take the host. The test instance gets the same caps.

| Key | Value | Why |
|---|---|---|
| `pids_limit` | `256` | Measured: 6 processes and threads at idle, 47 after 300 concurrent requests. |
| `mem_limit` | `512m` | Measured: about 100 MiB at idle, 130 MiB peak after 300 concurrent requests. The rest leaves room for badge rendering and large catalog imports. |
| `logging` | `json-file`, `max-size: 10m`, `max-file: "3"` | At most 30 MB of logs. |

If `podman inspect` shows the container `OOMKilled`, raise `mem_limit` in a `compose.override.yaml`.

Docker Compose and podman-compose 1.6.0 apply all three keys. podman-compose 1.0.6 ignores `pids_limit` without a warning, so upgrade it to get the process cap. Older podman releases ignore `max-file`. `scripts/check-compose.sh` fails if any of these keys is missing.
