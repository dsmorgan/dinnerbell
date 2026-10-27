# ADR 0005: Deterministic, explainable ranking before any LLM

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-21 |
| Amended by | [ADR 0007](0007-seeded-weighted-ranking.md): picks are a seeded, weighted draw among the places that pass every filter, not the top N by score |

## Context

The owner expects an assistant that takes natural-language requests such as "something we haven't had in a month that works for everyone who's in" and suggests meals. The question is whether the core selection logic should be a language model from the start.

## Decision

Version 1 ranks with a pure function: hard filters (vetoes, guest needs, role coverage, repeat window) followed by a weighted score (soft preferences, cuisine variety, novelty, suggestions, default mode, volunteer fit). Every proposal carries a sentence explaining its rank. Weights are household settings.

The assistant is a later milestone. It translates natural language into calls to the same domain API, and its proposals pass through the same filters. It never ranks on its own.

## Reasoning

- **Trust.** The family needs to see why a meal was proposed, and a veto must be a guarantee. A filter is a guarantee; a prompt is a request.
- **Testability.** A pure function over a snapshot is unit-tested with property-style assertions: no vetoed item ever appears, no uncovered mode ever appears, and a repeated cuisine never outranks an equivalent novel one.
- **Cost and latency.** Ranking runs on every call deadline. It should take milliseconds and no external call.
- **The assistant adds value on top, not instead.** Discovery of items that aren't in the catalog yet, parsing "tacos at 7, I'll cook" into an offer, and tweaking constraints conversationally are all better jobs for a language model than deciding between two restaurants everyone already likes.

## Consequences

- The ranking engine lives in `domain/ranking.py` with no imports from the service or database layers.
- Reason strings are generated from score components, not written by a model, so they are consistent and auditable.
- When the assistant milestone arrives, its tool surface is the service layer. It gets no direct database access and cannot bypass vetoes.
- The ranking weights are settings so the household can tune without a code change. The assistant may suggest weight changes but the admin applies them.
