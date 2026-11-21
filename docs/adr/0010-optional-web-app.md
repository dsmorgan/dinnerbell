# ADR 0010: An optional web app with push notifications, beside Slack

| | |
|---|---|
| Status | Accepted |
| Date | 2026-11-21 |
| Builds on | [ADR 0001](0001-slack-first-channel.md) (Slack stays the channel), [ADR 0002](0002-oidc-sign-in.md) (Slack sign-in stays) |
| Issue | [#138](https://github.com/dsmorgan/dinnerbell/issues/138) |

## Context

Slack has a steep learning curve. The web app already does everything a call needs, but every nudge is a Slack message, and every member signs in with Slack.

ADR 0001 ruled out a native iPhone app. Since iOS 16.4, a site added to the Home Screen can receive web push without an Apple Developer account. On an iPhone, the installed app shares no cookies with Safari, and links from Messages or Mail open in Safari. So a sign-in link or QR code would sign in Safari, not the app.

The owner's constraints: the feature is optional and changes nothing until an admin turns it on. A member can use Slack, the web app, or both.

## Decision

**An Admin switch, off by default.** While it's off, Dinnerbell serves no manifest, service worker, or code sign-in page, keeps no push subscriptions, and sends no pushes. Turning it off later deletes every push subscription and leaves sessions as they are.

**Each member chooses.** On **Preferences**, a member picks **Slack**, **Web app**, or **Both**. Existing and new members start on **Slack**. Members with no Slack account can still choose only **Web app**. The choice changes only how Dinnerbell reaches that member; channel posts still go to the channel.

**An installable web app.** Dinnerbell serves a web app manifest, icons, and a service worker from `web/static`, so the CSP doesn't change. A **Get the app** page shows the Add to Home Screen steps for iPhone and Android, and the **Turn on notifications** button gives the tap that iOS requires.

**Push mirrors what Slack sends the member.** A web-app member gets a push for each channel post they'd act on (a call opens, voting opens, the decision, a call off) and for every direct message Slack would send them (reminders, role requests, hand-offs, feedback reminders). Badge posts and the monthly recap stay on Slack and the web pages. A tap opens the matching page. The payload names the meal, the time, and what to do. It never names another member or their answer.

**A separate queue for push.** When the outbox queues a notification, Dinnerbell also queues one push row for each web-app member it concerns. Push rows have their own drainer, retries, and 24-hour expiry. The Slack outbox, its ordering, and its retries stay as they are, so an outage of either service never delays the other. A push that replaces an earlier one, such as voting closing, uses the same `Topic`, so the phone shows only the newest.

**Our own Web Push code.** Dinnerbell encrypts payloads per RFC 8291 with `cryptography`, signs VAPID tokens (RFC 8292) with `joserfc`, and sends them with `httpx`, all already installed. Pushes use the Declarative Web Push format (iOS 18.4 and later), which shows the notification even if the service worker fails, and Safari revokes permission when a push shows nothing. The service worker handles Android and older iOS. A `404` or `410` from the push service deletes the subscription.

**The VAPID key comes from the secret key.** Dinnerbell derives the VAPID key pair from `DINNERBELL_SECRET_KEY` with HKDF, so it adds no fifth secret and stores no key. Changing the secret key ends every push subscription, and members turn notifications on again. The same change already signs everyone out.

**One-time codes sign in a device.** A code is 10 characters from an alphabet without look-alikes. It's valid for 15 minutes, works once, and is stored only as a hash. An admin creates one for any active member on **Admin** > **Members**. A member who is signed in, such as with Slack on a laptop, creates one for themselves on **Preferences**. The member types the code on the installed app's **Sign in with a code** page, which signs in that device with an ordinary session, the same 90-day lifetime that renews with use. Five wrong tries void the code, and the page is rate-limited per address. **Preferences** lists the member's signed-in devices, and the member or an admin can sign out any of them. Guests can't get codes. Sign in with Slack stays unchanged.

## Reasoning

- **Off means off.** A household that never turns it on runs the same code paths, so the feature can't regress Slack-only installs.
- **A typed code works on an iPhone.** A second OIDC provider sends the member to its own page, and the redirect back can land in Safari. Passkeys still need a first sign-in, and they add the most code. A member with Slack doesn't need the admin at all.
- **A separate queue keeps Slack's guarantees.** The outbox's per-event ordering exists because Slack messages are edited and threaded. Pushes are neither, and tying them to Slack's delivery would let one service's outage block the other.
- **Deriving the key avoids a new secret.** A stored key would sit in every backup beside the subscriptions it reaches, and a fifth secret adds a setup step even for installs that never turn this on.
- **Our own code over pywebpush.** pywebpush is synchronous and pulls in three more packages; RFC 8291 takes about 150 lines on libraries Dinnerbell already uses.

## Consequences

- Apple's and Google's push services see when each subscribed device gets a push, and how big it is, but not its content, which only the device can decrypt. Admin > Members and the **Get the app** page say so.
- Phones must reach the public hostname, which setup already requires, and the host needs outbound HTTPS to `*.push.apple.com` and Google's push service.
- A web-only member has no Slack buttons, App Home, or slash command, and no badge art in posts. The web pages already offer everything a call needs.
- Push delivery under Focus modes and Low Power Mode is not verified. Before the issue closes, the owner tests on a real iPhone: install the app, sign in with a code, get the 3:00 PM call, answer, vote, and see the decision.
