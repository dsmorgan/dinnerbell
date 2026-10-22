# SPDX-License-Identifier: AGPL-3.0-or-later
"""Errors services raise for the web layer to translate. Messages are safe to show."""


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass
