# ADR 0009: An optional assistant drafts through the domain API, and a member confirms

| | |
|---|---|
| Status | Proposed |
| Date | 2026-11-09 |
| Builds on | [ADR 0005](0005-deterministic-ranking-first.md) (the assistant never ranks or filters), [ADR 0006](0006-test-instance.md) (flags and rehearsal) |
| Issue | [#17](https://github.com/dsmorgan/dinnerbell/issues/17), spec FR-51 |

## Context

FR-51 lets a member ask in plain language, "something we haven't had in a month that works for everyone in" or "tacos at 7, I'll cook", for proposals and offers that pass every filter. M6 adds drafting catalog items from a description or menu link.

A language model can't make guarantees, and a veto is a guarantee (ADR 0005). Calling one sends household data out of the house and costs money. And a household that never turns it on must see Dinnerbell exactly as it is today.

## Decision

**Optional for good.** The assistant exists only when the operator sets `DINNERBELL_ANTHROPIC_API_KEY` and an admin turns on **Assistant** in Admin, which is audited. Without both, no entry point renders, its routes return 404, and `/dinnerbell ask` replies as any unknown subcommand does. Nothing else depends on it. For rollout, the `assistant` flag (ADR 0006) gates it too: it ships behind the flag, is rehearsed on the test instance with its own key and cap, and the flag retires one release after it turns on in production.

**Anthropic, through its Python SDK.** The provider is Anthropic's Messages API with client-side tools, through the official `anthropic` package (Apache-2.0). The default model is `claude-sonnet-5-5`. `DINNERBELL_ASSISTANT_MODEL` can name another, but only a model in Dinnerbell's price table, so the cap can always be computed.

**It drafts, a member confirms.** The model can call only a fixed set of tools, each wrapping an existing use case as the member who asked:

- **Find candidates.** It runs ranking's hard filters for the open call, with optional narrowing such as "not eaten in 30 days", a cuisine, or a mode, and returns what passed with ranking's reasons.
- **Draft a proposal**, from those candidates only. **Draft an offer**, such as cooking tacos at 7.
- **Draft catalog items**, in the [catalog file format](../catalog-format.md).

No tool changes anything. A draft tool returns a draft that Dinnerbell shows the member exactly as it would post it, with **Confirm** and **Cancel**. **Confirm** calls the same service as the web form or Slack button, which applies every filter again, as ADR 0005 requires of any proposal. Catalog drafts go into the import preview, saved only when the member imports. With no open call, the assistant says so and can still draft an offer or catalog items; it never starts a call. Each request stands alone: no conversation is kept, and the request text isn't stored.

**Any active member, never a guest**, as guests never influence what's chosen.

**Minimal data, no names.** A request sends the member's text; the open call's meal, time, and roles offered; catalog items' names, kinds, tags, modes, and meals; and the filtered candidates with ranking's reasons, where members are "Member 1", "Member 2", and so on. Dinnerbell keeps the mapping and puts names back in the reply. No preferences, vetoes, ratings, emails, Slack IDs, or guest details leave the house; filters run locally, so the model never needs them.

**A monthly cost cap.** Each household has a cap in US dollars, $5 a month by default, set in Admin. Before each model call, Dinnerbell reserves the most it could cost (its input, plus `max_tokens` of output, at the table's prices) and refuses the call if that would pass the cap. Afterward it records the response's actual usage, cache reads and writes included, and releases the rest. A request makes at most a few calls and stops cleanly at the cap; the assistant then says it's out until the 1st, and everything else works. Usage is stored per household and month, as token counts only.

**Fetched pages and member text are data, never instructions.** A menu link is fetched only over HTTPS from public addresses, checked after DNS and on every redirect (no private, loopback, or link-local addresses), within 2 MB and 10 seconds, keeping only text. The page goes to a drafting call with no tools, which must answer in the catalog file format. The system prompt marks the page, the member's text, and catalog notes as untrusted. Injection can't widen what the assistant can do, because:

1. Its tools act as the member who asked, with that member's permissions, through services that check everything.
2. Every change waits for that member's **Confirm**.
3. Catalog drafts pass import validation and the preview.

## Reasoning

- **Guarantees stay in code.** The model chooses among candidates Dinnerbell already filtered, and services check again on **Confirm**. A prompt can't add a vetoed place, because no tool can.
- **Confirm makes mistakes cheap.** A misread request costs a tap on **Cancel**, not a proposal taken back in front of the household, as imports already work.
- **No names, no preferences.** The model needs the menu, not the people, and pseudonyms keep replies useful ("Member 2 can cook it").
- **A switch, not a permanent flag.** ADR 0006 flags are temporary. Optional for good is a household choice, so it belongs in Admin, behind a key only the operator provides.
- **Sonnet 5.5** handles multi-step tool use well at about $0.03 a request: roughly 150–200 a month fit the default cap.
- **The SDK** handles retries, typed errors, and tool-use shapes, which hand-written HTTP would re-implement.

## Consequences

- New runtime dependency: `anthropic`, which brings `httpx2` (alongside the app's `httpx`), `jiter`, `docstring-parser`, and `opentelemetry-api`. All are Apache-2.0, MIT, or BSD.
- New settings: `DINNERBELL_ANTHROPIC_API_KEY` (a secret), `DINNERBELL_ASSISTANT_MODEL`, and Admin's **Assistant** switch and monthly cap. New tables hold usage and the switch.
- The fetcher is new code that faces the internet, with its own tests for addresses, redirects, size, and time. M5's calendar polling can reuse it.
- The price table needs updating when Anthropic's prices or models change.
- A security lane tests injection through menus, notes, and request text, filter bypass, and the cap.
- Anthropic's API terms for the operator's account govern how long Anthropic keeps request data.
