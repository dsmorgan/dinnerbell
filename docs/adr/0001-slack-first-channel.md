# ADR 0001: Slack is the first channel, behind a channel adapter

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-21 |

## Context

The household is all Apple. iMessage has no public API. A native iOS app needs an Apple Developer account, TestFlight renewals every 90 days, and more build effort than the problem deserves. Web push works on iOS only for web apps added to the Home Screen, shows no action buttons on the notification, and the install step is the hardest thing to ask of a non-technical family member.

The time-sensitive part of the problem is a short conversation: who's in, who can pick up, vote, decision. That is what chat platforms with interactive messages do well. The depth, such as catalog, preferences, history, and badges, needs a real screen.

Candidates: Slack, Discord, Telegram, web push only.

## Decision

Slack carries the conversation. A mobile web app carries the depth, and every Slack message deep-links into it.

The notification layer is a `Channel` protocol. The domain emits typed notifications; the Slack adapter renders them to Block Kit and maps interactions back to service calls. Nothing outside `channels/slack` imports Slack types.

The app connects with Socket Mode, so Slack never calls an inbound URL and only the web app is exposed through Pangolin.

## Why Slack over the alternatives

- **Interactions.** Block Kit has buttons, select menus, date and time pickers, and modals with all of those inside. Discord modals are weaker. Telegram inline keyboards are good but the family doesn't use Telegram.
- **Mobile simplicity.** A single-workspace Slack app on a phone is one channel and a few direct messages. Discord's mobile app surrounds the same content with server lists and upsells.
- **Per-user home.** Slack's App Home gives each member a private dashboard inside Slack. Discord has no equivalent.
- **Adoption.** Some household members have used Slack, and the admin expects to be able to get everyone to install it.
- **Free plan limits don't bite.** The 90-day history limit and the 10-app cap don't matter because Dinnerbell is the system of record and is the only app.

## Consequences

- The family depends on a third-party service for notifications. If Slack changes its free plan, the adapter boundary makes a Discord port a contained piece of work. Discord is the planned second adapter.
- Members need a Slack account and the Slack app. The admin creates one workspace with one channel.
- Socket Mode is available only for apps installed in a single workspace, which is exactly this case. A future App Directory listing would need an HTTP events endpoint.
- Web push for Home Screen web apps stays on the backlog as an optional extra channel, not a replacement.
