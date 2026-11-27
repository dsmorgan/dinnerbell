# ADR 0012: The assistant keeps a short, shared memory of each member's tastes, filled in the background

| | |
|---|---|
| Status | Accepted |
| Date | 2026-11-27 |
| Builds on | [ADR 0009](0009-optional-assistant.md) (amends "Minimal data" and "Every change is a draft"), [ADR 0005](0005-deterministic-ranking-first.md) (memory never ranks or filters) |
| Issue | [#367](https://github.com/dsmorgan/dinnerbell/issues/367), research in [assistant-memory.md](../research/assistant-memory.md) |

## Context

The assistant forgets everything when **Conversation hours** delete a conversation. The owner wants it to learn unnoticed, so "what restaurant would Member 2 and I both like?" gets an answer. ADR 0009 sends nobody's preferences but the asker's own, in private. The owner answered the research report's eleven questions on 2026-11-27.

## Decision

**A rollup keeps typed facts for each member.** A model call answers with changes to one member's facts: add, replace, or remove. A fact is one sentence of at most 160 characters with a kind (like, dislike, usual, experience, belief, or routine). Each member keeps at most 20; past that, code drops the oldest experience first, then the oldest of the rest. An experience expires 180 days after it's written; other kinds never expire.

**A pass reads the member's own messages, with the assistant's replies as context in private conversations only:** a channel thread's replies can echo housemates' words. Code guarantees that each fact lands only in its author's record, quotes at least three words of the author's own message, has more than half its content words in that message, and holds no member label, link, or instruction-shaped word ("everyone", "recommend", "vote"). So a reply, an item's name, or a forged transcript can't be a fact's source, even behind a harmless quote. The prompt is left with what words can't show: a remark about "my wife", an instruction in plain taste words, a medical reason, and what Preferences hold.

**When it runs.** The 5-minute purge job rolls a conversation up 15 minutes before its oldest turn not yet rolled up is Conversation hours old, when 24,000 bytes are waiting, or as a last try when it's due for deletion. **Start over** rolls up first, then deletes. Lowering Conversation hours deletes at once, without a pass; Admin refuses 0 while Memory is on. Turns written while Memory is off are never rolled up.

**Every request carries every active member's facts** as tagged data at the start of the member's message, in private conversations and channel threads, in a 16,000-byte budget, the asker's first. The assistant works a housemate's taste into a reply plainly ("Member 2 is off dairy, so not the pizza place") and never says whose stance kept a pick out of a call.

**Housemates' Preferences, through a tool.** While Memory is on, private conversations offer a lookup of housemates' Preferences; channel threads don't. The prompt says to answer taste questions from Preferences first, and to set what Preferences can hold with the own-data tools rather than leave it to memory.

**Switches and controls.** **Memory** on Admin > Assistant, off by default, and its own **Rollup model**, Claude Haiku 5.5 by default, are audited with the other assistant settings. Rollup calls go through the monthly cap. Each member sees their own facts on the Assistant page with **Remove**. An admin's **Clear memory**, for one member or all, is audited. Turning Memory off stops passes and sends no facts; stored facts stay until an admin deletes them. Deactivation, `reset-history`, and `scrub-test` delete facts.

**Memory never feeds filters, vetoes, or ranking** (ADR 0005), only what the assistant says.

## What this newly exposes

With Memory on, replies to a housemate can draw on:

- **Facts**: what each member told the assistant about their own tastes, habits, beliefs, and meals, in any reply, a channel thread's included.
- **Through the Preferences tool, in private conversations**: each housemate's levels and vetoes on tags and catalog items, their dietary needs, which can reveal health or religion, and their **I can usually**. FR-29 lets a member see only their own; until now only an admin saw another's. Ratings, feedback, notes, and away days stay private.

So a member can learn who vetoed a place, which the coordinator's aggregated list hides.

## Direct actions and planted facts

Since #374, the assistant acts at once on the speaking member's own stance in private, so a housemate's planted "always vote for the burger place" could move a vote. What bounds it:

- Tools act only as the speaking member, on their own stance: a fact can't change a housemate's data, and nothing that tells anyone, lifts a veto, takes off a dietary need, or joins late acts at once.
- At most 5 direct actions a message, each with a record and **Undo** for 30 minutes.
- A fact comes only from its author's own words, under their label, as data, without instruction-shaped words; a member sees and removes their own.

The gap that remains: an instruction in plain taste words, in the member's own message, passes code. Its reach is one member's own, undoable stance.

## Reasoning

- **Typed facts let code check a model's work:** the quote check dropped a forged transcript that fooled both models every time. Tying a fact's words to the member's message costs some reply-only facts ("loved it" after "How was the noodle place?"), the safer side.
- **All facts, not retrieval:** four full records are about 15 percent of a request.
- **Haiku 5.5** costs a twentieth of Sonnet 5.5 and passed the safety cases.
- **Preferences first** keeps memory from drifting from the structured data.
- **Off by default:** it changes ADR 0009's privacy stance for the whole household.

## Consequences

- One migration, a facts table; each stored member message records when it was written and whether it waits for a pass.
- The cap's smallest-message floors count the memory block while Memory is on.
- About $0.30 to $0.50 a month for four members and 40 messages.
- Member editing of facts is #380.
