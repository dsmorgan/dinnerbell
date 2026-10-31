# ADR 0008: Ratings move the rater's own score, and the household sees a hint

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-31 |
| Builds on | [ADR 0007](0007-seeded-weighted-ranking.md) (the score and the draw are unchanged) |
| Issue | [#18](https://github.com/dsmorgan/dinnerbell/issues/18), spec FR-55 |

## Context

Since 2026-10-25, members rate each meal they ate on five levels, **Hated it** (−2) to **Loved it** (+2), and, for a restaurant, check what went wrong: **Wrong order**, **Late**, **Cold**, or **Other** with a few words. FR-55 asks that feedback influence ranking.

Real ratings are too few to model, so the design rests on a simulation: Dinnerbell's unchanged `rank` over 100 made-up four-person households, 180 dinners each. ADR 0007's promises must hold: vetoes are guarantees, nobody can tell who disliked what (FR-29), and ranking stays a pure function.

## Decision

**Your ratings move your own score.** Each member has an adjustment per item, worked out at ranking time from their ratings, in meal-date order:

- Each rating moves the adjustment toward its score, `adj ← adj + step × (score − adj)`, with step 0.75 for **Hated it** and **Didn't like it** and 0.5 otherwise.
- Between ratings, and up to the call's date, the adjustment halves every 90 days, counted from the meal's date.
- The member's score for the item is their stated score (0 if none) plus the adjustment, kept within −2 to +2. Ranking uses it wherever it used the stated score, at the same weight.

One rating of a new place gives −1.5, −0.75, 0, +0.5, or +1. Ratings count only while that member is eating (in or maybe). Logged meals count; guests can't rate; ratings never touch a veto.

**Your edit wins.** Setting a stance on an item on Preferences discards your earlier ratings of it. A neutral stance deletes its `preference` row, so a new table, `stance_edit`, keeps each member's last edit time per item; only ratings changed after it count.

**The Feedback setting.** An admin setting, 0 (off), 1, or 2, limits how far each rating pulls: its score counts as at most that much. New households start at 2; the migration sets existing ones to 0, and the admin sets it to 2 when ready to let ratings move scores.

**The household hint.** Everyone sees a hint for each item, from everyone's ratings and issues, naming nobody. Each rating weighs `0.5^(age in days / 90)`; P, N, and F are the weights of positive, negative, and **It was fine** ratings. A verdict needs two people, each with a rating from roughly the last 3 months, and P + N + F of at least 1.5. Then:

- **Mixed reviews** if P ≥ 0.5 and N ≥ 0.5: at least one of each from roughly the last 3 months.
- **Not well liked**, otherwise, if N is at least a quarter of the total.
- **Well liked**, otherwise, if P is at least half of the total.

The issue checkboxes add the most important issue checked in the last 90 days: **Wrong order**, then **Late**, **Cold**, and **Other**, the order the forms list them in. An issue needs no second person. The hint reads "Mixed reviews · recently late", "Well liked · a recent wrong order", or, alone, "Recently cold" or "A recent problem". The **Other** text is never shown to anyone else.

The hint replaces FR-49's loved count on the catalog and is the first reason on a drawn pick, on the web and in Slack. It never changes a score.

**Preferences** shows each member, and only them, what their ratings make their score, to half a point: "From your ratings: about +1", or "With your ratings: about +0.5" beside a stated score. It shows nothing at Feedback 0.

## Reasoning

- **Partway toward each rating.** The simulation compared editing the saved score, averaging ratings, pooling everyone's ratings, and moving partway. Partway keeps the most variety for nearly the same gain. Pooling gained most (+0.21 enjoyment against +0.16) but cut variety to 12.6 different meals every four weeks and lets one person's rating overrule another's taste.
- **Negatives step further.** With equal steps, one **Loved it** cancels one **Hated it**. A step of 0.75 for negatives cuts dinners someone disliked from 21% to 16% (17% with equal steps) and drops a place that went bad to 27% of votes a month or two later, against 31% today. A full step lets one bad night set −2.
- **It fades.** Places and people change; an old verdict gives way without anyone editing anything.
- **What else held up.** A great new place got 29% more dinners in its first four months; variety held at 14.0 different meals every four weeks (15.3 today); with one heavy rater, everyone else still gained. Feedback 1 gives about two-thirds of the gain.
- **The edit wins** because a stance someone sets is deliberate, and answers the ratings before it.
- **Off at first.** An existing household gets the hints now and lets ratings move scores when it's ready, with one setting.
- **A verdict, not counts.** A count can name the one person who gave it; a verdict needs two raters. The owner chose one of each for **Mixed reviews** (not a quarter of each) and issues from one person. The weight minimum is 1.5, not the proposed 2: two fresh ratings fall below 2 the next day, so a verdict would vanish at once. At 1.5, two ratings keep one for about five weeks.

## Consequences

- FR-49 changes: the household sees a verdict that can be negative. In a two-person household a hint can reveal the other person's view, and an issue hint says that someone checked it.
- Item scores needn't be whole. A hint is stored in a pick's reasons when it's drawn, like every reason.
- Once people stop picking a declining place, nobody rates it; the hint helps there.
