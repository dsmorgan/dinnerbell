# ADR 0011: Local weather for Dinnerbone's Den, from Open-Meteo, only if an admin turns it on

| | |
|---|---|
| Status | Accepted |
| Date | 2026-11-22 |
| Builds on | [ADR 0009](0009-optional-assistant.md) (fetching from the internet), [ADR 0010](0010-optional-web-app.md) (a fixed host allowlist for push) |
| Issue | [#319](https://github.com/dsmorgan/dinnerbell/issues/319), spec 11 ("Fun mode") |

## Context

The den's weather follows the season alone. #319 makes it vary, and can show the weather outside. That's Dinnerbell's first outbound call that isn't Slack, a push service, Anthropic, or a member's menu link, and it tells a third party roughly where the household lives.

The owner's constraints: an admin setting turns it on, never an environment variable or flag. Refreshes come at random times, 1 to 3 hours apart. Any failure falls back to seasonal weather without touching the rest of the app, and the logs say so without flooding.

## Decision

**Seasonal weather by default and as the fallback.** The den picks the day's weather from the season's weighted choices, seeded by the household, the local date, and the time of day, with no network. Piles of snow or leaves, and the puddle, show only on days whose weather allows them. The spec has the tables.

**Open-Meteo, with no key.** Current conditions come from `api.open-meteo.com/v1/forecast` as a WMO weather code, temperature, and wind for the domain to map. It's free for non-commercial use. Its data is CC BY 4.0, so while local weather is on, Today and a call's page show "Weather data by Open-Meteo.com", linked to its site.

**A postal code, geocoded once, on save.** Open-Meteo's geocoding API (`geocoding-api.open-meteo.com/v1/search`) documents `name` as "location name or postal code", filtered by `countryCode`. On 2026-11-22 we checked that a US ZIP code returns the populated place whose postcodes include it, and that an unknown code returns nothing. Saving sends the code and country once, then stores the place's coordinates rounded to 0.1° (about 11 km) and its name, which the card shows so the admin can check it. A code that finds no place, or a failed lookup, saves nothing and says why.

**A switch with a field, on Admin > Web app.** The card **Dinnerbone's weather** has a checkbox, **Show local weather**, a **ZIP or postal code**, and a **Country**, US by default (as **Web app** and **Assistant** pair a checkbox with its fields). Turning it on needs a valid code, and the code stays in the field while it's off. Each change writes a `household.weather_updated` audit row with `on` and whether the place changed, never the code or place. It's on the **Web app** tab because the den exists only on the web, and on a 390 px phone Admin's five tabs already wrap onto two rows.

**Privacy.** Open-Meteo sees the postal code and country on each save, the rounded coordinates on each refresh, and the server's IP address. Nothing about members leaves the house, and requests carry no cookies. The card says this. Logs carry the code, place, or coordinates only at debug level, and audit rows never.

**Egress rules.** The two hosts are a fixed allowlist in the domain, and Dinnerbell builds every URL itself. Each request must pass `fetch_policy.parse_target` (https, port 443, no user info) and the allowlist, follows no redirects, allows 5 seconds in all, reads at most 64 KB, and accepts only `application/json`. As in the push sender, there are no retries and no proxy or CA bundle from the environment. The hosts are fixed, so it skips the menu fetcher's every-address-is-public check, which guards links a member chose.

**Cached, with a randomized refresh.** A scheduler job looks every 5 minutes for households that are due. Before each request, it sets the next fetch to a random time, 1 hour to 3 hours less one check later, so fetches come 1 to 3 hours apart, a crash can't cause an extra one, and requests don't line up on the clock. The last good answer is shown for up to 6 hours, then the den goes seasonal. Pages read only that stored answer.

**Failure is quiet and contained.** A timeout, network error, bad status, redirect, oversized or non-JSON body, or an answer that makes no sense keeps the last good answer, or seasonal weather, and records when failures began. The first failure logs a warning with its reason code, later ones log at debug, and the next success logs at info. The card shows "Updated at 3:15 PM" or "Couldn't reach the weather service since Nov 22 at 1:05 PM".

**Off means off.** Turning it off deletes the stored coordinates, place, and weather in the same transaction, so no fetch runs, the credit goes, and the den is seasonal.

## Reasoning

- **Open-Meteo works anywhere, in one request.** The US National Weather Service covers only the US, wants contact details in the User-Agent, and takes two requests per refresh.
- **A postal code is the least an admin can give.** It's coarse, easy to type on a phone, and once geocoded, refreshes carry only rounded coordinates.
- **Geocoding on save tells the admin at once**, on the form, not as a silent fallback hours later. Its 5-second limit bounds only the admin's save, never a member's page.
- **The randomized window meets both limits**: hourly at most keeps us well within free use, and every 3 hours at least keeps the den close to the sky.
- **Storing the answer** keeps pages off the network, and survives a restart.
- **One warning, then quiet**: an outage of a few hours is normal and needs no action, and the card shows whether it's still failing.

## Consequences

- A new `local_weather` table holds each household's setting, rounded place, last answer, and failure state.
- While it's on, the host needs outbound HTTPS to `geocoding-api.open-meteo.com` and `api.open-meteo.com`. Off, the default, Dinnerbell makes no new connection.
- Open-Meteo's terms are the operator's to meet: its free API is for non-commercial use.
