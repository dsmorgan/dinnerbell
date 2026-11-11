# Configuration reference

Dinnerbell reads its settings from environment variables, which compose loads from `.env`. [setup.md](setup.md#9-run-dinnerbell) covers the values a first install needs; the defaults suit most households. `.env.example` lists every setting with a comment. An empty value counts as unset.

After you change `.env`, recreate the service so that the container picks up the change:

```sh
podman-compose down && podman-compose up -d
```

If you keep secrets in files, add `-f compose.yaml -f compose.secrets.yaml` to each command, as [Run compose with secret files](setup.md#run-compose-with-secret-files) shows.

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
| `DINNERBELL_TRUSTED_PROXIES` | None | Comma-separated proxy IP addresses whose `X-Forwarded-For` header Dinnerbell trusts for client addresses. Set one proxy address, never a range, and never an address that other clients share, such as a container gateway. Without it, the rate limiter treats all clients as one, which is safe but stricter: see [The sign-in rate limit](#the-sign-in-rate-limit). |
| `DINNERBELL_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. At `DEBUG`, each request logs the direct peer address and the resolved client address, which helps you find your proxy's address. |
| `DINNERBELL_SEED_FILE` | None | A catalog file to import on first start, while the catalog is empty. See [Load a catalog on first start](#load-a-catalog-on-first-start). |
| `DINNERBELL_DATABASE_URL` | `sqlite:////data/dinnerbell.db` | The SQLite database. Keep it under `/data`, the container's only writable volume. Backups go to `backups/` next to the database file. |
| `DINNERBELL_OIDC_SCOPES` | `openid email profile` | Scopes requested at sign-in. They must include `openid` and `email`. |
| `DINNERBELL_INSTANCE_LABEL` | None | Marks every page, Slack message, App Home, `/healthz`, and the logs, such as `TEST` on a [test instance](test-instance.md). Up to 16 letters, digits, spaces, or hyphens. Leave it empty in production. |
| `DINNERBELL_ANTHROPIC_API_KEY` | None | The Anthropic API key for the optional assistant. Without it, the assistant stays off and nothing changes. A secret. See [The assistant](#the-assistant). |
| `DINNERBELL_ASSISTANT_MODEL` | `claude-sonnet-5-5` | The model the assistant uses: `claude-sonnet-5-5`, `claude-opus-5-5`, or `claude-haiku-5-5`. Dinnerbell refuses to start on any other, because the monthly cap needs the model's prices. |
| `DINNERBELL_FEATURES` | None | Comma-separated feature flags to turn on. Dinnerbell refuses to start on an unknown name; the list is in `src/dinnerbell/domain/flags.py`. A flag that a release retired is ignored, with a warning in the log at startup, so remove it from this list when you see one. |

The container image sets `DINNERBELL_BUILD` on builds from `main`. Don't add it to `.env`: even an empty line replaces the image's value.

Dinnerbell warns at start about a `DINNERBELL_` variable it doesn't know, which is most likely a typo. The warning names the variable, never its value.

To keep the four required and Slack secrets out of `.env`, see [Keep secrets in files](setup.md#keep-secrets-in-files). The Anthropic key works the same way, with one more step: see [The assistant](#the-assistant).

## The assistant

The assistant answers members' questions in plain language ([ADR 0009](adr/0009-optional-assistant.md)). It's optional, and it works only when all three of these are set:

- `DINNERBELL_ANTHROPIC_API_KEY`, the key for your Anthropic account. Anthropic bills that account for what the assistant uses.
- The `assistant` flag in `DINNERBELL_FEATURES`. The flag hides the assistant while it's being built, even with a key set.
- **Turn on the assistant** on Admin > **Assistant**. The tab shows only with the key and the flag.

Otherwise, nothing changes: no page, tab, or reply mentions the assistant, and Dinnerbell sends Anthropic nothing.

### Use it on the web

While the assistant answers a member, their menu has an **Assistant** link to their own private conversation, with **Confirm** and **Cancel** on each draft and **Start over**. Otherwise the link is hidden and the page doesn't exist. See [The Assistant page](members.md#the-assistant-page).

### Talk to it in Slack

Members talk to the assistant in their direct message with Dinnerbell, or by mentioning @Dinnerbell in the household channel. Dinnerbell replies in the message's thread. Its App Home shows a line saying so while the assistant answers that member. For Slack to deliver those messages, the app needs the assistant variant of its manifest: `deploy/slack-app-manifest.assistant.yaml`. It's the default manifest plus a writable **Messages** tab, the `im:history` and `app_mentions:read` scopes, and the `message.im` and `app_mention` events. Apply it before you turn on **Turn on the assistant**: App Home tells members they can message Dinnerbell as soon as the switch is on.

1. At [api.slack.com/apps](https://api.slack.com/apps), open the Dinnerbell app and select **App Manifest**.
2. Paste the contents of `deploy/slack-app-manifest.assistant.yaml`, set your hostname in `redirect_urls` again, and select **Save Changes**.
3. Select **Install App** > **Reinstall to Workspace**, and approve the new scopes. The bot token stays the same.

To turn the assistant off for good, paste `deploy/slack-app-manifest.yaml` again and reinstall. Until you do, Dinnerbell ignores DMs and mentions while the assistant is off, and Slack shows members a Messages tab they can type in that gets no answers.

Dinnerbell reads only direct messages to it and messages that mention it, and only their text: a direct message with a file gets "I can read only text messages." It answers one message per member at a time, and active members only: a guest, a deactivated member, or someone not linked to a member gets no reply, and their message is neither stored nor sent. A direct message thread is private to its member. A channel thread is shared: everyone sees it, and each message acts as the member who wrote it.

Replies can't mention anyone or ping the channel, and they hold no links: Dinnerbell writes any address the assistant gives in a form Slack doesn't link, such as `hxxps[:]//example[.]com`.

Each draft the assistant makes comes as a card with **Confirm** and **Cancel**: in a channel thread, only the member who asked sees it. A catalog draft's **Confirm** gives a **Review the import** link, the one link Dinnerbell builds itself, from `DINNERBELL_BASE_URL`, to the web's add-only import preview. It works only for that member, signed in, for 24 hours; Dinnerbell keeps a confirmed catalog draft that long for it.

On Admin > **Assistant**, the admin also sets the household's **Monthly cap**, $1 to $100 in US dollars a calendar month in the household's time zone ($5 by default), and **Conversation hours**, how long Dinnerbell keeps a conversation after its last message, 0 to 168 (24 by default). Before each request to Anthropic, Dinnerbell sets aside the most that request could cost and refuses it if that would pass the cap, so spending never does; the page shows this month's usage in total and for each member. Every change there is in the audit log.

The cap is kept in the database, so it covers one installation: production and a [test instance](test-instance.md) each have their own cap and usage. As a backstop, also set a monthly spend limit on the key's workspace in the Anthropic Console, and give the test instance its own key, so its spending shows apart and a limit on one doesn't stop the other.

Dinnerbell sends Anthropic the conversation and what the assistant looks up, with members as "Member 1", "Member 2", and so on. It never sends names, emails, Slack IDs, preferences, vetoes, anyone's ratings, or notes, except what a member writes in their own message. To answer questions about what the household has eaten, it sends each item's household hint, such as "Well liked", the same words every member sees on Catalog. Your Anthropic API terms govern how long Anthropic keeps requests.

`compose.secrets.yaml` doesn't mount the key, because every file it lists must exist and the key is optional. To keep the key in a file, create `secrets/DINNERBELL_ANTHROPIC_API_KEY` the way [Keep secrets in files](setup.md#keep-secrets-in-files) describes, and add a `compose.override.yaml` next to `compose.yaml`:

```yaml
services:
  dinnerbell:
    secrets:
      - DINNERBELL_ANTHROPIC_API_KEY
secrets:
  DINNERBELL_ANTHROPIC_API_KEY:
    file: ./secrets/DINNERBELL_ANTHROPIC_API_KEY
```

Then add `-f compose.override.yaml` last to every command that already names `compose.secrets.yaml`.

`dinnerbell scrub-test` turns the assistant off and deletes every conversation and draft, so a test copy never sends production's conversations or confirms production's drafts.

## The sign-in rate limit

Dinnerbell allows 20 requests a minute to the sign-in pages under `/auth/` from each client address. A sign-in takes two requests, and a sign-out one. Past the limit, the page says "Too many sign-in attempts. Wait a minute and try again."

Dinnerbell knows a client's address only from the connection, or from `X-Forwarded-For` when the connection comes from an address in `DINNERBELL_TRUSTED_PROXIES`. Behind a reverse proxy, every connection comes from the proxy, so without that setting all clients share one count. One client that signs in over and over, or a script that hits `/auth/`, then makes everyone wait up to a minute to sign in.

Under rootless podman, you usually can't set `DINNERBELL_TRUSTED_PROXIES`. Podman forwards each connection to a published port from the container network's gateway, such as `10.89.0.1`, whether it comes from your proxy or from another device that can reach the port. Trusting the gateway would let any device that reaches the port directly write its own `X-Forwarded-For` and choose its count. Leave the setting empty and accept the shared count. The exception is a proxy that runs in a container on Dinnerbell's network and connects to Dinnerbell directly: its connections come from that container's own address, which you can trust. Give that container a fixed address, or update the setting when its address changes.

To see the address that Dinnerbell sees, set `DINNERBELL_LOG_LEVEL=DEBUG` and sign in: each request logs `peer_ip`, the connection's address, and `client_ip`, the address the rate limit counts. You can trust the proxy only when `peer_ip` is an address that nothing but the proxy connects from.

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
3. Create `compose.override.yaml` next to `compose.yaml` with this content, to mount the file read-only. Compose reads it automatically unless you pass `-f`, and git ignores it. With secrets in files, add `-f compose.override.yaml` last.

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
