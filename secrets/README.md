# Secret files

Production reads one file per secret from this directory (the test instance reads `secrets-test/`).
Compose mounts each at `/run/secrets/<name>` in the container, so no secret appears on a podman or
docker command line.

| File | What it holds |
|---|---|
| `DINNERBELL_SECRET_KEY` | Signing key, at least 32 random characters. Dinnerbell refuses the placeholder text and repetitive values |
| `DINNERBELL_OIDC_CLIENT_SECRET` | Sign-in client secret |
| `DINNERBELL_SLACK_BOT_TOKEN` | Slack bot token, `xoxb-...` |
| `DINNERBELL_SLACK_APP_TOKEN` | Slack app-level token, `xapp-...` |

Each `*.example` file here is a placeholder. Create the real file next to it without the
`.example` suffix, with one line and no trailing newline. Git ignores everything in this directory
except these placeholders and this note. Never commit a real secret. The commands are in
"Keep secrets in files" in `docs/setup.md`.
