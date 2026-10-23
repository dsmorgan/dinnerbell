#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Check that compose.yaml renders for production and, through deploy/dinnerbell-test, for the
# test instance, with the volume names, tags, and ports the docs promise, and that the wrapper
# refuses unsafe setups. CI runs it; run it locally after changing compose files, the wrapper,
# or an env example. It needs docker compose and jq. It also checks podman-compose when that's
# installed (CI installs Ubuntu 24.04's 1.0.6, the oldest version the docs support; 1.1.0, 1.5.0,
# and 1.6.0 were checked by hand).
#
# It writes .env and .env.test from the examples and deletes them afterward, so it refuses to
# run when either file already exists.
set -eu

CDPATH='' cd -- "$(dirname -- "$0")/.."

for file in .env .env.test; do
    [ ! -e "$file" ] || { echo "check-compose: $file exists; run this in a clean checkout." >&2; exit 1; }
done
trap 'rm -f .env .env.test .env.test.moved' EXIT
cp .env.example .env
cp .env.test.example .env.test

failures=0
pass() { echo "ok: $1"; }
fail() { echo "FAILED: $1" >&2; failures=$((failures + 1)); }

# expect DESCRIPTION COMMAND...: the command succeeds.
expect() {
    description=$1
    shift
    if "$@" >/dev/null 2>&1; then pass "$description"; else fail "$description"; fi
}

# refuses DESCRIPTION [VAR=value ...] [ARG ...]: the wrapper itself refuses `config --quiet`. A
# failure without the wrapper's "dinnerbell-test:" message, such as a compose error, doesn't count.
refuses() {
    description=$1
    shift
    if errors=$(env "$@" config --quiet 2>&1 >/dev/null); then
        fail "wrapper accepted: $description"
    elif printf '%s' "$errors" | grep -q '^dinnerbell-test: ' &&
        ! printf '%s' "$errors" | grep -q '^dinnerbell-test: project '; then
        pass "wrapper refused: $description"
    else
        fail "wrapper failed without refusing: $description"
    fi
}

# contains DESCRIPTION TEXT NEEDLE...: TEXT contains any of the needles.
contains() {
    description=$1
    text=$2
    shift 2
    for needle in "$@"; do
        if printf '%s' "$text" | grep -qF -- "$needle"; then
            pass "$description"
            return
        fi
    done
    fail "$description"
}

echo "== docker compose"
export DINNERBELL_COMPOSE="docker compose"
expect "production renders" docker compose config --quiet
expect "build override renders" docker compose -f compose.yaml -f compose.build.yaml config --quiet
# Renaming production's volume would start existing installs on an empty database.
production=$(docker compose -p dinnerbell config --format json)
expect "production: volume dinnerbell_dinnerbell-data, a release tag, port 8013" jq -e '
    .volumes["dinnerbell-data"].name == "dinnerbell_dinnerbell-data"
    and (.services.dinnerbell.image | test(":[0-9]+[.][0-9]+[.][0-9]+$"))
    and .services.dinnerbell.ports[0].published == "8013"' <<EOF
$production
EOF
test_instance=$(deploy/dinnerbell-test config --format json 2>/dev/null)
expect "test: project, volume, :main, port 8014, label" jq -e '
    .name == "dinnerbell-test"
    and .volumes["dinnerbell-data"].name == "dinnerbell-test_dinnerbell-data"
    and .services.dinnerbell.ports[0].published == "8014"
    and (.services.dinnerbell.image | endswith(":main"))
    and .services.dinnerbell.environment.DINNERBELL_INSTANCE_LABEL == "TEST"' <<EOF
$test_instance
EOF

echo "== wrapper refusals"
refuses "DINNERBELL_ENV_FILE=.env" DINNERBELL_ENV_FILE=.env deploy/dinnerbell-test
refuses "DINNERBELL_ENV_FILE=<absolute path to .env>" DINNERBELL_ENV_FILE="$PWD/.env" deploy/dinnerbell-test
refuses "COMPOSE_PROJECT_NAME=Dinnerbell-Test in the shell" COMPOSE_PROJECT_NAME=Dinnerbell-Test deploy/dinnerbell-test
refuses "a leading compose option" deploy/dinnerbell-test -p dinnerbell
for line in DINNERBELL_TAG=0.2.0 "export DINNERBELL_PORT=8013" DINNERBELL_ENV_FILE=.env.test \
    COMPOSE_PROJECT_NAME=dinnerbell-test 'COMPOSE_PROJECT_NAME="dinnerbell-test"  # production'; do
    printf '%s\n' "$line" >>.env
    refuses "$line in .env" deploy/dinnerbell-test
    cp .env.example .env
done
printf 'DINNERBELL_INSTANCE_LABEL=\n' >>.env.test
refuses "a second, empty label in .env.test" deploy/dinnerbell-test
sed 's/^DINNERBELL_INSTANCE_LABEL=.*/DINNERBELL_INSTANCE_LABEL=/' .env.test.example >.env.test
refuses "an empty label in .env.test" deploy/dinnerbell-test
mv .env.test .env.test.moved
refuses "no .env.test" deploy/dinnerbell-test
mv .env.test.moved .env.test
cp .env.test.example .env.test

if command -v podman-compose >/dev/null 2>&1; then
    echo "== podman-compose ($(podman-compose version 2>/dev/null | grep -i 'podman-compose' | tail -n 1))"
    # With --dry-run, podman-compose prints the podman commands it would run. Newer versions
    # print them only with --verbose, which 1.0.6 doesn't accept.
    pc="podman-compose"
    if podman-compose --help 2>&1 | grep -q -- '--verbose'; then pc="podman-compose --verbose"; fi
    production=$($pc --dry-run -p dinnerbell up -d 2>&1) || true
    test_instance=$(DINNERBELL_COMPOSE="$pc --dry-run" deploy/dinnerbell-test up -d 2>&1) || true
    # podman-compose 1.6.0 and later mount with --mount instead of -v.
    contains "production: volume dinnerbell_dinnerbell-data" "$production" \
        "dinnerbell_dinnerbell-data:/data" "source=dinnerbell_dinnerbell-data,destination=/data"
    contains "production: port 8013" "$production" "127.0.0.1:8013:8013"
    contains "test: volume dinnerbell-test_dinnerbell-data" "$test_instance" \
        "dinnerbell-test_dinnerbell-data:/data" "source=dinnerbell-test_dinnerbell-data,destination=/data"
    contains "test: port 8014" "$test_instance" "127.0.0.1:8014:8013"
    contains "test: image :main" "$test_instance" "ghcr.io/dsmorgan/dinnerbell:main"
    if printf '%s' "$test_instance" | grep -qF "dinnerbell_dinnerbell-data"; then
        fail "test never touches production's volume"
    else
        pass "test never touches production's volume"
    fi
fi

[ "$failures" -eq 0 ] || { echo "check-compose: $failures check(s) failed." >&2; exit 1; }
echo "check-compose: all checks passed."
