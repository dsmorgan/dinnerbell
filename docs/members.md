# Members and guests

Admins manage the household on **Admin** > **Members**. This page explains guests, joining from the channel, and the related controls. To link members' Slack accounts, see [Link everyone's Slack account](setup.md#link-everyones-slack-account).

## Guests

A guest eats with the household when a member invites them to a call. Guests answer In or Out, can offer to cook, pick up, or order, and can be assigned those roles. They never vote, suggest, or coordinate ([spec](spec.md) sections 3 and 5.5). Make a guest a member, and they can [coordinate](coordinating.md).

## Join from the channel

**Join from the channel** is on by default. Anyone in the channel who isn't in Dinnerbell can join as a guest, from **Join Dinnerbell** in Slack or by signing in on the web. Dinnerbell first checks with Slack that they're a real person in your workspace and in the channel, so bots, deactivated accounts, and people from other organizations can't join. You get a direct message for each join, with **Make member** and **Remove**. Joined guests appear in every member's **Invite a guest** list.

## A member's preferences

An admin can see and change any member's preferences (#181). On **Admin** > **Members**, select **Preferences** on the member's row, or select **Edit** and then their preferences. You get the member's own **Preferences** page, with a banner that says "You're editing Kim's preferences", and every control on it: **I can usually**, reminders, dietary needs, **Away**, fun mode, and each tag and item level. Your own row's **Preferences** opens your own page.

- Each change is saved as the member's, as if they had made it. The member isn't messaged.
- Your own ID opens your own **Preferences** page; you can't post to your own preferences through the admin's address.
- Each change writes an audit row with you as the actor and the member as the target: `member.preference_set` for a level, with the level before and after, `member.settings_updated` for **I can usually** and reminders, `member.needs_updated` for dietary needs, and `member.fun_mode_set` for fun mode. A save of settings, dietary needs, or fun mode that changes nothing writes no row. A member's own changes aren't audited, except dietary needs, which are audited as `guest.needs_updated`, and away days.
- Away days (FR-63) are audited whoever sets them, the member or you: `member.away_set`, with the first and last day and where they were set (`preferences`, `call`, or `admin`), and `member.away_cleared`. Setting them answers the member Out on calls still taking answers on those days, unless they answered already.
- The member's sign-in, sessions, and Slack link don't change.
- As for the member, dietary needs can't change while a call the member is in is voting. Change them once it's decided.
- Guests have no **Preferences** page. To change a guest's dietary needs, edit their row in **Guests**. A guest turns fun mode on or off on their own dietary-needs page.

## Controls on Members

These controls are on **Admin** > **Members**:

- To add only the people you choose, turn **Join from the channel** off. Someone who isn't linked then gets a private "I don't know your Slack account yet" message when they tap a button.
- **Add people from the channel** lists everyone in the channel who isn't in Dinnerbell, to add as a guest or a member.
- If someone you invited by email joins as a guest because their Slack email differs, select **Link to member** on their guest row to move their Slack account to their membership. Anyone can take any name in Slack, so check the Slack account and email shown on the row. If that email isn't the member's, or they use a Slack guest account, you type the member's name to confirm. Admins aren't offered; an admin sets their own Slack member ID.
- **Remove** signs a guest out and stops them from joining again until you select **Add back**.

Joining needs no extra scopes for a public channel. A private channel needs `groups:read`.
