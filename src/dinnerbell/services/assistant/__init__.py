# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optional assistant (ADR 0009, spec 6.14).

- ``runtime``: whether the operator configured it, and ``available``.
- ``client``: the ``ModelClient`` seam; ``anthropic_client`` is the only module that imports the
  ``anthropic`` package.
- ``conversation``: ``converse``, the bounded tool loop with its cost reservations.
- ``tools``: the tool registry and the actor wrapper. ``prompt``: the system prompt.
- ``admin``: Admin's **Assistant** settings and usage. ``retention``: deleting idle conversations.
"""
