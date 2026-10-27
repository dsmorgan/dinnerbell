# Architecture decision records

Each ADR records one decision and the reasoning behind it, in under about 1,000 words. Design detail belongs in the [spec](../spec.md), not in ADRs.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-slack-first-channel.md) | Slack is the first channel, behind a channel adapter | Accepted |
| [0002](0002-oidc-sign-in.md) | Generic OpenID Connect sign-in with Slack as the configured provider | Accepted |
| [0003](0003-agpl-license.md) | AGPL-3.0 license | Accepted |
| [0004](0004-stack.md) | Python, FastAPI, HTMX, SQLite, one container | Accepted |
| [0005](0005-deterministic-ranking-first.md) | Deterministic, explainable ranking before any LLM | Accepted, amended by 0007 |
| [0006](0006-test-instance.md) | Test in a second instance of the same image, not a second channel | Accepted |
| [0007](0007-seeded-weighted-ranking.md) | Seeded, weighted random ranking among places that pass every filter | Accepted |
