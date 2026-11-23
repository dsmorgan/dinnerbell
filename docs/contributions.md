# Contributions and badges

Dinnerbell counts what each person does to get a meal on the table, and awards a badge when a count reaches a tier. The rules are in [spec section 12](spec.md#12-metrics-and-badges).

## What counts

A call or offer counts the moment it's done. Each person gets one count per kind for it:

- **Cooked, picked up, ordered, coordinated:** whoever the meal assigned, guests included. On a pickup, ordering and picking up count separately, so one person who does both gets both counts. If the coordinator switches a pickup to delivery, whoever was picking up gets no pickup count.
- **At the table:** everyone who was in, including a late joiner who is taking part.
- **Answered quickly:** any answer within 15 minutes of a call opening, but not a late join or an Out your away days gave.
- **Ideas:** the person whose proposal won (**Bright Idea**), and again if it was a place or cuisine new to the household (**Explorer**); and the person who added a catalog item, the first time a call chose it (**Scout**). If the coordinator changed the place after the decision, these go by the place they ate at. A new name typed in for the change earns nobody an idea count.
- **Hosting:** the member who invited a guest who was in, or brought guests to an offer.
- **Leaving the choice to others (Gambler):** on a call with at least three proposals, a member who was in, not as a late joiner, and whose votes, when the call was done, were for every proposal or for none. Voting for none counts even when nobody voted. Votes you took back don't count.
- **Feedback:** the first time you give feedback on a meal. Changing it, or giving it again, doesn't count again.

Only members earn the at-the-table, answered-quickly, idea, hosting, Gambler, and feedback counts.

A guest without Slack, whom the inviting member answers for, earns no badges, because they can't see them. Their counts still go toward the household's totals and the monthly recap. When an admin links them to Slack or makes them a member, they get the badges those counts reach, without a post.

A call counts once, even if two people tap **Mark done** together. Called-off and discarded calls, and meals logged by hand, count for nothing.

### When an admin corrects a done meal

When an admin [corrects](coordinating.md#what-an-admin-can-do-beyond-that) who cooked, picked up, or ordered after the meal is done, the count moves to the person who has the role now, who earns the badges it reaches, announced as usual. The person who lost it loses each tier their counts reached before the correction and no longer reach, even one already announced, and even one first earned at a lower count before an admin raised it. A badge taken back before it was announced never is. One already announced leaves Stats, App Home, the household's newest badges, and future recaps; a post Slack already has stays. Correcting it back moves the count and its badges back.

When an admin corrects the place, the meal's counts follow the same way. Cooked, picked up, and ordered follow the roles the new way has, so a way without a role takes that count away. The idea counts go by the corrected place. Other meals keep their counts, even if the correction changes how new their place was, so a correction never costs someone a badge for a meal it didn't touch.

## Badges and tiers

Each of the 13 badges has five tiers, **First**, **Bronze**, **Silver**, **Gold**, and **Diamond**, and climbs one of three ladders:

| Ladder | Counts | Badges |
|---|---|---|
| Frequent | 1, 10, 50, 100, 200 | Chef, Road Runner, Order Up, Quarterback, Full Table, Early Bird, Food Critic, Gambler |
| Occasional | 1, 5, 20, 50, 100 | Bright Idea, Good Host |
| Rare | 1, 3, 10, 25, 50 | Scout, Explorer, On a Roll |

Each person earns a badge tier once, unless a correction takes it back. **On a Roll** counts weeks in a run, Monday to Sunday in your time zone, with at least one meal cooked, picked up, or ordered. A run survives one missed week, which doesn't count toward it; two missed weeks in a row end it.

## The badge art

Each badge is a tier frame with its own icon, such as a chef hat for Chef or a compass for Explorer. The tiers stay distinct in grayscale, and every page also names the badge and tier in text. On the web, Gold and Diamond sparkle, except under "reduce motion".

<a id="badge-art-in-slack"></a><a id="the-composite-picture-on-app-home"></a>In Slack, the art shows as images. Without the `files:write` scope, or when Slack refuses the art, Dinnerbell shows text tags such as 🥇 Chef · Gold instead. [badge-internals.md](badge-internals.md) explains how the art is drawn, uploaded, and cached.

## The Stats page

**Stats** in the navigation shows each badge at your tier, with progress to the next ("Meals cooked: 63 of 100 for Gold"), the badges you earned in the last three days, and your counts. Badges you haven't earned yet show softly, with the count that earns the first tier. Members also have a **Household** tab with household totals and the newest badges, with who earned them and when. Nobody is ranked, and nobody sees another member's counts.

A guest sees **Your badges** only: the badges for cooking, picking up, and ordering, On a Roll, and Quarterback if they earned it before guests stopped coordinating.

## App Home

Each member's App Home has a short **Your badges** section with their badges at their highest tiers, and the badge closest to its next tier, such as "Next: Chef · Silver, 7 to go (43 of 50 meals cooked)". A guest's App Home has no badge section, though a registered guest's badges are still announced in the channel.

## Changing the counts

An admin changes the tier counts on the **Badges** tab of **Admin**: five whole numbers per ladder, from 1 to 10,000, each higher than the one before. **Reset to defaults** asks first, then restores the table above. Raising a count takes nothing away, and lowering one quietly awards the tier to anyone who already has that count.

## Announcements

When a meal is done, Dinnerbell announces the badges it earned in the channel, within about 30 seconds, never twice.

- **One post for each done call,** with each member mentioned once, in name order. It never compares people. A post names at most 10 members and 6 badges each, and counts the rest. A badge earned at several tiers at once shows its highest.
- **The rarest badges come first:** highest tier first, then by ladder (Rare, Occasional, Frequent). When a list is cut short, the badges left off are the most common ones.
- **Food Critic gets no post,** because it's earned when a member gives feedback, often days later. It shows everywhere else. A badge says how many times someone gave feedback, never what they said.
- **Some badges are never announced:** badges earned by catching up, such as after an upgrade or a lowered count, and badges of a member who left.
- **A lost post stays lost.** If Slack refuses a post for good, or it waits more than 24 hours, such as during a long Slack outage, it's never sent. [troubleshooting.md](troubleshooting.md) shows how to spot one.
- **On the test instance,** posts go to the test channel. A restored copy of production announces nothing production hadn't.

## The monthly recap

On the first day of each month, from 9 a.m. household time, Dinnerbell posts the month before: how many meals you ate, the cuisines (and any new ones), new catalog items, a thank-you, and the badges earned. The thank-you lists who cooked, picked up, ordered, and coordinated, in name order and without counts, and thanks everyone who answered a call or pulled up a chair. It mentions no one, and names only active members. Badges are in the same order as in badge posts, as icons when Slack has the art and as text otherwise.

- The recap posts once a month. If Dinnerbell was down on the first, it posts once it's back, up to the seventh; after that, the month is skipped.
- A month with no meals posts nothing.
- Like badge posts, a recap that waits more than 24 hours for Slack is lost.
- If the household time zone isn't valid, the recap stays off, and Dinnerbell logs a warning.
- A restored copy on the test instance stays quiet until the next month's first, when it recaps the month from production's history.

## Badges for meals before the upgrade

<a id="the-first-start-after-the-upgrade"></a>The first start of a version with badges counts the done calls and feedback already in your log, once, using members' current roles. It awards every badge those counts reach without announcing them or listing them as new. Later badges and ladders catch up the same way. [badge-internals.md](badge-internals.md#backfills) has the details.

`dinnerbell reset-history` deletes the counts and the badges earned from them, along with the history. The badges don't come back.

## Privacy

A member sees their own counts and progress. The household sees totals and the newest badges, with names, because badges are announced in the channel. No view ranks members or orders them by a count. Nothing here holds anyone's preferences or what they said in feedback, and the log lines for counts and badges carry member IDs and display names, never emails or anything a member wrote.
