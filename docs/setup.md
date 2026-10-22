# Set up Dinnerbell

This guide takes you from nothing to a working Dinnerbell install: a Slack workspace and app, a container on your host, a public HTTPS hostname, and your family signed in.

## Status

Dinnerbell is under construction. Milestone M0 ships these parts, and this guide covers them fully:

- Sign in with Slack, and the members admin page
- `/healthz`
- The scheduler
- Nightly backups

These parts are not built yet:

- **The Slack bot** does not connect until milestone M2. Until then, `/healthz` reports Slack as `not_configured`, and the bot posts nothing. Steps 3 and 4 still apply, so your tokens are ready.
- **The catalog, catalog import, and `DINNERBELL_SEED_FILE`** arrive in milestone M1.

## Prerequisites

- A Linux host with podman and podman-compose, or Docker with the compose plugin.
- A public HTTPS hostname that a reverse proxy forwards to the host's port 8000. The reference setup uses Pangolin.
- A Slack account. You use its email address as the admin email.

## 0. Clone the repository

Later steps use files from the repository, so clone it first.

```sh
git clone https://github.com/dsmorgan/dinnerbell.git
cd dinnerbell
```

## 1. Create the Slack workspace and channel

1. Go to [slack.com/get-started](https://slack.com/get-started).
2. Create a new workspace for your household. The free plan is enough.
3. Create a channel named `#dinnerbell`.

The free plan limits history to 90 days and the number of apps to 10. Neither matters: Dinnerbell is the system of record, and it is the only app.

## 2. Create the Slack app

1. Open `deploy/slack-app-manifest.yaml` in the repository you cloned.
2. Replace `dinnerbell.example.com` in `redirect_urls` with your public hostname. Keep the path `/auth/callback`.
3. Go to [api.slack.com/apps](https://api.slack.com/apps) and select **Create New App**.
4. Select **From a manifest**.
5. Select your household workspace.
6. Paste the edited manifest as YAML.
7. Select **Next**, then **Create**.

If you change your hostname later, edit the redirect URL under **OAuth & Permissions**.

## 3. Create the app-level token

Socket Mode lets the app connect out to Slack, so Slack never calls your host. The manifest already enables Socket Mode. You still create the token.

1. In the app settings, open **Basic Information** and scroll to **App-Level Tokens**.
2. Select **Generate Token and Scopes**.
3. Name the token `dinnerbell`, add the `connections:write` scope, and select **Generate**.
4. Copy the token. It starts with `xapp-`. This is `DINNERBELL_SLACK_APP_TOKEN`.

## 4. Install the app and copy the bot token

1. Open **Install App** and select **Install to Workspace**, then **Allow**.
2. Copy the **Bot User OAuth Token**. It starts with `xoxb-`. This is `DINNERBELL_SLACK_BOT_TOKEN`.

## 5. Copy the sign-in credentials

1. Open **Basic Information** and find **App Credentials**.
2. Copy **Client ID** into `DINNERBELL_OIDC_CLIENT_ID`.
3. Reveal and copy **Client Secret** into `DINNERBELL_OIDC_CLIENT_SECRET`.
4. Set `DINNERBELL_OIDC_ISSUER` to `https://slack.com`. Slack publishes its discovery document at `https://slack.com/.well-known/openid-configuration`.

## 6. Find the team ID and channel ID

1. Open your workspace in a browser. The address looks like `https://app.slack.com/client/T0123ABCDEF/...`.
2. Copy the part that starts with `T`. This is `DINNERBELL_SLACK_TEAM_ID`. It is required with Slack sign-in, and Dinnerbell refuses to start without it. It makes Dinnerbell refuse sign-ins from any other Slack workspace; without it, someone in another workspace whose Slack email matches an invitation could claim it.
3. In the `#dinnerbell` channel, select the channel name at the top to open the channel details.
4. Copy the channel ID at the bottom of the **About** tab. It starts with `C`. This is `DINNERBELL_SLACK_CHANNEL`. If you don't see it there, copy it from the address bar when you open the channel in a browser.

## 7. Invite the bot to the channel

1. In `#dinnerbell`, type `/invite @Dinnerbell` and send it.
2. Select **Add to Channel** if Slack asks you to confirm.

## 8. Expose Dinnerbell through your reverse proxy

Point your hostname at the host's port 8000. With Pangolin:

1. Create a public HTTP resource for your hostname.
2. Set the target to the host running Dinnerbell, port 8000.
3. Open the resource's authentication settings and turn off **Use Platform SSO**. Pangolin turns it on by default for public resources.
4. Leave password, PIN, email OTP, and header auth off too.

Dinnerbell does its own sign-in. A second login in front of it breaks the OIDC redirect back from Slack and makes every device sign in twice. Pangolin still provides TLS, the hostname, and connection-level protections.

With authentication off, `/healthz` is public. It shows only coarse status.

`compose.yaml` publishes port 8000 on the address in `DINNERBELL_BIND`, which defaults to `127.0.0.1`. That default only works when Newt runs on the same host and not inside a container network. Otherwise, set `DINNERBELL_BIND` in `.env` to the host's address on the interface Newt reaches, for example its LAN address:

```sh
DINNERBELL_BIND=192.168.1.20
```

Avoid `0.0.0.0` unless a firewall limits who can reach the port.

## 9. Run Dinnerbell

1. Copy the example environment file.

   ```sh
   cp .env.example .env
   ```

2. Restrict access to it, because it holds secrets.

   ```sh
   chmod 600 .env
   ```

3. Generate a secret key.

   ```sh
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Or use `openssl rand -base64 48`.

4. Edit `.env` and fill in the values you collected.

   ```sh
   DINNERBELL_BASE_URL=https://dinnerbell.example.com
   DINNERBELL_SECRET_KEY=<generated key>
   DINNERBELL_ADMIN_EMAIL=you@example.com
   DINNERBELL_HOUSEHOLD_NAME=Home
   DINNERBELL_TIMEZONE=America/Chicago

   DINNERBELL_OIDC_ISSUER=https://slack.com
   DINNERBELL_OIDC_CLIENT_ID=<client ID>
   DINNERBELL_OIDC_CLIENT_SECRET=<client secret>
   DINNERBELL_SLACK_TEAM_ID=T0123ABCDEF

   DINNERBELL_SLACK_BOT_TOKEN=xoxb-...
   DINNERBELL_SLACK_APP_TOKEN=xapp-...
   DINNERBELL_SLACK_CHANNEL=C0123ABCDEF

   # Set this in the trusted-proxy step below.
   # DINNERBELL_TRUSTED_PROXIES=
   ```

   `DINNERBELL_ADMIN_EMAIL` must match the email on your Slack account. If you change it later, Dinnerbell invites the new address as another admin on the next start.

   `DINNERBELL_BASE_URL` must exactly match the URL people type: the same host, `https://`, and no explicit `:443` or path. Dinnerbell derives the sign-in redirect URL from it, and checks every form submission against it. With any other host, a LAN IP, an explicit `:443`, or plain `http://`, every form fails with "Cross-origin request refused". Plain `http://` also breaks sign-in, because the browser never stores the `Secure` session cookie over http.

   `DINNERBELL_HOUSEHOLD_NAME` and `DINNERBELL_TIMEZONE` take effect on first start only. After that, Dinnerbell reads them from its database, so set them correctly before you start it the first time.

5. Optional, M1 and later: load a starter catalog on first start. Copy `seed/example.yaml` to `seed/my-catalog.yaml` and edit it. The format is in [catalog-format.md](catalog-format.md).

   ```sh
   cp seed/example.yaml seed/my-catalog.yaml
   ```

6. Optional, M1 and later: mount the catalog file read-only. Create `compose.override.yaml` next to `compose.yaml` with this content. Both compose tools read it automatically.

   ```yaml
   services:
     dinnerbell:
       volumes:
         - ./seed/my-catalog.yaml:/seed/catalog.yaml:ro
   ```

7. Optional, M1 and later: point Dinnerbell at the mounted file by adding this line to `.env`. Dinnerbell imports the file on first start. You can also import a file later from the Catalog page.

   ```sh
   DINNERBELL_SEED_FILE=/seed/catalog.yaml
   ```

8. Build the image.

   ```sh
   podman-compose build
   ```

   With Docker: `docker compose build`

9. Start the service.

   ```sh
   podman-compose up -d
   ```

   With Docker: `docker compose up -d`

10. Check that it is healthy.

    ```sh
    curl https://dinnerbell.example.com/healthz
    ```

    The response reports database reachability, the scheduler's last tick, the Slack state (`not_configured` until M2), and the last backup time. It returns HTTP 503 when the database is unreachable or the scheduler has not ticked for 5 minutes, so the container health check and Uptime Kuma catch both.

### Set the trusted proxy

Rate limiting needs the real client IP. Dinnerbell reads `X-Forwarded-For` only from proxies you list in `DINNERBELL_TRUSTED_PROXIES`.

1. Add `DINNERBELL_LOG_LEVEL=DEBUG` to `.env`.
2. Recreate the service so it reads `.env`. A restart doesn't re-read it.

   ```sh
   podman-compose down && podman-compose up -d
   ```

   With Docker: `docker compose down && docker compose up -d`

3. Load your public URL in a browser.
4. Read the logs. At this level, the app logs the peer IP and the resolved client IP for each request.

   ```sh
   podman-compose logs dinnerbell
   ```

   With Docker: `docker compose logs dinnerbell`

5. Find the peer IP. It is the address the proxy connects from. With a Newt site, it is usually the address of the host or container that runs Newt. With a plain WireGuard site, it is the Pangolin server's tunnel address.

   Check the peer IP from a second client too, such as `curl` from another machine on your LAN. Under Docker Desktop, and with rootless podman's port forwarding, every connection to a published port can arrive from the same gateway address, such as `10.0.2.2` or the bridge gateway. If the second client shows the same peer IP as Newt, trusting that address lets any client that can reach the published port forge `X-Forwarded-For`. Trust it only if nothing but Newt can reach the port, for example because `DINNERBELL_BIND` and your firewall restrict it. Otherwise leave `DINNERBELL_TRUSTED_PROXIES` empty: rate limiting then treats all clients as one, which is safe but stricter.
6. Set the single peer address in `.env`, and remove the `DINNERBELL_LOG_LEVEL` line or set it back to `INFO`.

   ```sh
   DINNERBELL_TRUSTED_PROXIES=10.0.0.5
   ```

7. Recreate the service again.

   ```sh
   podman-compose down && podman-compose up -d
   ```

Set only the one address the proxy connects from. Never use `0.0.0.0/0`, or a LAN or podman subnet range. Anyone inside a trusted range can forge `X-Forwarded-For` and get around rate limiting.

## 10. Sign in as the admin

1. Open your public URL in a browser.
2. Select **Sign in with Slack** and approve the request.
3. Open **Members**.
4. Add each household member by the email address you will invite to Slack. Sign-in is invite-only, and the email must match the one on their Slack account.

## 11. Onboard family members

Invite each person to the Slack workspace from Slack (**Invite people**), using the same email address you added on **Members**. Then forward them the following message.

> **Set up Dinnerbell on your phone**
>
> 1. Install Slack from the App Store.
> 2. When Slack asks to send notifications, tap **Allow**.
> 3. Open the invitation email from Slack and accept the invite to our workspace.
> 4. In Slack, open the `#dinnerbell` channel.
> 5. Tap the channel name, then tap **Notifications**.
> 6. Choose **All new messages**.
> 7. Optional: open `https://dinnerbell.example.com` in Safari, tap Share, then tap **Add to Home Screen**. This lets you open Dinnerbell like an app.
> 8. Open Dinnerbell and tap **Sign in with Slack**.
> 9. Slack asks you to sign in again in Safari. Enter your email, type the code Slack emails you, then tap **Allow**.
> 10. If the Home Screen icon asks you to sign in, sign in once more.

## 12. Load the catalog

The catalog is your list of restaurants and home meals. [catalog-format.md](catalog-format.md) describes the file format, and [seed/example.yaml](../seed/example.yaml) is a starter file.

- **On first start:** mount a catalog file and set `DINNERBELL_SEED_FILE`, as described in step 9 (M1 and later).
- **After first start:** paste or upload the file on the Catalog page (M1 and later).

## Back up

Dinnerbell runs an online backup of its SQLite database every night at 03:30 household time into `/data/backups/`, and once at start if the newest backup is more than a day old. It keeps the 14 newest daily copies plus the newest copy from each of the last 8 weeks. In M0, only `/healthz` shows the last successful backup; the Admin page gains it later.

These backups live in the same volume as the database, so copy them off the host with your own backup routine. Copy `/data/backups/` only. Never copy the live `dinnerbell.db` or its WAL file, because a copy taken mid-write can be corrupt.

1. List the volumes. Compose prefixes the volume name with the project name, which is the directory name by default.

   ```sh
   podman volume ls
   ```

2. Find the volume's path on the host.

   ```sh
   podman volume inspect <volume> --format '{{.Mountpoint}}'
   ```

   With Docker, use `docker volume ls` and `docker volume inspect <volume> --format '{{.Mountpoint}}'`.

3. Point your backup routine at the `backups` directory inside that path.

The files are readable only by their owner, uid 10001 inside the container, because they contain member emails. Under rootful podman or Docker, run your backup routine as root. Under rootless podman, uid 10001 maps to one of your subordinate IDs, so your own user can't read the files directly. Copy them out through the user namespace instead:

```sh
mkdir -p ~/dinnerbell-backups
podman unshare cp -a "$(podman volume inspect <volume> --format '{{.Mountpoint}}')/backups/." ~/dinnerbell-backups/
podman unshare chown -R 0:0 ~/dinnerbell-backups
```

The trailing `/.` copies the directory's contents, so running the same commands again refreshes the copy instead of nesting a second `backups` directory inside it.

Inside `podman unshare`, your user is root, so the copy works, and the `chown` hands the copies back to your own user.

## Upgrade

1. Pull the new code.

   ```sh
   git pull
   ```

2. Rebuild and recreate the service.

   ```sh
   podman-compose build
   podman-compose up -d
   ```

   With Docker: `docker compose build` then `docker compose up -d`.

Database migrations run on start. Dinnerbell refuses to start if the database has a schema version it does not recognize, for example after a downgrade.

## Use a different sign-in provider

The web app works with any OpenID Connect provider that publishes a discovery document. To use Google:

1. In the Google Cloud console, open **Google Auth Platform** and configure the OAuth consent screen.
2. Under **Audience**, add your household members as test users, or publish the app.
3. Create an OAuth client of type **Web application**.
4. Add `https://dinnerbell.example.com/auth/callback` as an authorized redirect URI.
5. In `.env`, set the following values and leave `DINNERBELL_SLACK_TEAM_ID` unset.

   ```sh
   DINNERBELL_OIDC_ISSUER=https://accounts.google.com
   DINNERBELL_OIDC_CLIENT_ID=<Google client ID>
   DINNERBELL_OIDC_CLIENT_SECRET=<Google client secret>
   ```

Invite members by the email on their Google account. In version 1, Slack is still required for notifications, so you still complete sections 1 through 4, the channel ID in section 6, and section 7.

### Switch providers on an existing install

Each member is linked to the account they first signed in with. After you switch providers, everyone, including you, sees "That email is already linked" until those links are cleared. To clear them:

1. Stop the service.

   ```sh
   podman-compose down
   ```

2. Clear every member's link. Members keep their emails, roles, and history.

   ```sh
   podman-compose run --rm --entrypoint python dinnerbell -c "import sqlite3; db = sqlite3.connect('/data/dinnerbell.db'); db.execute('UPDATE member SET oidc_subject = NULL'); db.commit()"
   ```

   With Docker: `docker compose run --rm --entrypoint python dinnerbell -c "..."` with the same command.

   The command assumes the default `DINNERBELL_DATABASE_URL`, which puts the database at `/data/dinnerbell.db`. If you changed it, use your database's path in the command instead.

3. Start the service again. Each member's next sign-in with a verified matching email links the new account.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Slack shows "redirect_uri did not match" or similar after you select Sign in | The redirect URL in the Slack app differs from `https://<host>/auth/callback`, for example because you pasted the manifest without editing the hostname. | Under **OAuth & Permissions > Redirect URLs**, set the exact URL and save. Also check that `DINNERBELL_BASE_URL` matches. |
| Sign-in returns to a Pangolin login page or loops | Pangolin resource authentication is still on. | Turn off **Use Platform SSO**, and leave password, PIN, email OTP, and header auth off. |
| Sign-in completes but Dinnerbell says you are not invited | The email on the Slack account differs from the invited email, or the email is not verified. | Compare the emails on **Members** and in your Slack profile. Fix the member's email, or use `DINNERBELL_ADMIN_EMAIL` for the admin. |
| Sign-in refused for a person you invited | They signed in with a different Slack workspace, and `DINNERBELL_SLACK_TEAM_ID` blocks it. | Ask them to sign in to your household workspace. |
| `/healthz` reports Slack not connected | The app token is wrong or missing, Socket Mode is off, or the host cannot reach Slack. | Until M2, `not_configured` is expected. Afterward, check the logs. Confirm Socket Mode is on in the app settings and that the host can make outbound HTTPS connections. |
| Every form fails with "Cross-origin request refused", or sign-in loops back to the start page | `DINNERBELL_BASE_URL` doesn't exactly match the URL in the browser: another host, a LAN IP, an explicit `:443`, or `http://`. | Set `DINNERBELL_BASE_URL` to exactly the URL people type, `https://` and no port, then recreate the service. Open Dinnerbell only at that URL. |
| Sign-in says "That email is already linked" for everyone | You switched sign-in providers, so members are linked to accounts at the old provider. | Follow "Switch providers on an existing install". |
| Startup error: `DINNERBELL_SLACK_TEAM_ID is required` | The issuer is Slack and the team ID is empty. | Set `DINNERBELL_SLACK_TEAM_ID` as described in step 6, then recreate the service. |
| Startup error about a token prefix | The tokens are swapped or copied from the wrong page. | `DINNERBELL_SLACK_APP_TOKEN` starts with `xapp-` (Basic Information > App-Level Tokens). `DINNERBELL_SLACK_BOT_TOKEN` starts with `xoxb-` (Install App). |
| Rate limiting blocks everyone at once | Dinnerbell sees the proxy's address for every request. | Set `DINNERBELL_TRUSTED_PROXIES` to the proxy's single address, then recreate with `podman-compose down && podman-compose up -d`. A restart doesn't re-read `.env`. |
| Bot does not see the channel | The bot is not a member of the channel. | Run `/invite @Dinnerbell` in the channel. |
