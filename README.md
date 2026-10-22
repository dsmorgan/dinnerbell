# Dinnerbell

Dinnerbell helps a household answer, at a fixed time each day, three questions: who is eating, what are we eating, and who is cooking, picking up, or ordering it.

It asks the question in Slack at a scheduled time, collects who's in and what they can do, ranks meals that fit everyone eating (hard vetoes, soft preferences, variety, available roles), assigns the work, and records who did it so the household can celebrate contributions with badges.

It runs as one container on your own hardware. The household's catalog, preferences, history, and stats live in a mobile-friendly web app that Slack messages link into.

## Status

Pre-alpha. The requirements and architecture are written; the code is not yet. Start with the [spec](docs/spec.md) and the [decision records](docs/adr/).

## Why

Four adults, four schedules, one kitchen. The daily "what are we eating?" lands at the moment of hunger, takes too long, converges on the same cuisines, and lands on the same person. Dinnerbell moves the question earlier, makes the choice fit the people eating, and makes the decision stick.

## Planned stack

Python, FastAPI, HTMX, SQLite, Slack Bolt in Socket Mode, one container. See [ADR 0004](docs/adr/0004-stack.md).

## License

[AGPL-3.0-or-later](LICENSE). See [ADR 0003](docs/adr/0003-agpl-license.md) for why.
