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

Every award records when it was earned and whether the channel has been told: `pending`, `announced`, or `skipped`. A badge earned when a meal is done is `pending`. Nothing announces it yet; a later release posts the pending badges once and marks them `announced`. A badge earned by catching up (the backfill and a lowered count) is `skipped` and is never announced.

## The first start after the upgrade

The first start after the upgrade (revision `0016_contributions`) counts the meals already in your log, once. It adds up the done calls and the feedback you already gave, and awards every badge those counts reach as `skipped`, so the channel doesn't hear about old meals. It writes `contributions_backfilled` in the household settings when it finishes, and doesn't run again. After `dinnerbell reset-history`, the counts and badges go with the history they came from.

## Privacy

A member sees their own counts and progress. The household sees totals and the newest badges, with names, because badges are announced in the channel. No view ranks members or orders them by a count, and nothing here holds anyone's preferences or what they said in feedback; a count says only that feedback was given.

## Logs

Counting writes `contributions_recorded` and `badge_earned` lines (member ID and display name, the badge and tier, the call). They never carry emails or anything a member wrote.
