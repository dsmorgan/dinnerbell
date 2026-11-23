# Configuration reference

Dinnerbell reads its settings from environment variables, which compose loads from `.env`. `dinnerbell init` ([setup.md](setup.md#9-run-dinnerbell)) asks for the values a first install needs, and `.env.example` lists every setting with a comment. An empty value counts as unset.

After you change `.env`, recreate the service:

```sh
podman-compose down && podman-compose up -d
```

With secrets in files, add `-f compose.yaml -f compose.secrets.yaml` to each command ([Run compose with secret files](setup.md#run-compose-with-secret-files)).

## Settings

These settings are required:

| Setting | Meaning |
|---|---|
| `DINNERBELL_BASE_URL` | The public URL people open, exactly as they type it: the same scheme and host, no explicit `:443`, and no path. It must use `https`, except `http://localhost` or `http://127.0.0.1` for local testing. Any mismatch makes every form fail with "Cross-origin request refused". The sign-in redirect URL is this value plus `/auth/callback`; if your hostname changes, change the redirect URL under **OAuth & Permissions** in the Slack app too. |
| `DINNERBELL_SECRET_KEY` | Signs cookies and CSRF tokens. Dinnerbell refuses a key that is shorter than 32 characters, has fewer than 8 distinct characters, or contains placeholder text such as `REPLACE-ME`. Changing it invalidates open forms, sign-in pages, import previews, the assistant's **Review the import** links, unused sign-in codes, and every device's push notifications, but not sessions. A secret. |
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
| `DINNERBELL_HOUSEHOLD_NAME` | `Home` | The household name shown in the app. Read on first start only (see below). |
| `DINNERBELL_TIMEZONE` | `America/New_York` | The household time zone, as an IANA name such as `America/Chicago`. It sets when calls go out and when the nightly backup runs. Set it before first start: `.env.example` holds a placeholder that Dinnerbell refuses, and the default applies only when the variable is unset. Read on first start only (see below). |
| `DINNERBELL_TRUSTED_PROXIES` | None | Comma-separated proxy IP addresses whose `X-Forwarded-For` header Dinnerbell trusts for client addresses. Set one proxy address, never a range, and never an address that other clients share, such as a container gateway. Without it, the rate limiter counts all clients behind a proxy as one: see [The sign-in rate limit](#the-sign-in-rate-limit). |
| `DINNERBELL_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. At `DEBUG`, each request logs the peer and client addresses, which shows your proxy's address. |
| `DINNERBELL_SEED_FILE` | None | A catalog file to import on first start, while the catalog is empty. See [Load a catalog on first start](#load-a-catalog-on-first-start). |
| `DINNERBELL_DATABASE_URL` | `sqlite:////data/dinnerbell.db` | The SQLite database. Keep it under `/data`, the container's only writable volume. Backups go to `backups/` next to the database file. |
| `DINNERBELL_OIDC_SCOPES` | `openid email profile` | Scopes requested at sign-in. They must include `openid` and `email`. |
| `DINNERBELL_INSTANCE_LABEL` | None | Marks every page, Slack message, App Home, `/healthz`, and the logs, such as `TEST` on a [test instance](test-instance.md). Up to 16 letters, digits, spaces, or hyphens. Leave it empty in production. |
| `DINNERBELL_ANTHROPIC_API_KEY` | None | The Anthropic API key for the optional assistant. Without it, the assistant stays off. A secret. See [The assistant](#the-assistant). |
| `DINNERBELL_ASSISTANT_MODEL` | `claude-sonnet-5-5` | The assistant's model: `claude-sonnet-5-5`, `claude-opus-5-5`, or `claude-haiku-5-5`. Dinnerbell refuses to start on any other, because the monthly cap needs the model's prices. |
| `DINNERBELL_FEATURES` | None | Comma-separated feature flags to turn on. None exists now: 0.18.0 retired `assistant`. Dinnerbell refuses to start on an unknown name, and ignores a retired one with a warning at startup; remove it when you see one. |

Dinnerbell reads `DINNERBELL_HOUSEHOLD_NAME` and `DINNERBELL_TIMEZONE` on first start only and keeps them in its database; you can't change either after that yet ([#334](https://github.com/dsmorgan/dinnerbell/issues/334)). It ignores a later value, and while one differs, each start logs a warning: the time zone warning names both zones, and the name warning names neither. To stop it, set the variable back to the stored value, the name the app shows or the zone the warning names.

Don't add `DINNERBELL_BUILD` to `.env`: the image sets it on builds from `main`, and even an empty line replaces it. Dinnerbell warns at start about a `DINNERBELL_` variable it doesn't know, most likely a typo, naming the variable but never its value.

To keep the secrets out of `.env`, see [Keep secrets in files](setup.md#keep-secrets-in-files), and for the Anthropic key, [Keep the key in a file](#keep-the-key-in-a-file).

## The assistant

The optional assistant answers members' questions in plain language and drafts changes for them to confirm ([ADR 0009](adr/0009-optional-assistant.md)). It works only when both of these are set:

- `DINNERBELL_ANTHROPIC_API_KEY`. Anthropic bills that key's account for what the assistant uses.
- **Turn on the assistant** on **Admin** > **Assistant**. The tab shows only with the key.

Otherwise, no page, tab, or reply mentions the assistant, and Dinnerbell sends Anthropic nothing.

### Use it on the web

While the assistant is on, each active member's menu has an **Assistant** link to their own private conversation; guests get none. See [The Assistant page](members.md#the-assistant-page).

### Talk to it in Slack

Members message Dinnerbell directly, or mention @Dinnerbell in the household channel, and Dinnerbell replies in that message's thread. For Slack to deliver those messages, the app needs the assistant manifest, `deploy/slack-app-manifest.assistant.yaml`, which adds a writable **Messages** tab, the `im:history`, `app_mentions:read`, and `reactions:write` scopes, and the `message.im` and `app_mention` events. Apply it before you select **Turn on the assistant**, because App Home tells members they can message Dinnerbell as soon as the switch is on.

1. At [api.slack.com/apps](https://api.slack.com/apps), open the Dinnerbell app and select **App Manifest**.
2. Paste the contents of `deploy/slack-app-manifest.assistant.yaml`, set your hostname in `redirect_urls` again, and select **Save Changes**.
3. Select **Install App** > **Reinstall to Workspace**, and approve the new scopes. The bot token stays the same.

To turn the assistant off for good, paste `deploy/slack-app-manifest.yaml` again and reinstall. Until you do, Slack shows members a **Messages** tab where their messages get no answer.

How it behaves in Slack:

- It reads only text: a direct message with a file gets "I can read only text messages."
- It answers active members only, one message at a time each. A guest, a deactivated member, or someone not linked to a member gets no reply, and their message is neither stored nor sent.
- A direct message thread is private to its member. A channel thread is shared: everyone sees it, and each message acts as the member who wrote it. A draft card in a channel thread shows only to the member who asked.
- While it works, the thread shows "Dinnerbell is thinking…", or, where Slack won't show that status, an :hourglass_flowing_sand: reaction, which is what `reactions:write` is for.
- Replies can't mention anyone or ping the channel, and hold no links: Dinnerbell writes any address in a form Slack doesn't link, such as `hxxps[:]//example[.]com`. The one link it builds itself, from `DINNERBELL_BASE_URL`, is a catalog draft's **Review the import** link to the add-only import preview, which works only for that member, signed in, for 24 hours.

### Set the cap

On **Admin** > **Assistant**, set the household's **Monthly cap**, $1 to $100 a calendar month in the household's time zone ($5 by default), and **Conversation hours**, how long Dinnerbell keeps a conversation after its last message, 0 to 168 (24 by default). Before each request, Dinnerbell reserves the most it could cost and refuses it if that would pass the cap, so spending never does. The page shows this month's usage in total and for each member, and, if the cap is too low for even one short message, such as $1 with `claude-opus-5-5`, names the smallest cap that covers one; long conversations need more.

Production and a [test instance](test-instance.md) each keep their own cap and usage in their database. As a backstop, set a monthly spend limit on the key's workspace in the Anthropic Console, and give the test instance its own key, so one limit doesn't stop the other.

### What Dinnerbell sends Anthropic

**Admin** > **Assistant** sums this up; this section is the full list.

- **With every message:** the conversation and what the assistant looks up, with members shown as "Member 1", "Member 2", and so on, plus every active member's display name with their label, such as "Member 2: Theodore", so the assistant can tell who a member means by a name they type. Guests and former members are never listed. A name a member types, or the assistant writes, stays in the conversation as written until Conversation hours delete it, even after that member is deactivated.
- **For questions about what the household ate:** each item's household hint, such as "Well liked", the words every member sees on **Catalog**.
- **For a menu or recipe link a member gives it:** the text of that page, which Dinnerbell fetches. The separate call that drafts items from it carries only the page's text, with no conversation or member data.
- **Only when a member asks about their own data in private,** on the Assistant page or in a direct message: their preferences, vetoes, dietary needs, what they can usually do (cook, pick up, or order), ratings, feedback, and away days. Dietary needs can reveal health or religion.
- **Never:** emails, Slack IDs, an item's stored notes or links, or anyone else's preferences, vetoes, ratings, or notes, except what a member writes in their own message.

Your Anthropic API terms govern how long Anthropic keeps requests. Conversation hours and draft expiry delete only Dinnerbell's live copy: backups keep the conversations and drafts they hold until you delete the backup ([What a backup holds](backup-and-upgrade.md#what-a-backup-holds)).

### Keep the key in a file

`compose.secrets.yaml` doesn't mount the key, because every file it lists must exist and the key is optional. Create `secrets/DINNERBELL_ANTHROPIC_API_KEY` as [Keep secrets in files](setup.md#keep-secrets-in-files) describes, and add a `compose.override.yaml` next to `compose.yaml`:

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

The test instance doesn't read `compose.override.yaml`; see [Secrets in files for test](test-instance.md#secrets-in-files-for-test).

## Web push

While **Admin** > **Web app** is on, Dinnerbell sends push notifications to the devices members turned them on for ([ADR 0010](adr/0010-optional-web-app.md)). It needs no setting of its own, but the host needs outbound HTTPS (port 443) to the push services:

| Host | For |
|---|---|
| `fcm.googleapis.com`, `jmt17.google.com` | Chrome, Edge on Android, and other Chromium browsers |
| `updates.push.services.mozilla.com` | Firefox |
| `*.push.apple.com`, such as `web.push.apple.com` | Safari on iPhone, iPad, and Mac |
| `*.notify.windows.com` | Edge on Windows |

Dinnerbell posts only to these hosts, directly, as [Outbound connections](#outbound-connections) describes, and connects to none of them while the switch is off. A browser whose push service isn't listed gets "That browser's push service isn't one Dinnerbell knows.", and the log line `push subscription refused` names its host. The push key comes from `DINNERBELL_SECRET_KEY`, so changing the secret key ends every device's notifications until each member taps **Turn on notifications** again. Apple refuses pushes unless `DINNERBELL_BASE_URL` is https, and Dinnerbell warns at startup.

## Local weather

While **Dinnerbone's weather** on **Admin** > **Web app** is on, Dinnerbell asks Open-Meteo for the weather at the household's postal code ([ADR 0011](adr/0011-local-weather.md), [Dinnerbone's weather](members.md#dinnerbones-weather)). It needs no setting or key of its own, but the host needs outbound HTTPS (port 443) to:

| Host | For |
|---|---|
| `geocoding-api.open-meteo.com` | Finding the place for a postal code, once each time an admin saves it |
| `api.open-meteo.com` | The current weather there, every 1 to 3 hours |

Dinnerbell calls only these hosts, at addresses it builds itself, directly, as [Outbound connections](#outbound-connections) describes, and connects to neither while local weather is off, the default. If it can't reach them, the den shows the season's weather, and the log says `local weather failed; the den shows the seasonal weather` once, with the reason, until it recovers. Open-Meteo's free API is for non-commercial use: check that its terms fit your install.

## Outbound connections

Except for Slack's API calls and Socket Mode, every request Dinnerbell makes, to the sign-in provider, Anthropic, the push services, menu links, and the weather, ignores proxy and CA bundle environment variables, such as `HTTPS_PROXY` and `SSL_CERT_FILE`, and verifies TLS against Dinnerbell's bundled certificate authorities. None follows a redirect, except a menu link, which follows up to 5, checking that each address is public. The host must reach those services directly. [Allow only the outbound connections Dinnerbell needs](hardening.md#allow-only-the-outbound-connections-dinnerbell-needs) lists the hosts.

Slack's connection, its API calls and Socket Mode, still follows these environment variables:

- The first of `HTTPS_PROXY`, `https_proxy`, `HTTP_PROXY`, and `http_proxy` that is set, in that order, is the proxy for every Slack connection, so `HTTP_PROXY` applies to HTTPS too. An empty variable counts as not set. `NO_PROXY` has no effect.
- `SSL_CERT_FILE` and `SSL_CERT_DIR` replace the system's certificate authorities that Slack's connection verifies against.

Set none of them unless the host must reach Slack through a proxy. Whoever can set the container's environment can send Slack's traffic through a proxy of their choosing and, with `SSL_CERT_FILE` or `SSL_CERT_DIR`, make it trust their own certificate, which exposes the Slack tokens.

## The sign-in rate limit

Dinnerbell allows 20 requests a minute to the sign-in pages under `/auth/` from each client address. A sign-in takes two requests, and a sign-out one. Past the limit, the page says "Too many sign-in attempts. Wait a minute and try again."

Dinnerbell takes a client's address from the connection, or from `X-Forwarded-For` when the connection comes from an address in `DINNERBELL_TRUSTED_PROXIES`. Behind a reverse proxy, every connection comes from the proxy, so without that setting all clients share one count, and one client that hits `/auth/` over and over makes everyone wait up to a minute.

Under rootless podman, leave `DINNERBELL_TRUSTED_PROXIES` empty: every connection to a published port comes from the container network's gateway, such as `10.89.0.1`, so trusting it would let any device that reaches the port choose its own count. The exception is a proxy in a container on Dinnerbell's network that connects to Dinnerbell directly, whose address you can trust if you keep it fixed.

To see what Dinnerbell sees, set `DINNERBELL_LOG_LEVEL=DEBUG` and sign in: each request logs `peer_ip`, the connection's address, and `client_ip`, the address the rate limit counts. Trust the proxy only when nothing but the proxy connects from its `peer_ip`.

## Compose settings

Compose, not Dinnerbell, reads this setting from `.env`:

| Setting | Default | Meaning |
|---|---|---|
| `DINNERBELL_BIND` | `127.0.0.1` | The host address to publish port 8013 on, for production and the test instance. The default works only when your reverse proxy (for Pangolin, Newt) runs on the same host, outside a container network. Otherwise, set the host's address on the interface the proxy reaches, such as `192.168.1.20`. Avoid `0.0.0.0` unless a firewall limits who can reach the port. |

Don't set `DINNERBELL_TAG`, `DINNERBELL_PORT`, or `DINNERBELL_ENV_FILE` in `.env`. The defaults in `compose.yaml` are right for production, and `deploy/dinnerbell-test` refuses to run if `.env` sets them.

These docs are tested with podman-compose 1.0.6 (Ubuntu 24.04's), 1.1.0, 1.5.0, and 1.6.0, and Docker Compose 2.40.

## Load a catalog on first start

To have Dinnerbell import a catalog file the first time it starts, do this before that first start:

1. Write the file as `seed/my-catalog.yaml`, in the [catalog format](catalog-format.md). A bundle has no `seed/` directory, so create it. In a clone, start from the example; git ignores the rest of `seed/`.

   ```sh
   cp seed/example.yaml seed/my-catalog.yaml
   ```

2. Create `compose.override.yaml` next to `compose.yaml` to mount the file read-only. Compose reads it automatically unless you pass `-f`; with secrets in files, add `-f compose.override.yaml` last.

   ```yaml
   services:
     dinnerbell:
       volumes:
         - ./seed/my-catalog.yaml:/seed/catalog.yaml:ro
   ```

3. In `.env`, add this line:

   ```sh
   DINNERBELL_SEED_FILE=/seed/catalog.yaml
   ```

On first start, only the admin is a member, so the import skips every other cook. After you invite everyone, import the same file again on the **Catalog** page to fill in the cooks. Do that before you edit those items in the app, because the file's fields replace what's stored.

## Resource and log limits

`compose.yaml` caps the container, and the test instance, so a runaway process or a noisy log can't take the host.

| Key | Value | Why |
|---|---|---|
| `pids_limit` | `256` | Measured: 6 processes and threads at idle, 47 after 300 concurrent requests. |
| `mem_limit` | `512m` | Measured: about 100 MiB at idle, 130 MiB peak after 300 concurrent requests. The rest leaves room for badge rendering and large catalog imports. |
| `logging` | `json-file`, `max-size: 10m`, `max-file: "3"` | At most 30 MB of logs. |

If `podman inspect` shows the container `OOMKilled`, raise `mem_limit` in a `compose.override.yaml`.

Docker Compose and podman-compose 1.6.0 apply all three keys. podman-compose 1.0.6 ignores `pids_limit` without a warning, so upgrade it to get the process cap, and older podman releases ignore `max-file`.
