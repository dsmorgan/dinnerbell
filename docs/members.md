# Members and guests

Admins manage the household on **Admin** > **Members**. This page covers guests, joining from the channel, editing a member's preferences, the [web app](#the-web-app), [Dinnerbone's weather](#dinnerbones-weather), and the [Assistant page](#the-assistant-page). To link members' Slack accounts, see [Link everyone's Slack account](setup.md#link-everyones-slack-account).

## Guests

A guest eats with the household when a member invites them to a call. Guests answer In or Out, offer to cook, pick up, or order, and can be assigned those roles. They don't vote, suggest, coordinate, mark a meal done, or give feedback ([spec](spec.md) sections 3 and 5.5). To let a guest do those, make them a member.

## Join from the channel

**Join from the channel** is on by default. Anyone in the channel who isn't in Dinnerbell can join as a guest, from **Join Dinnerbell** in Slack or by signing in on the web. Dinnerbell first checks with Slack that they're a real person in your workspace and in the channel, so bots, deactivated accounts, and people from other organizations can't join. Each admin gets a direct message for each join, with **Make member** and **Remove**. Joined guests appear in every member's **Invite a guest** list.

## A member's preferences

An admin can see and change any member's preferences. On **Admin** > **Members**, select **Preferences** on the member's row, or select **Edit** and then their preferences. You get the member's own **Preferences** page, with a banner that says "You're editing Kim's preferences", and every control on it: **I can usually**, reminders, dietary needs, **Away**, fun mode, light or dark, **Notifications** while the [web app](#the-web-app) is on, and each tag and item level. **Preferences** on your own row opens your own page.

- Each change is saved as the member's, as if they had made it. The member isn't messaged, and their sign-in, sessions, and Slack link don't change.
- Each change writes an audit row with you as the actor and the member as the target: `member.preference_set` for a level, with the level before and after; `member.settings_updated` for **I can usually** and reminders; `member.needs_updated` for dietary needs; `member.fun_mode_set` for fun mode; `member.color_mode_set` for light or dark; and `member.notify_via_set` for **Notifications**. A save that changes nothing writes no row. A member's own changes aren't audited, except dietary needs (`guest.needs_updated`) and away days.
- Away days are audited whoever sets them: `member.away_set`, with the first and last day and where they were set (`preferences`, `call`, or `admin`), and `member.away_cleared`. Setting them answers the member Out on calls still taking answers on those days, unless they answered already.
- Dietary needs can't change while a call the member is in is voting. Change them once it's decided.
- Guests have no **Preferences** page. To change a guest's dietary needs, edit their row under **Guests**. Guests set their own fun mode and light, dark, or auto on their **Your dietary needs** page.

## Controls on Members

- To add only the people you choose, turn **Join from the channel** off. Someone who isn't linked then gets a private "I don't know your Slack account yet" message when they tap a button.
- **Add people from the channel** lists everyone in the channel who isn't in Dinnerbell, to add as a guest or a member.
- If someone you invited by email joins as a guest because their Slack email differs, select **Link to member** on their guest row to move their Slack account to their membership. Anyone can take any name in Slack, so check the Slack account and email shown on the row. If that email isn't the member's, or they use a Slack guest account, you type the member's name to confirm. Admins aren't listed; an admin sets their own Slack member ID.
- **Remove** signs a guest out and stops them from joining again until you select **Add back**.
- While the [web app](#the-web-app) is on, each active member's row has **Sign-in code**, which makes a [code](#sign-in-with-a-code) for them, and each other member's row has **Sign out everywhere**, which signs them out of every [device](#devices).

Joining needs no extra scopes for a public channel. A private channel needs `groups:read`.

## The web app

**Admin** > **Web app** has one switch, **Turn on the web app**, off by default ([ADR 0010](adr/0010-optional-web-app.md), spec [6.15](spec.md#615-web-app-and-push)). While it's off, members see no change, and Slack delivery is unchanged; Admin gains a **Web app** tab. Turning it on or off writes a `household.web_app_updated` audit row.

While it's on:

- Members can add Dinnerbell to their phone's Home Screen. **Get the app**, linked from the sign-in page, from **Preferences**, and from **Admin** > **Web app**, shows the steps for iPhone and Android.
- **Preferences** has **Notifications**: **Slack**, **Web app**, or **Both**. A member linked to Slack starts on **Both**, so Slack keeps reaching them and a device where they turn notifications on gets pushes too. A member without a Slack link has only **Web app**, and moves to **Both** once they're linked, unless they chose otherwise while linked before.
- Slack skips direct messages, such as reminders, to members on **Web app**, instead of failing to send them. Channel posts still go to the channel.
- Members can [sign in with a code](#sign-in-with-a-code), and see and sign out their [devices](#devices).

A member on **Web app** who hasn't turned notifications on anywhere gets no direct messages at all, as a member without Slack gets none today; Dinnerbell logs a warning for each one it skips for a member who isn't linked to Slack. Turning the switch off keeps each member's choice for when it's on again. It also keeps every session and sign-in code, but the code pages, **Devices**, and the buttons on **Admin** > **Members** disappear until it's on again. It deletes every device's notifications: each member turns them on again.

### Sign in with a code

The app on your Home Screen keeps its own sign-in, apart from the browser's, and a Slack sign-in started there can finish in the browser instead. So inside the app, **Sign in with a code** is the first button on the sign-in page. A member without a Slack account signs in only this way.

First, get a code:

- **If you're signed in somewhere else**, such as with Slack on a laptop: open **Preferences**, and under **Devices**, select **Get a sign-in code**.
- **If you aren't**, ask an admin. On **Admin** > **Members**, they select **Sign-in code** on your row, then give you the code in person or by text.

A code is 10 characters, such as `K7M2Q-9XRTD`. It works once, for 15 minutes. Dinnerbell shows it only once, on the page that made it. A new code replaces any code you haven't used, whoever made it. Guests and deactivated members can't get one.

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

The app stays signed in for 90 days after you last use it, as a Slack sign-in does. Signing in with a code on a device that was signed in as someone else signs that session out.

A member who has signed in only with a code shows as **Active, not linked to Slack** on **Admin** > **Members**; until then they show as **Invited**. A code never links a Slack account, so their email stays editable, and a Slack account with that verified email can still claim them by signing in with Slack, as for anyone invited. After that they show as **Active**, and their email is locked.

Dinnerbell stores a code only as a keyed hash, made with a key from `DINNERBELL_SECRET_KEY`, so a copy of the database can't be searched for codes. Changing the secret key ends every unused code. Deactivating a member, or signing them out everywhere, deletes their unused code too.

Making a code writes a `member.sign_in_code_created` audit row, and signing in with one writes `member.signed_in_with_code`; neither holds the code. Each address can try 20 sign-ins a minute. If more than 200 wrong codes arrive in an hour from all addresses together, Dinnerbell logs a warning, and **Admin** > **Web app** says someone may be guessing until an hour after the wrong codes stop. Nothing slows down and nobody is locked out: with 20 tries a minute per address, guessing a code before it expires is hopeless.

### Devices

**Preferences** > **Devices** lists each device you're signed in on, such as "iPhone · Safari", with when it signed in and when it was last seen. **This device** marks the one you're using. Devices that signed in before this list existed show "Unknown device".

- To sign a device out, such as a lost phone, select **Sign out** on its row. Signing out **This device** signs you out here.
- An admin can sign a member out of every device: on **Admin** > **Members**, select **Sign out everywhere** on their row. It also deletes any code of theirs not used yet, so a lost phone's code can't sign in afterward. It writes a `member.signed_out_everywhere` audit row. To sign yourself out everywhere, use **Devices** on your own **Preferences**.

### Turn notifications on or off

A member on **Web app** or **Both** gets a push on each device where they turned notifications on: for each direct message Slack would send them, such as a reminder, "You're cooking", or "Dinner moved to Taqueria", and for a call opening, voting opening, a shuffle of the picks (unless you shuffled), the decision, a call off, and a call that ends with everyone on their own (if you were in or maybe). They also get one for an offer posted for a meal, unless they made it or are away that day. The monthly recap, badges, and schedule changes don't push. A newer push about a call's posts replaces the older one on the lock screen, so "Dinner: you're on your own" replaces "Dinner: are you in?"; direct messages stay until you clear them. Turning off **Send me reminders** on **Preferences** stops reminder pushes too. A push names the meal, the time, and what to do, and a tap opens the call's page, or the offer's. Guests don't get pushes.

On an iPhone or iPad (iOS 16.4 or later), notifications work only in the app on the Home Screen, not in Safari:

1. Add Dinnerbell to the Home Screen, as **Get the app** shows, and open it from there.
2. Sign in in the app, such as with a code (see [Sign in with a code](#sign-in-with-a-code)). It keeps its own sign-in, apart from Safari's.
3. Tap the banner's **turn them on** link, or open **Preferences**, and under **Notifications**, tap **Turn on notifications**.
4. Tap **Allow**.

On Android, in Chrome or in the installed app:

1. On **Preferences**, under **Notifications**, tap **Turn on notifications**.
2. Tap **Allow**.

It then says "Notifications are on for this device." To stop them, tap **Turn off** there. **Get the app** has the same buttons. You need **Web app** or **Both** chosen above them.

Notifications go with the sign-in that turned them on. Signing out on the device, or the sign-in running out, stops them there. When you sign in again in the installed app, the first page you open turns them back on by itself, as long as the device still allows them. In a browser outside the app, open **Preferences** to turn them back on. If someone else signs in in the app on the device, it gets their notifications instead of yours, and none still waiting for you. If it says notifications are blocked, allow them for Dinnerbell in the device's settings (on iPhone, **Settings** > **Notifications** > **Dinnerbell**), and open the page again.

Each member can have up to 10 devices; turning on an eleventh drops the one turned on longest ago. Pushes go through the device's push service, from Apple, Google, Mozilla, or Microsoft, which sees when a device gets one and how big it is, but not what it says.

## Dinnerbone's weather

In fun mode, the den on Today and on each call's page shows the season's weather, a little different each day and the same for everyone in the household. To show the weather outside instead, turn on local weather ([ADR 0011](adr/0011-local-weather.md), spec 11, "Fun mode"). It works whether or not the web app is on.

1. Open **Admin** > **Web app**, and find **Dinnerbone's weather**.
2. Select **Show local weather**.
3. Enter your **ZIP or postal code**. For a household outside the US, also change **Country** to its two-letter code, such as `CA` or `GB`.
4. Select **Save**.

Dinnerbell looks the code up once, with Open-Meteo, and the card says which place it found, such as "Showing the weather near Elmford, Northshire". Check that it's near you. If Dinnerbell can't find the code, or can't reach Open-Meteo, nothing is saved and the card says so: check the code and the country, or try again in a few minutes.

Within about 5 minutes, the den shows the weather there, and Today and each call's page credit "Weather data by Open-Meteo.com". After that, Dinnerbell asks again at a random time every 1 to 3 hours. The card shows when the weather last arrived. If Open-Meteo can't be reached, the card says "Couldn't reach the weather service since" and the time, and the den keeps the last weather for up to 6 hours, then shows the season's weather until Open-Meteo answers again. Nothing else in Dinnerbell waits on it.

**What leaves Dinnerbell.** When you save, Dinnerbell sends Open-Meteo the postal code and country, to find the place. After that, it sends only the place's location rounded to about 10 km. Open-Meteo also sees the server's internet address. Nothing about members is sent. Open-Meteo's free service is for non-commercial use, and its data is licensed CC BY 4.0, which is why the pages credit it. Logs and the audit log never record the code, the place, or its location.

To turn it off, clear **Show local weather** and select **Save**. Dinnerbell stops asking at once, deletes the place and the weather it stored, and the den shows the season's weather. The code stays in the field for next time; clear it too if you'd rather it didn't. Each change writes a `household.weather_updated` audit row, with whether it's on and whether the code changed.

## The Assistant page

When the household's [assistant](configuration.md#the-assistant) is on, each member and admin has an **Assistant** link in the menu. Guests and deactivated members don't, and the page answers only the member who's signed in.

1. Select **Assistant**.
2. In **Message**, ask in plain words, such as "something we haven't had in a month" or "tacos at 7, I'll cook," and select **Send**, or press Enter. Shift+Enter starts a new line.
3. When the assistant drafts something, a card shows what **Confirm** will do. Select **Confirm** to do it, or **Cancel** to drop it. Nothing changes until you confirm.

The assistant can draft:

- **Catalog and offers:** a proposal, an offer of a catalog item, or new catalog items. New items open the import preview, which only adds; select **Apply** there.
- **Your part in a call:** answer in, out, or maybe, join late, vote or take back a vote, suggest a catalog item, take or give up a role, take back your offer, invite a guest or answer for one you invited, or start a call. Ask for several at once, such as "I'm in, put me down for pickup, and vote for the noodle place", and you get a card for each.
- **Your own data, in private only** (this page or a direct message with Dinnerbell, never a channel thread): your levels on **Preferences**, such as "Veto mushrooms"; feedback on a meal you ate, such as "Rate last night's dinner: hated it, it was cold"; your away days, such as "I'm away Thursday through Saturday."; your dietary needs, such as "Add vegetarian to my needs"; and **I can usually**, such as "Turn off pick up for me." Ask "What do I veto?", "Did I rate Tuesday's dinner?", or "Am I set to cook by default?" to look up your own. Dietary needs are the household's dietary tags, the ones **Preferences** lists. For a new one, add it as a dietary tag on an item in **Catalog** first. Feedback replaces what you gave on that meal before, and the card says what it replaces.
- **A call you coordinate, in private only:** lock a pick, break a tie, or shuffle the picks while voting; once it's decided, change the plan as **Adjust** does; hand off coordinating; mark the meal done once it's meal time; or call the call off, with a reason. "Lock the taco place, Member 2 picks it up" gives one card for both. Name people as you'd say them, such as "Sam picks it up", or mention them in Slack. The assistant knows each active member's display name, so "Theo" finds Theodore; when a name could be two people, it asks which one you mean. The card shows their names. If you started an ad-hoc call, you can call it off until it's decided. An admin who isn't the coordinator can't do these through the assistant; use the call's page. See [Coordinating a meal](coordinating.md#coordinate-through-the-assistant).
- **The catalog and meal log, anywhere, a channel thread included:** edit a catalog item, such as "Mark Siam Garden as delivery only", take one off the catalog or put it back; log a meal you had without a call, such as "We had Taqueria for lunch yesterday"; and edit or delete a meal you logged. An edit's card shows each change from old to new, and nothing else changes. Name cooks, or who ate, as you'd say them, such as "Kim cooks it too", or say "I": the assistant matches a typed name to the household's active members, on this page and in Slack, and asks which one you mean when a name could be two people. Ask "What meals did I log?" to see the ones you can change; in a channel thread, the reply leaves out who ate. You can change only meals you logged, even as an admin; an admin changes anyone's on **History**.

Each **Confirm** runs the same checks as the buttons on **Today**, **Preferences**, **Catalog**, **History**, and the feedback form. A draft that no longer fits says why on its card, and nothing changes. A draft that edits your levels, feedback, away days, dietary needs, **I can usually**, the plan, a catalog item, or a meal also stops if that changed elsewhere after it was drafted; ask again. A draft lasts 30 minutes, and only you can confirm it, once.

The conversation is private: other members and the admin don't see it. Dinnerbell keeps it for the household's **Conversation hours** after your last message, so follow-ups such as "something cheaper" work. At 0 hours, nothing is kept: each message stands alone, and the page shows only your latest exchange. **Start over** clears the conversation and cancels its waiting drafts.

Replies can use bold, italics, lists, and code. Links in them aren't clickable, so a page the assistant read can't slip you a link to tap.

What the assistant sees:

- Each active member's display name, with the label the conversation gives them, such as "Member 2", so it can tell who you mean by a name you type. Never a guest or a former member. A name you type, or one the assistant writes in a reply, stays in the conversation as written until **Conversation hours** delete it.
- The meal log, without who ate, and each item's household hint, such as "Well liked", as on **Catalog**. It never sees anyone else's preferences, ratings, or notes.
- Your own levels, ratings, feedback, away days, dietary needs, and **I can usually**, only when you ask in private, and with them nothing about anyone else, such as who else ate a meal or who cooked it.
- For a meal you logged, who ate, as labels, only in private.
- For a catalog edit, the item's name, kind, tags, ways, and meals; never its notes or links, which only your card shows.
- For a call you coordinate, who's in, as labels, with the roles they offered, how many guests are in, which picks are tied, and the plan; never anyone's vote, maybe, or out.
