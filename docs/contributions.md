# Contributions and badges

Dinnerbell counts what each person does to get a meal on the table, and awards a badge when a count reaches a tier. This page explains what counts, how badges are announced, and how to change the tiers. The rules are in [spec section 12](spec.md#12-metrics-and-badges).

## What counts

A call or offer counts the moment it's done. Each person gets one count per kind for it:

- **Cooked, picked up, ordered, coordinated:** whoever the meal assigned, guests included.
- **At the table:** everyone who was in, including a late joiner who is taking part.
- **Answered quickly:** any answer within 15 minutes of the call opening.
- **Ideas:** the person whose pick won; the person whose pick was new to the household; and the person who added a catalog item the first time it was chosen.
- **Hosting:** the member who brought guests.
- **Leaving the choice to others (Gambler):** on a call with at least three picks, a member who was in, not as a late joiner, and whose votes, when the call was done, were for every pick or for none. Voting for none counts even when no one else voted, so a call locked or decided with no votes gives it to every member who was in. Votes you took back don't count. Guests' votes are ignored, and offers have no picks to vote on.
- **Feedback:** the first time you give feedback on a meal. Changing it, or taking it back and giving it again, doesn't count again.

Only members earn the at-the-table, answered-quickly, idea, hosting, Gambler, and feedback counts: a guest doesn't choose the meal or give feedback.

A call counts once, even if it's finished twice or two people tap **Mark done** together. A call that's called off or discarded counts for nothing, and meals logged by hand don't count, because they have no roles or answers.

## Badges and tiers

Each of the 13 badges has five tiers, **First**, **Bronze**, **Silver**, **Gold**, and **Diamond**, and climbs one of three ladders:

| Ladder | Counts | Badges |
|---|---|---|
| Frequent | 1, 10, 50, 100, 200 | Full Table, Early Bird, Chef, Food Critic, Quarterback, Gambler, Road Runner, Order Up |
| Occasional | 1, 5, 20, 50, 100 | Bright Idea, Good Host |
| Rare | 1, 3, 10, 25, 50 | On a Roll, Scout, Explorer |

Each person earns a badge tier once. **On a Roll** counts weeks in a run, Monday to Sunday in your time zone, with at least one meal cooked, picked up, or ordered. A run survives one missed week, which doesn't count toward it; two missed weeks in a row end it.

## The badge art

Each badge is a tier frame with its own icon: Chef (chef hat), Road Runner (car), Order Up (takeout bag), Quarterback (dinner bell), Full Table (fork and knife), Early Bird (alarm clock), Bright Idea (lightbulb), Scout (recipe card), Explorer (compass), Good Host (extra chair), Food Critic (star), On a Roll (flame), and Gambler (a single die). The tiers differ in outline and lightness, so they stay distinct in grayscale: First is a teal ribbon rosette; Bronze, Silver, and Gold are medals on a navy strap; and Diamond is a faceted gem. On the web, Gold and Diamond sparkle, which stops under "reduce motion". Every page also writes the badge's name and tier in text.

<a id="badge-art-in-slack"></a><a id="the-composite-picture-on-app-home"></a>In Slack, the art shows as images in announcements and on App Home. Without the `files:write` scope, or when Slack refuses the art, Dinnerbell shows text tags such as 🥇 Chef · Gold instead, and never fails a post. [badge-art.md](badge-art.md) explains how the art is drawn, uploaded, and cached.

## The Stats page

**Stats** in the navigation opens `/stats`. It shows each badge at your tier, with progress to the next ("Meals cooked: 63 of 100 for Gold"), the badges you earned in the last two weeks, and your counts. Badges you haven't earned yet show softly, with the count that earns the first tier. Members also see the household: totals, and the newest badges with who earned them. Badges earned by catching up are held but never listed as new. Nobody is ranked, and nobody sees another member's counts.

A guest sees **Your badges** only: the four pitching-in badges and On a Roll, with no household section.

## App Home

Each member's App Home has a short **Your badges** section with only their own badges: each at its highest tier, as a text tag, as small icons in rows of 10 with a link to **Stats** for the names, or as one picture of all of them once it's ready. It also shows the badge closest to its next tier, such as "Next: Chef · Silver, 7 to go (43 of 50 meals cooked)". A guest's App Home has no badge section, though a guest's badges are still announced in the channel. If the section fails to load, Home still draws without it.

## Changing the counts

The tier counts are a household setting, `badge_thresholds`. An admin changes them on the **Badges** tab of **Members**:

- Set the five counts for each ladder, Frequent, Occasional, and Rare. Each is a whole number from 1 to 10,000, higher than the one before.
- Or select **Reset to defaults**.

Saving writes an audit row; saving the form unchanged does nothing. Raising a count takes nothing away. Lowering one quietly awards the tier to anyone who already has that count. A single list, from before there were three ladders, is read as the Frequent ladder.

## Announcements

A badge earned when a meal is done is announced in the channel. On every scheduler tick, every 30 seconds, Dinnerbell posts pending badges and marks them announced in one transaction, so a restart, a retry, or two ticks at once never post one twice, and a crash never loses one.

- **One post for each done call.** Everything the call earned goes in one message, with each member mentioned once, in name order. It never compares people. A post names at most 10 members and 6 badges each, and counts the rest ("and 3 more members earned badges too"). A badge earned at several tiers at once shows its highest.
- **Food Critic gets no post.** It's earned when a member gives feedback, often days after the call, so a post would interrupt the channel each time. It shows everywhere else: Stats, App Home, the monthly recap, and the household's newest badges. A badge says how many times someone gave feedback, never what they said.
- **Some badges are never announced:** badges earned by catching up (the backfills and a lowered count), and badges of a member who left. Food Critic awards made while Food Critic was private aren't announced either, and stay out of the recap and the newest badges.
- **A lost post stays lost.** A badge counts as announced when its post is queued. If Slack refuses the post for good, or it waits more than 24 hours, such as during a long Slack outage or with a bad token, the post is never sent. The log shows `notification expired` and `notification delivery gave up`, and `/healthz` counts it in `outbox.failed`.
- **On the test instance,** posts go to the test channel, and a member who isn't in it appears as a plain name instead of a mention (ADR 0006). `scrub-test` marks every pending badge so that it's never announced, so a restored copy of production announces nothing production hadn't.

Each award records when it was earned and its announcement state: `pending`, `announced`, or `skipped` (never announced).

## The monthly recap

On the first day of each month, from 9 a.m. household time, Dinnerbell posts the month before: how many meals you ate, the cuisines (and any you had never eaten), new catalog items, a thank-you, and the badges earned. The thank-you lists who cooked, picked up, ordered, and coordinated, each in name order, with no counts, so nobody can rank by eye; the only numbers are household totals. It also thanks everyone who answered a call or pulled up a chair. It mentions no one, and names only active members.

- The recap posts once per month, however often Dinnerbell restarts. The household setting `recap_posted` holds the last month recapped, such as `2026-10`; a value that isn't a `YYYY-MM` month, or is later than the current month, counts as none.
- If Dinnerbell was down on the first, the recap posts on its next tick, up to the seventh. After that, the month is skipped. Only the month just ended is ever recapped.
- A month with no meals posts nothing, but still counts as recapped.
- Like badge posts, a recap counts as posted when it's queued, so one that waits more than 24 hours for Slack is lost.
- If the household time zone isn't valid, the recap stays off, and Dinnerbell logs one warning.
- On the test instance, the recap posts to the test channel. `scrub-test` marks the latest due month as posted, so a restored copy stays quiet until the next month's first, when it recaps the month from the production history it was restored with.

## Badges for meals before the upgrade

<a id="the-first-start-after-the-upgrade"></a>The first start of a version with badges (database revision `0016_contributions`) counts the done calls and feedback already in your log, once, using members' current roles. It awards every badge those counts reach without announcing them or listing them as new. It works in batches of 200 calls and records `contributions_backfilled` in the household settings last, so a start that stops halfway finishes the rest next time.

Later additions catch up the same way, once each, without announcements:

- **Gambler:** a household that already ran the first count gets one catch-up, recorded as `gambler_backfilled`. It changes no other count, and a start that stops halfway finishes next time.
- **Three ladders and the grace week:** the tiers they put in reach, recorded as `badge_ladders_awarded`.

On every start, Dinnerbell also counts any done call that has no counts, which happens only if counting failed when the call was marked done; marking it done is never blocked by counting. Those badges aren't announced either.

`dinnerbell reset-history` deletes the counts and the badges earned from them, along with the history. The badges don't come back.

## Privacy

A member sees their own counts and progress. The household sees totals and the newest badges, with names, because badges are announced in the channel. No view ranks members or orders them by a count. Nothing here holds anyone's preferences or what they said in feedback.

## Logs

Counting writes `contributions_recorded` and `badge_earned` lines with the member ID and display name, the badge and tier, and the call. They never carry emails or anything a member wrote.
