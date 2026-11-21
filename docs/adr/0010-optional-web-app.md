# ADR 0010: An optional web app with push notifications, beside Slack

| | |
|---|---|
| Status | Accepted |
| Date | 2026-11-21 |
| Builds on | [ADR 0001](0001-slack-first-channel.md) (Slack stays the channel), [ADR 0002](0002-oidc-sign-in.md) (Slack sign-in stays) |
| Issue | [#138](https://github.com/dsmorgan/dinnerbell/issues/138), spec FR-53 and [6.15](../spec.md#615-web-app-and-push) |

## Context

Slack has a steep learning curve. The web app already does everything a call needs, but every nudge is a Slack message, and every member signs in with Slack.

ADR 0001 ruled out a native iPhone app. Since iOS 16.4, a site added to the Home Screen can receive web push without an Apple Developer account. As far as we've checked, the installed app shares no cookies with Safari, and links from Messages or Mail open in Safari, so a sign-in link or QR code would sign in Safari, not the app. The owner's rehearsal on a real iPhone confirms this before the issue closes.

The owner's constraints: the feature is optional and changes nothing until an admin turns it on, and a member can use Slack, the web app, or both.

## Decision

**An Admin switch, off by default.** While it's off, the push hook never runs, the manifest, service worker, and code sign-in routes answer 404, and Slack delivery is exactly as today. Turning it off deletes every push subscription.

**Each member chooses Slack, Web app, or Both.** Members who have a Slack user start on **Slack**; members without one start on **Web app**. While the switch is on, Slack direct messages skip members on **Web app**, instead of failing because they have no Slack user. Channel posts still go to the channel.

**Push mirrors what Slack sends the member, through its own queue.** A web-app member gets a push for the direct messages Slack would send them and for a named list of channel posts they'd act on, such as a call opening and the decision. Edits to Slack messages don't push, and neither do badges or the recap. When a call publishes a notification, Dinnerbell also queues push rows for the web-app members it concerns. The push queue has its own drainer and retries, and it applies the same staleness rules as the outbox, so a stale push is dropped, not sent late. The Slack outbox is unchanged, so an outage of either service never delays the other. A payload names the meal and what to do, and never another member's answer.

**Our own Web Push code.** Dinnerbell encrypts payloads per RFC 8291 with `cryptography`, signs VAPID tokens (RFC 8292) with `joserfc`, and sends them with `httpx`. Pushes use the Declarative Web Push format, which iOS 18.4 and later shows even if the service worker fails. That matters because Safari revokes permission after a push that shows nothing. The service worker handles Android and older iOS.

**The VAPID key comes from the secret key.** Dinnerbell derives the push key pair from `DINNERBELL_SECRET_KEY` for its own purpose, as it already derives the CSRF and sign-in keys, so it needs no new secret and stores no key. Changing the secret key ends every push subscription. The sender deletes a subscription the push service rejects, and members tap **Turn on notifications** again.

**A typed one-time code signs in a device.** An admin creates a code for a member, or a member who is already signed in, such as with Slack on a laptop, creates one for themselves. The member types it into the installed app and confirms the name it shows, and that device gets an ordinary session. A member with no Slack account has only this way in. Members can see and sign out their devices, and admins can too. Sign in with Slack is unchanged.

## Reasoning

- **Off means off.** With the switch off, no push code runs and Slack delivery takes no new branch, so Slack-only installs can't regress.
- **A typed code works on an iPhone.** A second OIDC provider sends the member to its own page, and the redirect back can land in Safari. Passkeys still need a first sign-in and add the most code. A member with Slack doesn't need the admin at all.
- **A separate queue keeps Slack's guarantees.** The outbox orders rows per event because Slack messages are edited and threaded. Pushes are neither, and sharing Slack's queue would let one service's outage block the other.
- **Deriving the key avoids a new secret.** A stored key would sit in every backup beside the subscriptions it reaches, and a new secret would add a setup step even for installs that never turn this on.
- **Our own code over pywebpush.** pywebpush is synchronous and pulls in three more packages. RFC 8291 is short on the libraries Dinnerbell already uses.

## Consequences

- Apple's and Google's push services see when each subscribed device gets a push, and how big it is, but not what it says. The **Get the app** page says so.
- The host needs outbound HTTPS to the push services.
- A web-only member has no Slack buttons, App Home, slash command, or badge art in posts. The web pages already cover a call.
- The service worker must be served from the site root with its own headers, not from `/static`, because of its scope and the CSP that `/static` gets.
- Before the issue closes, the owner tests on a real iPhone: install the app, sign in with a code, get the 3:00 PM call, answer, vote, and see the decision. Delivery under Focus modes and Low Power Mode is checked then.
