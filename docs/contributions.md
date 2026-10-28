# Contributions and badges

Dinnerbell counts what each person does to get a meal on the table, and awards a badge when the count reaches a tier. This page explains what counts, when, and how to change the tiers. The rules are in [spec section 12](spec.md#12-metrics-and-badges).

## What counts

A call or offer counts the moment it is done. Each person gets one count per kind for it:

- **Cooked, picked up, ordered, coordinated.** Whoever the meal assigned. Guests count here: a guest who picked up picked up.
- **At the table.** Everyone who was in, including a late joiner who is taking part.
- **Answered quickly.** An answer, any answer, within 15 minutes of the call opening.
- **Ideas.** The person whose pick won; the person whose pick was new to the household; and the person who added a catalog item the first time it was chosen.
- **Hosting.** The member who brought guests.
- **Leaving the choice to others.** On a call with at least three picks, a member who was in (not a late joiner) and whose votes, when the call was done, were every pick or none of them. Voting for none counts only when another member voted, so someone else decided; a call nobody voted on gives no one the badge. A guest's votes are ignored. Votes you took back don't count. A late joiner, a guest, and a member who was out or didn't answer don't earn it, and offers have no picks to vote on.
- **Feedback.** The first time you give feedback on a meal. Changing it, or taking it back and giving it again, doesn't count again.

Finishing a call twice, or two people tapping **Mark done** together, counts it once. A call that is called off or discarded counts for nothing. Meals someone logged by hand don't count: they have no roles or answers.

Only members earn the "at the table", "answered quickly", idea, hosting, feedback, and Gambler counts. A guest doesn't choose the meal or give feedback.

## Badges and tiers

Thirteen badges share one ladder: **First** (1), **Bronze** (10), **Silver** (50), **Gold** (100), **Diamond** (200). A badge tier is earned once per person. **On a Roll** counts weeks in a row, Monday to Sunday in your time zone, with at least one cooked, picked up, or ordered meal.

The counts are a household setting. The admin changes them with `badge_thresholds` in the household settings, five whole numbers that rise from 1 to 10,000. Raising a count takes nothing away. Lowering one awards the tiers people already have the counts for, without an announcement.

Gambler arrived after the first backfill, so a household that already ran it gets one catch-up: on the next start, Dinnerbell counts Gambler for every done call, awards the tiers it reaches as `skipped` (never announced), and records `gambler_backfilled` in the household settings. It changes no other count, and a start that stops halfway runs the rest next time. A new household's first backfill counts Gambler with everything else.

## The badge art

Each badge is a tier frame plus its own icon: Chef (chef hat), Road Runner (car), Order Up (takeout bag), Quarterback (dinner bell), Full Table (fork and knife), Early Bird (alarm clock), Bright Idea (lightbulb), Scout (recipe card), Explorer (compass), Good Host (extra chair), Food Critic (star), On a Roll (flame), Gambler (a single die). Tiers differ in outline and lightness, so they stay distinct in grayscale: First is a teal ribbon rosette, Bronze, Silver and Gold are medals on a navy strap, and Diamond is a faceted gem. Gold and Diamond sparkle on the web: twinkling four-point stars and a sheen that sweeps every few seconds. The sparkle is CSS, and it stops under "reduce motion". Every page also writes the badge's name and tier in text.

The art lives in `services/badge_art.py`, and pages draw it with the `badge_art`, `badge_chip`, and `badge_row` macros in `web/templates/_badge.html`. `src/dinnerbell/web/static/badges/` holds the same art as plain files: `icons/<badge>.svg` (13) and `frames/<tier>.svg` (5, with the stars drawn in). A test checks that the files match the code and pass the SVG allowlist.

### Badge art in Slack

Slack shows the same art as PNGs. `badge_art.png_for(key, tier)` draws `standalone_svg(key, tier)` (one still SVG: frame, icon, and stars) at 240 by 280 pixels with [resvg](https://github.com/RazrFalcon/resvg), through the `resvg-py` wheel, which carries the renderer and needs no system library. The badge keys (`chef`, `road_runner`, ...) and tier values (`first` ... `diamond`) are the stable names.

`services/badge_files.py` uploads a tier's PNG the first time a message or view wants it (`files.upload`, shared nowhere) and remembers the Slack file ID and a SHA-256 of the PNG in the `slack_badge_file` table. Restarts reuse the IDs; art that draws differently hashes differently, so it uploads again. Two requests for the same new tier share one upload. `BadgeFiles` takes the art from an `ArtSource` (a badge key and a tier to PNG bytes), and `png_for` is the only one today: another source needs no change to the upload or the cache.

Thirty to forty seconds after Slack connects, Dinnerbell uploads the art for every other tier in the background (the **prewarm**, #112): all 60 files, one at a time, 2 seconds apart, so nothing waits for Slack to process a file at the first announcement. It skips files already cached, so a restart picks up where the last run stopped, and it logs `badge art prewarmed` once when every tier is in. It stops after the same failure rules as first use: a missing `files:write` scope logs one `badge art not available` line and ends the run; any other failure waits out the 15-minute pause and tries again, up to three failures in a row. It never delays startup or serving, and a first use still uploads what's missing. A development run (`create_app` without `prewarm_badge_art`) never prewarms.

The badge announcement and **Your badges** on App Home reference the files in Slack image elements. Food Critic is never uploaded or shown as art. A member with more than three badges (`badge_text.THUMBNAIL_CUTOFF`) in one announcement gets one line naming them and a compact row of small icons for every badge they earned, in context blocks of up to 10 images (#114). With icons shown, the line has the plain names, with no tier emoji, because the icons show the tiers ("<@member> earned 8 badges: *Chef*, *Scout*, ..."); a badge without art keeps its tag. The text-only post is unchanged: its line names 6 badges and counts the rest ("and 2 more badges"). The post holds the 50 blocks Slack allows: 3 are fixed, and each of at most 10 members takes at most 3, so it never passes 33. Members with three or fewer keep a thumbnail row for each badge. Each member is laid out on their own, so a post can mix the two. If an upload fails or the app lacks the `files:write` scope, Dinnerbell logs one `badge art not available` line, shows the text tags as before, uploads nothing new for 15 minutes (files already uploaded still show), and never fails a post or a Home view. A post or Home view waits at most 3 seconds for a new upload, then shows text. If Slack refuses blocks and any file was uploaded in the same call (Slack may not have finished processing it), Dinnerbell waits 1.5 seconds and tries again, then 3 seconds and tries once more, leaving the cache alone; only a first post with new art that Slack refuses takes up to about 4.5 seconds longer. If the last try is refused too, Dinnerbell forgets the files, shows the text, and uploads nothing new for 15 minutes, so later posts don't pay the retry pauses again. If no file was new, such as one deleted in Slack, it forgets the cached files and shows the text at once; other refusals leave the cache alone. App Home works the same way. Each refusal logs one `slack refused badge art` line with `slack_error` (Slack's code) and `slack_messages` (Slack's `response_metadata.messages`, cut to 300 characters).

### The composite picture on App Home

**Your badges** can show one picture of everything a member holds (#114): each badge at its highest tier, in `BADGES` order, never Food Critic (it stays a text tag), in a grid of 7 to a row (13 badges make two rows) with cells about 64 pixels wide, small gaps, and a transparent background. `badge_art.composite_png(held)` draws it: each badge is the same `standalone_svg` drawing, nested in its cell, rendered by resvg at twice the size for sharp screens. `BadgeFiles` takes it from a `CompositeSource` (the held badges to PNG bytes), the same seam as `ArtSource`; `composite_png` is the only one today.

The `slack_badge_composite` table keeps one row per member: the Slack file ID, when it was uploaded, and a SHA-256 of the PNG. The hash covers the badges, their tiers, and the art, because the PNG does, so a new tier or a new art source draws a new picture, and the member's one row is replaced. Dinnerbell makes a picture in the background, never in the path of what a member waits for: right after a badge post goes out (for each member it mentions), and when a member opens Home and theirs is missing or out of date. It draws one member at a time, gives up on one after 60 seconds, shares the 15-minute pause after a failure with the art uploads, and never fails or delays a post or a Home view. Old files aren't deleted from Slack.

Home shows the picture as an image block, with alt text that lists the badge names, only when it has been in Slack for at least 30 seconds (`badge_files.COMPOSITE_SETTLE`), so a first view never has an image Slack hasn't processed. Until then it shows the compact rows of small icons, and with no art at all, the text tags. If Slack refuses the view, the same retry and forget rules as for the art apply, and a refused picture is forgotten so the next view makes it again. A guest, or a member who left or was deactivated, gets no new picture; an old row is never shown, because Home reads only the signed-in member's own. `reset-history` clears the table (it is drawn from the badges that reset deletes; a row left behind would describe badges nobody holds), and the next award or Home view draws a picture again.

When Home shows the badge's icon in the "Next" line, it leaves the tier emoji off the name; without art it keeps the emoji.

`badge_text.art_element` is the one place that decides how art is referenced. If Slack won't show a file that no channel shares (#110, hypothesis 2), change it to an `image_url` that points at a public PNG route (see #110 for the design: `GET /badge-art/{key}/{tier}.png`, no sign-in, immutable caching, only a valid key and tier, and a Pangolin bypass for that one path). Old files aren't deleted from Slack when art changes, and a `resvg-py` upgrade that draws differently uploads every tier again.

## The Stats page

**Stats** in the navigation opens `/stats`. You see each badge at your tier, with progress to the next ("Meals cooked: 63 of 100 for Gold"), the badges you earned in the last two weeks, and your counts. Badges you haven't earned yet show softly, with the count that earns the first tier. Members also see the household: totals, and the newest badges with the names of who earned them, newest first. Badges earned by catching up are held but never listed as new. Nobody is ranked, and one member never sees another's counts.

A guest sees **Your badges**: only the four pitching-in badges and On a Roll, and no household section.

## Changing the counts

Admins open **Admin > Badges** and set the five tier counts. Each must be a whole number from 1 to 10,000, and each higher than the one before. Saving writes an audit row; saving the form unchanged does nothing. Lowering a count awards the tier to anyone who already has that count, quietly. Raising one takes nothing away.

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

Each member's App Home has a short "Your badges" section: the badges they hold, each with its highest tier as a text tag (or, with art, as small icons in compact rows of 10, with a link to **Stats** for the names, or as one picture once it is ready: see [the composite picture](#the-composite-picture-on-app-home)), and the badge closest to its next tier ("Next: Chef · Silver, 7 to go (43 of 50 meals cooked)"). It shows only their own. A guest's App Home has no badge section (a guest's badges are still announced in the channel). If reading it fails, Home still draws without it.

## The first start after the upgrade

The first start after the upgrade (revision `0016_contributions`) counts the meals already in your log, once. It adds up the done calls and the feedback you already gave, and awards every badge those counts reach as `skipped`, so the channel doesn't hear about old meals, and the Stats page doesn't list them as new. It uses members' current roles, so someone who was a guest then and is a member now counts as a member. It works in batches of 200 calls and writes `contributions_backfilled` in the household settings last, so a start that is stopped halfway finishes the rest next time.

On every later start, Dinnerbell also counts any done call that has no counts. That can only happen if counting failed while the call was being marked done; marking it done is never blocked by that. A call counted late this way earns its badges as `skipped`.

`dinnerbell reset-history` deletes the counts and the badges earned from them, along with the history, and the pictures of those badges on App Home. The badges don't come back.

## Privacy

A member sees their own counts and progress. The household sees totals and the newest badges, with names, because badges are announced in the channel. No view ranks members or orders them by a count, and nothing here holds anyone's preferences or what they said in feedback; a count says only that feedback was given.

## Logs

Counting writes `contributions_recorded` and `badge_earned` lines (member ID and display name, the badge and tier, the call). They never carry emails or anything a member wrote.
