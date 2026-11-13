# Members and guests

Admins manage the household on **Admin** > **Members**. This page covers guests, joining from the channel, editing a member's preferences, and the [Assistant page](#the-assistant-page). To link members' Slack accounts, see [Link everyone's Slack account](setup.md#link-everyones-slack-account).

## Guests

A guest eats with the household when a member invites them to a call. Guests answer In or Out, offer to cook, pick up, or order, and can be assigned those roles. They don't vote, suggest, coordinate, mark a meal done, or give feedback ([spec](spec.md) sections 3 and 5.5). To let a guest do those, make them a member.

## Join from the channel

**Join from the channel** is on by default. Anyone in the channel who isn't in Dinnerbell can join as a guest, from **Join Dinnerbell** in Slack or by signing in on the web. Dinnerbell first checks with Slack that they're a real person in your workspace and in the channel, so bots, deactivated accounts, and people from other organizations can't join. Each admin gets a direct message for each join, with **Make member** and **Remove**. Joined guests appear in every member's **Invite a guest** list.

## A member's preferences

An admin can see and change any member's preferences. On **Admin** > **Members**, select **Preferences** on the member's row, or select **Edit** and then their preferences. You get the member's own **Preferences** page, with a banner that says "You're editing Kim's preferences", and every control on it: **I can usually**, reminders, dietary needs, **Away**, fun mode, light or dark, and each tag and item level. **Preferences** on your own row opens your own page.

- Each change is saved as the member's, as if they had made it. The member isn't messaged, and their sign-in, sessions, and Slack link don't change.
- Each change writes an audit row with you as the actor and the member as the target: `member.preference_set` for a level, with the level before and after; `member.settings_updated` for **I can usually** and reminders; `member.needs_updated` for dietary needs; `member.fun_mode_set` for fun mode; and `member.color_mode_set` for light or dark. A save that changes nothing writes no row. A member's own changes aren't audited, except dietary needs (`guest.needs_updated`) and away days.
- Away days are audited whoever sets them: `member.away_set`, with the first and last day and where they were set (`preferences`, `call`, or `admin`), and `member.away_cleared`. Setting them answers the member Out on calls still taking answers on those days, unless they answered already.
- Dietary needs can't change while a call the member is in is voting. Change them once it's decided.
- Guests have no **Preferences** page. To change a guest's dietary needs, edit their row under **Guests**. Guests set their own fun mode and light, dark, or auto on their **Your dietary needs** page.

## Controls on Members

- To add only the people you choose, turn **Join from the channel** off. Someone who isn't linked then gets a private "I don't know your Slack account yet" message when they tap a button.
- **Add people from the channel** lists everyone in the channel who isn't in Dinnerbell, to add as a guest or a member.
- If someone you invited by email joins as a guest because their Slack email differs, select **Link to member** on their guest row to move their Slack account to their membership. Anyone can take any name in Slack, so check the Slack account and email shown on the row. If that email isn't the member's, or they use a Slack guest account, you type the member's name to confirm. Admins aren't listed; an admin sets their own Slack member ID.
- **Remove** signs a guest out and stops them from joining again until you select **Add back**.

Joining needs no extra scopes for a public channel. A private channel needs `groups:read`.

## The Assistant page

When the household's [assistant](configuration.md#the-assistant) is on, each member and admin has an **Assistant** link in the menu. Guests and deactivated members don't, and the page answers only the member who's signed in.

1. Select **Assistant**.
2. In **Message**, ask in plain words, such as "something we haven't had in a month" or "tacos at 7, I'll cook," and select **Send**, or press Enter. Shift+Enter starts a new line.
3. When the assistant drafts something, a card shows what **Confirm** will do. Select **Confirm** to do it, or **Cancel** to drop it. Nothing changes until you confirm.

The assistant can draft:

- **Catalog and offers:** a proposal, an offer of a catalog item, or new catalog items. New items open the import preview, which only adds; select **Apply** there.
- **Your part in a call:** answer in, out, or maybe, join late, vote or take back a vote, suggest a catalog item, take or give up a role, take back your offer, invite a guest or answer for one you invited, or start a call. Ask for several at once, such as "I'm in, put me down for pickup, and vote for the noodle place", and you get a card for each.
- **Your own data, in private only** (this page or a direct message with Dinnerbell, never a channel thread): your levels on **Preferences**, such as "Veto mushrooms"; feedback on a meal you ate, such as "Rate last night's dinner: hated it, it was cold"; and your away days, such as "I'm away Thursday through Saturday." Ask "What do I veto?" or "Did I rate Tuesday's dinner?" to look up your own. Feedback replaces what you gave on that meal before, and the card says what it replaces.
- **A call you coordinate, in private only:** lock a pick, break a tie, or shuffle the picks while voting; once it's decided, change the plan as **Adjust** does; hand off coordinating; mark the meal done once it's meal time; or call the call off, with a reason. "Lock the taco place, Member 2 picks it up" gives one card for both. In Slack, mention the person, such as "@Sam picks it up". On this page, ask who's in first: the assistant knows people only as "Member 1", "Member 2", and so on, and the card shows their names. If you started an ad-hoc call, you can call it off until it's decided. An admin who isn't the coordinator can't do these through the assistant; use the call's page. See [Coordinating a meal](coordinating.md#coordinate-through-the-assistant).
- **The catalog and meal log, anywhere, a channel thread included:** edit a catalog item, such as "Mark Siam Garden as delivery only", take one off the catalog or put it back; log a meal you had without a call, such as "We had Taqueria for lunch yesterday"; and edit or delete a meal you logged. An edit's card shows each change from old to new, and nothing else changes. Name cooks, or who ate, with a Slack mention, such as "@Kim cooks it too", or say "I"; on this page the assistant can name only you. Ask "What meals did I log?" to see the ones you can change; in a channel thread, the reply leaves out who ate. You can change only meals you logged, even as an admin; an admin changes anyone's on **History**.

Each **Confirm** runs the same checks as the buttons on **Today**, **Preferences**, **Catalog**, **History**, and the feedback form. A draft that no longer fits says why on its card, and nothing changes. A draft that edits your levels, feedback, away days, the plan, a catalog item, or a meal also stops if that changed elsewhere after it was drafted; ask again. A draft lasts 30 minutes, and only you can confirm it, once.

The conversation is private: other members and the admin don't see it. Dinnerbell keeps it for the household's **Conversation hours** after your last message, so follow-ups such as "something cheaper" work. At 0 hours, nothing is kept: each message stands alone, and the page shows only your latest exchange. **Start over** clears the conversation and cancels its waiting drafts.

Replies can use bold, italics, lists, and code. Links in them aren't clickable, so a page the assistant read can't slip you a link to tap.

What the assistant sees:

- The meal log, without who ate, and each item's household hint, such as "Well liked", as on **Catalog**. It never sees anyone else's preferences, ratings, or notes.
- Your own levels, ratings, feedback, and away days, only when you ask in private, and with them nothing about anyone else, such as who else ate a meal or who cooked it.
- For a meal you logged, who ate, as labels, only in private.
- For a catalog edit, the item's name, kind, tags, ways, and meals; never its notes or links, which only your card shows.
- For a call you coordinate, who's in, as labels, with the roles they offered, how many guests are in, which picks are tied, and the plan; never anyone's vote, maybe, or out.
