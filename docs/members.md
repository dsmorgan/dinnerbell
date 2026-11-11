# Members and guests

Admins manage the household on **Admin** > **Members**. This page explains guests, joining from the channel, and the related controls, and how members use the [Assistant page](#the-assistant-page). To link members' Slack accounts, see [Link everyone's Slack account](setup.md#link-everyones-slack-account).

## Guests

A guest eats with the household when a member invites them to a call. Guests answer In or Out, can offer to cook, pick up, or order, and can be assigned those roles. They never vote, suggest, or coordinate ([spec](spec.md) sections 3 and 5.5). Make a guest a member, and they can [coordinate](coordinating.md).

## Join from the channel

**Join from the channel** is on by default. Anyone in the channel who isn't in Dinnerbell can join as a guest, from **Join Dinnerbell** in Slack or by signing in on the web. Dinnerbell first checks with Slack that they're a real person in your workspace and in the channel, so bots, deactivated accounts, and people from other organizations can't join. You get a direct message for each join, with **Make member** and **Remove**. Joined guests appear in every member's **Invite a guest** list.

## A member's preferences

An admin can see and change any member's preferences (#181). On **Admin** > **Members**, select **Preferences** on the member's row, or select **Edit** and then their preferences. You get the member's own **Preferences** page, with a banner that says "You're editing Kim's preferences", and every control on it: **I can usually**, reminders, dietary needs, **Away**, fun mode, light or dark, and each tag and item level. Your own row's **Preferences** opens your own page.

- Each change is saved as the member's, as if they had made it. The member isn't messaged.
- Your own ID opens your own **Preferences** page; you can't post to your own preferences through the admin's address.
- Each change writes an audit row with you as the actor and the member as the target: `member.preference_set` for a level, with the level before and after, `member.settings_updated` for **I can usually** and reminders, `member.needs_updated` for dietary needs, `member.fun_mode_set` for fun mode, and `member.color_mode_set` for light or dark. A save of settings, dietary needs, fun mode, or light or dark that changes nothing writes no row. A member's own changes aren't audited, except dietary needs, which are audited as `guest.needs_updated`, and away days.
- Away days (FR-63) are audited whoever sets them, the member or you: `member.away_set`, with the first and last day and where they were set (`preferences`, `call`, or `admin`), and `member.away_cleared`. Setting them answers the member Out on calls still taking answers on those days, unless they answered already.
- The member's sign-in, sessions, and Slack link don't change.
- As for the member, dietary needs can't change while a call the member is in is voting. Change them once it's decided.
- Guests have no **Preferences** page. To change a guest's dietary needs, edit their row in **Guests**. A guest turns fun mode on or off, and chooses light, dark, or auto, on their own dietary-needs page.

## Controls on Members

These controls are on **Admin** > **Members**:

- To add only the people you choose, turn **Join from the channel** off. Someone who isn't linked then gets a private "I don't know your Slack account yet" message when they tap a button.
- **Add people from the channel** lists everyone in the channel who isn't in Dinnerbell, to add as a guest or a member.
- If someone you invited by email joins as a guest because their Slack email differs, select **Link to member** on their guest row to move their Slack account to their membership. Anyone can take any name in Slack, so check the Slack account and email shown on the row. If that email isn't the member's, or they use a Slack guest account, you type the member's name to confirm. Admins aren't offered; an admin sets their own Slack member ID.
- **Remove** signs a guest out and stops them from joining again until you select **Add back**.

Joining needs no extra scopes for a public channel. A private channel needs `groups:read`.

## The Assistant page

When the household's assistant is on ([The assistant](configuration.md#the-assistant)), each member and admin has an **Assistant** link in the menu. Guests and deactivated members don't, and the page answers only the member who's signed in.

1. Select **Assistant**.
2. In **Message**, ask in plain words, such as "something we haven't had in a month" or "tacos at 7, I'll cook," and select **Send**. The reply appears under your message.
3. When the assistant drafts something, a card shows what **Confirm** will do. Select **Confirm** to do it, or **Cancel** to drop it. Nothing changes until you confirm. The assistant can draft:
   - A proposal, an offer of a catalog item, or new catalog items.
   - What you'd do on a call yourself: answer in, out, or maybe, join late, vote or take back a vote, suggest a catalog item, take or give up a role, take back an offer you made, invite a guest or answer for one you invited, or start a call. Ask for several at once, such as "I'm in, put me down for pickup, and vote for the noodle place", and you get a card for each.
   - On the Assistant page and in your direct messages with Dinnerbell only, never in a channel thread: your own levels on **Preferences**, such as "Veto mushrooms"; feedback on a meal you ate, such as "Rate last night's dinner: hated it, it was cold"; and your away days, such as "I'm away Thursday through Saturday." Ask "What do I veto?" or "Did I rate Tuesday's dinner?" and it looks up your own. Feedback replaces what you gave on that meal before, as the feedback form does, and the card says what it replaces.
   - In the same private places, for a call you coordinate: lock a pick, break a tie, or shuffle the picks while voting; once it's decided, change the plan as **Adjust** does (the meal time, the place, the way, and who cooks, orders, or picks up); hand off coordinating; mark the meal done once it's meal time; or call the call off, with a reason. You can say "lock the taco place, Member 2 picks it up", and get one card for both. In Slack, mention the person, such as "@Sam picks it up". On this page, ask who's in first: the assistant knows people only as "Member 1", "Member 2", and so on, and its reply and the card show their names. If you started an ad-hoc call, you can call it off until it's decided. An admin who isn't the coordinator can't do any of these through the assistant; use the call's page on the web.
   - Anywhere, a channel thread included: edit a catalog item, such as "Mark Siam Garden as delivery only", or take one off the catalog or put it back; log a meal you had without a call, such as "We had Taqueria for lunch yesterday"; and edit or delete a meal you logged. The card for an edit shows each change from what it is to what it becomes, and nothing else changes. Name cooks, or who ate, by mentioning them in Slack, such as "@Kim cooks it too", or say "I": the assistant knows people only by label, so on this page it can name only you. Ask "What meals did I log?" to see the ones you can change. You can change only the meals you logged, even as an admin; an admin changes anyone's on **History**.
   - Each one runs through the same checks as the buttons on **Today**, **Preferences**, **Catalog**, **History**, and the feedback form, so a draft that no longer fits shows why on its card, and nothing changes. A draft of your own levels, feedback, or away days, a change to the plan, a catalog edit, or a change to a meal also stops if what it changes changed elsewhere after it was drafted: ask again for a new one.
   - New catalog items open the import preview, which only adds: items the catalog has stay as they are. Select **Apply** there to add them.
   - A draft lasts 30 minutes, and only you can confirm it, once.

The conversation is private: other members and the admin don't see it. Dinnerbell keeps it for the household's **Conversation hours** after your last message, so follow-ups such as "something cheaper" work. At 0 hours, nothing is kept: each message stands alone, and the page shows only your latest message and its answer. To clear the conversation sooner, select **Start over**, which also cancels its waiting drafts.

Replies are plain text. Links in them aren't clickable, so a page the assistant read can't slip you a link to tap.

To answer questions about what the household has eaten, such as "When did we last have Thai?", the assistant sees the meal log without who ate, and each item's household hint, such as "Well liked", the same words you see on **Catalog**. It never sees anyone else's preferences, ratings, or notes. It sees your own levels, ratings, feedback, and away days only when you ask about them, on this page or in a direct message, never in a channel thread, and with them nothing about anyone else, such as who else ate a meal or who cooked it. For a meal you logged, it sees who ate, as labels. To edit a catalog item, it sees the item's name, kind, tags, ways, and meals, never its notes or links as they are: only your card shows those. For a call you coordinate, it sees who's in, as labels, with the roles they offered, how many guests are in, which picks are tied, and the plan: never anyone's vote, maybe, or out.
