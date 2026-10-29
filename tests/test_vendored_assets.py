# SPDX-License-Identifier: AGPL-3.0-or-later
"""The vendored htmx file is byte-for-byte what npm published, so a modified file fails CI."""

import hashlib
from importlib.resources import files

from dinnerbell.web.render import HTMX_FILE

# SHA-256 of `package/dist/htmx.min.js` in the npm package `htmx.org` at HTMX_VERSION (`npm pack
# htmx.org@2.0.11`, then `shasum -a 256 package/dist/htmx.min.js`). To update htmx, see
# CONTRIBUTING.md, "Update the vendored htmx".
HTMX_VERSION = "2.0.11"
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"


def test_htmx_file_name_carries_the_recorded_version() -> None:
    assert HTMX_VERSION in HTMX_FILE


def test_vendored_htmx_matches_the_npm_release() -> None:
    data = files("dinnerbell").joinpath("web", "static", HTMX_FILE).read_bytes()
    assert hashlib.sha256(data).hexdigest() == HTMX_SHA256, (
        f"{HTMX_FILE} differs from npm htmx.org@{HTMX_VERSION}. Restore it from the npm package, "
        "or follow CONTRIBUTING.md to update htmx on purpose."
    )
