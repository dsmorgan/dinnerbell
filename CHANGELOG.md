# Changelog

Dinnerbell follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/). Before version 1.0, a minor release can change configuration or behavior; its **Upgrade notes** say what to do.

Every release lists **Upgrade notes**. Read them before you upgrade, and always back up first (see "Upgrade" in [docs/setup.md](docs/setup.md)).

## [Unreleased]

### Security

- The image build now uses only locked, hashed inputs. The build backend (`hatchling` and its dependencies) used to come from PyPI at build time, unlocked and unhashed. It is now a `build` dependency group in `uv.lock`; the Containerfile installs it with `--require-hashes` into a throwaway venv and builds the wheel with `--no-build-isolation --offline`. The backend never reaches the final image.
- The final image no longer contains pip, which clears the pip advisories Grype reported. The app runs from its own virtual environment and never used it. The base image moves to the current Python 3.12.16 digest.
- The Slack manifests no longer request the unused `channels:history` scope, so a stolen bot token can't read channel history.
- `compose.yaml` (and so the test instance) now sets `pids_limit: 256`, `mem_limit: 512m`, and rotated `json-file` logs (10 MB, 3 files), and `scripts/check-compose.sh` checks them. See "Resource and log limits" in docs/setup.md for what each podman-compose version honors.
- A test now compares the vendored htmx file with the SHA-256 of the npm `htmx.org@2.0.11` release, so a modified file fails CI. `CONTRIBUTING.md` says how to update it.
- The promotion checklist and "Verify an image" now verify the attestation against the pulled image's digest, and docs/setup.md and docs/hardening.md say to pin production to that digest or the version tag, and that the test instance's `main` tag moves.
- The four secrets (`DINNERBELL_SECRET_KEY`, `DINNERBELL_OIDC_CLIENT_SECRET`, `DINNERBELL_SLACK_BOT_TOKEN`, and `DINNERBELL_SLACK_APP_TOKEN`) are now files in `secrets/` (test: `secrets-test/`) that compose mounts at `/run/secrets`, and no longer lines in `.env`. podman-compose passed every `.env` line to podman as `-e NAME=value`, so while podman created the container, each secret was on its command line, visible to other local users in `ps` and printed by `podman-compose --dry-run` and `--verbose` (#35). Dinnerbell, `deploy/dinnerbell-test`, its shared-secret check, and `dinnerbell config check` (new option `--secrets-dir`) all read the files. A missing, unreadable, directory, multi-line, or oversized (over 8 KiB) secret file, or a broken symlink, stops startup with a message that names the file. The new `deploy/check-secrets` checks the four files before `up` and prints no value. `secrets/` and `secrets-test/` are git-ignored, left out of the image build context, and `scripts/check-compose.sh` (run in CI) fails when git tracks anything there except the `*.example` placeholders.

### Changed

- Gambler now counts a member who voted for none of the picks even when no one else voted, so a call with a lone voter, only guests voting, or no votes (a lock or an automatic decision) gives it to every member who was in. The other conditions are unchanged: at least three picks, and a late joiner, a guest, and a member who was out or didn't answer don't earn it. Voting for every pick is unchanged (#125). Calls already counted keep their counts.
- The nightly release scan no longer fails when it runs between the publish job and `gh release create`: it accepts `:latest` one release ahead of the newest GitHub release when that release's tag exists, has no release yet, and the image was built within two hours. Any other mismatch still fails.
- The `help`, `prefs`, `catalog`, `history`, and `version` commands now answer someone Dinnerbell doesn't know with one short line ("You're not in Dinnerbell yet. Open the Dinnerbell app's Home tab to join, or ask the admin."), instead of the web app's address, the version, and the database revision (audit #14). Each Slack user Dinnerbell doesn't know also gets at most five answers per five minutes from the app's other commands, Home, and buttons, so a loop of commands can't use up the household's Slack rate limits. Past the limit, App Home shows "Try again in a few minutes." once. Joining with **Join Dinnerbell** is separate and unchanged.
- Dinnerbell serves `/catalog/` (any path with a trailing slash) as `/catalog` instead of redirecting. The redirect built its address from the `Host` header and `http`, so it sent people from `https://` to `http://` and reflected whatever Host a client sent.
- A refused sign-in is audited at most once per person and reason per hour, and an hourly job deletes `auth.sign_in_refused` rows older than 30 days, because they hold a stranger's email. Every other audit row stays. `docs/setup.md` now says that audit payloads hold member emails and that backups copy them.
- `/healthz` reports `"backup": {"failing": true}` and status `degraded` when the newest nightly backup failed. Each nightly backup now runs `PRAGMA quick_check`: a copy that fails is deleted and made again once, then logged as an error.
- Dinnerbell logs one warning at start that names any `DINNERBELL_*` environment variable that matches no setting, such as a typo like `DINNERBELL_TRUSTED_PROXY`. It never logs the value. The variables the compose files and scripts use (`DINNERBELL_TAG`, `DINNERBELL_BIND`, and so on) don't trigger it.
- `alembic downgrade` through the command line no longer deletes rows. It now runs with foreign keys off, like `dinnerbell migrate`, and checks every reference before it commits (#36). Production still rolls back by restoring a backup.
- Food Critic now shows everywhere like every other badge (#117), but never gets a post of its own, because a post would interrupt the channel each time someone gives feedback. Dinnerbell includes it in the monthly recap and the household's newest badges, uploads and prewarms its art (65 files, not 60), and draws it in the announcement, the compact icon rows, and the picture on App Home (up to 13 badges, two rows of 7 and 6). The badge says only how many times you gave feedback, never what you said: answers still aren't shown anywhere, not even as counts. Dinnerbell also no longer logs which meal you gave feedback on.

### Fixed

- A Slack or internet outage no longer drops queued notifications (#14). An unavailable channel used to burn all 8 attempts in about an hour, and every pending row failed for good. Now an outage doesn't count as an attempt: rows retry at least every 5 minutes and are delivered, in order, when Slack returns. A row older than 24 hours is marked failed with a logged reason instead of being sent late. A response reminder queued before the call went to a vote, was decided, or finished is also dropped unsent instead of arriving late. The 24-hour limit counts from the newest update merged into a row. It is final: after 24 hours a call-opened, proposals, decision, badge, or recap post is lost for good (badges are marked announced and the recap marker moves when the post is queued), and you see `notification expired` in the log and `outbox.failed` in `/healthz`. `/healthz` now also reports `outbox.oldest_pending_age` (seconds) and shows `degraded` when the oldest waiting notification is over an hour old. Slack auth errors that retrying can't fix (such as `missing_scope`) now fail the notification at once. A queued role-choosing DM and a maybe-resolved DM are also dropped when the call moved on or was called off. FR-34 and `docs/channels.md` now say what is guaranteed: at least once after a crash, with a duplicate possible after the 5-minute lease.
- A redelivered or double-tapped **Vote**, **I can cook**, **I can pick up**, **I'll order**, or **Count me in** button no longer undoes itself (audit #14). Dinnerbell ignores a button press whose Slack `trigger_id` it handled in the last 10 minutes (it remembers up to 2,048), which is how Slack repeats a press after a reconnect. It also ignores a second press of the same toggle by the same person within 2 seconds, and says so privately ("Got it, that's already counted. Tap again in a moment to undo."). A press minutes later still flips it, as before. Redelivered modal submissions (**Make an offer**, **Log a meal**, and the other forms) are ignored the same way, so they can't post a second offer or log a second meal. If handling a press or submission fails, Dinnerbell forgets it, so a retry goes through.
- A reminder or other direct message queued before a member was deactivated is no longer sent to their Slack account: Dinnerbell drops it with a log line (a warning for a role message, and a count in memory). Deactivated members also show as plain names, not `@` mentions, in posts (audit #14).
- Slack refusing a post with `invalid_arguments` for a reason that has nothing to do with badge art no longer erases the cached art files. Dinnerbell now forgets them only when the text-only retry goes through, and a failure while forgetting them can no longer make the outbox post the badge twice (audit #14).
- `DINNERBELL_SECRET_KEY` now refuses the placeholder from `secrets/DINNERBELL_SECRET_KEY.example`, any value that contains `replace`, `changeme`, `placeholder`, or `example`, and any value with fewer than 8 different characters. The error names the variable and never shows the value. The example file now holds `REPLACE-ME-with-48-random-characters`, and `deploy/check-secrets` flags a placeholder, short, or repetitive key before you start.
- Two processes migrating one database at once no longer crash the second with `database is locked`. Migrations start with `BEGIN IMMEDIATE`, so the second waits and finds the schema current. If the first holds the lock for about 15 seconds, `migrate` and `serve` print a clear message and exit with status 1.
- The nightly cleanup of unfinished backups now also removes `pre-*.partial` files (from `reset-history`) once they are over an hour old.
- A catalog file with a date that doesn't exist (`name: 2001-13-45`) or a number of thousands of digits no longer ends in a 500 page. The import shows a readable error.
- Docs (#99): the ranking page, the spec, and ADR 0007 no longer say a place in the vote is always drawn. The last place is only not reserved for a suggestion, so at temperature 0 it can be one. The 0.7.0 entry below still has the old wording.

### Upgrade notes

- **Slack manifest change (optional, no urgency).** This release drops the unused `channels:history` scope. Paste the new `deploy/slack-app-manifest.yaml` (or `.test.yaml`) under **App Manifest** in the app settings, set your hostname in it again, then select **Install App** > **Reinstall to Workspace**. The removal takes effect only after the reinstall, and nothing breaks before it.
- **A `DINNERBELL_SECRET_KEY` with a placeholder marker makes the service refuse to start, and under `restart: unless-stopped` it crash-loops.** Run `deploy/check-secrets` before `up`. The refused keys are the text in `secrets/DINNERBELL_SECRET_KEY.example`, any key containing `example`, `replace`, `changeme`, `placeholder`, `yoursecret`, or `secretkeyhere` (case, `-`, and `_` ignored), and any key with fewer than 8 distinct characters. Keep your old key if it is long and random: marker words and fewer than 8 distinct characters are the only new triggers. If you do rotate the key, that invalidates only open login pages and import previews, not sessions (sessions are random server-side tokens).
- **Hard stop: create four secret files before you run `up` on this release.** The new `compose.yaml` mounts `secrets/DINNERBELL_SECRET_KEY`, `secrets/DINNERBELL_OIDC_CLIENT_SECRET`, `secrets/DINNERBELL_SLACK_BOT_TOKEN`, and `secrets/DINNERBELL_SLACK_APP_TOKEN`. If you pull and run `up` first, the service won't start. What happens with a missing file depends on the tool: podman-compose 1.0.6 and 1.6.0 pass the path to podman as a `--volume` without checking it (confirmed in their source), and podman refuses a bind mount whose source is missing (documented, not run here). Docker creates a directory for a missing `-v` source; what Docker Compose does for a missing secret file is not verified. A directory in place of a file makes Dinnerbell stop with an "is a directory" message. Run `deploy/check-secrets` (new) first: it reports a missing file, a directory, a mode other than 600, and an empty file, and prints no value. Dinnerbell itself still accepts a secret from `.env` for this one release, with a warning for each; the next release ignores `.env` for these four. If a value is in both `.env` and its file with different values, Dinnerbell refuses to start. Run this from the clone, with the service still running:

  ```sh
  ( umask 077
    mkdir -p secrets
    for name in SECRET_KEY OIDC_CLIENT_SECRET SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
        sed -n "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}DINNERBELL_$name[[:space:]]*=[[:space:]]*//p" .env | tail -n 1 | tr -d '\r' | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" | tr -d '\n' >"secrets/DINNERBELL_$name"
    done )
  deploy/check-secrets --env-file .env      # every secret must say "match"
  for name in SECRET_KEY OIDC_CLIENT_SECRET SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
      chmod 600 "secrets/DINNERBELL_$name"
      podman unshare chown 10001:10001 "secrets/DINNERBELL_$name"
  done
  ```

  The `sed` reads each value from `.env` without putting it on a command line, removes one pair of enclosing quotes and nothing else, and leaves no trailing newline. `deploy/check-secrets --env-file .env` compares SHA-256 hashes and prints only `match` or `differs`; run it before the `chown`, because afterward your account can't read the files. If a value has a trailing `# comment` or an escape in double quotes, write that file by hand: `printf '%s' 'the value' >secrets/DINNERBELL_SECRET_KEY` (put a space before the command to keep it out of your shell history), then `chmod 600` and `chown` it. An empty file is fine for a Slack token you don't use, but the file must exist. Then delete the four lines from `.env`, pull the new image, and run `podman-compose down && podman-compose up -d`. Check that the log has no "is set in the environment" warning.
- With rootful Docker, replace `podman unshare chown 10001:10001` with `sudo chown 10001:10001`. The files must belong to UID 10001, the user inside the container, with mode 600: compose bind-mounts them as they are and ignores `uid`, `gid`, and `mode` for file secrets, so a file owned by your own account is unreadable inside the container. Your own account can no longer read the files afterward; to replace one, delete it, write it again, and `chown` it again.
- If Dinnerbell stops at start with `Can't read the secret file`, the owner or mode is wrong: run the `chmod` and `chown` again. If it says the path `is a directory`, the file didn't exist when the engine mounted it: remove the directory, create the file, and recreate the service. The table in "Keep secrets in files" in [docs/setup.md](docs/setup.md#keep-secrets-in-files) lists each message.
- **Test instance:** do the same for `.env.test`, into `secrets-test/` instead of `secrets/` (the `sed` reads `.env.test`). The new `deploy/dinnerbell-test` refuses to start without the four files in `secrets-test/`, and, as before, refuses a secret that test shares with production, now in files as well as in `.env` and `.env.test`. It compares SHA-256 hashes, and reads files owned by UID 10001 through `podman unshare` (with Docker, run it with `sudo`).
- Food Critic upgrade: no migration, settings, or Slack scope changes. Food Critic awards made before the upgrade stay out of the recap and the newest badges; ones earned after it appear in both. No Food Critic tier is ever announced.
- The next prewarm uploads 5 more files, one per Food Critic tier, two seconds apart.

## [0.8.0] - 2026-10-28

### Added

- Contributions and badges (M4, #15). When a call or offer is done, Dinnerbell counts what each person did: cooked, picked up, ordered, coordinated, at the table, an answer within 15 minutes, a pick that won, a new place or cuisine that won, a catalog item that was chosen, guests brought, feedback given, and calls where you left the choice to others. Thirteen badges (Chef, Road Runner, Order Up, Quarterback, Full Table, Early Bird, Bright Idea, Scout, Explorer, Good Host, Food Critic, On a Roll, Gambler) share one ladder of five tiers: First, Bronze, Silver, Gold, and Diamond at 1, 10, 50, 100, and 200. A badge is earned once per person and tier. See [docs/contributions.md](docs/contributions.md).
  - Counting is exact: finishing a call twice, or two people tapping **Mark done** at once, counts it once. Guests count for the roles they take; only members count for the rest.
  - The tier counts are a household setting. An admin changes them under **Admin > Badges**; lowering one awards newly reached tiers quietly, and raising one takes nothing away.
  - **Announcements.** When a done call earns badges, the channel gets one warm post for the call that mentions each member once, in name order, and never compares people. A badge is announced exactly once, even across restarts and retries. A big backlog is capped at 10 members and 6 badges each.
  - **Monthly recap.** In the first days of each month, Dinnerbell posts a recap of the month before: meals, cuisines, new catalog items, a thank-you for each person who cooked, picked up, ordered, or coordinated, and the badges earned. A month with no meals posts nothing.
  - **Stats page.** **Stats** shows your badges with progress to the next tier, what you earned lately, and your counts, plus the household's totals and newest badges. It never ranks anyone. A guest sees only their own pitching-in badges.
  - **Food Critic is private.** It counts feedback, and who gave feedback is as private as what they said, so it is never announced, recapped, or shown to the household.
- Gambler badge (#108). You earn **Gambler** for a call where you left the choice to others: you were in (and not a late joiner) and voted for every pick, or for none of them when someone else voted, on a call with at least three picks. A vote you took back doesn't count, and a guest never earns it. It uses a single die as its icon.
- Badge art in Slack (#105, #110, #112, #114). Announcements and **Your badges** on App Home show the same art as the Stats page, instead of text tags.
  - Dinnerbell draws each badge and tier as a PNG and uploads it to Slack. Thirty to forty seconds after Slack connects, it uploads all 60 tiers in the background, two seconds apart, so the first announcement doesn't show broken images. It skips what is cached, resumes after a restart, stops on a missing `files:write` scope with one log line, and gives up after three failures in a row.
  - A member with one to three badges in an announcement gets a thumbnail beside each. With four or more, one line names them and a row of small icons shows every badge.
  - On App Home, **Your badges** shows one picture of every badge you hold (up to 7 to a row, Food Critic left out), a link to **Stats**, and the next badge's icon in the "Next" line. Dinnerbell draws and uploads the picture after a badge post and when you open Home, and shows it once it has been in Slack for 30 seconds; until then you see a row of small icons.
  - If Slack refuses a post that uses a file uploaded a moment ago, Dinnerbell waits 1.5 seconds and posts again, then 3 seconds and posts once more, before it falls back to text. Every refusal logs one `slack refused badge art` line with Slack's error code. After the last try fails, Dinnerbell stops uploading for 15 minutes.
  - If an upload fails, or the app lacks the new scope, you see the text tags as before. A failure never fails a post or a Home view.

### Upgrade notes

- **Back up first.** The database migrates through `0016_contributions`, `0017_slack_badge_file`, `0018_gambler`, and `0019_slack_badge_composite`. They add three empty tables and let one table store a new kind; they move no row.
- **Reinstall the Slack app to grant `files:write`.** Badge art needs a new bot scope. Paste the current `deploy/slack-app-manifest.yaml` (and `deploy/slack-app-manifest.test.yaml` for a test instance) under **App Manifest**, set your hostname again, then select **Install App** > **Reinstall to Workspace**. The bot token doesn't change. Until you reinstall, badges show as text tags, and the log says `badge art not available`.
- **The first start counts your history, quietly.** Dinnerbell counts the meals already in your log, in batches, and awards the badges they reach as already announced: nothing posts to the channel for old meals. A second start doesn't count again, and a start stopped halfway finishes next time. Gambler counts for every call already done in the same way.
- **`dinnerbell reset-history` now also deletes every contribution count and every badge earned**, along with the history; they don't come back. It keeps the cache of uploaded badge art.
- The first scheduler tick after the upgrade announces any badge a done call earned that the channel hasn't heard about, one post per call. The recap posts for the month before when you upgrade during the first seven days of a month.
- The image adds `resvg-py` (MIT, with the MPL-2.0 resvg renderer inside), a prebuilt wheel of about 1.5 MB with no system libraries.
- To go back, restore your backup. Downgrading the schema instead drops the new tables, so every count and badge is lost, and a later upgrade counts the meal log afresh.

## [0.7.0] - 2026-10-28

### Fixed

- The Catalog page draws tags and meals as small chips again. In 0.6.0 the meal chips picked up the meal-log card's padding, which stretched every chip on the row.

### Added

- Coordinator powers (#87): the coordinator can choose how the winning pick is had and who does it, break a tie, and shuffle the picks. With nobody coordinating, or a guest, an admin can break a tie and shuffle; a guest coordinating can still choose roles, as they can change assignments.
  - **Assign roles.** When the winning pick's role has no clear owner, because more than one person offered for its way of getting it or nobody did, the coordinator gets a direct message and the decision shows **Assign roles** (and the event page has an **Assign roles** form). They can switch a place that allows more than one way, such as pickup to delivery, and choose anyone who's in; a volunteer is preselected. Someone assigned who hadn't offered is told the coordinator assigned them. Nobody is told about a role until it's settled. If the coordinator doesn't choose within 15 minutes (**Admin > Ranking**, "Minutes for the coordinator to choose roles"), the first person who offered does it, as before. With no coordinator, the role is assigned at once as before, and an admin can still use **Assign roles**.
  - **Break the tie.** When picks are tied for the most votes, the coordinator sees **Break the tie** beside each tied pick, on the picks message and the event page, and chooses one even if they didn't vote. It decides at once. If nobody chooses, the deadline breaks the tie in this order: the coordinator's counted vote on a tied pick, then an admin's vote (only with no coordinator, or a guest coordinating), then the tied pick eaten longest ago (never eaten counts as longest), then the order the picks were drawn in. The decision says how: "Tied; Kim (coordinator) chose", "Tied; least recently eaten", or "Tied; drawn at random". A late joiner's vote that doesn't count never breaks or makes a tie.
  - **Shuffle picks.** While voting, the coordinator can redraw the picks, up to 3 times a call. With nobody having voted, every drawn pick is replaced; after that, only picks with no votes, counted or not. Picks a member added, an offer made, or someone suggested never go. The new picks pass every rule the first draw does (vetoes, guests' needs, someone to do it, the variety window, one pick per place, the meal) and leave out the places just shuffled out unless nothing else fits. The picks message updates in place with "Kim shuffled the picks (2 replaced)." and mentions no one, a short note goes in the call's thread, and the web shows the new set on refresh. Each shuffle is audited and logged with its counts.
- Breakfast calls, started when you want one (#58). Breakfast works like lunch and dinner: in, maybe, or out, roles, offers, proposals, a vote, a decision, and who does what.
  - No breakfast call ever posts on its own. The breakfast schedule starts off; any member can turn it on from the **Schedule** page if the household ever wants a daily one. Its defaults: the call at 7:00 AM, 30 minutes to answer, 15 to vote, the reminder 10 minutes before answers close, cook and pickup as the usual ways to get it, and 8:00 AM as the usual meal time. All of them can be changed there, like lunch's and dinner's.
  - Start one with `/dinnerbell call breakfast` (add `tomorrow` or a date), from **Start a call** on the web's Today page, or with **Start a breakfast call** on App Home. A breakfast called off today shows **Start breakfast again**, on Home and the web.
  - Ranking proposes places whose meals include breakfast, and tops up with others, marked such as "Usually for lunch and dinner.", when too few fit.
  - Everything that names a meal knows breakfast: `/dinnerbell cancel breakfast`, the "which one?" reply when more than one call is open ("Breakfast, lunch, and dinner are all open today."), offers, logging a meal on the web and in Slack, History, **Admin > Ranking**'s Skip a day (once breakfast's schedule is on), `/dinnerbell schedule`, and, on a test instance, `/dinnerbell-test discard breakfast`.
  - App Home's start buttons keep their places, and **Start a breakfast call** comes last: **Make an offer**, then dinner, lunch, and breakfast. The Schedule page and `/dinnerbell schedule` list breakfast last too. History lists a day's meals newest first: dinner, then lunch, then breakfast.
- Join a call late, during the vote (#76). If you missed the answers, by saying out, being a maybe when answers closed, or not answering, you can still join while the call is voting: tap **I'm in (late)** on the call message, your Your answer card, App Home, or the event page. Voting or adding a pick while you're not in asks "You missed the answers. Join late?" first, and joins you if you say yes. Guests are unchanged: they come by invitation, while answers are open.
  - You're in, and the call message, the picks, and the decision say you joined late. You count in the head count, you can offer to cook, pick up, or order (on your card, Home, or the web), and you can be assigned a role.
  - Your votes show, marked "Kim (late)", but don't count toward the tally, a tie, or the decision until the coordinator turns on **Vote counts** for you. Then they count like anyone's.
  - The coordinator can turn **Taking part** off, and you're out for this call, and told so. Only the coordinator can change either switch, or an admin when nobody's coordinating (or a guest is). They're on the event page and in the ⋯ beside each late joiner on the picks message. Each change is audited and updates the call's messages.
  - Your dietary needs and vetoes don't remove picks already made. Only you see which picks don't fit your preferences, on your Your answer card, App Home, and the event page; nobody else, the coordinator included, sees anything that could say what you veto. Guests you bring count as any guest does: picks added from then on must meet their dietary needs, and a pick already made that misses them stays and warns everyone, on the decision too. Your own needs stay off the decision's "Dietary needs" line, which would give them away. Joining late clears guests and an eat window left on an earlier answer. A pick you add still has to suit everyone who answered on time; if it conflicts only with your own needs or vetoes, it's added.
  - A maybe whose answer the deadline turned out keeps their vetoes counting after joining late, as before. Joining after the decision isn't offered: ask the coordinator, or make an offer.

### Changed

- The picks and the decision post at the top level of the channel, with a mention, instead of in the call's thread (#92). The newest message in the channel is now the thing to act on, and on a phone nothing hides in a thread.
  - Both start with a title line, such as "Dinner picks · Tuesday, Oct 27" and "Dinner decision · Tuesday, Oct 27", so posts from different calls on one day can't be confused.
  - The picks post mentions everyone who is in, guests with Slack and late joiners included, so opening the vote pings people. The decision mentions everyone who is in, not only the people with a role. Only these first posts mention anyone: when a post is edited (a vote, a late joiner, a new meal time), the same people show as plain names, because Slack may notify a mention an edit adds. The mention list comes after the instructions, and a very large household's list is split and capped, so it never pushes out the instructions, assignments, or dietary needs. Assignees still get their direct message. On a test instance, the notification guard still limits mentions to the test channel's members.
  - While voting, the call message says the picks are posted below. Once decided, it names the result: "It's decided: Pho Place, pickup, at 6:30 PM".
  - Changing the meal time updates the call message too, since it names the result.
  - A call-off notice still replies under the call message.
- A call on App Home has the same buttons as your Your answer card, so you can do everything from Home without going back to the channel (#84): **In**, **Maybe**, and **Out**, then **I can cook**, **I can pick up**, **I'll order**, **I'll coordinate** while nobody is, **Details**, and **Invite a guest**, with your choices marked as on the card ("✓ I can cook"). A guest's Home has their **In**, **Out**, the role offers, and **I'll coordinate**. Home and the card draw these buttons from the same code, so they can't drift apart again.
  - A tap on Home works as a tap on the card does, and Home shows your new choice at once, with a line saying what changed.
  - Home's buttons name the call's run, like the card's. A Home left open since the call started again, or since answers closed, changes nothing and says why, as the card does.
  - Answering for a guest without Slack stays on the card (tap **Answer** on the call).
  - The buttons come in short rows, as on the card, so Slack doesn't hide any under **+ N more**.
  - Home says who's coordinating a call, or that you are.
- Suggestions can no longer fill the whole vote (#85). A place someone suggested is still proposed ahead of the draw, but at most one fewer than the number of proposals are guaranteed that way, so at least one place is always drawn: with the default 3 proposals, one member's three suggestions give 2 guaranteed places and a draw for the third. When more places were suggested, the best-scoring ones go in (equal scores are chosen at random), and the rest stay in the draw, where a suggestion makes them much more likely. With 1 proposal per call, a suggestion no longer skips the draw.

### Fixed

- Shutting down no longer cancels a scheduler job in the middle of a database query (#93). A job that is running when Dinnerbell stops now finishes, up to the shutdown deadline, and only a job that outlasts it is cancelled. Cancelling mid-query could leave a database worker thread to fail with "Event loop is closed" as the process exited.
- Linking a guest to a different Slack user by hand (Admin > Members) clears the Slack email and the "Slack guest account" mark that came from the old account, so the link picker's "same email" label and the confirmation page no longer describe the old account (#70).
- An upgrade that stops at migration 0011 because a member has an empty email now says which member (by ID) and logs the usual critical line, instead of failing with a raw database error. It still changes nothing (#70).
- Saving **Admin > Ranking** without changing anything no longer writes an audit entry when a weight or the randomness was hand-edited to more than 6 digits. The form now shows such values in full (#85).
- Ranking can no longer propose a place with no chance of being drawn at randomness 0, however unlikely the random number (#85). Draws also use a random-number method Python guarantees stays the same for a seed, so a Python upgrade can't change which places a call proposes.

### Upgrade notes

- The database migrates to revision `0014_late_join` when Dinnerbell starts. It adds `late` and `vote_counts` to `response` in place; every row is kept, and each existing answer is on time. To go back, restore your backup; downgrading the schema instead drops those columns, so late joiners become ordinary answers.
- The database migrates to revision `0015_coordinator` when Dinnerbell starts. It adds five columns to `event` in place: a shuffle count, the shuffle note, the coordinator's deadline to choose roles, and how a tie was broken. No row is rebuilt or lost. To go back, restore your backup; downgrading the schema instead drops those columns, which forgets a call's shuffle count and tie note, and refuses while any call is still waiting for its coordinator to choose roles, because an older release would never assign that role.
- A call that is already decided when you upgrade keeps the roles it has. A new setting, "Minutes for the coordinator to choose roles", starts at 15 on **Admin > Ranking**. No Slack scope changes, so no reinstall.
- No migration, settings, or Slack changes. Each queued notification keeps the thread it was written with, so a call that is mid-vote when you upgrade can end up split: its picks sit in the call's thread, and its decision posts at the top level. At most one call at upgrade time is affected. Upgrade between calls to avoid it. A picks or decision post made before the upgrade has no "In:" list, and its first edit after the upgrade shows names, not mentions.
- The database migrates to revision `0013_breakfast` when Dinnerbell starts. It rebuilds the `event` and `meal_log` tables so a call, an offer, or a logged meal can be breakfast. Every row is kept, along with the tables' indexes and foreign keys, and call and meal IDs are still never reused. Like 0012, it must run with foreign keys off, as `dinnerbell serve` and `dinnerbell migrate` do; run any other way, it refuses. To go back, restore your backup. Downgrading the schema instead refuses while any breakfast call, offer, or meal exists, rather than delete them.
- No settings or Slack changes. Households keep their lunch and dinner schedules; breakfast's starts off, so nothing new posts until someone starts a breakfast call.

## [0.6.0] - 2026-10-27

### Added

- Catalog items say which meals they're for: breakfast, lunch, dinner, or any mix (#72). Most places aren't breakfast places, so the default is lunch and dinner, for new items, items imported without the field, and every item you already have.
  - Ranking proposes items for the call's meal: a lunch call never proposes a dinner-only place while enough lunch places fit everyone who's in. When too few do, items for other meals fill the rest of the vote, after every lunch pick, with "Usually for dinner." as their first reason. Vetoes, guest needs, coverage, and the variety window apply to both, and the variety window comes first, across every meal: a place eaten in the last week comes back only when nothing fresh fits for any meal.
  - The Catalog page has **Breakfast**, **Lunch**, and **Dinner** checkboxes on the add and edit forms (at least one; new items start with Lunch and Dinner), shows each item's meals, and filters by meal.
  - **Add a proposal** and **Make an offer**, on the web and in Slack, list items for the meal first. The rest stay pickable, marked such as "(usually dinner)", because people know better than the catalog. The Details suggestion list puts the call's meal first too.
  - Catalog files have an optional `meals` list, such as `meals: [breakfast]`. Leaving it out of a new entry means lunch and dinner; leaving it out of an update keeps what the item has. Exports always write it, so copying the catalog from production to test carries it. The format stays version 1: older files import as before, but an export from this release has a field that 0.5.0 and earlier refuse.
  - A free-text pick that wins a call, or something new offered, joins the catalog for that meal and the default: lunch and dinner.
  - The `catalog item created` log line, and a new `catalog item updated` line, carry the item's name and its `meals`.

### Changed

- Proposals are drawn at random, weighted by score, instead of the top scores with ties broken by name (#78). With a fresh catalog most places score the same, so every call used to propose the same three places, first in alphabetical order. See [How Dinnerbell picks proposals](docs/ranking.md), with a worked example.
  - Vetoes, guest needs, coverage, the variety window, meal fit, and one pick per place still apply first, and absolutely. The draw only chooses among places that pass all of them, and the vote still gets its usual number of picks.
  - Places with the same score have the same chance, whatever their names. A place 1 point better is about 2.7 times as likely to be picked, so the best place usually makes the vote but not every time.
  - A place someone suggested for the call is always proposed when it fits, ahead of the draw.
  - After each pick, places sharing its cuisine become less likely, so a vote rarely has three of one cuisine when others fit.
  - The same call always draws the same picks. A re-run, or starting a called-off call again, draws again. The order drawn is the proposals' rank, which still breaks a tied vote when neither the coordinator nor an admin voted for a tied place.
  - **Admin > Ranking** has a new **Randomness** setting, from 0 to 3, default 1. Lower sticks closer to the best scores, and 0 always picks them in order, with ties drawn at random. Higher mixes the picks up more. Changing it is audited with the other ranking settings.

- The logs tell the story of a call (#75). Every action on a call or offer writes one `info` line, from Slack, the web, the command line, or the scheduler, with the same fields: the call (`event_id`, `run`, `slot`, `day`), its state before and after, who acted by ID and display name (`member`), the place (`item`, `mode`), and the `channel`. The scheduler logs each step, such as `voting`, `decided`, and `abandoned` with its `end_reason`. A refusal is logged once, at `info`, with a stable `reason` code, such as `not_in`, `responses_closed`, or `cook_list`, and the message the member saw. Each Slack tap and scheduler run has a `request_id`, as web requests do, so one tap's lines group together. Deliveries to Slack log the notification's call and kind, and the posted message's channel and timestamp. Notes and call-off reasons appear only as their length. See "Read the logs" in [docs/setup.md](docs/setup.md#read-the-logs).

### Fixed

- App Home shows today's calls and offers first, with their buttons, then the start buttons and the calls coming up (#51). Calls and offers still open from earlier days, for example after the scheduler was down, come last: the three most recent, and a line such as "…and 47 more from earlier days on Today on the web" for the rest. A call from an earlier day that never posted shows there too, not under **Coming up** with a time that's past. Before, enough of them pushed today's calls, the start buttons, and the links off the tab. A guest's Home lists today's invitations first in the same way.
- Adding a proposal while voting no longer fails with "Nobody who's in offered to pickup for that." when nobody had offered to pick up (#74). Whoever adds a proposal does it: adding one offers the role its mode needs (pick up, cook, or order) on your answer, so it's covered, and the call's roster shows it. The Add proposal form, on the web and in Slack, says so. You need to be in: until joining late arrives (#76), someone who isn't is told "You're not in for this call. Joining late is coming soon. Ask someone who's in to add it." For a home meal with a cook list you're not on, you can still add it when a cook on the list is in and offered to cook; you take no role, and it's refused if no such cook is in. Assignments work as before: the winning pick's role goes to whoever offered it first, which can be you even when another pick in that mode wins.
- A refusal that names a mode says "pick up", "cook", or "order" instead of a role's code, such as "Nobody who's in offered to pick up for that."
- The **Pick up** checkbox on Preferences says it doesn't volunteer you for a call. It never did: only offering on the call, or adding a pickup pick, does.
- A tap on a Slack answer card or reminder for a call that moved on no longer swallows why it changed nothing: the card says the call moved on, and the reason, such as "Responses are closed for this call.", follows in a private line. It's logged at `info` with the member and call IDs (#75).
- A vote no longer offers the same place twice, such as once for pickup and once for delivery (#77). A call has one pick per place, in any mode:
  - Ranking proposes each place once, in its best mode: the one that's a usual choice, or that a volunteer cook knows, with ties going to cook, then pickup, then delivery.
  - Picks that offers added before voting no longer take the place of ranked picks: the vote still gets its usual number of ranked picks, then the offers'.
  - Adding a place that's already a pick, in any mode, is refused: "Pho Place is already a pick (delivery). Vote for it instead." The Add proposal list marks those places "(already a pick)".
  - An offer for a place that Dinnerbell proposed, or that you added, in another mode takes that pick over: it switches to the offer's mode, says "Kim offered to order it.", keeps its votes, and counts toward your 3 added picks. An offer for another member's pick, or for a pick in the same mode, is refused.

### Upgrade notes

- Proposals change from this release: the same catalog and people now give varied picks from call to call (#78). No migration is needed; Randomness starts at 1 until an admin saves another value. To keep score order, set **Randomness** to 0 on **Admin > Ranking**; ties are then drawn at random rather than by name.
- The database migrates to revision `0012_catalog_meals` when Dinnerbell starts. It rebuilds the `catalog_item` table to add `meals`, the meals each item is for, stored as bits (breakfast 1, lunch 2, dinner 4) with a check that at least one is set. Every item gets lunch and dinner, so proposals stay as they were until you change an item. Every row, and every row that refers to an item, is kept. Like 0011, it must run with foreign keys off, as `dinnerbell serve` and `dinnerbell migrate` do; run any other way, it refuses. To go back, restore your backup; downgrading the schema instead drops which meals each item is for.
- Copy the catalog between production and test only between instances on this release or later: an older instance refuses an export's `meals` field as an unknown field.

## [0.5.0] - 2026-10-25

### Added

- `dinnerbell reset-history` clears the history from testing before you go live (#56): every call and everything in it, the meal log, feedback on meals, and the outbox. It keeps the catalog, members and roles, Slack links, preferences and vetoes, the schedule and settings, sign-ins, and the audit log. It works on production. Before it changes anything, it shows how many rows it deletes and keeps in each table, refuses while a call is open, asks you to type `reset` (or takes `--yes`), and backs up the database to `backups/pre-reset-VERSION-TIME.db`, checking the backup before it goes on. It runs in one transaction and writes a `history.reset` audit row with the counts. Days you skipped, or whose call you called off, from today on stay off. Messages from earlier calls stay in Slack unchanged. See "Going live" in [docs/setup.md](docs/setup.md#going-live).
- Offers (#13, FR-4, FR-5). Anyone making or getting a meal with servings to spare can offer it: "Sam is picking up Siam Garden for lunch at 12:30 PM. 3 servings spare." Use **Make an offer** on App Home, `/dinnerbell offer`, or **Make an offer** on the web's Today page; pick something from the catalog or type something new with its cuisine, how it arrives, the day and time, and the spare servings (1 to 20). The offer posts in the Dinnerbell channel with **Count me in** (tap again to drop out) and **+1 guest**. Guests take seats, and so do the offerer's own guests. Once it's full, the buttons give way to **Can't make it**, and whoever made the offer gets one direct message. The offer stops taking people at its time, and it's done two hours later, when it goes in the meal log like a call's meal; its message then has **How was it?**. Whoever made it, or an admin, can call it off, even after its time. Each member can have up to 3 offers on for one meal on one day.
  - An offer for a meal whose call is already open becomes a pick on that call instead of a separate message: you're counted in and offering to cook, pick up, or order, and it passes the same vetoes and guest needs as any added proposal. While voting, only someone who's in can add one. If someone who answers later vetoes it, it's dropped when voting starts, as ranking would never have proposed it.
  - A cooking offer posted before that meal's call replaces the call. If the call is scheduled, it's called off quietly, and whoever started it hears. If the daily call hasn't been created yet, it doesn't go out while the offer stands; if the offer is called off before answers would have closed, the call goes out after all.
  - Lunch offers start as pickup at the usual lunch time, and dinner offers with the dinner schedule's first usual way to get it, so offers are the normal way to share lunch. App Home shows today's offers with **Count me in**, **+1 guest**, and **Can't make it**, and your place in each.
- `/dinnerbell log` opens a form to log a meal the household ate outside Dinnerbell: what, which meal and day, and who ate. It no longer says "coming soon", and neither does `/dinnerbell offer`.
- Your answer card and App Home show how many guests you're bringing.
- Guests (#38). A guest eats with the household when a member invites them to a call, one call at a time. Once invited, a guest answers In or Out, offers to cook, pick up, or order, can be assigned those roles, and can coordinate the logistics (Close now, marking it done, the meal time, who does what), but never votes, proposes, suggests, vetoes, locks a pick, breaks a tie, calls a call off, starts a call, or makes or joins an offer. Their dietary needs always apply. With a guest coordinating, a tied vote goes to whoever invited them, then to an admin.
  - Any member invites a guest from **Invite a guest** on their Your answer card, or on the call's web page: a saved guest, or someone new with their dietary needs. A guest with Slack gets a direct message with **In** and **Out**. For a guest without Slack, the member who invited them answers and offers roles for them on their card or the web, and gets their assignment messages.
  - A guest with Slack signs in with Slack and sees only the calls they're invited to and a page for their own dietary needs. Every other page refuses them, on the server.
  - Someone in the Dinnerbell channel whom Dinnerbell doesn't know gets **Join Dinnerbell** when they tap a button, open App Home, or run the command, and Sign in with Slack joins them too. Only real people of the workspace who are in the channel can join: no bots, deactivated accounts, or people from another organization. They join as guests, and each admin gets a direct message with **Make member** and **Remove**. Joins are rate-limited and audited, and someone you removed can't join again until you add them back.
  - A guest coordinating can close answers early, but not the vote: "Decide now" picks the plan. Up to 5 dietary needs each.
  - When the dietary needs of who's eating rule out every pick, the on-your-own message says that nothing fits everyone's dietary needs, never whose.
  - Someone whose Slack email belongs to a member Dinnerbell can't link them to on its own isn't made a guest: they're asked to ask you, and you get a message to link them by hand. You also get one when the hourly email match finds a member's Slack account held by someone else. Adding a member with the email of a guest who joined from the channel offers to promote that guest instead.
  - Admin > Members lists guests: add, rename, set their dietary needs, link a Slack guest account by hand, make one a member or an admin (with their email), or remove one. **Join from the channel** turns joining on or off; it's on. **Add people from the channel** adds anyone in the channel who isn't in Dinnerbell yet, as a guest or a member.
  - Members can set their own dietary needs on Preferences; a guest keeps theirs when made a member. Like guests' needs, they're hard constraints whenever you're in or maybe. Messages now say "Dietary needs" instead of "Guests' dietary needs".
  - If someone you invited by email joins as a guest because their Slack email differs, **Link to member** on their guest row moves their Slack account to their membership. Anyone can take any name in Slack, so the join message and the guest row show the Slack account and the email Slack shares. When that email isn't the member's, or they use a Slack guest account, you see both emails and type the member's name to link anyway. A guest is never linked to an admin; an admin sets their own Slack member ID.
  - On a test instance, `scrub-test` renames every guest "Guest <ID>" and clears their Slack user, Slack email, sign-in, and dietary needs, and the audit rows that name them.

### Changed

- Meal IDs are never reused, so a History or feedback page left open while meals are deleted, such as by `dinnerbell reset-history`, can't save to a different meal. The upgrade rebuilds the `meal_log` table (migration 0009) and keeps every row and its feedback.

### Security

- Checking an email address takes time in proportion to its length, whatever it contains (CodeQL `py/polynomial-redos`). Addresses were already capped at 254 characters, so this wasn't exploitable. The check is a little stricter too: it refuses an empty part between the dots of the domain, such as `sam@.example.com`, `sam@example..com`, or `sam@example.com.`.

### Upgrade notes

- The database migrates to revision `0009_meal_log_autoincrement` when Dinnerbell starts. It rebuilds the `meal_log` table so a deleted meal's ID is never given to a new meal, keeping every meal and its feedback. It's quick for a household's data. Back up first, as always. To go back to an earlier release, restore that backup: older releases refuse a database at a revision they don't know.
- The database then migrates to revision `0010_offers`, after `0009_meal_log_autoincrement`. It rebuilds the `event` table to add an offer's spare servings, `capacity`, with a check that only offers have one, from 1 to 20; every row is kept, and event IDs are still never reused. Like migration 0007, it must run with foreign keys off, as `dinnerbell serve` and `dinnerbell migrate` do; run any other way, it refuses rather than delete call data. Back up first. To go back to an earlier release, restore that backup; downgrading the schema instead deletes every offer.
- No Slack scope changes, so no reinstall. Optional: both Slack app manifests list `offer` and `log` in the slash command's usage hint. To show them, edit the command's usage hint in the app's **Slash Commands** settings to match `deploy/slack-app-manifest.yaml` (or `deploy/slack-app-manifest.test.yaml` for test).
- The database then migrates to revision `0011_guests`. It rebuilds the `member` table so a guest has no email, with a check that guests never have one and everyone else does, adds `slack_email` for the email Slack shares when a guest joins (never used to sign in) and `slack_guest_account`, and adds guests' dietary needs, whether an admin removed them, and the `invitation` table. Every row is kept. Like 0010, it must run with foreign keys off, as `dinnerbell serve` and `dinnerbell migrate` do; run any other way, it refuses. To go back, restore your backup; downgrading the schema instead deletes every guest.
- The stricter email check refuses `sam@.example.com`, `sam@example..com`, and `sam@example.com.`. If `DINNERBELL_ADMIN_EMAIL` has one of those shapes, Dinnerbell stops at start with a configuration error; fix it in `.env` first. A member whose stored email has one can't be edited or matched by email until an admin corrects it.
- Joining is on as soon as you upgrade. Anyone in the Dinnerbell channel can join as a guest from then on, and joined guests appear in every member's **Invite a guest** list. If you don't want that, turn it off on Admin > Members right after the upgrade.
- Joining from the channel needs no new Slack scope either: it reads the channel's members with `conversations.members` and the person with `users.info`, which `channels:read` and `users:read` already allow. A private channel also needs `groups:read`, as before.
- `reset-history` clears the new `invitation` table with the calls, and keeps guests, who are rows of `member`.

## [0.4.0] - 2026-10-25

### Added

- Any member can change a meal's standing schedule (FR-44, #12): whether its call goes out every day, the call time, how long people have to answer and vote, the reminder, the usual meal time, and the usual ways to get it. Open **Schedule** in the web app's menu. Each change is posted in the Dinnerbell channel with who made it, what changed, and the first day it applies, and the audit log records the old and new values. A change applies from the next call: a call that's already out, or scheduled for a later day, keeps its times, and a call started again uses the new ones. If today's call hasn't gone out yet, it follows the new schedule when it still can; moving the call to a time that has already passed today keeps today's call at the old time, and the post says so. Saving without a change posts nothing, and a save over someone else's newer change is refused so it can't undo theirs.
- `/dinnerbell schedule` shows each meal's call time, when answers and voting close, and the usual meal time, with a **Change the schedule** button that opens the Schedule page. It no longer says "coming soon".
- Ask about a later day's meal now (#59). A call you start for a later day still posts at that day's usual time, and now you can post it early instead: select **Post it now** on the reply in Slack, on App Home, or on the call's web card, add `now` (or `early`) to the command, as in `/dinnerbell call lunch tomorrow now`, or check **Post it now** on the web's start form. Responses open at once; the response deadline, the reminder, and voting stay at that day's usual times. The call message names the day ("Who's in for lunch on Sunday, Oct 25? Answer by 11:30 AM that day."). Any member who could start the call can post it, a call that's posted already is left as it is, and posting is recorded in the audit log. Re-running a call posted early, or calling it off and starting it again, posts the new run at once.
- App Home and Today list calls scheduled for later under **Coming up** ("Lunch · Sunday, Oct 25 · posts at 10:30 AM"), with **Post it now**, and **Call it off** for whoever may call it off.
- Feedback after meals (#12, FR-45 to FR-49). Everyone who ate a meal can say how it was, for 7 days after it, and change their answer until then. Each part is optional: the food (loved it, fine, or not again); the place, for a restaurant, with issues such as late or cold; and, for a home meal, a thank-you to the cook with an optional note of up to 200 characters. Feedback doesn't change proposals yet.
  - In Slack, a done call's decision message has **How was it?**, and App Home lists meals from the last week you haven't rated yet.
  - On the web, History and a done call's page have **How was it?**, and show your own answers.
  - The catalog shows how many loved each item's food and place. "Fine," "not again," and place issues are kept for later but never shown, not even as counts. Cooks see who thanked them and their notes; everyone else sees only how many thank-yous there were. Nobody, admins included, sees who chose "not again", on any page, in Slack, or in the catalog file.
  - Turn on the feedback reminder in Preferences to get one direct message after each meal you ate. It's off by default, and muting direct-message reminders stops it too.
  - Changing a meal's item in History deletes its feedback, and taking someone off a meal deletes theirs, because it was about a meal they no longer ate.
  - On a test instance, discarding a call deletes the feedback on its meal, and `scrub-test` deletes all feedback; both show how many rows, as a number only.
- Slack says which version is answering. `/dinnerbell version` (`/dinnerbell-test version` on test) replies privately with the version as the web footer shows it, such as `0.3.0`, or `0.3.0+abc1234` for a build from `main`; the instance label, if one is set; the database's schema revision; and a link to the release notes, or to the commit for a `main` build. The help reply lists `version` and ends with a version line, such as "Dinnerbell 0.3.0+abc1234 · TEST", and so does the bottom of App Home.
- On a test instance only (`DINNERBELL_INSTANCE_LABEL` set), an admin can discard a call so a fresh one can start for the same slot and day (#60): **Discard (test)** on the call's card, behind a confirmation page; `/dinnerbell-test discard [lunch|dinner] [day]`; or `dinnerbell discard-call ID [--yes]`, which shows what it will delete first. Discarding deletes the call with its answers, suggestions, proposals, votes, decision, assignments, logged meal, and unsent notifications, edits its Slack messages to "Discarded (test)", and writes an audit row. On test, a called-off call can also start again at any time, not only until the usual meal time plus two hours. On production none of this appears: the pages are a 404, the slash command and the CLI refuse, and the rules don't change. See [docs/setup.md](docs/setup.md#discard-a-call-and-start-again).

### Changed

- Admin > Schedule is now **Admin > Ranking**. It keeps the ranking weights, proposals per call, variety window, and **Skip a day**; call times and windows moved to the Schedule page.
- A schedule that's on must close voting before the usual meal time; you can always turn a slot off. Saving one that doesn't, such as a 3:00 PM call with 90 minutes to answer and 45 to vote and a 5:00 PM meal, explains how to fix it. Schedules already saved keep working.
- Call IDs are never reused. Before, a new call could take the ID of a call deleted before it. The upgrade rebuilds the `event` table (migration 0007) and keeps every row.
- In Slack, you can now see what you chose on a call (#42, #52). While responses are open, the call message has one **Answer** button instead of **In**, **Maybe**, **Out**, the role offers, **I'll coordinate**, and **Details**. **Answer** posts a Your answer card only you see, with those buttons and your answer and offers selected: green, and marked "✓ " so you can tell without color. Taps on the card change the card in place instead of adding another message. Tapping **Answer** again adds a new card, because Slack can't change one person's view of a shared message. The card replaces the "Thanks! You're offering to cook" lines, which piled up. While voting with nobody coordinating, the call message still has **I'll coordinate**.
- The reminder direct message and App Home show your answer selected the same way. A tap on the reminder changes the reminder itself, instead of adding a confirmation under it.
- A card or reminder left over from before a call moved on (responses closed, called off, or started again) says "This call moved on." instead of answering the newer call.

### Fixed

- Starting a call for a later day in Slack no longer says "Started … It's posted in the Dinnerbell channel." when nothing is posted until that day (#59). The reply says "Scheduled the lunch call for Sunday, Oct 25. It posts in #dinnerbell at 10:30 AM that day." A scheduled call's web card says when it posts, and which day, instead of "Opens at 10:30 AM."
- A call scheduled for a later day no longer hides **Start a dinner call** (or lunch) for today on App Home.
- **I'll coordinate** on a voting call message names the call's run, so a message from before a call-off and restart can't make you coordinator of the new run (#54).
- **Start the vote now** and **Decide now** no longer show once the window's deadline has passed. Until the scheduler's next tick moved the call on, up to 30 seconds later, the web card and App Home still offered them, and selecting one only said the call had moved on.
- In Slack, picking **Start the vote now** or **Decide now** from the call's ⋯ menu after the deadline says the call has moved on right away, instead of opening a confirmation that could only fail.
- Tapping **I'll coordinate** again when you already coordinate no longer says someone else is coordinating.
- Details saved in Slack after the call was called off and started again no longer change the new call.

### Upgrade notes

- The audit log names schedule changes differently. Ranking changes on Admin > Ranking are now `household.ranking_updated`, and a member's change to a slot's schedule is `household.slot_schedule_updated`. Rows written before you upgrade keep `household.schedule_updated`. If you search the audit log for schedule changes, search for all three.
- The database migrates to revision `0008_meal_feedback` when Dinnerbell starts, in two steps: `0007_event_autoincrement` rebuilds the `event` table so call IDs are never reused, keeping every row, and `0008_meal_feedback` adds an empty `feedback` table. Both are quick for a household's data. Back up first, as always. To go back to an earlier release, restore that backup: older releases refuse a database at a revision they don't know.
- Decision messages for calls that finished before you upgrade don't get **How was it?** until they're redrawn. Those meals still take feedback from App Home and the web.
- Members who already turned on the feedback reminder in Preferences, which did nothing until now, start getting it for meals that finish after the upgrade, unless they muted direct-message reminders.
- Optional: the Slack app manifests list `version` in the slash command's usage hint. To show it in Slack, open the app's **Slash Commands** settings and edit the command's usage hint to match `deploy/slack-app-manifest.yaml` (or `deploy/slack-app-manifest.test.yaml` for test). `/dinnerbell version` works without this.
- Optional, for test: the test Slack app's manifest lists `discard` in the slash command's usage hint. To show it, edit the `/dinnerbell-test` command's usage hint to match `deploy/slack-app-manifest.test.yaml`.
- Call messages and reminders already posted before you upgrade keep their old buttons until they're redrawn. They still work: the call's old buttons post your card, and an old reminder's buttons reply with a line of text.

## [0.3.0] - 2026-10-24

### Added

- A logo: Dinnerbone, the family's hamster. It shows in the web header, as the browser tab icon, and as the icon of a Home Screen bookmark on iPhone. To use it in Slack, upload `deploy/slack-app-icon-1024.png` in the Slack app's **Basic Information** settings (see [docs/setup.md](docs/setup.md#2-create-the-slack-app)). `scripts/render-logo.sh` regenerates the PNG icons from the SVGs.
- A test instance (`DINNERBELL_INSTANCE_LABEL` set) shows Dinnerbot, Dinnerbone as a half-built blueprint, in the header, the browser tab, and the Home Screen icon, so you can tell it from production at a glance. Upload `deploy/slack-app-icon-test-1024.png` as the test Slack app's icon.
- Export the catalog as a catalog file, in the format import reads ([docs/catalog-format.md](docs/catalog-format.md#export-the-catalog)). Any member can select **Download catalog** on the Catalog page, and `dinnerbell catalog export [--output FILE] [--force]` writes it from the command line, reading the database read-only. Exports are sorted, so two exports of the same catalog are identical, and importing one back changes nothing. They never include cooks, members, preferences, history, or anything else personal. To copy the catalog between production and test, see [docs/setup.md](docs/setup.md#copy-the-catalog-between-production-and-test).
- `dinnerbell config check` names every missing or invalid setting, never its value, and exits 2 if there are any. `--env-file FILE` reads an env file over the environment, in compose's syntax; `-` reads standard input.
- `deploy/dinnerbell-test` checks the env file before every command that can start a container, such as `up`, `run`, and `start`, with `dinnerbell config check` in a one-off container of the same image that has no network, volume, or published port. A missing setting, such as `DINNERBELL_ADMIN_EMAIL`, now stops it with the setting's name, instead of starting a container that restarts forever. `DINNERBELL_ENGINE` picks the container engine for the check.
- `deploy/dinnerbell-test` refuses to start test when `.env.test` and `.env` share `DINNERBELL_SECRET_KEY`, `DINNERBELL_SLACK_BOT_TOKEN`, `DINNERBELL_SLACK_APP_TOKEN`, or `DINNERBELL_OIDC_CLIENT_SECRET`, or when any line of `.env.test` contains production's value of one of them. It never prints the values.
- [docs/setup.md](docs/setup.md#test-instance) shows how both instances share one clone, how to write `.env.test` from `.env` without copying production's secrets, and what to do when the test port hangs.
- Calling a call off isn't admin-only anymore (FR-56). The coordinator can call off a call at any point, and whoever started an ad-hoc call can until it's decided. An optional reason of up to 140 characters goes with it.
  - The reply in the call's thread names who called it off and why, and mentions everyone who was in or maybe and the coordinator. The call message says "Called off by" with the reason, and a decision is struck through.
  - When a decided call is called off, each member assigned to cook, pick up, or order gets a direct message, and assignment or reminder messages that haven't gone out yet are dropped.
  - A call that was never posted, such as one scheduled for a later day, posts nothing. Its starter gets a direct message if someone else called it off.
- Responses close on their own once every active member has answered and nobody is a maybe. If everyone is out, the call ends on-your-own at the next scheduler tick. Otherwise it waits until no response has changed for 10 minutes, so you can add guests, an eat window, or an offer after tapping **In**; with two or more eating, someone must also be able to cook, pick up, or order. Voting then gets its usual length. Answers kept from before a re-run or restart don't count.
- Starting a call for a slot and day that was called off starts it again, for any member, as long as it isn't past the usual meal time plus two hours. It starts with no coordinator.
- On the web, a call's card on Today and on its own page shows **Call it off** to whoever may call it off, and the coordinator and admins get **Start the vote now** while responses are open and **Decide now** while voting. **Call it off** opens a small form with an optional reason and asks you to confirm. A called-off call reads "Called off by {name}" with the reason, and anyone can select **Start dinner again** (or lunch) until the usual meal time plus two hours. Every post is checked again on the server, and a refusal shows on the card in plain words. **Start the vote now** and **Decide now** change nothing if the call has moved on, or if someone else closed the window first, and a card left open while someone called the call off and started it again can't act on the new run; either way, the card asks you to reload.
- Admins can skip one day's call, today or up to 7 days ahead, before it goes out, from **Skip a day** on Admin > Schedule. It offers only meals that have a daily call, and refuses a meal that's off in the schedule. Nothing is posted, the skip is audited, and anyone can still start that call on the day.
- Call it off and Close now in Slack (FR-56):
  - While a window is open, the call message has a ⋯ menu with **Call it off** and **Start the vote now** or **Decide now**. Once decided, the call and decision messages have a **Call it off** button. Anyone who isn't allowed gets a private reply saying who can.
  - **Call it off** opens a modal with an optional reason. Close now asks you to confirm, and changes nothing if the call has moved on by then, or if someone else closed the window first. A menu, button, or modal left over from before a call was re-run or started again does nothing.
  - App Home shows **Call it off** and Close now only to people who can use them, and **Start dinner again** or **Start lunch again** after a call-off, until it's too late.
  - `/dinnerbell cancel [lunch|dinner] [day] [reason]` calls off a call. Without a slot, it calls off that day's only open call, and asks which one when lunch and dinner are both open.
  - A called-off decision message is struck through and says who called it off and why.

### Changed

- The event page's admin-only **Cancel this call** and **Close now** buttons are gone: **Call it off**, **Start the vote now**, and **Decide now** replace them for admins and members alike. For a called-off call, admins see **Start dinner again** instead of **Re-run**. The `POST /events/{id}/admin/cancel` route is removed; use `POST /events/{id}/cancel`.

### Fixed

- The Invite someone form on Admin > Members now defaults to Member. It used to preselect Admin, so inviting someone without changing the role made them an admin.
- A `DINNERBELL_DATABASE_URL` that isn't a URL, or has a malformed port, is reported as a configuration error, without quoting the URL, instead of stopping startup with a traceback.

### Security

- Slack button, menu, and modal values must match exactly what Dinnerbell wrote. A value with a trailing newline, such as `7\n`, is now refused like any other value it didn't write.
- Files served from `/static` carry a Content-Security-Policy that allows nothing (`default-src 'none'` with `sandbox`), so an SVG opened directly can't run script or load anything.

### Upgrade notes

- The database migrates to revision `0006_event_ended_by` when Dinnerbell starts. It adds two empty columns to `event`, so it's quick. Back up first, as always.
- Optional: the Slack app manifests list `cancel` in the slash command's usage hint. To show it in Slack, open the app's **Slash Commands** settings and edit the command's usage hint and description to match `deploy/slack-app-manifest.yaml` (or `deploy/slack-app-manifest.test.yaml` for test). `/dinnerbell cancel` works without this.

## [0.2.0] - 2026-10-23

### Added

- Prebuilt container images on GitHub Container Registry at `ghcr.io/dsmorgan/dinnerbell`. Each release publishes `X.Y.Z`, `X.Y`, and `latest`, and every push to `main` publishes `main` and the short commit SHA. Images carry build provenance and SBOM attestations; [docs/setup.md](docs/setup.md#verify-an-image) shows how to verify one.
- The footer and `/healthz` show the running version, such as `0.2.0`, or `0.2.0+abc1234` for a build from `main`.
- Test instance support ([ADR 0006](docs/adr/0006-test-instance.md)): run a second copy with its own database, Slack app, and hostname.
  - `DINNERBELL_INSTANCE_LABEL` shows a marker such as `TEST` on the web, in Slack posts, on App Home, and in `/healthz`. While it's set, Dinnerbell mentions and messages only members of the configured channel.
  - `DINNERBELL_SLACK_COMMAND` names the slash command. Default: `/dinnerbell`.
  - `DINNERBELL_FEATURES` turns on feature flags. Unknown names stop startup.
  - `dinnerbell scrub-test --keep EMAIL [--keep EMAIL ...] [--yes]` makes a restored production backup safe to use in test. It shows what it will change and asks you to confirm.
  - `deploy/dinnerbell-test` runs every test command with the test project, volume, env file, tag, and port, and refuses unsafe combinations. `deploy/slack-app-manifest.test.yaml` and `.env.test.example` set up the rest.
- `compose.yaml` reads `DINNERBELL_TAG`, `DINNERBELL_ENV_FILE`, and `DINNERBELL_PORT`, which only `deploy/dinnerbell-test` sets. Never set them in `.env` or `export` them: an exported `DINNERBELL_ENV_FILE=.env.test` makes production's own compose commands run with test's settings on production's database.

### Changed

- `compose.yaml` pulls the release image instead of building from source. To build from source, add `-f compose.build.yaml`.
- Production's clone follows release tags: to upgrade, check out the new `vX.Y.Z` tag instead of running `git pull` on `main`.

### Upgrade notes

You switch from building the image to pulling it. Follow "Switch a source-built install to the prebuilt image" in [docs/setup.md](docs/setup.md), which also has undo steps. In short:

1. Note the commit you run now, and back up the database.
2. Run `git fetch --tags && git checkout v0.2.0`, then `podman-compose pull`.
3. Recreate the service: `podman-compose down && podman-compose up -d`.

Run these as the same user and from the same directory as your install. Your data volume keeps its name, so the database carries over. If your install predates M2, its migrations run on start.

podman-compose 1.0.6 or later is required.

## [0.1.0] - 2026-10-23

The source-built version, never published as an image. It covers milestones M0 through M2.

### Added

- **M0:** the container, configuration, migrations, nightly backups, `/healthz`, and sign-in with Slack (or any OpenID Connect provider) for invited members.
- **M1:** the catalog with file import, preferences and vetoes, meal history, and the members admin page.
- **M2:** dinner calls in Slack: the scheduled call, reminders, ranked proposals and votes, the decision, assignments, the abandon path, admin overrides, App Home, and `/dinnerbell call`.

[Unreleased]: https://github.com/dsmorgan/dinnerbell/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/dsmorgan/dinnerbell/releases/tag/v0.8.0
[0.7.0]: https://github.com/dsmorgan/dinnerbell/releases/tag/v0.7.0
[0.6.0]: https://github.com/dsmorgan/dinnerbell/releases/tag/v0.6.0
[0.5.0]: https://github.com/dsmorgan/dinnerbell/releases/tag/v0.5.0
[0.4.0]: https://github.com/dsmorgan/dinnerbell/releases/tag/v0.4.0
[0.3.0]: https://github.com/dsmorgan/dinnerbell/releases/tag/v0.3.0
[0.2.0]: https://github.com/dsmorgan/dinnerbell/releases/tag/v0.2.0
[0.1.0]: https://github.com/dsmorgan/dinnerbell/tree/c179898
