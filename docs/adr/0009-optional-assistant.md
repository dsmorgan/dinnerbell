# ADR 0009: An optional assistant drafts through the domain API, and a member confirms

| | |
|---|---|
| Status | Accepted |
| Date | 2026-11-10 |
| Builds on | [ADR 0005](0005-deterministic-ranking-first.md) (the assistant never ranks or filters), [ADR 0006](0006-test-instance.md) (flags and rehearsal) |
| Issue | [#17](https://github.com/dsmorgan/dinnerbell/issues/17), spec FR-51 |

## Context

FR-51 lets a member ask in plain language, "something we haven't had in a month that works for everyone in" or "tacos at 7, I'll cook", for proposals and offers that pass every filter, and M6 adds catalog drafts from a description or menu link.

A language model can't make guarantees, and a veto is a guarantee (ADR 0005). Calling one sends household data out and costs money, and a household that never turns it on must see no change.

## Decision

**Always optional.** The assistant works only when all three are set: the operator's `DINNERBELL_ANTHROPIC_API_KEY`, the `assistant` flag (ADR 0006), and **Assistant** in Admin, which is audited and shows only with the key and flag. Otherwise no entry point renders, and Dinnerbell ignores DMs and mentions. Nothing else depends on it, and the default Slack app is unchanged. The flag hides a half-built assistant even with a key set, and retires one release after it turns on in production.

**Anthropic, through its Python SDK.** Anthropic's Messages API with client-side tools, through the official `anthropic` package (Apache-2.0). The default model is `claude-sonnet-5-5`; `DINNERBELL_ASSISTANT_MODEL` can name another in Dinnerbell's price table.

**Conversations, in three places.** A member talks to the assistant on the web's **Assistant** page, in their DM with Dinnerbell, or, by mentioning @Dinnerbell in the household channel, in a thread everyone sees. The web page and DMs are private. In a channel thread, each message acts as its author; others' earlier messages are only quoted context. Dinnerbell reads only DMs to it and mentions.

History is kept so follow-ups such as "something cheaper" work, for the admin's choice of 0 to 168 hours after the last message (default 24). At 0, each message stands alone.

**What the member can do, never what only an admin can.** Each tool wraps the service behind a form or button, acting as the member it's answering, without admin rights. Over time, tools cover a member's actions: answering, voting, suggesting, proposing, offering, inviting guests, taking and assigning roles, coordinating, logging and rating meals, and editing the catalog and their own preferences. Admin-only functions are never tools: members and guest records, others' settings, household settings such as ranking and Feedback, badges and contribution counts, tag merges and renames, and admin overrides on calls. Coordinating, a slot's standing schedule, and the member's own preferences, ratings, feedback, and away days work only in private conversations.

The first build has #17's tools: find candidates (a new read-only use case over ranking's filters, voting calls only), look up catalog items, and drafts of a proposal, an offer of an existing item, or new catalog items in the [catalog file format](../catalog-format.md). Each later tool is a follow-up issue under this ADR.

**Every change is a draft the member confirms.** A draft says what **Confirm** will do, such as join late or replace tonight's scheduled call, and only its member sees and confirms it. **Confirm** calls the same service as the form, with the same checks, so a proposal passes every filter again (ADR 0005). Catalog drafts only add, through the import preview.

**Active members only.** Guests can't propose, vote, or offer. A message from anyone else gets no reply and is neither stored nor sent.

**Minimal data, no names.** Dinnerbell sends the conversation, the call's basics, and what tools return. Members appear as "Member 1", "Member 2", and so on, fixed for each conversation; Dinnerbell puts names back only for display. No preferences, vetoes, ratings, notes, emails, Slack IDs, or stored guest details leave the house, except the member's own, in a private conversation, when they ask. Candidates come in name order, without hints, "favorite" sentences, or what the filters removed.

Amended 2026-11-11 (#264, owner's decision): the meal-history tool may also send each item's household hint (ADR 0008), the same text every member sees. No per-member ratings are sent.

**A monthly cost cap.** Each household has a cap in US dollars, $5 a month by default, set in Admin. Dinnerbell reserves each model call's worst case first, so spending never passes it; Admin shows usage per member. At the cap, the assistant is unavailable until next month, and everything else works.

**Fetched pages and catalog text are data, never instructions.** A menu link is fetched only over HTTPS from a public address. A drafting call with no tools reads the page. Injection can't widen what the assistant can do: tools act as the member, and every change waits for their **Confirm**.

## Reasoning

- **Guarantees stay in code.** Services check everything on **Confirm**, so a prompt can't add a vetoed place.
- **Confirm makes mistakes cheap:** a misread request costs a tap on **Cancel**.
- **Member parity, minus admin.** One rule, the member's own permissions without admin, covers every future tool, so adding one needs no new ADR. Admin changes affect the whole household, so they stay on Admin's pages.
- **Shared threads can't widen anyone's permissions:** each message carries only its author's, and only a draft's member can confirm it. Coordinating and personal data stay private, so a housemate can't steer a coordinator's request and nobody's stance appears in public.
- **No names, no preferences** but the member's own, privately. The model needs the menu, not the people. Candidates only while voting, when the filters are fixed, keep FR-29: asking before and after a housemate answers reveals nothing.
- **A switch, not a permanent flag:** ADR 0006 flags are temporary, and staying optional is a household choice.
- **Sonnet 5.5** handles multi-step tool use well, at about $0.03 for a fresh request.

## Consequences

- New runtime dependency: `anthropic` and its dependencies, including `httpx2` alongside the app's `httpx`. All are Apache-2.0, MIT, or BSD.
- Assistant variants of the Slack manifests add a writable Messages tab, DM history, and mentions. Operators who turn the assistant on apply them and reinstall.
- New settings: `DINNERBELL_ANTHROPIC_API_KEY` (a secret), `DINNERBELL_ASSISTANT_MODEL`, and household settings for the switch, cap, and retention.
- The fetcher faces the internet; M5's calendar polling can reuse it.
- The price table follows Anthropic's prices.
- The operator's Anthropic API terms govern how long Anthropic keeps requests.
