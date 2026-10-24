# Changelog

Dinnerbell follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/). Before version 1.0, a minor release can change configuration or behavior; its **Upgrade notes** say what to do.

Every release lists **Upgrade notes**. Read them before you upgrade, and always back up first (see "Upgrade" in [docs/setup.md](docs/setup.md)).

## [Unreleased]

### Added

- A logo: Dinnerbone, the family's hamster. It shows in the web header, as the browser tab icon, and as the icon of a Home Screen bookmark on iPhone. To use it in Slack, upload `deploy/slack-app-icon-1024.png` in each Slack app's **Basic Information** settings (see [docs/setup.md](docs/setup.md#2-create-the-slack-app)). `scripts/render-logo.sh` regenerates the PNG icons from the SVGs.

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
