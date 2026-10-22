# ADR 0002: Generic OpenID Connect sign-in, Slack as the configured provider

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-21 |

## Context

The web app needs to know who the user is to record responses, preferences, and contributions. The Slack adapter already identifies people by Slack user ID. Pangolin sits in front of the app and can require its own login, but it is a separate identity from the one Slack sees, and stacking two logins means two sign-in screens per device.

A later phase reads Google Calendar availability. Google OAuth for `calendar.readonly` is a sensitive scope that requires app verification for consumer Gmail accounts, and unverified apps in testing mode expire refresh tokens after seven days. Choosing Google as the login provider would not avoid that.

Options: Pangolin-only auth, Sign in with Slack, Sign in with Google, magic-link email, or a generic OIDC client with the provider as configuration.

## Decision

The web app implements a generic OpenID Connect client: discovery, authorization code flow, `state`, `nonce`, and PKCE where the provider supports it. The provider is configuration.

The household configures Slack as the provider. Every member already has Slack for notifications, Slack publishes an OIDC discovery document, and the OIDC subject maps directly to the Slack user ID, so there is no account-linking step.

Membership is invite-only. The admin invites by email; the first sign-in with a verified matching email activates the member and stores the OIDC subject. Unknown emails are refused.

The Pangolin resource runs with resource authentication off. Pangolin provides TLS, the hostname, and connection protections; the app owns authentication.

Calendar access in the later phase uses each member's Google Calendar secret iCal URL, polled read-only, which needs no Google OAuth app at all.

## Consequences

- One identity across Slack and the web. Deep links from Slack open the web app with no extra login after the first device sign-in.
- Open-source users without Slack set three environment variables to point at Google or any other OIDC provider. Nothing in the code is Slack-specific on the auth path.
- The app is the only barrier on a public hostname, so the security requirements in the spec (sessions, CSRF, rate limits, headers, audit) are mandatory, not optional.
- If the household later wants Pangolin's login as a second layer, it can be enabled on the resource at the cost of a second sign-in per device. The app does not depend on Pangolin headers either way.
- Magic-link email was rejected because sending mail reliably from a homelab is its own project.
