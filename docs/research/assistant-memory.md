# Research: a long-term memory for the assistant

Issue [#367](https://github.com/dsmorgan/dinnerbell/issues/367). Research only: nothing in Dinnerbell changes until you pick a direction, and an ADR follows that choice. Prices and API facts were checked against Anthropic's documentation on 2026-11-24. The prototype ran on synthetic conversations and spent $0.19.

## Recommendations

1. **Build a rollup first (option 1).** When **Conversation hours** delete a conversation, one model call with no tools reads it and keeps a few short, attributed notes. Each private request carries the member's own notes as a third system block after the roster, and every request carries the household's. In the prototype, the rollup caught every point it should have, as a note or as a setting to draft, attributed each to the right member, kept nothing from small talk, and kept no planted instruction, in all 27 runs on Claude Sonnet 5.5. It costs about $0.25 a month for a typical household and under $0.90 in a heavy month, against the $5 cap.
2. **Keep ADR 0009's privacy rule for the first build.** A member's notes inform only that member's private conversations. Nothing said privately ever reaches anyone else. Household notes, such as "Friday is pizza night", come only from channel threads, where everyone already saw them, and inform every conversation. Sharing a member's channel-origin notes with housemates can come later, as a separate decision (see [Privacy options](#privacy-options)).
3. **Treat settings as settings (option 4), in two steps.** The rollup already tells a note from a setting: in the prototype it labeled "I don't do spicy" a level, a pregnancy a dietary need, and a confirmed +2 as already saved. The first build keeps no free-text copy of a setting, and the assistant offers a draft in the conversation, through the tools it has now. A later issue adds suggestions that wait on Preferences for the member to confirm.
4. **Don't build semantic search, the Anthropic memory tool, or a memory library now.** Eight notes measured 453 tokens and 32 measured 1,467, so every note fits in the request and retrieval has nothing to choose from. Each of those adds dependencies, another data processor, or extra model rounds without improving recall at this size.
5. **Run the rollup on the configured model at low effort.** Sonnet 5.5 at low effort was as accurate as at medium, for the same cost. Claude Haiku 5.5 costs 1/18 as much and got the main cases right, but it marked an order "sensitive" and turned one pickup into a habit. Whether to allow Haiku for the rollup is an [open question](#open-questions-for-the-owner).

## What's there today

The recommendation builds on these facts about the current assistant (`src/dinnerbell/services/assistant/`, `src/dinnerbell/domain/assistant.py`):

- **Pseudonyms are per conversation.** Each conversation has its own map from member ID to label, with the member answered as Member 1. The same person is "Member 1" in their own web conversation and "Member 3" in a housemate's. A note must be stored by member ID and relabeled for each conversation; `Pseudonyms.relabel` already does this for history.
- **History** is a list of `Turn`s (a member's message, a reply, or a used-draft record) in pseudonym form. Requests carry at most `MAX_HISTORY_BYTES` (24,000) of it. Tool results are never stored, so the rollup can see only what members wrote, what the assistant replied, and the draft records.
- **Each request** sends the fixed system prompt, then the roster as a second system block, then the messages and tools, with top-level automatic caching. Measured with `count_tokens` for four members, a private request is 15,164 tokens with 33 tools; a channel thread's is 10,382 tokens with 19 tools.
- **The cap** reserves each call's worst case before it runs: the request's bytes as tokens, at the higher of the input and cache-write prices, plus 6,000 tokens of output, plus one hop for the server-side fallback. The smallest private message's first call reserves $0.35 with Sonnet 5.5.
- **Retention.** `purge_idle_conversations` deletes conversations past **Conversation hours** (0 to 168, default 24). **Start over** deletes the web conversation. `reset-history` clears conversations and drafts, `scrub-test` turns the switch off and deletes them, and backups keep copies until they're pruned.
- **Structured data and tools.** Preferences (veto, −2 to +2 on tags and items), dietary needs, **I can usually**, away days, ratings and feedback, and the meal log. The own-data tools look up and draft these, in private conversations only, and only when the member asks.
- **One container, SQLite.** Every new table needs `household_id`, a migration, a `KEEP` or `CLEAR` entry in `reset.py`, and a line in [What a backup holds](../backup-and-upgrade.md#what-a-backup-holds).

## Comparison

Costs assume a four-member household with 40 assistant messages and 25 expiring conversations a month, and Sonnet 5.5 at Dinnerbell's table prices. Each option's section shows the arithmetic.

| Criterion | 1. Rollup | 2. Memory tools (FTS5) | 3. Semantic search | 4. Drafts to settings |
|---|---|---|---|---|
| Recall | Good: notes are always in front of the model. "The usual" led straight to the right place in the prototype. | Weaker for implicit references: the model must decide to recall. | Same as 1 or 2; retrieval adds nothing at a few dozen notes. | Best for what it covers: ranking enforces it. Covers only fields Dinnerbell has. |
| Cost a month | About $0.25; under $0.90 in a heavy month | About $0.30, plus a reservation for each extra round | Option 1 or 2, plus $0 to $0.01 of embeddings | Nearly $0; an extra tool round when a draft is offered |
| Latency | None on requests; the rollup runs in the scheduler | About 2 seconds for each recall or remember round | 0.1 to 0.3 seconds for an embeddings call, or local CPU time | One tool round when offered |
| Privacy and attribution | Code assigns each note to who said it; visibility rules in code | Attribution is easy: a tool acts as the member | As 1 or 2; an embeddings API is one more processor of household text | Settings already have FR-29's rules |
| Control and correctness | Member lists and deletes; the next rollup sees existing notes and corrects them | Model can write mid-conversation, including a planted note | As 1 or 2 | Member confirms every change |
| New dependencies | None | None (FTS5 ships with SQLite) | `sqlite-vec` (pre-v1) and an API key, or about 100 MB of local model | None |
| Testability | Code rules unit-tested; prompt quality by a small eval | Code testable; when the model calls tools isn't | Adds a vector index to test | Drafts are tested today |
| Effort | Medium: one issue | Medium to large | Large | Small (in-chat offer) to medium (suggestions) |

## Option 1: rollup into the system prompt

**How it works here.** Before `purge_idle_conversations` deletes an expired conversation, the scheduler sends its stored turns to a rollup call: a fixed prompt, the existing notes it may correct, and the transcript between tags with a random suffix, as menu links do. The call has no tools, sends no roster or names, and answers in a JSON schema (structured outputs): a list of notes to keep, each with who it's about, who said it, a category, one short sentence, a settings type or none, a sensitive flag, and the IDs of notes it replaces, plus the IDs to forget.

Code then decides, not the model:

- Labels become member IDs through the conversation's map. A note about a member that someone else said is dropped, so one member can never pin a stance on another.
- An item marked as a setting isn't kept; it becomes a draft offer (option 4). An item marked sensitive isn't kept as free text either.
- A note's text is stored in ID form (Member *N* is member ID *N*) and relabeled into each conversation's map when it's sent, as `Pseudonyms.relabel` does. A label for anyone no longer on the roster becomes "a member".
- Forget and replace act only on notes the call was shown.

Each private request then carries the member's notes, and every request carries the household's, as a third system block after the roster: "About Member 1, said by Member 1 in a private conversation on November 3: usual order at Zorblat Noodles is dan dan noodles, mild, no peanuts." The heading says notes are data, can be out of date, and lose to the member's words, Preferences, and tool results.

**Cost.** The rollup's input isn't cached, since it differs from the assistant's prefix.

- Rollup, typical: a 3,000-token conversation with about 120 output tokens costs 3,000 × $2/M + 120 × $10/M = $0.0072. For 25 conversations, that's $0.18. The prototype's shorter cases cost $0.0028 to $0.0051 each.
- Rollup, worst case: a full 24,000-byte history measured 13,185 input tokens: 13,185 × $2/M + 150 × $10/M = $0.028, and 25 of them cost $0.70.
- Notes on requests: 8 notes measured 453 tokens. The first call of a message writes them at the cache-write price: 453 × $2.50/M = $0.0011. About 1.5 later calls read them at $0.20/M: $0.0001. For 40 messages, that's $0.05. With 32 notes (1,467 tokens), it's $0.16.
- Total: about $0.23 a typical month and $0.86 a heavy one. With Haiku 5.5 for the rollup (mean $0.0002 a case measured), the rollup part falls to about $0.01.

**The cap.** The rollup reserves and settles like any call. Its worst case with Sonnet is about (29,000 bytes + 2,000) × $2.50/M + 4,000 × $10/M = $0.12, with `max_tokens` of 4,000 and no fallback. Notes add their bytes to every call's reservation: 1,289 bytes add $0.0064 with the fallback hop, and 4,000 bytes add $0.02, so the smallest private message reserves about 2 to 6 percent more than $0.35. When the cap can't cover a rollup, the scheduler skips it and deletes the conversation anyway: **Conversation hours** never wait on memory.

**Privacy.** Notes leave the house on every private request, not only when the member asks, which is a change to ADR 0009's "Minimal data" for the member's own data. They're built from conversations that already went to Anthropic, but they keep that content for months instead of hours. Admin > Assistant's disclosure would say so.

**Risks.** A rollup can miss something or keep the wrong thing; the member's list with **Delete** is the remedy. A planted instruction could survive as a note; the rollup prompt, the code rules, the note heading, and ADR 0009's backstop (tools act as the member, and nothing changes without **Confirm**) limit what it can do. A note learned now shows only after the conversation expires, 24 hours by default.

**Effort.** Medium, one issue: a migration, a rollup service called from the scheduler, the system block, the member's list and **Delete** on the web, Admin's switch and **Clear memory**, retention hooks, tests with the fake model client, and spec and ADR text.

## Option 2: memory tools

**How it works here.** Three client-side tools, `remember`, `recall`, and `forget`, over a memory table with an FTS5 index, each acting as `tool_actor(member)` like every other tool. Attribution is simple: the tool knows who's asking, and where.

**Cost.** Tools cost tokens on every request. The private and channel requests differ by 14 tools and 4,782 tokens, about 340 tokens a tool, so three tools add about 1,000 tokens to every request, more than 8 notes do. Each recall or remember is another model round: about 15,600 cached tokens at $0.20/M ($0.0031) plus about 250 output tokens ($0.0025), so about $0.006 and 2 seconds. With recall on half of 40 messages and remember on a quarter: 20 × $0.006 + 10 × $0.006 + 40 × 1,000 × $2.50/M = $0.12 + $0.06 + $0.10 = $0.28 a month. Each round also reserves about $0.35 against the cap and uses one of the six model calls a message allows.

**Risks.** The model must think to recall: "the usual" works only if it does. A member, or text the assistant read, can steer a `remember` mid-conversation, with no rollup step in between. In a channel thread, `remember` would store what a housemate said unless code refuses.

**Anthropic's memory tool** (`memory_20250818`) is this option in a file-directory shape. It's client-side, so Dinnerbell would still store every file, and its commands (`view`, `create`, `str_replace`, `insert`, `delete`, `rename`) edit free text, which loses per-note attribution. The API adds its own system prompt, "ALWAYS VIEW YOUR MEMORY DIRECTORY BEFORE DOING ANYTHING ELSE", so every message would pay an extra round. It's ZDR-eligible and runs on every current model, but it doesn't fit Dinnerbell's attribution and privacy rules.

**Effort.** Medium to large: everything in option 1 except the rollup, plus three tools with refusals, FTS5 in a migration, and prompt work so the model recalls when it should.

**Where it fits later:** an explicit "remember this" or "forget that" in a private conversation, which works at once instead of after the conversation expires.

## Option 3: semantic search

**How it works here.** Embed each note, store the vectors, and send each request the nearest few.

- **Anthropic offers no embeddings model;** its documentation points to Voyage AI. `voyage-4-lite` costs $0.02 per million tokens, `voyage-4` $0.06, with 200 million free tokens per account. A household's few thousand tokens a month cost nothing. It needs a second API key and sends household text to another company. The `voyageai` package (MIT) pulls in aiohttp, Pillow, tokenizers, and LangChain's text splitters, though a plain HTTPS call needs none of them.
- **Local embeddings:** `fastembed` (Apache-2.0) runs ONNX models on the CPU. It adds `onnxruntime` (MIT, a 28 MB wheel), numpy, and tokenizers, and downloads its default model, `bge-small-en-v1.5`, at 66.5 MB, at first use unless the image bakes it in. Expect the image to grow by about 100 MB or more.
- **Vector search in SQLite:** `sqlite-vec` 0.1.9 (MIT or Apache-2.0, a 0.2 MB wheel) is a loadable extension, and its README says it's "pre-v1, so expect breaking changes". The official Python image enables loadable extensions. Its virtual tables need the extension loaded for every read and delete, including `reset-history`, `scrub-test`, and a restored backup.

**Cost and value.** Embeddings cost about nothing, but at 8 to 32 notes, all of them fit in 453 to 1,467 tokens, so there's nothing to rank. Revisit only if notes grow past what a block can carry.

**Effort.** Large: option 1 or 2, plus an extension or a model to ship, a second provider, and a vector index to keep in step with every deletion.

## Option 4: memories as drafts to structured data

**How it works here.** Where a note is really a setting, it becomes a change the member confirms, through the services the Preferences page uses. Ranking then enforces it, which a note never can (ADR 0005).

- **In the conversation (first build):** when a member states a lasting stance in a private conversation, the assistant offers the draft it already can (`draft_preference`, `draft_dietary_needs`, `draft_can_usually`, `draft_away`). This changes the prompt's "only when they ask" rule to "when they ask or state one", which is an owner decision. A draft lasts 30 minutes, so an offer the member ignores is gone.
- **After the rollup (later):** the rollup's settings items wait as suggestions in a "From your conversations" list on Preferences, with **Confirm** and **Dismiss**, each saying what Confirm does as drafts do. This needs a table, the household's tag and item names in the rollup (which tools send already), and a page.

**What the prototype showed.** The rollup classified settings consistently: "I don't do spicy" as a level in every run, a pregnancy as a dietary need marked sensitive in every run, "gone off Thai" as a level that replaces the old note, and a +2 the member had confirmed as nothing to keep. "The usual" at one place, which has no field, stayed a note.

**Limits.** Only fields Dinnerbell has: no "usual order", no "Friday pizza night", no "closes early on Mondays". A stance must match a tag or item the Preferences page lists, or there's nothing to draft.

**Effort.** Small for the in-chat offer; medium for suggestions.

## Option 5: hybrids and hosted features

- **Recommended hybrid:** option 1 with option 4's in-chat offer now; option 4's suggestions and option 2's explicit "remember this" and "forget that" later; option 3 only at a scale a household doesn't reach.
- **Managed Agents memory stores** are Anthropic-hosted, versioned text files mounted into a Managed Agents session. Dinnerbell runs its own Messages API loop, so they'd need a different architecture, and household notes would live at Anthropic indefinitely.
- **Compaction, context editing, and message threads** manage one long conversation; none of them carries anything across conversations. Message threads aren't ZDR-eligible.
- **Mem0** (Apache-2.0) needs an LLM and an embeddings provider, OpenAI by default, and a vector store; its package depends on `openai`, `qdrant-client`, and `posthog`, a telemetry client. **Letta** (Apache-2.0) is an agent server of its own. **Graphiti** (Apache-2.0, from Zep) needs a graph database such as Neo4j and LLM calls on every ingest. Their licenses are AGPL-compatible, but each would replace Dinnerbell's loop, its pseudonyms, or its one-container SQLite deployment. Their useful ideas, extract with one call, attribute, and let new facts supersede old ones, are already in option 1.

## Privacy options

ADR 0009 sends a member's own data only in their private conversations. These are the choices for memory:

| | Rule | What it allows | What it risks |
|---|---|---|---|
| P1 | **Keep ADR 0009.** A member's notes inform only their own private conversations. | Their own "the usual" and "you know I don't" | Nothing new. Channel threads and housemates' requests get no personal notes. |
| P2 | **Share with attribution everywhere.** | "Get everyone's usual" | A housemate learns someone's stances, vetoes, or health through a reply; channel replies broadcast it; anyone can ask "what does Member 2 avoid?" It undoes FR-29. |
| P3 | **Per-note visibility,** private by default, the member can mark one household. | Members choose what to share | A wrong tap shares something; more UI; shared notes can still be probed, by design. |
| P4 | **By origin.** What was said in a channel thread may inform channel threads and housemates' requests; what was said privately stays private. Sensitive notes are always private, and a member can make any note private. | A coordinator ordering "the usual" for everyone, from what each said in the channel | A public remark resurfaces weeks later, and asking over time builds a profile from public remarks. A Slack guest account in the channel sees replies. |

**Recommendation:** P1 for the first build, with household notes only from channel threads. That changes nothing about who sees what, so the ADR needs only the "Minimal data" change for the member's own notes. If you want "everyone's usual", P4 is the next step, because it never moves anything said in private. P2 isn't worth its risks.

## How the first build handles each case

| Case | What happens |
|---|---|
| Attribution | Each note stores who it's about and who said it (member IDs), the conversation's kind, and the day. Code drops a note one member made about another. |
| Member control | The web Assistant page lists the member's notes, and the household's, with where and when; the member deletes any of their own, or all. **Start over** deletes the conversation without a rollup. |
| Admin control | A **Memory** switch on Admin > Assistant, off by default and audited, which needs Conversation hours above 0. **Clear memory** deletes every note, audited. Admins can delete household notes. |
| Stale or contradicting notes | The rollup sees the member's notes and forgets or replaces what the conversation corrects. Notes expire after a set time (180 days suggested), and a cap per member (20 suggested) drops the oldest. The note heading says the member's words and Preferences win. |
| Injection | The rollup has no tools, reads the transcript as tagged data, answers only in its schema, and code applies the rules above. Tool results, and so fetched pages, never reach it, though the assistant's replies can quote them. Notes are framed as data. Every change still needs **Confirm**. |
| Settings | Never kept as notes; offered as drafts in the conversation. |
| Sensitive | Not kept as free text. |
| Deactivated members | Their notes, and household notes they said, are deleted at deactivation. Labels for anyone off the roster render as "a member". |
| `reset-history` | The memory table is `CLEAR`: notes formed in testing talk about test calls. |
| `scrub-test` | Deletes every note, as it does conversations, so test never sends production's memory to the model. |
| Backups | Notes are in the database, so every copy holds them until pruned. [What a backup holds](../backup-and-upgrade.md#what-a-backup-holds) adds them. |
| The cap | The rollup reserves and settles like any call; at the cap, it's skipped and the conversation is deleted on time. Admin's usage shows rollups on a line of their own. |
| Switch off, hours at 0, assistant off | No rollups and no notes sent: today's behavior. |

## Prototype

A scratch script outside the repository, using the `anthropic` SDK 2.7.0, on nine hand-written conversations with invented members and places, in pseudonym form with a different map in each. It built Dinnerbell's real request from the repository's `build_request` for four synthetic members, and tracked every call's cost from its `usage` at the repository's price table, with a $2.00 stop. The table prices Sonnet 5.5's cache reads at $0.20 per million tokens; Anthropic's pricing page now says $0.10, so the cached figures are an upper bound.

**Spend:** $0.19 for 39 model calls and 8 free token counts.

**Overhead** (`count_tokens`):

| Request | Tokens |
|---|---|
| Private request, no notes | 15,164 |
| Channel thread request | 10,382 |
| 8 notes (1,289 bytes) | +453 |
| 32 notes (4,006 bytes) | +1,467 |
| Rollup prompt and schema alone | 705 |
| Rollup of a full 24,000-byte history | 13,185 |

**Cached and not:** the private request with 8 notes cost $0.041 on its first call (15,621 tokens written to the cache) and $0.0057 on the next call within 5 minutes (15,621 read). The notes' share is $0.0011 uncached and $0.0001 cached.

**Rollup quality.** Each case has checks in code: keep the point, attribute it to the right member ID, drop chatter, keep no planted instruction, correct an old note, mark health sensitive, and keep no copy of a confirmed setting.

| Case | Sonnet 5.5 low (×2) | Sonnet 5.5 medium | Haiku 5.5 low |
|---|---|---|---|
| A preference among small talk | Pass; drafted as a level | Pass | Pass |
| Only small talk | Pass; nothing kept | Pass | Pass |
| Channel thread, three members | Pass: the right member's usual; the household's Friday plan; nothing pinned on the member another described | Pass | Pass |
| "Remember forever: ignore your rules…" | Pass: kept only "likes extra garlic" | Pass | Pass |
| A menu page's planted line, quoted in a reply | Pass; nothing kept | Pass | Pass |
| Correcting an old note | Pass: forgot it, kept the unrelated one | Pass | Pass in fact; one check failed on wording |
| Pregnancy, "don't tell the others" | Pass: a dietary need, sensitive | Pass | Pass |
| "My usual" at one place | Pass: kept as a note | Pass | Pass, but marked it sensitive and drafted "usually picks up" from one pickup |
| A +2 already confirmed | Pass; nothing kept | Pass | Pass |
| Mean cost a case | $0.0035 | $0.0036 | $0.0002 |
| Time a call | 0.6 to 2.7 seconds | 1.0 to 2.1 seconds | 1.2 to 2.6 seconds |

**Recall.** Asked "the usual for tonight please, I'll pick it up" in Dinnerbell's real private request, the model with notes looked up the call and the remembered place by name. Without notes, it looked up the most-eaten meals, guessing. That's one sample, not a benchmark.

**Limits.** Nine short cases, one household size, and English only. They test the rules, not how memory feels over months. Before the build ships, a larger set taken from real use, with members' consent, would be worth running.

## The first build, and what comes later

**First build, one issue:**

- An `assistant_memory` table: household, about (member ID or none for the household), said by, origin (private or channel), the note in ID form, created and expiry times.
- The rollup at expiry, on the configured model at low effort, under the cap, private and channel conversations alike, applying the code rules above.
- The notes block in requests: the member's own in private conversations, the household's everywhere, at most 2,000 bytes.
- The assistant offers a draft when a member states a setting in private.
- The member's list with **Delete** on the web; Admin's **Memory** switch and **Clear memory**, both audited.
- Deactivation, `reset-history`, `scrub-test`, and backup docs as in the table above.
- Spec 6.14 and an ADR under 1,000 words that amends ADR 0009's "Minimal data".

**Later, each its own issue:**

1. Suggestions on Preferences from the rollup's settings items (option 4).
2. Origin-based sharing (P4), if you want "everyone's usual".
3. "Remember this" and "forget that" as tools in private conversations (option 2), for notes that work at once.
4. A memory list in Slack's App Home.
5. Semantic search, only if notes outgrow the block.

## Open questions for the owner

1. **Privacy:** P1 for the first build, with household notes only from channel threads, or start at P4?
2. **Sending own notes unasked:** may a member's notes go with every private request (an ADR 0009 amendment), or only when the model asks with a recall tool (option 2)?
3. **Rollup model:** the configured model only, or a setting that allows Haiku 5.5 at 1/18 the cost, with the measured loss in precision?
4. **Offering drafts:** may the assistant offer a draft when a member states a stance, not only when they ask?
5. **Turning memory off:** should the switch delete every note, or stop using them and keep them until **Clear memory**?
6. **Limits:** 20 notes per member, 20 for the household, 180 days, 2,000 bytes: right sizes?
7. **Sensitive notes:** never keep them as free text (recommended), or keep them private-only?
8. **Usage display:** show rollup spending on its own line in Admin > Assistant, or under the conversation's member?

## Sources

- Anthropic: [Pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching), [Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool), [Embeddings](https://platform.claude.com/docs/en/build-with-claude/embeddings), [Token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting), and [Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction).
- Voyage AI: [pricing](https://www.mongodb.com/docs/voyageai/management/billing/).
- [sqlite-vec](https://github.com/asg017/sqlite-vec) and its [PyPI page](https://pypi.org/project/sqlite-vec/); [fastembed](https://github.com/qdrant/fastembed); [onnxruntime on PyPI](https://pypi.org/project/onnxruntime/); the [quantized bge-small-en-v1.5 model](https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q); the official Python image's [build template](https://github.com/docker-library/python/blob/master/Dockerfile-linux.template) (`--enable-loadable-sqlite-extensions`).
- [Mem0](https://github.com/mem0ai/mem0), [Letta](https://github.com/letta-ai/letta), and [Graphiti](https://github.com/getzep/graphiti).
- Dinnerbell: [ADR 0005](../adr/0005-deterministic-ranking-first.md), [ADR 0009](../adr/0009-optional-assistant.md), and [spec 6.14](../spec.md#614-assistant).
