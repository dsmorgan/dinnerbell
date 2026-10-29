# Secret files (optional)

By default the four secrets are lines in `.env`. To keep them off podman's command line, you can
put each in a file here instead and start with `compose.secrets.yaml`:

```sh
podman-compose -f compose.yaml -f compose.secrets.yaml up -d
```

Compose then mounts each file at `/run/secrets/<name>` in the container. The test instance reads
`secrets-test/`, and `deploy/dinnerbell-test` uses it on its own when the files are there.

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
