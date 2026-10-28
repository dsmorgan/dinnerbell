# Contributions and badges

Dinnerbell counts what each person does to get a meal on the table, and awards a badge when the count reaches a tier. This page explains what counts, when, and how to change the tiers. The rules are in [spec section 12](spec.md#12-metrics-and-badges).

## What counts

A call or offer counts the moment it is done. Each person gets one count per kind for it:

- **Cooked, picked up, ordered, coordinated.** Whoever the meal assigned. Guests count here: a guest who picked up picked up.
- **At the table.** Everyone who was in, including a late joiner who is taking part.
- **Answered quickly.** An answer, any answer, within 15 minutes of the call opening.
- **Ideas.** The person whose pick won; the person whose pick was new to the household; and the person who added a catalog item the first time it was chosen.
- **Hosting.** The member who brought guests.
- **Feedback.** The first time you give feedback on a meal. Changing it, or taking it back and giving it again, doesn't count again.

Finishing a call twice, or two people tapping **Mark done** together, counts it once. A call that is called off or discarded counts for nothing. Meals someone logged by hand don't count: they have no roles or answers.

Only members earn the "at the table", "answered quickly", idea, hosting, and feedback counts. A guest doesn't choose the meal or give feedback.

## Badges and tiers

Twelve badges share one ladder: **First** (1), **Bronze** (10), **Silver** (50), **Gold** (100), **Diamond** (200). A badge tier is earned once per person. **On a Roll** counts weeks in a row, Monday to Sunday in your time zone, with at least one cooked, picked up, or ordered meal.

The counts are a household setting. The admin changes them with `badge_thresholds` in the household settings, five whole numbers that rise from 1 to 10,000. Raising a count takes nothing away. Lowering one awards the tiers people already have the counts for, without an announcement.

## Announcements

Every award records when it was earned and whether the channel has been told: `pending`, `announced`, or `skipped`. A badge earned when a meal is done is `pending`. On every scheduler tick (30 seconds), Dinnerbell posts the pending badges and marks them `announced` in one transaction, so a restart, a retry, or two ticks at once never post one twice, and a crash never loses one. A badge earned by catching up (the backfill and a lowered count) is `skipped` and is never announced. The awards are marked `announced` when the post is queued, not when it lands: if Slack then refuses the post for good (the outbox gives up after its retries), the award stays `announced` and that post is lost. Nothing posts it again.

- **One post for each done call.** Everything the call earned goes in one message, with each member mentioned once, in name order. It never compares people. A post names at most 10 members and 6 badges each; the rest are counted ("and 3 more members earned badges too"). A badge earned at several tiers at once shows its highest.
- **Food Critic is never announced.** Who gave feedback is as private as what they said (FR-49), so this badge is marked `skipped`, left out of the monthly recap and the household's newest badges, and shown only to the member. Every other badge is shown with the member's name.
- **A member who left** is never announced.
- **On the test instance**, the post goes to the test channel, and a member who isn't in that channel appears as a plain name instead of a mention (ADR 0006). `scrub-test` marks every pending badge `skipped`, so a restored copy of production announces nothing production hadn't.

## The monthly recap

On the first day of each month from 9 a.m., in your household time zone, Dinnerbell posts the month before: how many meals you ate, the cuisines (and any you had never eaten), the new catalog items, a thank-you, and the badges earned. The thank-you lists who cooked, who picked up, who ordered, and who coordinated, each list in name order. It shows no one's count, because a list of numbers lets anyone rank by eye; the numbers it does show are household totals. Being missing from a list says nothing, and the recap thanks everyone who answered a call or pulled up a chair. It mentions no one, and it names only members who are still active.

The household setting `recap_posted` holds the last month recapped, such as `2026-10`, so the recap posts once however often Dinnerbell restarts. If Dinnerbell was down on the first, the recap posts on its next tick up to the seventh; after that it leaves the month out, and only the month just ended is ever recapped. A month with no meals posts nothing: there is nothing to celebrate, and a "0 meals" note isn't news. The marker still moves on.

On the test instance, the recap posts to the test channel like any post. `scrub-test` marks the latest due month as posted, so a restored copy stays quiet until the next month starts. Then the test instance posts one recap on that month's first, from the production history it was restored with. A marker that is not a `YYYY-MM` month, or is later than the current month, counts as none. If the household time zone is not valid, the recap stays off and Dinnerbell logs one warning.

## App Home

Each member's App Home has a short "Your badges" section: the badges they hold, each with its highest tier as a text tag, and the badge closest to its next tier ("Next: Chef · Silver, 7 to go (43 of 50 meals cooked)"). It shows only their own. A guest's App Home has no badge section (a guest's badges are still announced in the channel). If reading it fails, Home still draws without it. The badge art replaces the text tags later.

## The first start after the upgrade

The first start after the upgrade (revision `0016_contributions`) counts the meals already in your log, once. It adds up the done calls and the feedback you already gave, and awards every badge those counts reach as `skipped`, so the channel doesn't hear about old meals, and the Stats page doesn't list them as new. It uses members' current roles, so someone who was a guest then and is a member now counts as a member. It works in batches of 200 calls and writes `contributions_backfilled` in the household settings last, so a start that is stopped halfway finishes the rest next time.

On every later start, Dinnerbell also counts any done call that has no counts. That can only happen if counting failed while the call was being marked done; marking it done is never blocked by that. A call counted late this way earns its badges as `skipped`.

`dinnerbell reset-history` deletes the counts and the badges earned from them, along with the history. The badges don't come back.

## Privacy

A member sees their own counts and progress. The household sees totals and the newest badges, with names, because badges are announced in the channel. No view ranks members or orders them by a count, and nothing here holds anyone's preferences or what they said in feedback; a count says only that feedback was given.

## Logs

Counting writes `contributions_recorded` and `badge_earned` lines (member ID and display name, the badge and tier, the call). They never carry emails or anything a member wrote.
