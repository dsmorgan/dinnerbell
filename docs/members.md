# Members and guests

Admins manage the household on **Admin** > **Members**. This page covers guests, joining from the channel, [linking Slack accounts](#link-slack-accounts), editing a member's preferences, the [web app](#the-web-app), [adding a member without Slack](#add-a-member-without-slack), [Dinnerbone's weather](#dinnerbones-weather), and the [Assistant page](#the-assistant-page).

## Guests

A guest eats with the household when a member invites them to a call. Guests answer In or Out, offer to cook, pick up, or order, and can be assigned those roles. They don't vote, suggest, coordinate, mark a meal done, or give feedback ([spec](spec.md) sections 3 and 5.5). To let a guest do those, make them a member.

## Join from the channel

**Join from the channel** is on by default. Anyone in the channel who isn't in Dinnerbell can join as a guest, from **Join Dinnerbell** in Slack or by signing in on the web. Dinnerbell first checks with Slack that they're a real person in your workspace and in the channel, so bots, deactivated accounts, and people from other organizations can't join. Each admin gets a direct message for each join, with **Make member** and **Remove**. Joined guests appear in every member's **Invite a guest** list.

## Link Slack accounts

Dinnerbell needs each member's Slack account to mention them, message them, and accept their button taps. **Admin** > **Members** shows who is linked. Dinnerbell links a member in any of these ways:

- When they sign in with Slack.
- At start and every hour, for active members whose Dinnerbell email matches their Slack email. This links only full members of your workspace, never guests or people from other organizations.
- By hand: on **Admin** > **Members**, select **Edit**, and enter their Slack member ID. In Slack, open their profile, select the three dots, then **Copy member ID**.

A link you set or clear by hand stays as you left it. To hand a member back to automatic linking, select **Back to automatic** on their edit page.

## A member's preferences

An admin can see and change any member's preferences. On **Admin** > **Members**, select **Preferences** on the member's row. You get the member's own **Preferences** page, with a banner that says whose it is, and every control on it.

- Each change is saved as the member's, as if they had made it. The member isn't messaged, and their sign-in, sessions, and Slack link don't change.
- Each change writes an audit row with you as the actor. A member's own changes aren't audited, except dietary needs and away days.
- Setting away days answers the member Out on calls still taking answers on those days, unless they answered already.
- Dietary needs can't change while a call the member is in is voting. Change them once it's decided.
- Guests have no **Preferences** page. To change a guest's dietary needs, edit their row under **Guests**. Guests set their own fun mode and light, dark, or auto on their **Your dietary needs** page.

## Controls on Members

- To add only the people you choose, turn **Join from the channel** off. Someone who isn't linked then gets a private "I don't know your Slack account yet" message when they tap a button.
- **Add people from the channel** lists everyone in the channel who isn't in Dinnerbell, to add as a guest or a member.
- If someone you invited by email joins as a guest because their Slack email differs, select **Link to member** on their guest row to move their Slack account to their membership. Anyone can take any name in Slack, so check the Slack account and email shown on the row. If that email isn't the member's, or they use a Slack guest account, you type the member's name to confirm. Admins aren't listed; an admin sets their own Slack member ID.
- **Remove** signs a guest out and stops them from joining again until you select **Add back**.
- Each member's row but your own has [**Sign out everywhere**](#sign-out-a-lost-device).
- While the web app is on, each active member's row also has **Sign-in code**, which makes a [code](#sign-in-with-a-code) for them.

Joining needs no extra scopes for a public channel. A private channel needs `groups:read`.

### Sign out a lost device

A sign-in lasts 90 days after its last use. If a member loses a phone or laptop that's signed in to Dinnerbell, or it's stolen, an admin signs them out of it:

1. On **Admin** > **Members**, find the member's row.
2. Select **Sign out everywhere**.

That ends the member's sessions on every device, whether or not the web app is on, and deletes any [sign-in code](#sign-in-with-a-code) of theirs not used yet. The member then signs in again on the devices they still have. You don't need to deactivate the member.

While the web app is on, a member can also sign out one device on their own **Preferences** > [**Devices**](#devices). Admins don't have the button on their own row: ask another admin, or, while the web app is on, sign out each device on **Devices**.

## The web app

**Admin** > **Web app** has one switch, **Turn on the web app**, off by default ([ADR 0010](adr/0010-optional-web-app.md), spec [6.15](spec.md#615-web-app-and-push)). While it's off, members see no change.

While it's on:

- Members can add Dinnerbell to their phone's Home Screen. **Get the app**, linked from the sign-in page, from **Preferences**, and from **Admin** > **Web app**, shows the steps for iPhone and Android.
- **Preferences** has **Notifications**: **Slack**, **Web app**, or **Both**. A member linked to Slack starts on **Both**. A member without a Slack link has only **Web app**, and moves to **Both** once they're linked, unless they chose otherwise while linked before.
- Slack skips direct messages, such as reminders, to members on **Web app**. Channel posts still go to the channel.
- Members can [sign in with a code](#sign-in-with-a-code), and see and sign out their [devices](#devices).

A member on **Web app** who hasn't turned notifications on anywhere gets no direct messages at all.

Turning the switch off keeps each member's **Notifications** choice, every session, and every sign-in code, but hides the code pages, **Devices**, and **Sign-in code** until it's on again. It deletes every device's notifications, so each member turns them on again.

### Add a member without Slack

A member doesn't need Slack while the web app is on. They answer calls in the app and get push notifications.

1. On **Admin** > **Web app**, turn on the web app, if it's off.
2. On **Admin** > **Members**, add the member with any email address they use. Dinnerbell sends nothing to it.
3. Select **Sign-in code** on their row, and give them the code in person or by text. It works once, for 15 minutes.
4. On their phone, they open your Dinnerbell address, select **Get the app**, follow its steps, and [sign in with the code](#sign-in-with-a-code) in the installed app.
5. They [turn on notifications](#turn-notifications-on-or-off).

Their **Notifications** choice is **Web app**. If they join Slack later with the same email, Dinnerbell links them, and they move to **Both**.

### Sign in with a code

The app on your Home Screen keeps its own sign-in, apart from the browser's, and a Slack sign-in started there can finish in the browser instead. So inside the app, **Sign in with a code** is the first button on the sign-in page. A member without a Slack account signs in only this way.

First, get a code:

- **If you're signed in somewhere else**, such as with Slack on a laptop: open **Preferences**, and under **Devices**, select **Get a sign-in code**.
- **If you aren't**, ask an admin. On **Admin** > **Members**, they select **Sign-in code** on your row, then give you the code in person or by text.

A code is 10 characters, such as `K7M2Q-9XRTD`. It works once, for 15 minutes, and shows only once, on the page that made it. A new code replaces any code you haven't used. Guests and deactivated members can't get one.

On an iPhone or iPad:

1. Open **Get the app** in Safari, and follow its steps to add Dinnerbell to your Home Screen.
2. Open Dinnerbell from your Home Screen.
3. Tap **Sign in with a code**.
4. Type the code, and tap **Next**. Capitals, spaces, and the dash don't matter.
5. Check that the page says "You're signing in as" and your name, then tap **Continue**.

On Android:

1. Open **Get the app** in Chrome, and follow its steps to install Dinnerbell.
2. Open Dinnerbell from your Home screen.
3. Tap **Sign in with a code**, type the code, and tap **Next**.
4. Check your name, then tap **Continue**.

The app stays signed in for 90 days after you last use it. Signing in with a code on a device that was signed in as someone else signs that session out.

A member who has signed in only with a code shows as **Active, not linked to Slack** on **Admin** > **Members**; before that, as **Invited**. A code never links a Slack account, so their email stays editable, and a Slack account with that verified email can still claim them by signing in with Slack. After that they show as **Active**, and their email is locked.

Changing `DINNERBELL_SECRET_KEY` ends every unused code. If more than 200 wrong codes arrive in an hour, **Admin** > **Web app** says someone may be guessing. Nobody is locked out: each address can try only 20 sign-ins a minute, too few to guess a code before it expires.

### Devices

**Preferences** > **Devices** lists each device you're signed in on, such as "iPhone · Safari", with when it signed in and when it was last seen. **This device** marks the one you're using.

- To sign a device out, such as a lost phone, select **Sign out** on its row. Signing out **This device** signs you out here.
- To sign yourself out everywhere, sign out each device here. An admin can sign a member out of every device with [Sign out everywhere](#sign-out-a-lost-device).

### Turn notifications on or off

If you're on **Web app** or **Both**, you get a push on each device where you turned notifications on. Pushes cover each direct message Slack would send you, such as a reminder or "You're cooking"; a call opening, voting opening, a shuffle (unless you shuffled), the decision, a call off, and a call that ends with everyone on their own (if you were in or maybe); and an offer posted for a meal, unless you made it or are away that day. The monthly recap, badges, and schedule changes don't push. Turning off **Send me reminders** on **Preferences** stops reminder pushes too. A tap opens the call's page, or the offer's. Guests don't get pushes.

On an iPhone or iPad (iOS 16.4 or later), notifications work only in the app on the Home Screen, not in Safari:

1. Add Dinnerbell to the Home Screen, as **Get the app** shows, and open it from there.
2. Sign in in the app, such as with a code (see [Sign in with a code](#sign-in-with-a-code)).
3. Tap the banner's **turn them on** link, or open **Preferences**, and under **Notifications**, tap **Turn on notifications**.
4. Tap **Allow**.

On Android, in Chrome or in the installed app:

1. On **Preferences**, under **Notifications**, tap **Turn on notifications**.
2. Tap **Allow**.

It then says "Notifications are on for this device." To stop them, tap **Turn off** there. You need **Web app** or **Both** chosen above them.

Notifications go with the sign-in that turned them on. Signing out on the device, or the sign-in running out, stops them there. When you sign in again in the installed app, they turn back on by themselves, as long as the device still allows them; in a browser, open **Preferences** to turn them back on. If someone else signs in in the app on the device, it gets their notifications instead of yours. If it says notifications are blocked, allow them for Dinnerbell in the device's settings (on iPhone, **Settings** > **Notifications** > **Dinnerbell**), and open the page again.

Each member can have up to 10 devices; turning on an eleventh drops the one turned on longest ago. Pushes go through the device's push service, from Apple, Google, Mozilla, or Microsoft, which sees when a device gets one and how big it is, but not what it says.

## Dinnerbone's weather

In fun mode, the den on Today and on each call's page shows the season's weather. To show the weather outside instead, turn on local weather ([ADR 0011](adr/0011-local-weather.md)). It works whether or not the web app is on.

1. Open **Admin** > **Web app**, and find **Dinnerbone's weather**.
2. Select **Show local weather**.
3. Enter your **ZIP or postal code**. For a household outside the US, also change **Country** to its two-letter code, such as `CA` or `GB`.
4. Select **Save**.

The card says which place Dinnerbell found, such as "Showing the weather near Elmford, Northshire". Check that it's near you. If Dinnerbell can't find the code, or can't reach Open-Meteo, nothing is saved and the card says so: check the code and the country, or try again in a few minutes.

Within about 5 minutes, the den shows the weather there, and then updates every 1 to 3 hours. If Open-Meteo can't be reached, the card says "Couldn't reach the weather service since" and the time, and the den keeps the last weather for up to 6 hours, then shows the season's weather until Open-Meteo answers again. Nothing else in Dinnerbell waits on it.

**What leaves Dinnerbell.** When you save, Dinnerbell sends Open-Meteo the postal code and country, to find the place. After that, it sends only the place's location rounded to about 10 km. Open-Meteo also sees the server's internet address. Nothing about members is sent. Open-Meteo's free service is for non-commercial use, and its data is licensed CC BY 4.0, which is why the pages credit it.

To turn it off, clear **Show local weather** and select **Save**. Dinnerbell stops asking at once and deletes the place and the weather it stored. The code stays in the field for next time; clear it too if you'd rather it didn't.

## The Assistant page

When the household's [assistant](configuration.md#the-assistant) is on, each member and admin has an **Assistant** link in the menu. Guests and deactivated members don't, and the page answers only the member who's signed in.

1. Select **Assistant**.
2. In **Message**, ask in plain words, such as "something we haven't had in a month" or "tacos at 7, I'll cook," and select **Send**, or press Enter. Shift+Enter starts a new line.
3. When the assistant drafts something, a card shows what **Confirm** will do. Select **Confirm** to do it, or **Cancel** to drop it. Nothing changes until you confirm.

The assistant can draft:

- **Catalog and offers:** a proposal, an offer of a catalog item, or new catalog items. New items open the import preview, which only adds; select **Apply** there.
- **Your part in a call:** answer, join late, vote or take back a vote, suggest a catalog item, take or give up a role, take back your offer, invite a guest or answer for one you invited, or start a call. Ask for several at once, such as "I'm in, put me down for pickup, and vote for the noodle place", and you get a card for each.
- **Your own data, in private only** (this page or a direct message with Dinnerbell, never a channel thread): your levels on **Preferences**, such as "Veto mushrooms"; feedback on a meal you ate; your away days; your dietary needs, from the household's dietary tags (for a new one, add it as a dietary tag on an item in **Catalog** first); and **I can usually**. You can also ask about your own, such as "What do I veto?" Feedback replaces what you gave on that meal before, and the card says what it replaces.
- **A call you [coordinate](coordinating.md#coordinate-through-the-assistant), in private only:** lock a pick, break a tie, or shuffle the picks while voting; once it's decided, change the plan as **Adjust** does; hand off coordinating; mark the meal done once it's meal time; or call the call off, with a reason. If you started an ad-hoc call, you can call it off until it's decided. Name people as you'd say them, such as "Sam picks it up"; when a name could be two people, the assistant asks which one you mean. An admin who isn't the coordinator uses the call's page instead.
- **The catalog and meal log, anywhere, a channel thread included:** edit a catalog item, take one off the catalog or put it back; log a meal you had without a call, such as "We had Taqueria for lunch yesterday"; and edit or delete a meal you logged. An edit's card shows each change from old to new. You can change only meals you logged, even as an admin; an admin changes anyone's on **History**.

Each **Confirm** runs the same checks as the buttons on the other pages. A draft that no longer fits says why on its card, and nothing changes. A draft that edits something, such as your levels, the plan, a catalog item, or a meal, also stops if that changed elsewhere after it was drafted; ask again. A draft lasts 30 minutes, and only you can confirm it, once.

The conversation is private: other members and the admin don't see it. Dinnerbell keeps it for the household's **Conversation hours** after your last message, so follow-ups such as "something cheaper" work. At 0 hours, each message stands alone. **Start over** clears the conversation and cancels its waiting drafts. Links in replies aren't clickable, so a page the assistant read can't slip you a link to tap.

What the assistant sees:

- Each active member's display name, with a label such as "Member 2", so it can tell who you mean. Never a guest or a former member. A name you type, or one the assistant writes, stays in the conversation as written until **Conversation hours** delete it.
- The meal log, without who ate, and each item's household hint, such as "Well liked". It never sees anyone else's preferences, ratings, or notes.
- Your own levels, ratings, feedback, away days, dietary needs, and **I can usually**, only when you ask in private, and with them nothing about anyone else.
- For a meal you logged, who ate, as labels, only in private.
- For a catalog edit, the item's name, kind, tags, ways, and meals; never its notes or links.
- For a call you coordinate, who's in, as labels, with the roles they offered, how many guests are in, which picks are tied, and the plan; never anyone's vote, maybe, or out.

[What Dinnerbell sends Anthropic](configuration.md#what-dinnerbell-sends-anthropic) has the full list.
