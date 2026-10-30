# Troubleshoot Dinnerbell

Find your symptom in the table. To see what Dinnerbell reports about itself, [check /healthz](#check-healthz) and [read the logs](#read-the-logs). For the test instance, see [Troubleshoot the test instance](test-instance.md#troubleshoot-the-test-instance).

The commands use podman. With Docker, replace `podman-compose` with `docker compose` and `podman` with `docker`.

## Common problems

| Symptom | Cause | Fix |
|---|---|---|
| Slack says "redirect_uri did not match" after **Sign in** | The app's redirect URL isn't exactly `https://<host>/auth/callback`. | Fix it under **OAuth & Permissions** > **Redirect URLs**, and check `DINNERBELL_BASE_URL`. |
| Sign-in returns to a Pangolin login page or loops | Pangolin authentication is on. | Turn off **Use Platform SSO**, and leave password, PIN, email OTP, and header auth off. |
| Every form fails with "Cross-origin request refused", or sign-in loops to the start page | `DINNERBELL_BASE_URL` differs from the URL in the browser: another host, a LAN IP, an explicit `:443`, or `http://`. | Set it to exactly the URL people type, and recreate the service. Open Dinnerbell only at that URL. |
| Sign-in works, but Dinnerbell says you aren't invited | The Slack email differs from the invited one, or isn't verified. | Compare the emails on **Members** and in Slack, and fix the member's email or `DINNERBELL_ADMIN_EMAIL`. |
| Sign-in refused for someone you invited | They signed in to another Slack workspace. | Ask them to sign in to your household workspace. |
| Everyone sees "That email is already linked" | You switched sign-in providers. | See [Switch providers on an existing install](setup.md#switch-providers-on-an-existing-install). |
| A member taps a button and Slack says "I don't know your Slack account yet" | Their Slack account isn't linked, often because the emails differ. | Ask them to sign in to the web app once, or link them on **Members**. |
| Startup error: `DINNERBELL_SECRET_KEY: is still a placeholder` | The key holds placeholder text, such as from `secrets/DINNERBELL_SECRET_KEY.example`, or is repetitive. | [Generate a key](setup.md#9-run-dinnerbell) and set it. `deploy/check-secrets` reports this for secret files too. |
| Startup error: `DINNERBELL_SLACK_TEAM_ID is required` | The issuer is Slack, and the team ID is empty. | Set it as in [section 6](setup.md#6-find-the-team-id-and-channel-id), and recreate the service. |
| Startup error about a token prefix | The tokens are swapped, or copied from the wrong page. | `DINNERBELL_SLACK_APP_TOKEN` starts with `xapp-` (**App-Level Tokens**); `DINNERBELL_SLACK_BOT_TOKEN` starts with `xoxb-` (**Install App**). |
| Startup error: `Can't read the secret file`, or `is a directory` | A secret file has the wrong owner or mode, or didn't exist at mount. | See the table in [Keep secrets in files](setup.md#keep-secrets-in-files). |
| The log warns `ignoring environment variables that match no setting` | A `DINNERBELL_` name in `.env` is misspelled, such as `DINNERBELL_TRUSTED_PROXY`, so that setting keeps its default. The warning names the variable, never its value. | Fix the name (see [configuration.md](configuration.md)), and recreate the service. |
| `migrate` or `serve` says `The database is locked` | Another process, such as `migrate` or `reset-history` during a start, held the write lock for about 15 seconds. Nothing changed. | Wait for it to finish, and run the command again. |
| Production starts with an empty database | Compose created a new volume, because you ran it from another directory, with another project name, or as another user. | Run `podman-compose down`, and start it again from the original directory as the original user. Your data is still in the old volume. |
| `/healthz` reports `"slack": "not_configured"` | `DINNERBELL_SLACK_BOT_TOKEN`, `DINNERBELL_SLACK_APP_TOKEN`, or `DINNERBELL_SLACK_CHANNEL` is missing. The log says which. | Set all three, and recreate the service: `podman-compose down && podman-compose up -d`. |
| `/healthz` reports `"slack": "disconnected"` for more than a minute | The host can't reach Slack. | Allow outbound HTTPS and WebSocket connections to `slack.com` and `wss-primary.slack.com`. |
| The log says `Slack refused DINNERBELL_SLACK_BOT_TOKEN` | The bot token is wrong, revoked, or from another app. Dinnerbell holds every notification until it's fixed. | Copy the **Bot User OAuth Token** from **Install App** again, and recreate the service. |
| The log says `Slack refused DINNERBELL_SLACK_APP_TOKEN` | The app-level token is wrong or lacks `connections:write`, or Socket Mode is off. | Create the token again ([section 3](setup.md#3-create-the-app-level-token)), check that **Socket Mode** is on, and recreate the service. |
| The log says `The bot token belongs to Slack workspace T…, but DINNERBELL_SLACK_TEAM_ID is T…` | The app is installed in another workspace. | Install it in your household workspace, or correct the team ID. |
| The log says `Slack can't find the channel` | `DINNERBELL_SLACK_CHANNEL` isn't a channel ID, or the channel is private and the app lacks `groups:read`. | Copy the ID that starts with `C` ([section 6](setup.md#6-find-the-team-id-and-channel-id)), and use a public channel. |
| The log says `The bot isn't a member of the channel` | The bot isn't invited. | Send `/invite @Dinnerbell` in the channel. Dinnerbell notices within a minute. |
| The log says `The channel … is archived` | The channel is archived. | Unarchive it, or set `DINNERBELL_SLACK_CHANNEL` to another channel. |
| `/healthz` shows `outbox.failed` above 0 | Slack refused some notifications for good, such as a direct message to someone with no Slack link. | Find `notification delivery gave up` in the log, and fix the link or the channel. Later notifications still go out. |
| `/healthz` shows `stuck_calls` above 0 and `"status": "degraded"` | A call's next step, such as closing its answers, has failed on every tick for more than an hour, so the call can't move on. A restart doesn't help, so `/healthz` keeps HTTP 200. | Open **Members**: a banner on every admin tab lists each stuck call. Open the call and call it off; you can also re-run a voting or decided call from its **Admin** menu. Report the `could not advance call` log line, which names the call and the failing step, on the Dinnerbell issue tracker. |
| Badges show as text tags, and the log says `badge art not available` with `missing_scope` | The Slack app lacks the `files:write` scope. Dinnerbell retries every 15 minutes. | Paste the current manifest under **App Manifest** and reinstall the app (see [Upgrade](setup.md#upgrade)). |
| Badges show as text tags, and the log says `slack refused badge art` | Slack refused the art. The line has Slack's `slack_error` and `slack_messages`. | Report the line on the Dinnerbell issue tracker (#110). The text badges were already posted. |

## Check /healthz

`https://<your hostname>/healthz` reports Dinnerbell's state as JSON:

| Key | Meaning |
|---|---|
| `status` | `ok`; `degraded` when something below needs attention; or `error`, with HTTP 503, when the database is unreachable or the scheduler hasn't ticked for 5 minutes. Everything else returns HTTP 200, so the container keeps running and you can sign in to fix it. |
| `version` | The running version, also shown in the page footer. |
| `database` | `ok` or `unreachable`. |
| `scheduler` | Its `status` and `last_tick`. A tick older than 90 seconds reports `degraded`. |
| `slack` | `connected`: the startup checks passed and Socket Mode is up. `disconnected`: Dinnerbell can't reach Slack and is retrying. `error`: a startup check failed; the log says which and how to fix it. `not_configured`: a Slack setting is missing, and the bot posts nothing. `disconnected` and `error` report `degraded`. |
| `outbox` | `backlog`: notifications waiting to be sent. `failed`: notifications that gave up. `oldest_pending_age`: seconds the oldest one has waited, or `null`. Over an hour reports `degraded`; a notification that waits 24 hours is never sent. |
| `backup` | `last_success`, and `failing`, which reports `degraded`. |
| `stuck_calls` | Calls whose next step has been due for more than an hour. Above 0, or `null` when the check failed or is out of date, reports `degraded`. |
| `instance`, `slack_dms_dropped` | On a test instance only: its label, and the direct messages withheld from people outside the test channel. |

Notifications wait in the outbox while Slack is down. To be alerted on `degraded`, see [Monitor it](hardening.md#monitor-it).

## Read the logs

Dinnerbell writes one JSON line per log entry to standard output. Every action on a call or offer, and every refusal, writes one `info` line, so you can follow what happened to a call and why something was refused. Logs never contain emails, tokens, sign-in subjects, or calendar URLs. Notes and call-off reasons appear only as their length, `note_length`.

| Field | What it holds |
|---|---|
| `msg` | What happened: an action such as `answer`, `offer_role`, `propose`, `vote`, `lock`, `assign`, `call_off`, `offer_posted`, `join`, `join_late`, or `late_switch`; a scheduler step such as `opened`, `voting`, `decided`, `abandoned`, or `stale_closed`; or `refused`. Deliveries to Slack are `notification delivered`, `notification delivery failed` (it retries), `notification delivery gave up`, and `notification closed` (superseded, such as by a call-off). |
| `event_id`, `run`, `slot`, `day` | The call or offer. `run` goes up each time a call starts again. |
| `state_before`, `state_after` | The call's state before and after. A refusal has `state` instead. |
| `member_id`, `member` | Who acted. Scheduler lines have neither. |
| `item_id`, `item`, `mode` | The place, such as the pick proposed, voted for, or decided. |
| `channel` | Where the action came from: `slack`, `web`, `cli`, or `scheduler`. |
| `request_id` | One tap, request, or scheduler run. The web returns it in the `X-Request-ID` header. |
| `reason`, `refusal` | On a refusal: a short code, such as `not_in`, `responses_closed`, `cook_list`, or `stale_run`, and the message the member saw. |
| `kind`, `channel_id`, `message_ts` | On a delivery: the notification, and where Slack posted it. |
| `end_reason` | Why a call ended on its own: `too_few`, `no_coverage`, or `missed`. |

When a call's next step keeps failing, the `could not advance call` line logs once at once, then at most once an hour, with the traceback, `event_id`, the failing `step`, and `failures` since the previous line.

To follow call 12, filter on its ID. With `jq`, show the story; `grep -o` drops anything compose puts before each line's JSON. To see everything one tap did, filter on its `request_id` instead.

```sh
podman-compose logs dinnerbell 2>&1 | grep '"event_id": 12,'
podman-compose logs dinnerbell 2>&1 | grep -o '{.*}' \
  | jq -c 'select(.event_id == 12) | {ts, msg, member, item, state_after, reason, refusal}'
```

