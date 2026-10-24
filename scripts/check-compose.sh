#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Check that compose.yaml renders for production and, through deploy/dinnerbell-test, for the
# test instance, with the volume names, tags, and ports the docs promise, and that the wrapper
# refuses unsafe setups. CI runs it; run it locally after changing compose files, the wrapper,
# or an env example. It needs docker compose, jq, and uv. It also checks podman-compose when that's
# installed (CI installs Ubuntu 24.04's 1.0.6, the oldest version the docs support; 1.1.0, 1.5.0,
# and 1.6.0 were checked by hand).
#
# The wrapper's configuration check runs `dinnerbell config check` in the image. Here a stub
# engine runs this checkout's `dinnerbell config check` with uv instead, and a stub compose
# records whether anything would have started, so no image is pulled and nothing runs.
#
# It writes .env and .env.test from the examples and deletes them afterward, so it refuses to
# run when either file already exists.
set -eu

CDPATH='' cd -- "$(dirname -- "$0")/.."

for file in .env .env.test; do
    [ ! -e "$file" ] || { echo "check-compose: $file exists; run this in a clean checkout." >&2; exit 1; }
done
stubs=$(mktemp -d)
trap 'rm -f .env .env.test .env.test.moved; rm -rf "$stubs"' EXIT
cp .env.example .env
cp .env.test.example .env.test

# Stand-ins for the container engine (also on PATH as podman and docker) and for compose (also
# docker-compose). Both log their name and arguments to $STUB_LOG. STUB_IMAGE=missing makes the
# image absent; STUB_RUN=old answers like an image without `config check`, and STUB_RUN=broken
# like an engine that can't start a container.
STUB_LOG="$stubs/log"
export STUB_LOG
cat >"$stubs/engine" <<'STUB'
#!/bin/sh
printf '%s %s\n' "$(basename "$0")" "$*" >>"$STUB_LOG"
case "$1" in
    image) [ "${STUB_IMAGE:-}" != missing ]; exit ;;
    pull) exit 0 ;;
    run)
        case "${STUB_RUN:-}" in
            old) echo "dinnerbell: error: argument COMMAND: invalid choice: 'config'" >&2; exit 2 ;;
            broken) echo "Error: something went wrong" >&2; exit 125 ;;
        esac
        # Skip the engine's options and the image, and drop the shell's DINNERBELL_* variables,
        # which the container wouldn't have.
        while [ "$#" -gt 0 ] && [ "$1" != config ]; do shift; done
        for name in $(env | sed -n 's/^\(DINNERBELL_[A-Z0-9_]*\)=.*/\1/p'); do unset "$name"; done
        exec uv run --quiet --frozen dinnerbell "$@"
        ;;
esac
exit 99
STUB
cat >"$stubs/compose" <<'STUB'
#!/bin/sh
printf 'compose %s\n' "$*" >>"$STUB_LOG"
STUB
chmod +x "$stubs/engine" "$stubs/compose"
mkdir "$stubs/bin"
ln -s ../engine "$stubs/bin/podman"
ln -s ../engine "$stubs/bin/docker"
ln -s ../compose "$stubs/bin/docker-compose"

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

echo "== pre-flight checks"
test_image=$(jq -r .services.dinnerbell.image <<EOF
$test_instance
EOF
)
# Made-up values for every required setting and secret in .env.test.
test_key=check-compose-test-secret-key-0123456789abcdef
test_secrets="$test_key check-compose-test-client-secret xoxb-check-compose-test xapp-check-compose-test"
filled_env_test() {
    sed -e "s|^DINNERBELL_SECRET_KEY=.*|DINNERBELL_SECRET_KEY=$test_key|" \
        -e 's|^DINNERBELL_ADMIN_EMAIL=.*|DINNERBELL_ADMIN_EMAIL=admin@example.com|' \
        -e 's|^DINNERBELL_OIDC_CLIENT_ID=.*|DINNERBELL_OIDC_CLIENT_ID=111.222|' \
        -e 's|^DINNERBELL_OIDC_CLIENT_SECRET=.*|DINNERBELL_OIDC_CLIENT_SECRET=check-compose-test-client-secret|' \
        -e 's|^DINNERBELL_SLACK_TEAM_ID=.*|DINNERBELL_SLACK_TEAM_ID=T0123ABCD|' \
        -e 's|^DINNERBELL_SLACK_BOT_TOKEN=.*|DINNERBELL_SLACK_BOT_TOKEN=xoxb-check-compose-test|' \
        -e 's|^DINNERBELL_SLACK_APP_TOKEN=.*|DINNERBELL_SLACK_APP_TOKEN=xapp-check-compose-test|' \
        -e 's|^DINNERBELL_SLACK_CHANNEL=.*|DINNERBELL_SLACK_CHANNEL=C0123ABCD|' \
        .env.test.example
}

# start [VAR=value ...] deploy/dinnerbell-test ARG...: run the wrapper with the stub engine and
# compose. Sets $status, $output (stdout and stderr), and $log (what the stubs were asked to do).
start() {
    : >"$STUB_LOG"
    status=0
    output=$(env DINNERBELL_COMPOSE="$stubs/compose" DINNERBELL_ENGINE="$stubs/engine" "$@" 2>&1) || status=$?
    log=$(cat "$STUB_LOG")
}

