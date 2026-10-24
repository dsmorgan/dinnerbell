#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Regenerate every logo PNG from the committed SVGs. Run it after changing logo.svg or
# logo-mark.svg, and commit the PNGs it writes.
#
# It needs resvg 0.48.1 exactly (RESVG_VERSION below): another version can antialias differently
# and change every PNG. resvg is a single binary with no system dependencies. Install it with any
# of these:
#
#   cargo install resvg --version 0.48.1 --locked
#   the release binary from https://github.com/linebender/resvg/releases/tag/v0.48.1
#   brew install resvg            (only while Homebrew still ships 0.48.1)
#
# Set RESVG to the binary's path if it isn't on your PATH.
set -eu

RESVG_VERSION=0.48.1
RESVG=${RESVG:-resvg}
# The cream of the logo's outer ring, behind icons that must be opaque.
BACKGROUND='#FFF3E0'
# The light blue of the test mascot's outer ring, behind its opaque icons.
TEST_BACKGROUND='#EAF4FF'

CDPATH='' cd -- "$(dirname -- "$0")/.."

command -v "$RESVG" >/dev/null 2>&1 || {
    echo "render-logo: $RESVG not found; see the install notes at the top of this script." >&2
    exit 1
}
found=$("$RESVG" --version)
[ "$found" = "$RESVG_VERSION" ] || {
    echo "render-logo: needs resvg $RESVG_VERSION, found $found." >&2
    exit 1
}

static=src/dinnerbell/web/static

# render SVG WIDTH OUT [resvg options...]
render() {
    svg=$1 width=$2 out=$3
    shift 3
    "$RESVG" "$@" --width "$width" "$svg" "$out"
    echo "wrote $out"
}

# Favicon fallback for browsers without SVG favicons: the face-only mark, which reads at 16 px.
render "$static/logo-mark.svg" 32 "$static/favicon-32.png"
# iOS fills transparency with black, so Home Screen and Slack icons get an opaque background.
render "$static/logo.svg" 180 "$static/apple-touch-icon.png" --background "$BACKGROUND"
# Slack accepts 512 to 2000 px. Upload it in the production Slack app; see docs/setup.md.
render "$static/logo.svg" 1024 deploy/slack-app-icon-1024.png --background "$BACKGROUND"

# The test instance's mascot, shown when DINNERBELL_INSTANCE_LABEL is set.
render "$static/logo-test-mark.svg" 32 "$static/favicon-test-32.png"
render "$static/logo-test.svg" 180 "$static/apple-touch-icon-test.png" --background "$TEST_BACKGROUND"
render "$static/logo-test.svg" 1024 deploy/slack-app-icon-test-1024.png --background "$TEST_BACKGROUND"
