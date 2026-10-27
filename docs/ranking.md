# How Dinnerbell picks proposals

When answers close on a call, Dinnerbell proposes a few places to vote on, 3 unless your admin changed it. This page explains how it chooses them, what it never does, and how to tune it. The precise rules are in [spec section 7](spec.md#7-ranking), and the reasoning is in [ADR 0007](adr/0007-seeded-weighted-ranking.md).

In short: Dinnerbell first throws out everything that doesn't work for tonight, scores what's left, always includes places someone suggested, then draws the rest at random. A better score makes a place more likely, but it doesn't always win.

## What can never be picked

These rules are absolute. No score, suggestion, or lucky draw gets around them:

- **Vetoes.** If anyone who's in or maybe vetoed a place, or one of its tags, it's out.
- **Guests' needs.** If a guest needs a dietary tag, such as vegan, every proposal has it.
- **Someone to do it.** A pickup needs someone who offered to pick up, a home meal needs someone who offered to cook and can cook it, and delivery needs someone who's in and can order.
- **Recent repeats.** A place you ate within the variety window, 7 days by default, is out, unless nothing else fits at all.
- **Already a pick.** A place that's already on the call, from an offer, isn't proposed again. Each proposal is a different place.
- **The meal.** Places for tonight's meal come first. Places for other meals, such as a breakfast place on a dinner call, are proposed only when too few dinner places fit, and their first reason says "Usually for breakfast."

Everything that passes all of these is a candidate. Chance only ever chooses among candidates.

## What makes a place more likely

Each candidate gets a score, a sum of these parts, each multiplied by its weight on Admin:

| Part | What earns it | Default weight |
|---|---|---|
| Soft preferences | Each person's +2 to −2 for the place, plus half their scores for its tags | 1 |
| Cuisine variety | A penalty if you had its cuisine recently, bigger the more recent | 3 |
| Never had it | +1 if the household has never eaten there | 1 |
| Suggested today | +1 for each person who suggested it on this call | 2 |
| Usual way to get it | +0.5 if it's the slot's usual way, such as pickup | 1 |
| Volunteer knows the dish | +0.5 if the volunteer cook is on its cook list | 1 |

A place that someone suggested for this call is always proposed, if it passes the rules above. A suggestion is a request, not a preference.

The rest of the proposals are drawn at random, and each point of score makes a place more likely by the same factor. At the default randomness, a place 1 point better is about 2.7 times as likely to be picked, and 2 points better about 7.4 times. Places with the same score always have the same chance. Names never matter.

After each pick, places that share its cuisine become less likely: their chances drop to 0.3 of what they were. So a vote rarely has three taco places when other cuisines fit.

## Why the picks differ between calls

Each call has its own draw, so two calls with the same people and the same catalog can propose different places. That's on purpose: before, a new household saw the same three places, first in alphabetical order, on every call.

The draw isn't redrawn behind your back. Dinnerbell seeds it from the call itself, so the same call always gets the same picks. If an admin re-runs a call, or someone starts a called-off call again, that's a new run, and it draws again.

The order of the proposals is the order they were drawn, suggestions first. It matters once: if a vote ties and neither the coordinator nor an admin voted for one of the tied places, the one drawn first wins.

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

Burger Barn is in because Sam suggested it. That leaves two places to draw from the other four. Each place's weight compares its score with the best one, Siam Garden's 2.5:

| Place | Score | Weight | Chance of the next pick | Chance of making the vote |
|---|---|---|---|---|
| Siam Garden | 2.5 | 1 | 53% | 84% |
| Pho Place | 1.5 | 0.37 | 20% | 49% |
| Curry House | 1.5 | 0.37 | 20% | 49% |
| Taqueria | 0.5 | 0.14 | 7% | 19% |

The chance of the next pick is the place's weight over the total, 1.87. Once a place is picked, the draw repeats over the rest. The last column comes from 100,000 simulated calls with Dinnerbell's own ranking code.

So Siam Garden usually makes the vote, but about one call in six it doesn't, and Taqueria gets a turn about one call in five. If Siam Garden had another Thai place behind it with the same score, that place's weight would drop from 1 to 0.3 once Siam Garden was picked.

## Tune it on Admin

An admin changes these on **Admin > Ranking**. Changes apply from the next call.

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
