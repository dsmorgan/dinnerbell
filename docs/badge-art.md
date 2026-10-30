# Badge art internals

This page is for maintainers. It explains how Dinnerbell draws badge art and shows it in Slack. [contributions.md](contributions.md) describes the badges themselves.

## On the web

The art lives in `services/badge_art.py`. Pages draw it with the `badge_art`, `badge_chip`, and `badge_row` macros in `web/templates/_badge.html`. The Gold and Diamond sparkle (twinkling four-point stars and a sheen that sweeps every few seconds) is CSS.

`src/dinnerbell/web/static/badges/` holds the same art as plain files: `icons/<badge>.svg` (13) and `frames/<tier>.svg` (5, with the stars drawn in). A test checks that the files match the code and pass the SVG allowlist.

## PNGs for Slack

Slack shows the art as PNGs. `badge_art.png_for(key, tier)` draws `standalone_svg(key, tier)`, one still SVG of frame, icon, and stars, at 240 by 280 pixels with [resvg](https://github.com/RazrFalcon/resvg), through the `resvg-py` wheel, which needs no system library. The badge keys (`chef`, `road_runner`, ...) and tier values (`first` ... `diamond`) are the stable names.

`services/badge_files.py` uploads a tier's PNG the first time a message or view needs it (`files.upload`, shared nowhere), and stores the Slack file ID and a SHA-256 of the PNG in the `slack_badge_file` table. Restarts reuse the IDs. Art that draws differently hashes differently, so it uploads again; a `resvg-py` upgrade that changes the drawing uploads every tier again. Two requests for the same new tier share one upload. Old files are never deleted from Slack.

`BadgeFiles` takes the art from an `ArtSource` (badge key and tier to PNG bytes). `png_for` is the only source, and another source needs no change to the upload or the cache.

### Prewarm

30 to 40 seconds after Slack connects, Dinnerbell uploads the art for every tier in the background (#112): all 65 files, one at a time, 2 seconds apart, so the first announcement doesn't wait for Slack to process a file. It skips cached files, so a restart picks up where the last run stopped, and logs `badge art prewarmed` once every tier is in. A missing `files:write` scope logs one `badge art not available` line and ends the run. Any other failure waits out the 15-minute pause and tries again, up to three failures in a row. The prewarm never delays startup or serving, and a first use still uploads anything missing. A development run (`create_app` without `prewarm_badge_art`) never prewarms.

## In announcements and App Home

The badge announcement and **Your badges** on App Home reference the files in Slack image elements:

- A member with more than three badges (`badge_text.THUMBNAIL_CUTOFF`) in one announcement gets one line naming them, with plain names and no tier emoji ("<@member> earned 8 badges: *Chef*, *Scout*, ..."), and a compact row of small icons, in context blocks of up to 10 images (#114). A badge without art keeps its tag.
- A member with three or fewer gets a thumbnail row for each badge.
- Each member is laid out on their own, so one post can mix the two. The text-only post names 6 badges and counts the rest ("and 2 more badges").
- A post stays within Slack's 50 blocks: 3 are fixed, and each of at most 10 members takes at most 3, so it never passes 33.
- When Home shows a badge's icon in the "Next" line, it leaves the tier emoji off the name.

`badge_text.art_element` is the one place that decides how art is referenced. If Slack won't show a file that no channel shares (#110, hypothesis 2), change it to an `image_url` for a public PNG route. #110 has the design: `GET /badge-art/{key}/{tier}.png`, no sign-in, immutable caching, only valid keys and tiers, and a Pangolin bypass for that path.

### Failures

Art never fails or blocks a post or a Home view:

- A post or Home view waits at most 3 seconds for a new upload, then shows text.
- If an upload fails or the app lacks `files:write`, Dinnerbell logs one `badge art not available` line, shows text tags, and uploads nothing new for 15 minutes. Files already uploaded still show.
- If Slack refuses blocks and a file was uploaded in the same call, Slack may not have processed it yet, so Dinnerbell waits 1.5 seconds and tries again, then 3 seconds and tries once more, keeping the cache. Only a first post with new art that Slack refuses takes up to about 4.5 seconds longer. If the last try is refused too, Dinnerbell forgets the files, shows text, and uploads nothing new for 15 minutes.
- If no file was new, such as when one was deleted in Slack, Dinnerbell forgets the cached files and shows text at once. Other refusals keep the cache.
- Each refusal logs one `slack refused badge art` line, with `slack_error` (Slack's code) and `slack_messages` (Slack's `response_metadata.messages`, cut to 300 characters).

App Home follows the same rules.

## The composite picture on App Home

**Your badges** on App Home can show one picture of everything a member holds (#114): each badge at its highest tier, in `BADGES` order, 7 to a row (at most 13 badges, so two rows), in cells about 64 pixels wide with small gaps and a transparent background. `badge_art.composite_png(held)` nests each badge's `standalone_svg` in its cell, and resvg renders it at twice the size for sharp screens. `BadgeFiles` takes it from a `CompositeSource` (held badges to PNG bytes), the same seam as `ArtSource`.

The `slack_badge_composite` table keeps one row per member: the Slack file ID, the upload time, and a SHA-256 of the PNG. The hash covers the badges, their tiers, and the art, so a new tier or new art draws a new picture and replaces the member's row. `reset-history` keeps the table; a stale row is never shown, because Home compares its hash with the badges the member holds now.

Dinnerbell draws a picture in the background, never while a member waits: right after a badge post goes out, for each member it mentions, and when a member opens Home and theirs is missing or out of date. It draws one member at a time, gives up on one after 60 seconds, and shares the 15-minute pause after a failure with the art uploads.

Home shows the picture as an image block, with alt text listing the badge names, only once it has been in Slack for 30 seconds (`badge_files.COMPOSITE_SETTLE`), so a first view never shows an image Slack hasn't processed. Until then it shows the compact icon rows, or text tags with no art. If Slack refuses the view, the same retry and forget rules apply, and a refused picture is drawn again on the next view. A guest, or a member who left or was deactivated, gets no new picture, and Home reads only the signed-in member's own row.
