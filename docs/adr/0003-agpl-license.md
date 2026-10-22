# ADR 0003: AGPL-3.0 license

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-21 |

## Context

Dinnerbell will be public on GitHub because the problem is common. The owner wants the project to stay open and does not want a hosting provider to take the code, improve it privately, and offer it as a closed service.

Candidates: MIT or Apache-2.0 (permissive), GPL-3.0, AGPL-3.0, and source-available licenses such as SSPL or BSL.

## Decision

Dinnerbell is licensed under the GNU Affero General Public License, version 3.

## Reasoning

- **AGPL closes the network loophole.** Under GPL, running modified code as a service without distributing it triggers no obligation. AGPL section 13 requires anyone who runs a modified version for users over a network to offer those users the source. This is the clause large hosting providers avoid, and it is the specific concern.
- **It is still open source.** AGPL is OSI-approved. SSPL and BSL are not, and choosing them would shut the project out of some package indexes and distributions and signal that it isn't a community project.
- **Permissive licenses don't address the concern.** MIT and Apache allow closed hosted forks outright.
- **The cost is acceptable.** Some companies forbid contributions to AGPL projects. For a household tool with hobbyist contributors, that is a small loss.

## Consequences

- `LICENSE` contains the full AGPL-3.0 text. Each source file carries a short SPDX header: `SPDX-License-Identifier: AGPL-3.0-or-later`.
- Dependencies must be AGPL-compatible. Permissive and LGPL libraries are fine. The Slack SDK (MIT), FastAPI (MIT), SQLAlchemy (MIT), and HTMX (BSD) are all compatible.
- No contributor license agreement. Contributions are accepted under the same license, documented in `CONTRIBUTING.md` with a Developer Certificate of Origin sign-off.
- AGPL does not stop anyone from using the software internally or from running an unmodified copy as a service. It only requires that modifications offered as a service be published.
