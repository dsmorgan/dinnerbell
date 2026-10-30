# Troubleshoot Dinnerbell

Find your symptom in the table. A startup error names the setting and how to fix it; [setup.md](setup.md#9-run-dinnerbell) has the rules for each value, and [Keep secrets in files](setup.md#keep-secrets-in-files) covers secret file errors. For the test instance, see [Troubleshoot the test instance](test-instance.md#troubleshoot-the-test-instance).

After you fix a setting, recreate the service: `podman-compose down && podman-compose up -d`. With Docker, use `docker compose`.

## Common problems

| Symptom | Cause | Fix |
|---|---|---|
| Slack says "redirect_uri did not match" after **Sign in** | The app's redirect URL isn't exactly `https://<host>/auth/callback`. | Fix it under **OAuth & Permissions** > **Redirect URLs**, and check `DINNERBELL_BASE_URL`. |
| Sign-in returns to a Pangolin login page or loops | Pangolin authentication is on. | Turn off **Use Platform SSO**, and leave password, PIN, email OTP, and header auth off. |
| Every form fails with "Cross-origin request refused", or sign-in loops to the start page | `DINNERBELL_BASE_URL` differs from the URL in the browser: another host, a LAN IP, an explicit `:443`, or `http://`. | Set it to exactly the URL people type. Open Dinnerbell only at that URL. |
| Sign-in works, but Dinnerbell says you aren't invited | The Slack email differs from the invited one, or isn't verified. | Compare the emails on **Members** and in Slack, and fix the member's email or `DINNERBELL_ADMIN_EMAIL`. |
| Sign-in refused for someone you invited | They signed in to another Slack workspace. | Ask them to sign in to your household workspace. |
| A member taps a button and Slack says "I don't know your Slack account yet" | Their Slack account isn't linked, often because the emails differ. | Ask them to sign in to the web app once, or link them on **Members**. |
| Startup error about a token prefix | The tokens are swapped. | The app token starts with `xapp-`, and the bot token with `xoxb-`. |
| The log warns `ignoring environment variables that match no setting` | A `DINNERBELL_` name in `.env` is misspelled, so that setting keeps its default. | Fix the name (see [configuration.md](configuration.md)). |
| `migrate` or `serve` says `The database is locked` | Another process, such as `reset-history`, held the write lock. Nothing changed. | Wait for it to finish, and try again. |
| Production starts with an empty database | You ran compose from another directory, with another project name, or as another user, so it created a new volume. | Run `podman-compose down`, and start it again as before. Your data is still in the old volume. |
| `/healthz` reports `"slack": "not_configured"` | `DINNERBELL_SLACK_BOT_TOKEN`, `DINNERBELL_SLACK_APP_TOKEN` (in `.env` or its secret file), or `DINNERBELL_SLACK_CHANNEL` is missing. The log says which. | Set all three. |
| `/healthz` reports `"slack": "disconnected"` for more than a minute | The host can't reach Slack. | Allow outbound HTTPS and WebSocket connections to `slack.com` and `wss-primary.slack.com`. |
| The log says `Slack refused DINNERBELL_SLACK_BOT_TOKEN` | The bot token is wrong, revoked, or from another app. Notifications wait until it's fixed. | Copy the **Bot User OAuth Token** from **Install App** again. |
| The log says `Slack refused DINNERBELL_SLACK_APP_TOKEN` | The app-level token is wrong or lacks `connections:write`, or Socket Mode is off. | Create the token again ([section 3](setup.md#3-create-the-app-level-token)), and check that **Socket Mode** is on. |
| The log says `The bot token belongs to Slack workspace T…, but DINNERBELL_SLACK_TEAM_ID is T…` | The app is installed in another workspace. | Install it in your household workspace, or correct the team ID. |
| The log says `Slack can't find the channel` | `DINNERBELL_SLACK_CHANNEL` isn't a channel ID, or the channel is private and the app lacks `groups:read`. | Copy the ID that starts with `C` ([section 6](setup.md#6-find-the-team-id-and-channel-id)), and use a public channel. |
| The log says `The bot isn't a member of the channel` | The bot isn't invited. | Send `/invite @Dinnerbell` in the channel. Dinnerbell notices within a minute. |
| The log says `The channel … is archived` | The channel is archived. | Unarchive it, or set `DINNERBELL_SLACK_CHANNEL` to another channel. |
| `/healthz` shows `outbox.failed` above 0 | Slack refused some notifications for good, such as a direct message to someone with no Slack link. | Find `notification delivery gave up` in the log, and fix the link or the channel. |
| `/healthz` shows `stuck_calls` above 0 | A call's next step, such as closing its answers, has failed for more than an hour. A restart doesn't help. | On **Members**, a banner lists each stuck call. Call it off, or re-run a voting or decided call from its **Admin** menu. Report the `could not advance call` log line on the issue tracker. |
| Badges show as text, and the log says `badge art not available` with `missing_scope` | The Slack app lacks `files:write`. | Paste the current manifest and reinstall the app (see [Upgrade](setup.md#upgrade)). |
| Badges show as text, and the log says `slack refused badge art` | Slack refused the art. | Report the line, with its `slack_error`, on the issue tracker (#110). |

## Check /healthz

`https://<your hostname>/healthz` reports Dinnerbell's state as JSON:

| Key | Meaning |
|---|---|
| `status` | `ok`; `degraded` when something below needs attention, with HTTP 200, so you can still sign in to fix it; or `error`, with HTTP 503, when the database is unreachable or the scheduler hasn't ticked for 5 minutes. |
| `version` | The running version, also shown in the page footer. |
| `database` | `ok` or `unreachable`. |
| `scheduler` | Its `status` and `last_tick`. A tick older than 90 seconds reports `degraded`. |
| `slack` | `connected`. `disconnected`: Dinnerbell can't reach Slack and is retrying. `error`: a startup check failed, and the log says how to fix it. `not_configured`: a Slack setting is missing, and the bot posts nothing. `disconnected` and `error` report `degraded`. |
| `outbox` | `backlog`: notifications waiting, such as while Slack is down. `failed`: notifications that gave up. `oldest_pending_age`: seconds the oldest has waited; over an hour reports `degraded`, and after 24 hours it's never sent. |
| `backup` | `last_success`, and `failing`, which reports `degraded`. |
| `stuck_calls` | Calls whose next step has been due for more than an hour. Above 0, or `null` when the check failed or is out of date, reports `degraded`. |
| `instance`, `slack_dms_dropped` | On a test instance only: its label, and the direct messages withheld from people outside the test channel. |

To be alerted on `degraded`, see [Monitor it](hardening.md#monitor-it).

## Read the logs

Dinnerbell writes one JSON line per log entry to standard output, including one `info` line for every action on a call and every refusal. Logs never contain emails, tokens, sign-in subjects, calendar URLs, or what members write.

| Field | What it holds |
|---|---|
| `msg` | What happened: an action such as `answer`, `propose`, `vote`, or `call_off`; a scheduler step such as `opened`, `voting`, or `decided`; `refused`; or a delivery, such as `notification delivered` or `notification delivery gave up`. |
| `event_id`, `run`, `slot`, `day` | The call or offer. `run` goes up each time a call starts again. |
| `state_before`, `state_after` | The call's state before and after. A refusal has `state` instead. |
| `member_id`, `member` | Who acted. Scheduler lines have neither. |
| `item_id`, `item`, `mode` | The place, such as the pick proposed, voted for, or decided. |
| `channel` | Where the action came from: `slack`, `web`, `cli`, or `scheduler`. |
| `request_id` | One tap, request, or scheduler run. The web returns it in the `X-Request-ID` header. |
| `reason`, `refusal` | On a refusal: a short code, such as `not_in`, `responses_closed`, `cook_list`, or `stale_run`, and the message the member saw. |
| `kind`, `channel_id`, `message_ts` | On a delivery: the notification, and where Slack posted it. |
| `end_reason` | Why a call ended on its own: `too_few`, `no_coverage`, or `missed`. |

To follow call 12, filter on its ID; with `jq`, show the story. To see everything one tap did, filter on its `request_id` instead.

```sh
podman-compose logs dinnerbell 2>&1 | grep '"event_id": 12,'
podman-compose logs dinnerbell 2>&1 | grep -o '{.*}' \
  | jq -c 'select(.event_id == 12) | {ts, msg, member, item, state_after, reason, refusal}'
```

