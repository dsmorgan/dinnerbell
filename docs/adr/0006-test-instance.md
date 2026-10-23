# ADR 0006: Test in a second instance, not a second channel

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-23 |

## Context

To test today, the owner points `DINNERBELL_SLACK_CHANNEL` at `#dinnerbell-test`, recreates the production container, then switches back. While it's switched, production's scheduled calls post to the test channel, and every test call writes to the real meal log. Testing needs to be part of the product.

Two designs were compared:

- **One instance, two stages.** Each event and outbox row carries a `stage`, either `live` or `rehearsal`, set by the channel the call started in. Rehearsals skip meal-log writes, notify only members of the test channel, and turn features on with per-stage flags in Admin.
- **Two instances.** A second container from the same image, with its own database, Slack app, secret key and hostname. It runs unreleased builds while production runs a release.

The owner leaned toward one instance because it looked simpler to run.

## Decision

Run test as a **second instance of the same image**.

- Production pins a release tag, such as `ghcr.io/dsmorgan/dinnerbell:0.5.0`. Test runs `:main`, or a release before you promote it. To promote, you check out the new release, pull and restart.
- Test has its own Slack app ("Dinnerbell Test", command `/dinnerbell-test`), volume, `DINNERBELL_SECRET_KEY`, OIDC client and hostname. The hostname sits behind Pangolin authentication. The container is stopped when you aren't using it.
- `DINNERBELL_SLACK_COMMAND` names the slash command. `DINNERBELL_INSTANCE_LABEL` puts a visible `TEST` marker on the web header, channel posts, App Home and `/healthz`.
- Feature flags come from `DINNERBELL_FEATURES` and are checked against a registry in the code. An unknown name stops startup. Use a flag only when a feature must ship to production turned off.
- A `scrub-test` command makes a restored production backup safe to use in test. It refuses to run unless an instance label is set.
- While an instance label is set, the Slack adapter mentions and DMs only members of the configured channel.
- No schema change.

## Why

- **Isolation by construction.** Test data lives in a different file, so nothing in test can reach the real meal log, variety, History, Stats or badges. The one-instance design gets the same result only if every current and future write path checks `stage`. That's a tax on M3 and M4, and when it fails, the result is the one the owner most wants to avoid: a fake dinner in real stats or a family member pinged for nothing.
- **Unreleased code stays out of production.** The production process faces the internet and never runs untested code. In the one-instance design, a bug on the rehearsal path takes down production, and flag toggles add a new admin write path to protect.
- **It tests what actually breaks a self-hosted app.** Migrations, dependency bumps and base-image updates are the riskiest changes. Dependabot will now propose them every week. Only a second instance runs them against a copy of real data before the family sees them. One instance can check an upgrade only after it's live.
- **Least risk to fresh M2 code.** Four small configuration changes compare with a migration that widens the call-uniqueness constraint and edits the outbox, interactions, lifecycle and health code. M2 merged days ago and hasn't yet carried a week of real dinners.
- **It fits other self-hosters.** A second compose project with its own env file is a pattern operators already know.

**Ideas taken from the one-instance design:** a code registry for flags, with on and off tests for each flag; limiting notifications to channel members; keeping channel IDs out of `/healthz`; and rehearsing a full call on the release image as the promotion check.

**What this costs:** a one-time setup (Slack app, Pangolin resource, env file) of about half an hour with the docs. It also adds a second set of secrets on the host, and test data drifts from production until you refresh it. The one-instance design avoids these, but swaps a one-time cost for a permanent one.

## Consequences

- Production's `DINNERBELL_SLACK_CHANNEL` points at the real channel and never changes again.
- The prebuilt-image work comes first, because test runs `:main` from GHCR.
- The two instances never share a Slack app token. Socket Mode delivers each event to one open connection, so a shared token would split button taps between them.
- Rolling back means restoring the backup taken before the upgrade, because migrations only move forward. The promotion checklist says so.
- A restored backup in test is production data. It stays on the host with the same permissions, and you scrub it before you start test.
- Flags are temporary. Each flag is removed one release after it turns on in production.
- If Dinnerbell ever serves several households or becomes a hosted service, revisit this decision.
