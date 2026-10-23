# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Slack adapter (spec section 10, ADR 0001). Nothing outside this package imports Slack.

- ``connection``: startup checks, Socket Mode, and linking members by email.
- ``channel``: ``SlackChannel``, the ``Channel`` protocol over the Web API.
- ``messages`` and ``views``: Block Kit for notifications, modals, and the Home tab.
- ``interactions``: buttons, modals, the Home tab, and the slash command.
- ``preview``: sample messages for ``dinnerbell slack preview``.
"""
