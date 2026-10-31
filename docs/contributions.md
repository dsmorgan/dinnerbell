# Contributions and badges

Dinnerbell counts what each person does to get a meal on the table, and awards a badge when a count reaches a tier. This page explains what counts, how badges are announced, and how to change the tiers. The rules are in [spec section 12](spec.md#12-metrics-and-badges).

## What counts

A call or offer counts the moment it's done. Each person gets one count per kind for it:

- **Cooked, picked up, ordered, coordinated:** whoever the meal assigned, guests included.
- **At the table:** everyone who was in, including a late joiner who is taking part.
- **Answered quickly:** any answer within 15 minutes of the call opening.
- **Ideas:** the person whose proposal won; the person whose proposal was new to the household; and the person who added a catalog item the first time it was chosen.
- **Hosting:** the member who brought guests.
- **Leaving the choice to others (Gambler):** on a call with at least three proposals, a member who was in, not as a late joiner, and whose votes, when the call was done, were for every proposal or for none. Voting for none counts even when nobody voted. Votes you took back don't count, and offers have no proposals to vote on.
- **Feedback:** the first time you give feedback on a meal. Changing it, or taking it back and giving it again, doesn't count again.

Only members earn the at-the-table, answered-quickly, idea, hosting, Gambler, and feedback counts: a guest doesn't choose the meal or give feedback.

A call counts once, even if two people tap **Mark done** together. Called-off and discarded calls, and meals logged by hand, count for nothing.

## Badges and tiers

Each of the 13 badges has five tiers, **First**, **Bronze**, **Silver**, **Gold**, and **Diamond**, and climbs one of three ladders:

| Ladder | Counts | Badges |
|---|---|---|
| Frequent | 1, 10, 50, 100, 200 | Full Table, Early Bird, Chef, Food Critic, Quarterback, Gambler, Road Runner, Order Up |
| Occasional | 1, 5, 20, 50, 100 | Bright Idea, Good Host |
| Rare | 1, 3, 10, 25, 50 | On a Roll, Scout, Explorer |

Each person earns a badge tier once. **On a Roll** counts weeks in a run, Monday to Sunday in your time zone, with at least one meal cooked, picked up, or ordered. A run survives one missed week, which doesn't count toward it; two missed weeks in a row end it.

## The badge art

Each badge is a tier frame with its own icon: Chef (chef hat), Road Runner (car), Order Up (takeout bag), Quarterback (dinner bell), Full Table (fork and knife), Early Bird (alarm clock), Bright Idea (lightbulb), Scout (recipe card), Explorer (compass), Good Host (extra chair), Food Critic (star), On a Roll (flame), and Gambler (a single die). First is a teal ribbon rosette; Bronze, Silver, and Gold are medals on a navy strap; and Diamond is a faceted gem, so the tiers stay distinct in grayscale. On the web, Gold and Diamond sparkle, except under "reduce motion", and every page also names the badge and tier in text.

<a id="badge-art-in-slack"></a><a id="the-composite-picture-on-app-home"></a>In Slack, the art shows as images. Without the `files:write` scope, or when Slack refuses the art, Dinnerbell shows text tags such as 🥇 Chef · Gold instead. [badge-internals.md](badge-internals.md) explains how the art is drawn, uploaded, and cached.

## The Stats page

**Stats** in the navigation shows each badge at your tier, with progress to the next ("Meals cooked: 63 of 100 for Gold"), the badges you earned in the last two weeks, and your counts. Badges you haven't earned yet show softly, with the count that earns the first tier. Members also see household totals, and the newest badges with who earned them. Nobody is ranked, and nobody sees another member's counts.

A guest sees **Your badges** only: the four pitching-in badges and On a Roll, with no household section.

## App Home

Each member's App Home has a short **Your badges** section with their own badges at their highest tiers, as art or text tags, and the badge closest to its next tier, such as "Next: Chef · Silver, 7 to go (43 of 50 meals cooked)". A guest's App Home has no badge section, though a guest's badges are still announced in the channel.

## Changing the counts

An admin changes the tier counts on the **Badges** tab of **Admin**: five whole numbers per ladder, from 1 to 10,000, each higher than the one before. **Reset to defaults** restores the table above. Each change writes an audit row. Raising a count takes nothing away, and lowering one quietly awards the tier to anyone who already has that count.

## Announcements

When a meal is done, Dinnerbell announces the badges it earned in the channel, within about 30 seconds. A restart or a retry never posts a badge twice.

- **One post for each done call.** Everything the call earned goes in one message, with each member mentioned once, in name order. It never compares people. A post names at most 10 members and 6 badges each, and counts the rest ("and 3 more members earned badges too"). A badge earned at several tiers at once shows its highest.
- **Food Critic gets no post.** It's earned when a member gives feedback, often days after the call, so a post would interrupt the channel each time. It shows everywhere else. A badge says how many times someone gave feedback, never what they said.
- **Some badges are never announced:** badges earned by catching up, such as after an upgrade or a lowered count, and badges of a member who left.
- **A lost post stays lost.** If Slack refuses a post for good, or it waits more than 24 hours, such as during a long Slack outage or with a bad token, it's never sent. The log shows `notification delivery gave up`, and `/healthz` counts it in `outbox.failed`.
- **On the test instance,** posts go to the test channel, and a member who isn't in it appears as a plain name. A restored copy of production announces nothing production hadn't.

## The monthly recap

On the first day of each month, from 9 a.m. household time, Dinnerbell posts the month before: how many meals you ate, the cuisines (and any new ones), new catalog items, a thank-you, and the badges earned. The thank-you lists who cooked, picked up, ordered, and coordinated, in name order and without counts, so nobody can rank by eye, and thanks everyone who answered a call or pulled up a chair. It mentions no one, and names only active members.

- The recap posts once a month, however often Dinnerbell restarts. If Dinnerbell was down on the first, it posts on the next start, up to the seventh; after that, the month is skipped.
- A month with no meals posts nothing.
- Like badge posts, a recap that waits more than 24 hours for Slack is lost.
- If the household time zone isn't valid, the recap stays off, and Dinnerbell logs a warning.
- A restored copy on the test instance stays quiet until the next month's first, when it recaps the month from production's history.

## Badges for meals before the upgrade

<a id="the-first-start-after-the-upgrade"></a>The first start of a version with badges counts the done calls and feedback already in your log, once, using members' current roles. It awards every badge those counts reach without announcing them or listing them as new. Later badges and ladders catch up the same way. [badge-internals.md](badge-internals.md#backfills) has the details.

`dinnerbell reset-history` deletes the counts and the badges earned from them, along with the history. The badges don't come back.

## Privacy

A member sees their own counts and progress. The household sees totals and the newest badges, with names, because badges are announced in the channel. No view ranks members or orders them by a count. Nothing here holds anyone's preferences or what they said in feedback.

## Logs

Counting writes `contributions_recorded` and `badge_earned` lines with the member ID and display name, the badge and tier, and the call. They never carry emails or anything a member wrote.
