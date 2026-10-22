# Set up Dinnerbell

This guide takes you from nothing to a working Dinnerbell install: a Slack workspace and app, a container on your host, a public HTTPS hostname, and your family signed in.

## Status

Dinnerbell is under construction. These parts of the guide depend on features that are not built yet:

- **Slack notifications** (calls, votes, decisions, App Home, `/dinnerbell`) arrive in milestone M2. Until then, the Slack app connects and `/healthz` reports it, but the bot posts nothing.
- **Catalog import from the Catalog page** arrives in milestone M1. `DINNERBELL_SEED_FILE` is the way to load a catalog before then.

Sign-in, member invitations, backups, and `/healthz` do not depend on either.

## Prerequisites

- A Linux host with podman and podman-compose, or Docker with the compose plugin.
- A public HTTPS hostname that a reverse proxy forwards to the host's port 8000. The reference setup uses Pangolin.
- A Slack account. You use its email address as the admin email.

## 1. Create the Slack workspace and channel

1. Go to [slack.com/get-started](https://slack.com/get-started) and create a new workspace for your household. The free plan is enough.
2. Create a channel named `#dinnerbell`.

The free plan limits history to 90 days and the number of apps to 10. Neither matters: Dinnerbell is the system of record, and it is the only app.

## 2. Create the Slack app

1. Open `deploy/slack-app-manifest.yaml` in this repository.
2. Replace `dinnerbell.example.com` in `redirect_urls` with your public hostname. Keep the path `/auth/callback`.
3. Go to [api.slack.com/apps](https://api.slack.com/apps), select **Create New App**, then **From a manifest**.
4. Select your household workspace.
5. Paste the edited manifest as YAML, then select **Next** and **Create**.

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

1. Open your workspace in a browser. The address looks like `https://app.slack.com/client/T0123ABCDEF/...`. The part that starts with `T` is the team ID. This is `DINNERBELL_SLACK_TEAM_ID`. Dinnerbell refuses sign-ins from any other Slack workspace when you set it.
2. In the `#dinnerbell` channel, select the channel name at the top to open the channel details. The channel ID is at the bottom of the **About** tab and starts with `C`. This is `DINNERBELL_SLACK_CHANNEL`. If you do not see it there, copy it from the address bar when you open the channel in a browser.

## 7. Invite the bot to the channel

1. In `#dinnerbell`, type `/invite @Dinnerbell` and send it.
2. Select **Add to Channel** if Slack asks you to confirm.

## 8. Expose Dinnerbell through your reverse proxy

Point your hostname at the host's port 8000. With Pangolin:

1. Create a public HTTP resource for your hostname. Set the target to the host running Dinnerbell, port 8000.
2. Turn off resource authentication. Pangolin enables its own login (Platform SSO) on public resources by default, so disable it and every other Pangolin authentication method on this resource.

Dinnerbell does its own sign-in. A second login in front of it breaks the OIDC redirect back from Slack and makes every device sign in twice. Pangolin still provides TLS, the hostname, and connection-level protections.

Next, set `DINNERBELL_TRUSTED_PROXIES` to the address your reverse proxy connects from, so rate limiting sees real client IPs instead of the proxy's. With Pangolin, this is the source address of the Newt or Gerbil tunnel traffic as the container sees it. Check it after your first request with `podman logs dinnerbell`, or in the Dinnerbell request log at `DINNERBELL_LOG_LEVEL=DEBUG`, and set the value to that address or its CIDR range. Separate multiple entries with commas. If you leave it unset, Dinnerbell treats the proxy address as the client.

## 9. Run Dinnerbell

1. Clone the repository and change into it.

   ```sh
   git clone https://github.com/<owner>/dinnerbell.git
   cd dinnerbell
   ```

2. Copy the example environment file.

   ```sh
   cp .env.example .env
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

   DINNERBELL_TRUSTED_PROXIES=<proxy address or CIDR>
   ```

   `DINNERBELL_ADMIN_EMAIL` must match the email on your Slack account. Keep the file private: it holds secrets.

5. Build the image.

   ```sh
   podman build -t dinnerbell .
   ```

   With Docker: `docker build -t dinnerbell .`

6. Start the service.

   ```sh
   podman-compose up -d
   ```

   With Docker: `docker compose up -d`

7. Check that it is healthy.

   ```sh
   curl https://dinnerbell.example.com/healthz
   ```

   The response reports database reachability, the Slack socket state, and the last backup time.

## 10. Sign in as the admin

1. Open your public URL in a browser.
2. Select **Sign in with Slack** and approve the request.
3. Open **Admin > Members**.
4. Add each household member using the email on their Slack account. Sign-in is invite-only, and the email must match.

## 11. Onboard family members

Invite each person to the Slack workspace from Slack (**Invite people**), then forward them the following message.

> **Set up Dinnerbell on your phone**
>
> 1. Install Slack from the App Store.
> 2. Open the invitation email from Slack and accept the invite to our workspace.
> 3. Open `https://dinnerbell.example.com` in Safari and select **Sign in with Slack**. You only need to do this once per device.
> 4. Optional: in Safari, select Share, then **Add to Home Screen**, to open Dinnerbell like an app.
> 5. In Slack, open the `#dinnerbell` channel, select the channel name, select **Notifications**, and choose **All new messages**.

## 12. Load the catalog

The catalog is your list of restaurants and home meals. The file format is described in [catalog-format.md](catalog-format.md), and [seed/example.yaml](../seed/example.yaml) is a starter file.

- **Before first start:** copy your file into the container's `/data` volume, or mount it, and set `DINNERBELL_SEED_FILE` to its path inside the container. Dinnerbell imports it on first start.
- **After first start:** paste or upload the file on the Catalog page. This arrives in milestone M1.

## Back up

Dinnerbell runs an online backup of its SQLite database every night into `/data/backups/`. It keeps 14 daily and 8 weekly copies. The Admin page and `/healthz` show the last successful backup.

These backups live in the same volume as the database. Copy `/data` off the host with your own backup routine.

## Upgrade

1. Pull the new code.

   ```sh
   git pull
   ```

2. Rebuild and restart.

   ```sh
   podman build -t dinnerbell .
   podman-compose up -d
   ```

   With Docker: `docker build -t dinnerbell .` then `docker compose up -d`.

Database migrations run on start. Dinnerbell refuses to start if the database has a schema version it does not recognize, for example after a downgrade.

## Use a different sign-in provider

The web app works with any OpenID Connect provider that publishes a discovery document. To use Google:

1. In the Google Cloud console, create an OAuth client of type **Web application**.
2. Add `https://dinnerbell.example.com/auth/callback` as an authorized redirect URI.
3. In `.env`, set the following values and leave `DINNERBELL_SLACK_TEAM_ID` unset.

   ```sh
   DINNERBELL_OIDC_ISSUER=https://accounts.google.com
   DINNERBELL_OIDC_CLIENT_ID=<Google client ID>
   DINNERBELL_OIDC_CLIENT_SECRET=<Google client secret>
   ```

Members must still be invited by the email on their Google account. In version 1, Slack is still required for notifications, so you still complete steps 1 through 4 and 6 through 7.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Slack shows "redirect_uri did not match" or similar after you select Sign in | The redirect URL in the Slack app differs from `https://<host>/auth/callback`, for example because you pasted the manifest without editing the hostname. | Under **OAuth & Permissions > Redirect URLs**, set the exact URL and save. Also check that `DINNERBELL_BASE_URL` matches. |
| Sign-in returns to a Pangolin login page or loops | Pangolin resource authentication is still on. | Turn off every Pangolin authentication method on the resource. |
| Sign-in completes but Dinnerbell says you are not invited | The email on the Slack account differs from the invited email, or the email is not verified. | Compare the emails on **Admin > Members** and in your Slack profile. Fix the member's email, or use `DINNERBELL_ADMIN_EMAIL` for the admin. |
| Sign-in refused for a person you invited | They signed in with a different Slack workspace, and `DINNERBELL_SLACK_TEAM_ID` blocks it. | Ask them to sign in to your household workspace. |
| `/healthz` reports Slack not connected | The app token is wrong or missing, Socket Mode is off, or the host cannot reach Slack. | Check the logs. Confirm Socket Mode is on in the app settings and that the host can make outbound HTTPS connections. |
| Startup error about a token prefix | The tokens are swapped or copied from the wrong page. | `DINNERBELL_SLACK_APP_TOKEN` starts with `xapp-` (Basic Information > App-Level Tokens). `DINNERBELL_SLACK_BOT_TOKEN` starts with `xoxb-` (Install App). |
| Rate limiting blocks everyone at once | Dinnerbell sees the proxy's address for every request. | Set `DINNERBELL_TRUSTED_PROXIES` to the proxy's address and restart. |
| Bot does not see the channel | The bot is not a member of the channel. | Run `/invite @Dinnerbell` in the channel. |
