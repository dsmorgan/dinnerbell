# Changelog

Dinnerbell follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/). Before version 1.0, a minor release can change configuration or behavior; its **Upgrade notes** say what to do.

Every release lists **Upgrade notes**. Read them before you upgrade, and always back up first (see "Upgrade" in [docs/setup.md](docs/setup.md)).

## [Unreleased]

### Added

- A logo: Dinnerbone, the family's hamster. It shows in the web header, as the browser tab icon, and as the icon of a Home Screen bookmark on iPhone. To use it in Slack, upload `deploy/slack-app-icon-1024.png` in the Slack app's **Basic Information** settings (see [docs/setup.md](docs/setup.md#2-create-the-slack-app)). `scripts/render-logo.sh` regenerates the PNG icons from the SVGs.
- A test instance (`DINNERBELL_INSTANCE_LABEL` set) shows Dinnerbot, Dinnerbone as a half-built blueprint, in the header, the browser tab, and the Home Screen icon, so you can tell it from production at a glance. Upload `deploy/slack-app-icon-test-1024.png` as the test Slack app's icon.
- Export the catalog as a catalog file, in the format import reads ([docs/catalog-format.md](docs/catalog-format.md#export-the-catalog)). Any member can select **Download catalog** on the Catalog page, and `dinnerbell catalog export [--output FILE] [--force]` writes it from the command line, reading the database read-only. Exports are sorted, so two exports of the same catalog are identical, and importing one back changes nothing. They never include cooks, members, preferences, history, or anything else personal. To copy the catalog between production and test, see [docs/setup.md](docs/setup.md#copy-the-catalog-between-production-and-test).
- `dinnerbell config check` names every missing or invalid setting, never its value, and exits 2 if there are any. `--env-file FILE` reads an env file over the environment, in compose's syntax; `-` reads standard input.
- `deploy/dinnerbell-test` checks the env file before every command that can start a container, such as `up`, `run`, and `start`, with `dinnerbell config check` in a one-off container of the same image that has no network, volume, or published port. A missing setting, such as `DINNERBELL_ADMIN_EMAIL`, now stops it with the setting's name, instead of starting a container that restarts forever. `DINNERBELL_ENGINE` picks the container engine for the check.
- `deploy/dinnerbell-test` refuses to start test when `.env.test` and `.env` share `DINNERBELL_SECRET_KEY`, `DINNERBELL_SLACK_BOT_TOKEN`, `DINNERBELL_SLACK_APP_TOKEN`, or `DINNERBELL_OIDC_CLIENT_SECRET`, or when any line of `.env.test` contains production's value of one of them. It never prints the values.
- [docs/setup.md](docs/setup.md#test-instance) shows how both instances share one clone, how to write `.env.test` from `.env` without copying production's secrets, and what to do when the test port hangs.
- Calling a call off isn't admin-only anymore (FR-56). The coordinator can call off a call at any point, and whoever started an ad-hoc call can until it's decided. An optional reason of up to 140 characters goes with it.
  - The reply in the call's thread names who called it off and why, and mentions everyone who was in or maybe and the coordinator. The call message says "Called off by" with the reason, and a decision is struck through.
  - When a decided call is called off, each member assigned to cook, pick up, or order gets a direct message, and assignment or reminder messages that haven't gone out yet are dropped.
  - A call that was never posted, such as one scheduled for a later day, posts nothing. Its starter gets a direct message if someone else called it off.
- Responses close on their own once every active member has answered and nobody is a maybe. If everyone is out, the call ends on-your-own at the next scheduler tick. Otherwise it waits until no response has changed for 10 minutes, so you can add guests, an eat window, or an offer after tapping **In**; with two or more eating, someone must also be able to cook, pick up, or order. Voting then gets its usual length. Answers kept from before a re-run or restart don't count.
- Starting a call for a slot and day that was called off starts it again, for any member, as long as it isn't past the usual meal time plus two hours. It starts with no coordinator.
- On the web, a call's card on Today and on its own page shows **Call it off** to whoever may call it off, and the coordinator and admins get **Start the vote now** while responses are open and **Decide now** while voting. **Call it off** opens a small form with an optional reason and asks you to confirm. A called-off call reads "Called off by {name}" with the reason, and anyone can select **Start dinner again** (or lunch) until the usual meal time plus two hours. Every post is checked again on the server, and a refusal shows on the card in plain words. A card left open while someone called the call off and started it again can't act on the new run; it asks you to reload.
- Admins can skip one day's call, today or up to 7 days ahead, before it goes out, from **Skip a day** on Admin > Schedule. It offers only meals that have a daily call, and refuses a meal that's off in the schedule. Nothing is posted, the skip is audited, and anyone can still start that call on the day.
- Call it off and Close now in Slack (FR-56):
  - The call and decision messages have a ⋯ menu with **Call it off** and, while a window is open, **Start the vote now** or **Decide now**. Anyone who isn't allowed gets a private reply saying who can.
  - **Call it off** opens a modal with an optional reason. Close now asks you to confirm, and changes nothing if the call has moved on by then.
  - App Home shows **Call it off** and Close now only to people who can use them, and **Start dinner again** or **Start lunch again** after a call-off, until it's too late.
  - `/dinnerbell cancel [lunch|dinner] [day] [reason]` calls off a call. Without a slot, it calls off that day's only open call, and asks which one when lunch and dinner are both open.
  - A called-off decision message is struck through and says who called it off and why.

### Changed

- The event page's admin-only **Cancel this call** and **Close now** buttons are gone: **Call it off**, **Start the vote now**, and **Decide now** replace them for admins and members alike. For a called-off call, admins see **Start dinner again** instead of **Re-run**. The `POST /events/{id}/admin/cancel` route is removed; use `POST /events/{id}/cancel`.

### Fixed

- The Invite someone form on Admin > Members now defaults to Member. It used to preselect Admin, so inviting someone without changing the role made them an admin.
- A `DINNERBELL_DATABASE_URL` that isn't a URL, or has a malformed port, is reported as a configuration error, without quoting the URL, instead of stopping startup with a traceback.

### Security

- Slack button, menu, and modal values must match exactly what Dinnerbell wrote. A value with a trailing newline, such as `7\n`, is now refused like any other value it didn't write.
- Files served from `/static` carry a Content-Security-Policy that allows nothing (`default-src 'none'` with `sandbox`), so an SVG opened directly can't run script or load anything.

### Upgrade notes

- The database migrates to revision `0006_event_ended_by` when Dinnerbell starts. It adds two empty columns to `event`, so it's quick. Back up first, as always.
- Optional: the Slack app manifests list `cancel` in the slash command's usage hint. To show it in Slack, open the app's **Slash Commands** settings and edit the command's usage hint and description to match `deploy/slack-app-manifest.yaml` (or `deploy/slack-app-manifest.test.yaml` for test). `/dinnerbell cancel` works without this.

## [0.2.0] - 2026-10-23

### Added

- Prebuilt container images on GitHub Container Registry at `ghcr.io/dsmorgan/dinnerbell`. Each release publishes `X.Y.Z`, `X.Y`, and `latest`, and every push to `main` publishes `main` and the short commit SHA. Images carry build provenance and SBOM attestations; [docs/setup.md](docs/setup.md#verify-an-image) shows how to verify one.
- The footer and `/healthz` show the running version, such as `0.2.0`, or `0.2.0+abc1234` for a build from `main`.
- Test instance support ([ADR 0006](docs/adr/0006-test-instance.md)): run a second copy with its own database, Slack app, and hostname.
  - `DINNERBELL_INSTANCE_LABEL` shows a marker such as `TEST` on the web, in Slack posts, on App Home, and in `/healthz`. While it's set, Dinnerbell mentions and messages only members of the configured channel.
  - `DINNERBELL_SLACK_COMMAND` names the slash command. Default: `/dinnerbell`.
  - `DINNERBELL_FEATURES` turns on feature flags. Unknown names stop startup.
  - `dinnerbell scrub-test --keep EMAIL [--keep EMAIL ...] [--yes]` makes a restored production backup safe to use in test. It shows what it will change and asks you to confirm.
  - `deploy/dinnerbell-test` runs every test command with the test project, volume, env file, tag, and port, and refuses unsafe combinations. `deploy/slack-app-manifest.test.yaml` and `.env.test.example` set up the rest.
- `compose.yaml` reads `DINNERBELL_TAG`, `DINNERBELL_ENV_FILE`, and `DINNERBELL_PORT`, which only `deploy/dinnerbell-test` sets. Never set them in `.env` or `export` them: an exported `DINNERBELL_ENV_FILE=.env.test` makes production's own compose commands run with test's settings on production's database.

### Changed

- `compose.yaml` pulls the release image instead of building from source. To build from source, add `-f compose.build.yaml`.
- Production's clone follows release tags: to upgrade, check out the new `vX.Y.Z` tag instead of running `git pull` on `main`.

### Upgrade notes

You switch from building the image to pulling it. Follow "Switch a source-built install to the prebuilt image" in [docs/setup.md](docs/setup.md), which also has undo steps. In short:

1. Note the commit you run now, and back up the database.
2. Run `git fetch --tags && git checkout v0.2.0`, then `podman-compose pull`.
3. Recreate the service: `podman-compose down && podman-compose up -d`.

Run these as the same user and from the same directory as your install. Your data volume keeps its name, so the database carries over. If your install predates M2, its migrations run on start.

podman-compose 1.0.6 or later is required.

## [0.1.0] - 2026-10-23

The source-built version, never published as an image. It covers milestones M0 through M2.

### Added

- **M0:** the container, configuration, migrations, nightly backups, `/healthz`, and sign-in with Slack (or any OpenID Connect provider) for invited members.
- **M1:** the catalog with file import, preferences and vetoes, meal history, and the members admin page.
- **M2:** dinner calls in Slack: the scheduled call, reminders, ranked proposals and votes, the decision, assignments, the abandon path, admin overrides, App Home, and `/dinnerbell call`.

[Unreleased]: https://github.com/dsmorgan/dinnerbell/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/dsmorgan/dinnerbell/releases/tag/v0.2.0
[0.1.0]: https://github.com/dsmorgan/dinnerbell/tree/c179898
