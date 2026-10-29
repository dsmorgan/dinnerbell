#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the release bundle, dinnerbell-X.Y.Z.tar.gz, and its SHA-256 file in OUTDIR (default dist/):
# everything an install needs without a clone. X.Y.Z is __version__, and compose.yaml must already
# default to that image tag, as a release commit does. CI runs this on every build and attests the
# bundle on a release tag. The archive is reproducible: the same commit gives the same bytes, with
# sorted entries, one fixed time (the commit's), owner root, fixed modes, and gzip -n.
#   scripts/build-bundle.sh [OUTDIR]
set -eu

CDPATH='' cd -- "$(dirname -- "$0")/.."
outdir=${1:-dist}

version=$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' src/dinnerbell/__init__.py)
case "$version" in
    *[!0-9.]* | "" | .* | *. | *..*)
        echo "build-bundle: can't read a plain X.Y.Z version from src/dinnerbell/__init__.py." >&2
        exit 1
        ;;
esac
grep -qF "image: ghcr.io/dsmorgan/dinnerbell:\${DINNERBELL_TAG:-$version}" compose.yaml || {
    echo "build-bundle: compose.yaml's default image tag isn't $version. A release commit sets both." >&2
    exit 1
}
# The commit's time, so a rebuild of the same commit matches; 0 outside a git checkout.
epoch=$(git log -1 --format=%ct 2>/dev/null || echo 0)

# path in the bundle, then path in the repository. Scripts keep mode 755, everything else 644.
FILES="
INSTALL.md docs/install.md
LICENSE LICENSE
compose.yaml compose.yaml
compose.secrets.yaml compose.secrets.yaml
.env.example .env.example
.env.test.example .env.test.example
deploy/slack-app-manifest.yaml deploy/slack-app-manifest.yaml
deploy/slack-app-manifest.test.yaml deploy/slack-app-manifest.test.yaml
deploy/dinnerbell-test deploy/dinnerbell-test
deploy/check-secrets deploy/check-secrets
deploy/compose.test.yaml deploy/compose.test.yaml
deploy/compose.test-secrets.yaml deploy/compose.test-secrets.yaml
"

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
root="$stage/dinnerbell"
printf '%s\n' "$FILES" | while read -r target source; do
    [ -n "$target" ] || continue
    [ -f "$source" ] || { echo "build-bundle: $source is missing." >&2; exit 1; }
    mkdir -p "$(dirname -- "$root/$target")"
    cp "$source" "$root/$target"
    if [ -x "$source" ]; then chmod 755 "$root/$target"; else chmod 644 "$root/$target"; fi
done

mkdir -p "$outdir"
name="dinnerbell-$version.tar.gz"
# Python's tarfile, not tar: GNU and BSD tar spell the options for a reproducible archive differently.
python3 -I - "$stage" "$epoch" <<'EOF' | gzip -n -9 >"$outdir/$name"
import os, sys, tarfile

stage, epoch = sys.argv[1], int(sys.argv[2])
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
    paths = []
    for directory, subdirectories, files in os.walk(stage):
        for entry in subdirectories + files:
            paths.append(os.path.relpath(os.path.join(directory, entry), stage))
    for path in sorted(paths):
        info = archive.gettarinfo(os.path.join(stage, path), arcname=path)
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mtime = epoch
        info.mode = 0o755 if info.isdir() or info.mode & 0o100 else 0o644
        if info.isdir():
            archive.addfile(info)
        else:
            with open(os.path.join(stage, path), "rb") as stream:
                archive.addfile(info, stream)
EOF

cd "$outdir"
{ sha256sum "$name" 2>/dev/null || shasum -a 256 "$name"; } >"$name.sha256"
echo "build-bundle: wrote $outdir/$name and $name.sha256" >&2
