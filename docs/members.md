# Members and guests

Admins manage the household on **Members**. This page explains guests, joining from the channel, and the related controls. To link members' Slack accounts, see [Link everyone's Slack account](setup.md#link-everyones-slack-account).

## Guests

A guest eats with the household when a member invites them to a call. Guests answer In or Out and can pitch in, but never vote or suggest ([spec](spec.md) sections 3 and 5.5).

## Join from the channel

**Join from the channel** is on by default. Anyone in the channel who isn't in Dinnerbell can join as a guest, from **Join Dinnerbell** in Slack or by signing in on the web. Dinnerbell first checks with Slack that they're a real person in your workspace and in the channel, so bots, deactivated accounts, and people from other organizations can't join. You get a direct message for each join, with **Make member** and **Remove**. Joined guests appear in every member's **Invite a guest** list.

## Controls on Members

These controls are on **Members**:

- To add only the people you choose, turn **Join from the channel** off. Someone who isn't linked then gets a private "I don't know your Slack account yet" message when they tap a button.
- **Add people from the channel** lists everyone in the channel who isn't in Dinnerbell, to add as a guest or a member.
- If someone you invited by email joins as a guest because their Slack email differs, select **Link to member** on their guest row to move their Slack account to their membership. Anyone can take any name in Slack, so check the Slack account and email shown on the row. If that email isn't the member's, or they use a Slack guest account, you type the member's name to confirm. Admins aren't offered; an admin sets their own Slack member ID.
- **Remove** signs a guest out and stops them from joining again until you select **Add back**.

Joining needs no extra scopes for a public channel. A private channel needs `groups:read`.
