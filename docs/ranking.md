# How Dinnerbell picks proposals

When answers close on a call, Dinnerbell proposes places to vote on: 3, unless an admin changed it. It rules out everything that doesn't work for tonight, scores what's left, adds places someone suggested, and draws the rest at random. A better score makes a place more likely, but it doesn't always win.

This page explains the rules and how to tune them. The precise rules are in [spec section 7](spec.md#7-ranking), and the reasoning is in [ADR 0007](adr/0007-seeded-weighted-ranking.md).

## What can never be picked

No score, suggestion, or draw gets around these rules:

- **Vetoes.** If anyone who's in or maybe vetoed a place, or one of its tags, it's out.
- **Guests' needs.** If a guest needs a dietary tag, such as vegan, every proposal has it.
- **Someone to do it.** A pickup needs someone who offered to pick up, a home meal needs someone who offered to cook and can cook it, and delivery needs someone who's in and can order.
- **Recent repeats.** A place you ate within the variety window, 7 days by default, is out, unless nothing else fits at all.
- **Already a pick.** A place that's already on the call, from an offer, isn't proposed again. Each proposal is a different place.
- **The meal.** Places for tonight's meal come first. Places for other meals, such as a breakfast place on a dinner call, are proposed only when too few dinner places fit, and their first reason says "Usually for breakfast."

Everything that passes them is a candidate, and chance chooses only among candidates.

## What makes a place more likely

Each candidate's score is the sum of these parts, each multiplied by its weight, which an admin can [tune](#tune-the-ranking):

| Part | What earns it | Default weight |
|---|---|---|
| Soft preferences | Each person's +2 to −2 for the place, plus half their scores for its tags | 1 |
| Cuisine variety | A penalty if you had its cuisine recently, bigger the more recent | 3 |
| Never had it | +1 if the household has never eaten there | 1 |
| Suggested today | +1 for each person who suggested it on this call | 2 |
| Usual way to get it | +0.5 if it's the slot's usual way, such as pickup | 1 |
| Volunteer knows the dish | +0.5 if the volunteer cook is on its cook list | 1 |

A place someone suggested for this call goes in ahead of the draw, if it passes the rules above: a suggestion is a request, not a preference. The last place in every vote is kept for the draw, so suggestions fill at most one fewer than the number of proposals: with 3 proposals, 2. When more places were suggested than that, the best-scoring suggestions go in, with ties chosen at random, and the others stay in the draw, where their suggestion makes them much more likely. The same limit covers a top-up with places for other meals.

The rest of the proposals are drawn at random, and each point of score makes a place more likely by the same factor. At the default randomness, a place 1 point better is about 2.7 times as likely to be picked, and 2 points better about 7.4 times. Names never matter.

After each pick, places that share its cuisine drop to 0.3 of their chance, so a vote rarely has three taco places when other cuisines fit.

## Why the picks differ between calls

Each call has its own draw, so two calls with the same people and catalog can propose different places. Dinnerbell seeds the draw from the call itself, so the same call always gets the same picks. If an admin re-runs a call, or someone starts a called-off call again, that's a new run, and it draws again.

Proposals are listed in the order they were drawn, suggestions first. The order matters only when a vote ties. Ties go to the coordinator's choice, then the coordinator's vote, then an admin's vote when nobody coordinates, then the place you ate at longest ago (never counts as longest), and only then to the one drawn first. The decision says which rule decided it.

## Shuffling the picks

If the picks don't appeal, the coordinator can tap **Shuffle picks** while voting, or an admin can when nobody, or a guest, coordinates. A shuffle draws again with the call's next seed, so it's as reproducible as the first draw. Each call allows 3 shuffles.

- **Nobody has voted yet:** every drawn pick is replaced.
- **Someone has voted:** only picks with no votes are replaced. A vote that doesn't count yet, such as a late joiner's, still keeps its pick, because it is someone's choice.
- **Never replaced:** picks a member added, picks from an offer, and places someone suggested.
- **The same rules hold.** The new picks pass everything in [What can never be picked](#what-can-never-be-picked), including a late joiner's guests' needs. A place that was just shuffled out comes back only if nothing else fits. If nothing else fits at all, the shuffle is refused and doesn't count.

The picks message says who shuffled and how many were replaced, such as "Kim shuffled the picks (2 replaced).", and the call's thread gets a short note that mentions nobody.

## A worked example

It's a dinner call. Alex and Sam are in, and Alex offered to pick up. Pickup is the usual way, so every pickup place earns +0.5. The weights are the defaults, and Randomness is 1.

| Place | Why | Score |
|---|---|---|
| Sushi Spot | Someone vetoed it | Out |
| Pizza Co | You had it 2 days ago | Out |
| Burger Barn | Sam suggested it (+2), usual way (+0.5) | 2.5, always in |
| Siam Garden | Alex gave it +1, never had it (+1), usual way (+0.5) | 2.5 |
| Pho Place | Never had it (+1), usual way (+0.5) | 1.5 |
| Curry House | Never had it (+1), usual way (+0.5) | 1.5 |
| Taqueria | Had it 20 days ago, usual way (+0.5) | 0.5 |

Burger Barn is in because Sam suggested it. With 3 proposals, up to 2 can be suggestions, so one suggestion leaves two places to draw from the other four. Each place's weight compares its score with the best one, Siam Garden's 2.5:

| Place | Score | Weight | Chance of the next pick | Chance of making the vote |
|---|---|---|---|---|
| Siam Garden | 2.5 | 1 | 53% | 84% |
| Pho Place | 1.5 | 0.37 | 20% | 49% |
| Curry House | 1.5 | 0.37 | 20% | 49% |
| Taqueria | 0.5 | 0.14 | 7% | 19% |

The chance of the next pick is the place's weight over the total, 1.87. Once a place is picked, the draw repeats over the rest. The last column comes from 100,000 simulated calls with Dinnerbell's own ranking code.

So Siam Garden usually makes the vote, but about one call in six it doesn't, and Taqueria gets a turn about one call in five. If Siam Garden had another Thai place behind it with the same score, that place's weight would drop from 1 to 0.3 once Siam Garden was picked.

<a id="tune-it-on-admin"></a>

## Tune the ranking

An admin changes these on the **Ranking** tab of **Members**. Changes apply from the next call to close answers.

- **Weights** say how much each part of the score counts, from 0 to 10. 0 turns a part off. For example, raise **Cuisine variety** to avoid the same cuisine two days running, or raise **Never had it** to try more new places.
- **Randomness** says how far the draw strays from the best scores, from 0 to 3:

  | Randomness | What it does | Siam Garden makes the vote | Taqueria makes the vote |
  |---|---|---|---|
  | 0 | Always the best scores; a coin toss between equals | 100% | 0% |
  | 1, the default | A point better is about 2.7 times as likely | 84% | 19% |
  | 2 | A point better is about 1.6 times as likely | 69% | 32% |
  | 3 | A point better is about 1.4 times as likely | 63% | 38% |

  The last two columns are for the worked example. If the same places keep winning, raise it. If the picks feel too random, lower it.

Vetoes, guests' needs, coverage, and the variety window hold at every setting.
