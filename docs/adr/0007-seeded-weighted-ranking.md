# ADR 0007: Seeded, weighted random ranking among places that pass every filter

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-27 |
| Amends | [ADR 0005](0005-deterministic-ranking-first.md) |

## Context

ADR 0005 made ranking a pure function: hard filters, then a weighted score, then the top N by score. Ties went to the item eaten longest ago, then to the name, A to Z.

Ties are the common case, not the edge case. In a fresh catalog most places have no preferences, have never been eaten, and weren't suggested, so they score the same. Never-eaten places also tie on "eaten longest ago", so the name decided. A new household saw the same three restaurants, alphabetically first, on every call until history built up (#78). Even with different scores, the same inputs always gave the same top N, so the fourth-best place never came up at all.

The household wants picks that are random when everything else is equal, and more likely, but not certain, for a better score. ADR 0005's guarantees must hold: a veto is a guarantee, every pick explains itself, and ranking is a fast pure function that tests can pin down.

## Decision

Ranking keeps ADR 0005's hard filters and score, and replaces "take the top N" with a seeded weighted draw:

- **Randomness only among places that pass every filter.** Vetoes, guest needs, coverage, the variety window, meal fit, and one pick per place are applied first and stay absolute. The draw chooses among what's left; it can't bring anything back.
- **Weighted by score.** Within each group, the meal's and then the top-up's, a place's weight is `exp((score − best) / T)`. The draw is sampling without replacement, one Efraimidis–Spirakis step per pick, with keys compared as logarithms. Equal scores have equal chances until a pick shares their cuisine (see "Variety within a vote" below).
- **A temperature setting.** `T` is a household ranking setting, default 1.0, from 0 to 3, edited on Admin next to the weights. At T = 1 a place one point better is about 2.7 times as likely. T = 0 is score order with random tie-breaks, for a household that wants the old behavior without the name bias.
- **Suggestions are requests, not preferences.** A place suggested for the call that passes every filter is proposed before the draw, but at most `limit − 1` places are guaranteed this way across both groups, so the last place is not reserved for a suggestion. When more fit, the best-scoring suggestions go in, with ties drawn at random, and the rest join the draw.
- **Variety within a vote.** After each pick, places sharing one of its cuisines keep 0.3 of their weight. It's a constant in the domain, not a setting.
- **An explicit seed.** The snapshot carries a seed, a SHA-256 digest of the household, the call, and its run. `rank` builds its own `random.Random` from it and never touches the global generator. The same snapshot always gives the same picks; a re-run is a new run and draws again.
- **The stored order is the drawn order.** It's each proposal's rank, which FR-16 already uses to break a tie at the deadline. Reasons are unchanged: winning a draw isn't a reason.

## Reasoning

- **A pure function, made pure by its inputs.** Randomness comes in as data, like `now`. ADR 0005's testability survives: a test pins the seed and gets one exact answer, and the property tests run over hundreds of seeds instead of one order.
- **Guarantees stay filters.** Every promise the household relies on is a hard filter applied before any chance is involved. The draw only decides among options that were all acceptable, so "never vetoed" and "never uncovered" hold for every seed, and the property tests check them over every seed they run.
- **Weights already mean "how much this counts".** A softmax keeps that meaning: each point of score multiplies a place's chances by the same factor, whatever else is in the catalog. The temperature is a single dial between "always the best" and "anything that fits", and 0 keeps the strict order available.
- **A seed from the call, not the clock.** A seed derived from the call and its run is reproducible after a restart, in tests, and in a bug report, without storing anything new. Python's `hash()` changes between processes, so it can't be the source. A re-run is the natural moment for a fresh draw: the household asked for the call again.
- **Suggestions first.** Someone who suggests a place asked for it. Leaving it to chance, even weighted, would turn a request into a hint. The cap keeps a vote from being nothing but requests: one member's three suggestions (`MAX_SUGGESTIONS`) would otherwise fill the default vote of 3 with no draw at all, and the rest of the household's preferences would count for nothing.
- **Cheap.** A draw is a few passes over the candidates, well under a millisecond for a household catalog, with no external call.

## Consequences

- ADR 0005's example property, "a repeated cuisine never outranks an equivalent novel one", holds at T = 0. Above 0 the repeated cuisine is less likely to come first, but it can.
- The same inputs and run still give the same picks, so a bug report with the call and run reproduces.
- Tests that assumed alphabetical order now assert sets, or set T = 0, or check frequencies over 2,000 seeds with loose bounds.
- Ranking reports how many candidates shared each pick's score, so the event page can later say "chosen at random among 4 equally good options". It isn't stored or shown yet.
- A **Shuffle picks** button for the coordinator draws again with the next seed, a shuffle counter in the seed's derivation (#87, spec FR-61). The first draw keeps its seed.
- Feedback-informed ranking (M7) can add a score component without touching the draw.
