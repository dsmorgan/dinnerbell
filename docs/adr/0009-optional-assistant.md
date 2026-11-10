# ADR 0009: An optional assistant drafts through the domain API, and a member confirms

| | |
|---|---|
| Status | Proposed |
| Date | 2026-11-10 |
| Builds on | [ADR 0005](0005-deterministic-ranking-first.md) (the assistant never ranks or filters), [ADR 0006](0006-test-instance.md) (flags and rehearsal) |
| Issue | [#17](https://github.com/dsmorgan/dinnerbell/issues/17), spec FR-51 |

## Context

FR-51 lets a member ask in plain language, "something we haven't had in a month that works for everyone in" or "tacos at 7, I'll cook", for proposals and offers that pass every filter. M6 adds drafting catalog items from a description or menu link.

A language model can't make guarantees, and a veto is a guarantee (ADR 0005). Calling one sends household data out of the house and costs money. A household that never turns it on must see Dinnerbell exactly as it is today.

## Decision

**Always optional.** The assistant works only when all three are set: the operator's `DINNERBELL_ANTHROPIC_API_KEY`, the `assistant` flag (ADR 0006), and **Assistant** in Admin, which is audited and shows only with the key and flag. Otherwise no entry point renders, and Dinnerbell ignores mentions and `/dinnerbell ask`. Nothing else depends on it. The flag hides a half-built assistant even where a key is set; it's rehearsed on the test instance and retires one release after it turns on in production.

**Anthropic, through its Python SDK.** The provider is Anthropic's Messages API with client-side tools, through the official `anthropic` package (Apache-2.0). The default model is `claude-sonnet-5-5`. `DINNERBELL_ASSISTANT_MODEL` can name another, but only one in Dinnerbell's price table, so the cap can always be computed.

**Conversations, in three places.** A member talks to the assistant on the web's **Assistant** page, in a thread in their DM with Dinnerbell, or in a channel thread everyone sees, by mentioning @Dinnerbell. The web page and DM threads are private to the member. In a channel thread, any member can join by mentioning @Dinnerbell, and each message acts as the member who wrote it. Dinnerbell reads only DMs to it and messages that mention it.

Each conversation's history is stored so follow-ups such as "something cheaper" work. The admin sets how long, in hours from 0 to 168 (default 24) after the last message; then Dinnerbell deletes it. At 0, nothing is stored and each message stands alone.

**It drafts, a member confirms.** The model calls only fixed tools, as the member whose message it's answering: find candidates (a new read-only use case over ranking's filters), look up catalog items, and draft a proposal, an offer of an existing item, or catalog items in the [catalog file format](../catalog-format.md). Catalog drafts only add, never update. No tool changes anything. Each draft says what **Confirm** will do, such as join late or replace tonight's scheduled call, and only its member can confirm it. **Confirm** calls the same service as the web form or Slack button, with the same checks, so a proposal passes every filter again (ADR 0005). Catalog drafts open the import preview. The assistant never starts a call.

**Any active member, never a guest**, because guests can't propose, vote, or offer. A guest's mention gets no reply.

**Minimal data, no names.** Dinnerbell sends the conversation, the call's basics, and what tools return. Members appear as "Member 1", "Member 2", and so on; Dinnerbell puts names back in replies. No preferences, vetoes, ratings, notes, emails, Slack IDs, or guest details leave the house, and candidates carry no reasons built from them, such as hints or "A favorite with tonight's group." Names inside members' messages or item names go out as written. The model can see what a call's filters removed, but never why or for whom.

**A monthly cost cap.** Each household has a cap in US dollars, $5 a month by default, set in Admin, for the calendar month in the household's time zone. Dinnerbell reserves each model call's worst case before making it, so spending never passes the cap. At the cap, the assistant is unavailable until the 1st, and everything else works.

**Fetched pages and catalog text are data, never instructions.** A menu link is fetched only over HTTPS from a public address, checked after DNS and on every redirect. The page goes to a drafting call with no tools. Injection can't widen what the assistant can do: tools act as the member, through services that check everything, and every change waits for that member's **Confirm**.

## Reasoning

- **Guarantees stay in code.** The model chooses among candidates Dinnerbell already filtered, and services check again on **Confirm**. A prompt can't add a vetoed place, because no tool can.
- **Confirm makes mistakes cheap.** A misread request costs a tap on **Cancel**. Imports already work this way.
- **Shared threads are safe** because each message carries only its author's permissions, and only a draft's member can confirm it.
- **No names, no preferences.** The model needs the menu, not the people, and FR-29 holds even in a channel thread: a reply can't hint at a housemate's stance.
- **A switch, not a permanent flag.** ADR 0006 flags are temporary; staying optional is a household choice, so it belongs in Admin.
- **Sonnet 5.5** handles multi-step tool use well at about $0.03 a request: roughly 150–200 a month fit the default cap.

## Consequences

- New runtime dependency: `anthropic` and its dependencies, including `httpx2` alongside the app's `httpx`. All are Apache-2.0, MIT, or BSD.
- The Slack app needs `im:history`, `app_mentions:read`, and the `message.im` and `app_mention` events. Operators update both manifests and reinstall.
- New settings: `DINNERBELL_ANTHROPIC_API_KEY` (a secret) and `DINNERBELL_ASSISTANT_MODEL`. The switch, cap, and retention are household settings; new tables hold usage and conversations.
- The fetcher is new code that faces the internet. M5's calendar polling can reuse it.
- The price table needs updating when Anthropic's prices or models change.
- Anthropic's API terms for the operator's account govern how long Anthropic keeps request data.