# Conditions on the last start.
logged() { printf '%s\n' "$log" | grep -q -- "^$1"; }
said() { printf '%s' "$output" | grep -qF -- "$1"; }
started() { logged "compose "; }
checked() { logged "engine run "; }
prints_no_secret() {
    for secret in $test_secrets; do
        ! said "$secret" || return 1
    done
}
started_cleanly() { [ "$status" -eq 0 ] && checked && started && prints_no_secret; }
refused() { [ "$status" -ne 0 ] && ! started && said "dinnerbell-test: " && prints_no_secret; }
refused_naming() { refused && said "$1"; }
refused_unchecked() { refused && ! checked && said "$1"; }
started_unchecked() { [ "$status" -eq 0 ] && started && ! checked; }
pulled_and_started() { logged "engine pull $test_image\$" && started_cleanly; }
started_with_warning() { [ "$status" -eq 0 ] && started && said "predates dinnerbell config check"; }
# The engine's options for the check, everything before the image, each with a space on both sides.
engine_options() { printf '%s\n' "$log" | sed -n "s|^engine run \(.*\) $test_image config check.*| \1 |p"; }
has_option() { engine_options | grep -qF -- " $1 "; }
lacks_option() { ! engine_options | grep -qF -- " $1"; }

# check DESCRIPTION CONDITION...: report whether the condition holds.
check() {
    description=$1
    shift
    if "$@"; then pass "$description"; else fail "$description"; fi
}

filled_env_test >.env.test
start deploy/dinnerbell-test up -d
check "a valid .env.test starts, and nothing prints a secret" started_cleanly
check "the check runs compose's image, $test_image, with the env file on stdin" \
    logged "engine run .* $test_image config check --env-file -\$"
for option in -i --rm --pull=never "--network none" --read-only "--cap-drop ALL" \
    "--security-opt no-new-privileges"; do
    check "the check runs with $option" has_option "$option"
done
for option in -v --volume --mount -p --publish -P --env-file -e --env --privileged --cap-add; do
    check "the check runs without $option" lacks_option "$option"
done

filled_env_test | sed 's/^DINNERBELL_ADMIN_EMAIL=.*/DINNERBELL_ADMIN_EMAIL=/' >.env.test
for command in "up -d" "run --rm dinnerbell scrub-test --keep you@example.com" start restart create; do
    # shellcheck disable=SC2086 # $command holds the subcommand and its arguments.
    start deploy/dinnerbell-test $command
    check "no admin email: $command names DINNERBELL_ADMIN_EMAIL and starts nothing" \
        refused_naming "DINNERBELL_ADMIN_EMAIL is required but not set."
done
start deploy/dinnerbell-test down
check "no admin email: down still runs, without the check" started_unchecked

filled_env_test >.env.test
for name in DINNERBELL_SECRET_KEY DINNERBELL_SLACK_BOT_TOKEN DINNERBELL_SLACK_APP_TOKEN \
    DINNERBELL_OIDC_CLIENT_SECRET; do
    shared=$(sed -n "s/^$name=//p" .env.test)
    cp .env.example .env
    printf 'export %s="%s"  # copied from test\n' "$name" "$shared" >>.env
    start deploy/dinnerbell-test up -d
    check "shared $name: refused by name, without printing it" refused_unchecked "same $name"
done
cp .env.example .env
printf 'DINNERBELL_SECRET_KEY=%s\nDINNERBELL_SECRET_KEY=production-secret-key-0123456789abcdef\n' \
    "$test_key" >>.env
start deploy/dinnerbell-test up -d
check "a shared key that .env assigns again later starts: the last assignment counts" started_cleanly
cp .env.example .env

start STUB_IMAGE=missing deploy/dinnerbell-test up -d
check "a missing image is pulled before the check" pulled_and_started
start STUB_RUN=old deploy/dinnerbell-test up -d
check "an image without config check starts, with a warning" started_with_warning
start STUB_RUN=broken deploy/dinnerbell-test up -d
check "a check that can't run starts nothing" refused_naming "exit 125"

start PATH="$stubs/bin:$PATH" DINNERBELL_ENGINE= deploy/dinnerbell-test up -d
check "the check uses podman with podman-compose" logged "podman run "
start PATH="$stubs/bin:$PATH" DINNERBELL_ENGINE= DINNERBELL_COMPOSE=docker-compose deploy/dinnerbell-test up -d
check "the check uses docker with docker compose" logged "docker run "

if command -v podman-compose >/dev/null 2>&1; then
    echo "== podman-compose ($(podman-compose version 2>/dev/null | grep -i 'podman-compose' | tail -n 1))"
    # With --dry-run, podman-compose prints the podman commands it would run. Newer versions
    # print them only with --verbose, which 1.0.6 doesn't accept.
    pc="podman-compose"
    if podman-compose --help 2>&1 | grep -q -- '--verbose'; then pc="podman-compose --verbose"; fi
    production=$($pc --dry-run -p dinnerbell up -d 2>&1) || true
    test_instance=$(DINNERBELL_COMPOSE="$pc --dry-run" DINNERBELL_ENGINE="$stubs/engine" \
        deploy/dinnerbell-test up -d 2>&1) || true
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
