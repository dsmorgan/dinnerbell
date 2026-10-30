#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Check that compose.yaml renders for production and, through deploy/dinnerbell-test, for the
# test instance, with the volume names, tags, and ports the docs promise, with secrets from the env
# file by default and from files with compose.secrets.yaml (the test wrapper adds it when
# secrets-test/ holds the files), and that the wrapper refuses unsafe setups. CI runs it; run it locally after changing compose files, the wrapper,
# or an env example. It needs docker compose, jq, and uv. It also checks podman-compose when that's
# installed (CI installs Ubuntu 24.04's 1.0.6, the oldest version the docs support; 1.1.0, 1.5.0,
# and 1.6.0 were checked by hand).
#
# The wrapper's configuration check runs `dinnerbell config check` in the image. Here a stub
# engine runs this checkout's `dinnerbell config check` with uv instead, and a stub compose
# records whether anything would have started, so no image is pulled and nothing runs.
#
# It writes .env, .env.test, and the secret files in secrets/ and secrets-test/ (made-up values)
# and deletes them afterward, so it refuses to run when any of them already exists.
# shellcheck disable=SC2016 # Single quotes hold literal secrets, jq programs, and awk programs here.
set -eu

CDPATH='' cd -- "$(dirname -- "$0")/.."

SECRET_NAMES="DINNERBELL_SECRET_KEY DINNERBELL_SLACK_BOT_TOKEN DINNERBELL_SLACK_APP_TOKEN DINNERBELL_OIDC_CLIENT_SECRET"
for file in .env .env.test secrets-test; do
    [ ! -e "$file" ] || { echo "check-compose: $file exists; run this in a clean checkout." >&2; exit 1; }
done
for name in $SECRET_NAMES; do
    [ ! -e "secrets/$name" ] || { echo "check-compose: secrets/$name exists; run this in a clean checkout." >&2; exit 1; }
done
stubs=$(mktemp -d)
cleanup() {
    chmod -R u+rwX secrets-test 2>/dev/null || true
    rm -f .env .env.test .env.test.moved
    rm -rf secrets-test "$stubs"
    for name in $SECRET_NAMES; do rm -f "secrets/$name"; done
}
trap cleanup EXIT
mkdir secrets-test
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
    # What podman unshare does for a file the account can't read: it reads it in a user namespace
    # where the file is the account's own. Here that is a chmod.
    unshare) shift; chmod u+r "$3" 2>/dev/null; exec "$@" ;;
    run)
        case "${STUB_RUN:-}" in
            old)
                # What the 0.2.0 image prints.
                echo "usage: dinnerbell [-h] [--version] COMMAND ..." >&2
                echo "dinnerbell: error: argument COMMAND: invalid choice: 'config' (choose from 'serve', 'migrate')" >&2
                exit 2
                ;;
            broken) echo "Error: something went wrong" >&2; exit 125 ;;
        esac
        # Skip the engine's options and the image, but collect the secret files mounted with -v into
        # a directory standing in for /run/secrets. Drop the shell's DINNERBELL_* variables, which
        # the container wouldn't have.
        mounts=$(mktemp -d)
        while [ "$#" -gt 0 ] && [ "$1" != config ]; do
            if [ "$1" = -v ]; then
                spec=${2%:ro}
                ln -s "${spec%%:/run/secrets/*}" "$mounts/${spec#*:/run/secrets/}"
            fi
            shift
        done
        for name in $(env | sed -n 's/^\(DINNERBELL_[A-Z0-9_]*\)=.*/\1/p'); do unset "$name"; done
        exec uv run --quiet --frozen dinnerbell "$@" --secrets-dir "$mounts"
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
# An awk that fails when the wrapper compares secrets, and works otherwise.
mkdir "$stubs/broken-awk"
cat >"$stubs/broken-awk/awk" <<STUB
#!/bin/sh
case " \$* " in *" anycase=yes "*) exit 2 ;; esac
exec $(command -v awk) "\$@"
STUB
chmod +x "$stubs/broken-awk/awk"

failures=0
pass() { printf 'ok: %s\n' "$1"; }
fail() { printf 'FAILED: %s\n' "$1" >&2; failures=$((failures + 1)); }

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
expect "secrets override renders" docker compose -f compose.yaml -f compose.secrets.yaml config --quiet
# Renaming production's volume would start existing installs on an empty database.
production=$(docker compose -p dinnerbell config --format json)
production_files=$(docker compose -p dinnerbell -f compose.yaml -f compose.secrets.yaml config --format json)
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
# By default nothing is mounted at /run/secrets, so the env file alone starts the service (0.9.0
# mounted the files unconditionally, and an upgrade with secrets in .env didn't start).
no_secrets_filter='.services.dinnerbell.secrets == null and .secrets == null'
expect "production: no secret files by default, so .env alone starts it" jq -e "$no_secrets_filter" <<EOF
$production
EOF
expect "test without secrets-test/ files: no secret files, so .env.test alone starts it" \
    jq -e "$no_secrets_filter" <<EOF
$test_instance
EOF
# With compose.secrets.yaml, each secret is a file mounted at /run/secrets/<name>: production's in
# ./secrets, test's in ./secrets-test, and the service lists all four. No env file gives a value.
secrets_json='["DINNERBELL_SECRET_KEY","DINNERBELL_SLACK_BOT_TOKEN","DINNERBELL_SLACK_APP_TOKEN","DINNERBELL_OIDC_CLIENT_SECRET"]'
secrets_filter='
    . as $root
    | (.services.dinnerbell.secrets | map(.source) | sort) == ($names | sort)
    and all(.services.dinnerbell.secrets[]; .target == "/run/secrets/" + .source)
    and all($names[]; . as $n | $root.secrets[$n].file | endswith($dir + $n))
    and (.services.dinnerbell.environment // {} | to_entries
        | map(select((.key | test("SECRET|TOKEN")) and (.value // "") != "")) | length) == 0'
expect "production with compose.secrets.yaml: the four secrets are files in secrets/, mounted at /run/secrets" \
    jq -e --argjson names "$secrets_json" --arg dir /secrets/ "$secrets_filter" <<EOF
$production_files
EOF
for name in $SECRET_NAMES; do : >"secrets-test/$name"; done
test_instance_files=$(deploy/dinnerbell-test config --format json 2>/dev/null)
for name in $SECRET_NAMES; do rm -f "secrets-test/$name"; done
expect "test with secrets-test/ files: the four secrets are files in secrets-test/, mounted at /run/secrets" \
    jq -e --argjson names "$secrets_json" --arg dir /secrets-test/ "$secrets_filter" <<EOF
$test_instance_files
EOF

# Resource and log caps (docs/configuration.md, "Resource and log limits"): a process limit, a memory
# limit, and rotated json-file logs, for production and for the test instance.
limits_filter='
    .services.dinnerbell as $s
    | ($s.pids_limit | tonumber) > 0
    and ($s.mem_limit | tonumber) > 0
    and $s.logging.driver == "json-file"
    and ($s.logging.options["max-size"] | length) > 0
    and ($s.logging.options["max-file"] | tonumber) > 0'
expect "production: pids_limit, mem_limit, and json-file log rotation" jq -e "$limits_filter" <<EOF
$production
EOF
expect "test: pids_limit, mem_limit, and json-file log rotation" jq -e "$limits_filter" <<EOF
$test_instance
EOF
expect "production with compose.secrets.yaml: the same limits" jq -e "$limits_filter" <<EOF
$production_files
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
production_key="production-secret-key-0123456789abcdef"
production_bot_token="xoxb-production-token-0123456789"
test_client_secret=check-compose-test-client-secret
test_bot_token=xoxb-check-compose-test
test_app_token=xapp-check-compose-test
test_secrets="$test_key $test_client_secret $test_bot_token $test_app_token
$production_key $production_bot_token"
# .env.test as the docs write it: no secrets in it.
filled_env_test() {
    sed -e 's|^DINNERBELL_ADMIN_EMAIL=.*|DINNERBELL_ADMIN_EMAIL=admin@example.com|' \
        -e 's|^DINNERBELL_OIDC_CLIENT_ID=.*|DINNERBELL_OIDC_CLIENT_ID=111.222|' \
        -e 's|^DINNERBELL_SLACK_TEAM_ID=.*|DINNERBELL_SLACK_TEAM_ID=T0123ABCD|' \
        -e 's|^DINNERBELL_SLACK_CHANNEL=.*|DINNERBELL_SLACK_CHANNEL=C0123ABCD|' \
        .env.test.example
}
# .env.test with the four secrets as lines, the default layout (without secrets-test/ files).
env_test_with_secrets() {
    filled_env_test | grep -Ev '^DINNERBELL_(SECRET_KEY|OIDC_CLIENT_SECRET|SLACK_BOT_TOKEN|SLACK_APP_TOKEN)='
    printf 'DINNERBELL_SECRET_KEY=%s\nDINNERBELL_OIDC_CLIENT_SECRET=%s\n' "$test_key" "$test_client_secret"
    printf 'DINNERBELL_SLACK_BOT_TOKEN=%s\nDINNERBELL_SLACK_APP_TOKEN=%s\n' "$test_bot_token" "$test_app_token"
}
# write_secrets DIR VALUE...: the four secret files in DIR, owner-only and without a newline, with
# the given values in the order of $SECRET_NAMES; "-" writes an empty file.
write_secrets() {
    directory=$1
    shift
    mkdir -p "$directory"
    for name in $SECRET_NAMES; do
        value=$1
        shift
        [ "$value" != - ] || value=
        rm -f "$directory/$name"
        (umask 077 && printf '%s' "$value" >"$directory/$name")
    done
}
test_secret_files() { write_secrets secrets-test "$test_key" "$test_bot_token" "$test_app_token" "$test_client_secret"; }
test_secret_files

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
started_with_files() {
    started_cleanly && logged "compose .* -f compose.secrets.yaml .* -f deploy/compose.test-secrets.yaml "
}
started_from_env() {
    started_cleanly && ! logged "compose .*compose.secrets.yaml" && ! logged "compose .*compose.test-secrets.yaml" &&
        [ "$(engine_options | grep -c -- ' -v ')" -eq 0 ] && said "secrets from .env.test"
}
refused() { [ "$status" -ne 0 ] && ! started && said "dinnerbell-test: " && prints_no_secret; }
refused_naming() { refused && said "$1"; }
refused_unchecked() { refused && ! checked && said "$1"; }
started_with_podman() { [ "$status" -eq 0 ] && started && logged "podman run " && prints_no_secret; }
started_unchecked() { [ "$status" -eq 0 ] && started && ! checked; }
pulled_and_started() { logged "engine pull $test_image\$" && started_cleanly; }
started_with_dotenv_warning() { started_cleanly && said "could not parse statement starting at line"; }
parsing_agrees() { [ "$dotenv" = "$expected" ] && [ "$wrapper" = "$expected" ] && prints_no_secret; }
started_with_warning() { [ "$status" -eq 0 ] && started && said "predates dinnerbell config check"; }
# The engine's options for the check, everything before the image, each with a space on both sides.
engine_options() { printf '%s\n' "$log" | sed -n "s|^engine run \(.*\) $test_image config check.*| \1 |p"; }
has_option() { engine_options | grep -qF -- " $1 "; }
only_secret_mounts() { [ "$(engine_options | grep -o -- ' -v ' | wc -l)" -eq 4 ]; }
no_secret_in_log() {
    for secret in $test_secrets; do
        ! printf '%s' "$log" | grep -qF -- "$secret" || return 1
    done
}
lacks_option() { ! engine_options | grep -qF -- " $1"; }

# check DESCRIPTION CONDITION...: report whether the condition holds.
check() {
    description=$1
    shift
    if "$@"; then pass "$description"; else fail "$description"; fi
}

# The default: no files in secrets-test/, and the four secrets in .env.test.
rm -rf secrets-test
env_test_with_secrets >.env.test
start deploy/dinnerbell-test up -d
check "secrets in .env.test and no secrets-test/: starts without compose.secrets.yaml or mounts" started_from_env
mkdir secrets-test
start deploy/dinnerbell-test up -d
check "an empty secrets-test/ counts as no files" started_from_env
filled_env_test >.env.test
start deploy/dinnerbell-test up -d
check "no secrets anywhere: the check names DINNERBELL_SECRET_KEY and starts nothing" \
    refused_naming "DINNERBELL_SECRET_KEY is required but not set."
test_secret_files

filled_env_test >.env.test
start deploy/dinnerbell-test up -d
check "a valid .env.test starts, and nothing prints a secret" started_cleanly
check "secret files in secrets-test/: compose gets compose.secrets.yaml and compose.test-secrets.yaml" \
    started_with_files
check "the check runs compose's image, $test_image, with the env file on stdin" \
    logged "engine run .* $test_image config check --env-file -\$"
for option in -i --rm --pull=never "--log-driver none" "--network none" --read-only "--cap-drop ALL" \
    "--security-opt no-new-privileges"; do
    check "the check runs with $option" has_option "$option"
done
for option in --volume --mount -p --publish -P --env-file -e --env --privileged --cap-add; do
    check "the check runs without $option" lacks_option "$option"
done
# Its only mounts are test's four secret files, read-only, at the paths compose uses; the files
# themselves, never their contents, are on the command line.
for name in $SECRET_NAMES; do
    check "the check mounts secrets-test/$name read-only at /run/secrets/$name" \
        has_option "-v $PWD/secrets-test/$name:/run/secrets/$name:ro"
done
check "the check has no mount besides the four secret files" only_secret_mounts
check "no command line holds a secret value" no_secret_in_log

filled_env_test | sed 's/^DINNERBELL_ADMIN_EMAIL=.*/DINNERBELL_ADMIN_EMAIL=/' >.env.test
for command in "up -d" "run --rm dinnerbell scrub-test --keep you@example.com" start restart create \
    "scale dinnerbell=1" watch; do
    # shellcheck disable=SC2086 # $command holds the subcommand and its arguments.
    start deploy/dinnerbell-test $command
    check "no admin email: $command names DINNERBELL_ADMIN_EMAIL and starts nothing" \
        refused_naming "DINNERBELL_ADMIN_EMAIL is required but not set."
done
for command in down stop kill rm logs ps pull config images top port pause unpause "exec dinnerbell true" version; do
    # shellcheck disable=SC2086 # $command holds the subcommand and its arguments.
    start deploy/dinnerbell-test $command
    check "no admin email: $command still runs, without the check" started_unchecked
done
printf 'DINNERBELL_ADMIN_EMAIL=admin@example.com\nDINNERBELL_FEATURES=invalid choice: %s\n' "'config'" >>.env.test
start deploy/dinnerbell-test up -d
check "a setting whose value looks like an old image's error is still refused" \
    refused_naming "DINNERBELL_FEATURES: unknown feature flag"

filled_env_test >.env.test
printf 'this line is not valid\n' >>.env.test
start deploy/dinnerbell-test up -d
check "a valid .env.test with a line python-dotenv skips starts, and shows the warning" \
    started_with_dotenv_warning

if [ "$(id -u)" -ne 0 ]; then
    for file in .env .env.test; do
        filled_env_test >.env.test
        chmod 000 "$file"
        start deploy/dinnerbell-test up -d
        chmod 600 "$file"
        check "an unreadable $file is refused" refused_naming "can't read $file"
    done
fi

# Test's secret files: each must exist, and the check reads them as the service would.
for name in $SECRET_NAMES; do
    test_secret_files
    rm "secrets-test/$name"
    start deploy/dinnerbell-test up -d
    check "no secrets-test/$name: refused by name, nothing started" refused_unchecked "secrets-test/$name not found"
    mkdir "secrets-test/$name"
    start deploy/dinnerbell-test up -d
    check "secrets-test/$name is a directory: refused by name" refused_unchecked "secrets-test/$name is a directory"
    rmdir "secrets-test/$name"
done
test_secret_files
start deploy/dinnerbell-test down
check "down needs no secret files" started_unchecked
rm -rf secrets-test
mkdir secrets-test
start deploy/dinnerbell-test ps
check "ps needs no secret files" started_unchecked
test_secret_files
write_secrets secrets-test "$test_key" "$test_bot_token" "$test_app_token" -
start deploy/dinnerbell-test up -d
check "an empty OIDC client secret file: the check names DINNERBELL_OIDC_CLIENT_SECRET and the file" \
    refused_naming "DINNERBELL_OIDC_CLIENT_SECRET is required but not set. Set it in the env file, or put it in the file "
test_secret_files
write_secrets secrets-test "$test_key" - - "$test_client_secret"
start deploy/dinnerbell-test up -d
check "empty Slack token files: Slack is optional, so test starts" started_cleanly
test_secret_files
if [ "$(id -u)" -ne 0 ]; then
    chmod 000 secrets-test/DINNERBELL_SECRET_KEY
    start deploy/dinnerbell-test up -d
    chmod 600 secrets-test/DINNERBELL_SECRET_KEY
    check "a secret file the service user can't read: refused, naming the file" \
        refused_naming "Can't read the secret file"
fi
test_secret_files

# Production's files, kept in secrets/ (never read by test's compose files, which replace the paths
# compose.secrets.yaml gives). A secret that test
# shares with them, however each instance holds it, is refused by name, and no value is printed.
production_secret_files() {
    write_secrets secrets "$production_key" "$production_bot_token" xapp-production "production-client-secret"
}
production_secret_files
start deploy/dinnerbell-test up -d
check "separate secret files start, and no command line holds a secret" started_cleanly
check "separate secret files: no command line holds a secret value" no_secret_in_log
for name in $SECRET_NAMES; do
    production_secret_files
    cp "secrets-test/$name" "secrets/$name"
    start deploy/dinnerbell-test up -d
    check "$name in both secret files: refused by name, without printing it" \
        refused_unchecked "same $name"
    production_secret_files
    cp .env.example .env
    printf '%s=%s\n' "$name" "$(cat "secrets-test/$name")" >>.env
    start deploy/dinnerbell-test up -d
    check "$name in test's file and production's .env: refused" refused_unchecked "same $name"
    cp .env.example .env
    production_secret_files
    cp "secrets/$name" "secrets/$name.moved"
    cp "secrets-test/$name" "secrets/$name"
    printf '%s=%s\n' "$name" "$(cat "secrets/$name.moved")" >>.env.test
    start deploy/dinnerbell-test up -d
    check "$name in production's file and test's .env.test: refused" refused_unchecked "same $name"
    rm -f "secrets/$name.moved"
    filled_env_test >.env.test
    test_secret_files
done
production_secret_files
cp secrets-test/DINNERBELL_SECRET_KEY "secrets/DINNERBELL_SECRET_KEY"
printf '\n' >>"secrets/DINNERBELL_SECRET_KEY"
start deploy/dinnerbell-test up -d
check "a shared key with a trailing newline in production's file is still refused" \
    refused_unchecked "same DINNERBELL_SECRET_KEY"
production_secret_files
if [ "$(id -u)" -ne 0 ]; then
    chmod 000 secrets/DINNERBELL_SECRET_KEY
    start deploy/dinnerbell-test up -d
    check "a production secret file this account can't read counts as shared" \
        refused_unchecked "can't read secrets/DINNERBELL_SECRET_KEY"
    # With podman, podman unshare reads it, as it does for a file chowned to the container user.
    start DINNERBELL_ENGINE="$stubs/bin/podman" deploy/dinnerbell-test up -d
    check "podman unshare reads a file owned by the container user, and test starts" \
        started_with_podman
    chmod 600 secrets/DINNERBELL_SECRET_KEY
fi
production_secret_files
write_secrets secrets - - - -
start deploy/dinnerbell-test up -d
check "empty production files share nothing with empty Slack files" started_cleanly
for name in $SECRET_NAMES; do rm -f "secrets/$name"; done

# The default layout: secrets as lines in .env.test and .env, and no files in secrets-test/.
rm -rf secrets-test
env_test_with_secrets >.env.test
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

# Each case: whether .env.test and .env share the secret key, the .env.test line, and the .env
# line. @KEY@ stands for the key, and @BOM@ for a UTF-8 byte-order mark. python-dotenv must agree,
# reading as podman-compose does but with the byte-order mark stripped and names in any case.
dotenv_shares_key() {
    uv run --quiet --frozen python - 2>/dev/null <<'PY'
import sys

from dotenv import dotenv_values


def key(path):
    with open(path, encoding="utf-8-sig") as stream:
        values = dotenv_values(stream=stream, interpolate=False)
    return {name.upper(): value for name, value in values.items()}.get("DINNERBELL_SECRET_KEY")


test, production = key(".env.test"), key(".env")
sys.exit(0 if test and test == production else 1)
PY
}
while IFS= read -r case_line; do
    expected=${case_line%%|||*}
    lines=${case_line#*|||}
    test_line=${lines%%|||*}
    production_line=${lines#*|||}
    for line in "$test_line" "$production_line"; do
        printf '%s\n' "$line" | sed -e "s/@KEY@/$test_key/g" -e "s/@BOM@/$(printf '\357\273\277')/"
    done >"$stubs/lines"
    { sed -n 1p "$stubs/lines"; env_test_with_secrets | grep -v '^DINNERBELL_SECRET_KEY='; } >.env.test
    { sed -n 2p "$stubs/lines"; grep -v '^DINNERBELL_SECRET_KEY=' .env.example; } >.env
    if dotenv_shares_key; then dotenv=same; else dotenv=different; fi
    start deploy/dinnerbell-test up -d
    if said "same DINNERBELL_SECRET_KEY"; then wrapper=same; else wrapper=different; fi
    check "parsing: $expected for python-dotenv and the wrapper: $test_line / $production_line" \
        parsing_agrees
done <<'CASES'
same|||DINNERBELL_SECRET_KEY=@KEY@|||DINNERBELL_SECRET_KEY="@KEY@"#prod
different|||DINNERBELL_SECRET_KEY="@KEY@ #x"|||DINNERBELL_SECRET_KEY="@KEY@ #y"
same|||DINNERBELL_SECRET_KEY="@KEY@\"q"|||DINNERBELL_SECRET_KEY='@KEY@"q'
same|||DINNERBELL_SECRET_KEY="@KEY@\\"|||DINNERBELL_SECRET_KEY='@KEY@\\'
different|||DINNERBELL_SECRET_KEY="@KEY@\n"|||DINNERBELL_SECRET_KEY=@KEY@
same|||DINNERBELL_SECRET_KEY=@KEY@|||@BOM@DINNERBELL_SECRET_KEY=@KEY@
same|||DINNERBELL_SECRET_KEY=@KEY@|||dinnerbell_secret_key=@KEY@
different|||DINNERBELL_SECRET_KEY=@KEY@#notcomment|||DINNERBELL_SECRET_KEY=@KEY@
same|||DINNERBELL_SECRET_KEY=@KEY@   # comment|||  export DINNERBELL_SECRET_KEY = '@KEY@'  # c
different|||DINNERBELL_SECRET_KEY="@KEY@" junk|||DINNERBELL_SECRET_KEY=@KEY@
CASES

# Each case: lines for .env.test, separated by "@@", that hold production's key (@PROD@, from
# .env) where the parser can't see it. The wrapper must refuse each one by name, because a line
# contains production's value.
while IFS= read -r case_line; do
    printf '%s\n' "$case_line" | sed -e "s/@PROD@/$production_key/g" -e "s/@KEY@/$test_key/g" |
        tr '@' '\n' | sed '/^$/d' >"$stubs/lines"
    { env_test_with_secrets | grep -v '^DINNERBELL_SECRET_KEY='; cat "$stubs/lines"; } >.env.test
    { cat .env.example; printf 'DINNERBELL_SECRET_KEY=%s\n' "$production_key"; } >.env
    start deploy/dinnerbell-test up -d
    check "production's key inside .env.test is refused: $case_line" \
        refused_unchecked "contains production's DINNERBELL_SECRET_KEY"
done <<'CASES'
'DINNERBELL_SECRET_KEY'=@PROD@
COPY=@PROD@@@DINNERBELL_SECRET_KEY=${COPY}
DINNERBELL_SECRET_KEY="@PROD@@@"
dinnerbell_secret_key=@PROD@@@DINNERBELL_SECRET_KEY=@KEY@
# DINNERBELL_SECRET_KEY=@PROD@@@DINNERBELL_SECRET_KEY=@KEY@
DINNERBELL_SECRET_KEY=@KEY@@@OLD_KEY="@PROD@"  # from production
CASES
env_test_with_secrets >.env.test
printf '# old: %s\n' "$production_bot_token" >>.env.test
{ cat .env.example; printf 'DINNERBELL_SLACK_BOT_TOKEN=%s\n' "$production_bot_token"; } >.env
start deploy/dinnerbell-test up -d
check "production's bot token in a comment in .env.test is refused by name" \
    refused_unchecked "contains production's DINNERBELL_SLACK_BOT_TOKEN"
for token in xoxb-15-bytes-a xoxb-16-bytes-ab; do
    env_test_with_secrets >.env.test
    printf '# old: %s\n' "$token" >>.env.test
    { cat .env.example; printf 'DINNERBELL_SLACK_BOT_TOKEN=%s\n' "$token"; } >.env
    start deploy/dinnerbell-test up -d
    if [ "${#token}" -ge 16 ]; then
        check "a ${#token}-byte production token inside .env.test is refused" \
            refused_unchecked "contains production's DINNERBELL_SLACK_BOT_TOKEN"
    else
        check "a ${#token}-byte production token inside .env.test doesn't count" started_cleanly
    fi
done
start PATH="$stubs/broken-awk:$PATH" deploy/dinnerbell-test up -d
check "a comparison that fails is refused, not passed" refused_unchecked "can't compare DINNERBELL_SECRET_KEY"

test_secret_files
cp .env.example .env
filled_env_test >.env.test

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
    # Made-up secrets in the files, to show that none reaches a podman command line.
    production_secret_files
    test_secret_files
    production=$($pc --dry-run -p dinnerbell up -d 2>&1) || true
    production_files=$($pc --dry-run -p dinnerbell -f compose.yaml -f compose.secrets.yaml up -d 2>&1) || true
    test_instance=$(DINNERBELL_COMPOSE="$pc --dry-run" DINNERBELL_ENGINE="$stubs/engine" \
        deploy/dinnerbell-test up -d 2>&1) || true
    # podman-compose 1.6.0 and later mount with --mount instead of -v.
    contains "production: volume dinnerbell_dinnerbell-data" "$production" \
        "dinnerbell_dinnerbell-data:/data" "source=dinnerbell_dinnerbell-data,destination=/data"
    # The memory limit and the log driver reach podman in every version. pids_limit reaches it only
    # in 1.6.0 and later (1.0.6 drops it without a warning, which docs/configuration.md says).
    contains "production: memory limit" "$production" "-m 512m" "--memory 512m" "--memory=512m" "-m=512m"
    contains "production: log driver json-file" "$production" "--log-driver=json-file"
    contains "production: log-opt max-size" "$production" "--log-opt=max-size=10m"
    if podman-compose --help 2>&1 | grep -q -- '--verbose'; then
        contains "production: pids limit" "$production" "--pids-limit 256" "--pids-limit=256"
    fi
    contains "production: port 8013" "$production" "127.0.0.1:8013:8013"
    contains "test: volume dinnerbell-test_dinnerbell-data" "$test_instance" \
        "dinnerbell-test_dinnerbell-data:/data" "source=dinnerbell-test_dinnerbell-data,destination=/data"
    contains "test: port 8014" "$test_instance" "127.0.0.1:8014:8013"
    contains "test: image :main" "$test_instance" "ghcr.io/dsmorgan/dinnerbell:main"
    # By default nothing is mounted at /run/secrets. With compose.secrets.yaml, the secret files are
    # mounted by path (podman-compose 1.0.6 and 1.6.0 both bind-mount them).
    if printf '%s' "$production" | grep -qF "/run/secrets/"; then
        fail "production mounts nothing at /run/secrets by default"
    else
        pass "production mounts nothing at /run/secrets by default"
    fi
    for name in $SECRET_NAMES; do
        contains "production with compose.secrets.yaml: secrets/$name mounted at /run/secrets/$name" \
            "$production_files" "/secrets/$name:/run/secrets/$name:"
        contains "test: secrets-test/$name mounted at /run/secrets/$name" "$test_instance" \
            "/secrets-test/$name:/run/secrets/$name:"
    done
    leaked=no
    for secret in $test_secrets xapp-production production-client-secret; do
        if printf '%s%s' "$production_files" "$test_instance" | grep -qF -- "$secret"; then leaked=yes; fi
    done
    if [ "$leaked" = no ]; then pass "no secret value in podman-compose's commands"; else fail "no secret value in podman-compose's commands"; fi
    if printf '%s' "$test_instance" | grep -qF "dinnerbell_dinnerbell-data"; then
        fail "test never touches production's volume"
    else
        pass "test never touches production's volume"
    fi
fi

echo "== deploy/check-secrets"
test_secret_files
expect "check-secrets: four good files in secrets-test/" deploy/check-secrets secrets-test
chmod 644 secrets-test/DINNERBELL_SECRET_KEY
check_fails() { ! "$@" >/dev/null 2>&1; }
check_prints_no_secret() {
    out=$(deploy/check-secrets secrets-test 2>&1)
    for secret in $test_secrets; do
        ! printf '%s' "$out" | grep -qF -- "$secret" || return 1
    done
}
check "check-secrets: mode 644 is a problem" check_fails deploy/check-secrets secrets-test
chmod 600 secrets-test/DINNERBELL_SECRET_KEY
rm secrets-test/DINNERBELL_SLACK_APP_TOKEN
check "check-secrets: a missing file is a problem" check_fails deploy/check-secrets secrets-test
mkdir secrets-test/DINNERBELL_SLACK_APP_TOKEN
check "check-secrets: a directory is a problem" check_fails deploy/check-secrets secrets-test
rmdir secrets-test/DINNERBELL_SLACK_APP_TOKEN
test_secret_files
: >secrets-test/DINNERBELL_SECRET_KEY
check "check-secrets: an empty key is a problem" check_fails deploy/check-secrets secrets-test
test_secret_files
cp secrets/DINNERBELL_SECRET_KEY.example secrets-test/DINNERBELL_SECRET_KEY
chmod 600 secrets-test/DINNERBELL_SECRET_KEY
check "check-secrets: the example placeholder key is a problem" check_fails deploy/check-secrets secrets-test
test_secret_files
# Non-ASCII keys count by character, as the app does: 40 characters from 5 distinct ones is
# repetitive, and 32 distinct accented ones is fine (as many bytes as 64, so a byte count passes both).
printf '%s' "éééééééééééééééààààààààààààààààèèèèèèèè" >secrets-test/DINNERBELL_SECRET_KEY
check "check-secrets: a repetitive non-ASCII key is a problem" check_fails deploy/check-secrets secrets-test
printf '%s' "àáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿāă" >secrets-test/DINNERBELL_SECRET_KEY
expect "check-secrets: a long non-ASCII key is fine" deploy/check-secrets secrets-test
test_secret_files
write_secrets secrets-test "$test_key" - - "$test_client_secret"
expect "check-secrets: empty Slack tokens are fine" deploy/check-secrets secrets-test
check "check-secrets prints no secret value" check_prints_no_secret
test_secret_files

echo "== moving secrets from .env to files"
# Run the first snippet in docs/setup.md's "Keep secrets in files" on a .env whose values hold both
# kinds of quote, then compare with deploy/check-secrets --env-file, in a scratch copy of the layout.
mig="$stubs/mig"
mkdir -p "$mig/deploy"
cp deploy/check-secrets "$mig/deploy/"
{
    printf 'DINNERBELL_BASE_URL=https://dinner.example.com\n'
    printf 'export DINNERBELL_SECRET_KEY = "ab'"'"'c\\"d-0123456789-0123456789-0123456789"\n'
    printf 'DINNERBELL_OIDC_CLIENT_SECRET='"'"'it"s a $secret'"'"'\r\n'
    printf 'DINNERBELL_SLACK_BOT_TOKEN=xoxb-"plain"-'"'"'mixed'"'"'\n'
    printf 'DINNERBELL_SLACK_APP_TOKEN=\n'
} >"$mig/.env"
awk '/^### Keep secrets in files/ { f = 1 } f && /^   ```sh/ { c++; if (c == 1) { g = 1; next } } g && /^   ```$/ { exit } g { sub(/^   /, ""); print }' \
    docs/setup.md >"$stubs/migrate.sh"
(cd "$mig" && sh "$stubs/migrate.sh")
check "migration keeps quotes inside a value, drops one enclosing pair and the line ending" \
    test "$(cat "$mig/secrets/DINNERBELL_OIDC_CLIENT_SECRET")" = 'it"s a $secret' \
    -a "$(cat "$mig/secrets/DINNERBELL_SLACK_BOT_TOKEN")" = "xoxb-\"plain\"-'mixed'"
migration_report=$("$mig/deploy/check-secrets" --env-file "$mig/.env" 2>&1) || true
check "check-secrets --env-file: four matches, and no value in the output" \
    test "$(printf '%s\n' "$migration_report" | grep -c ': match$')" -eq 4 -a "$(printf '%s' "$migration_report" | grep -c 'plain\|a \$secret\|ab.c')" -eq 0
printf 'x' >"$mig/secrets/DINNERBELL_SECRET_KEY"
migration_report=$("$mig/deploy/check-secrets" --env-file "$mig/.env" 2>&1) || true
check "check-secrets --env-file: a file that differs says differs" \
    sh -c 'printf "%s\n" "$1" | grep -q "^DINNERBELL_SECRET_KEY: differs$"' _ "$migration_report"

echo "== build context"
# The image build must never copy secret files (.dockerignore links to .containerignore).
for ignore in .containerignore .dockerignore; do
    for dir in secrets secrets-test; do
        expect "$ignore excludes $dir/" grep -qx "$dir" "$ignore"
    done
done

echo "== secret files in git"
is_placeholder() { grep -qi replace "$1"; }
is_not_placeholder() { ! is_placeholder "$1"; }
# Only placeholders and the note may be tracked in secrets/ and secrets-test/, and git must ignore
# the real files. The placeholders hold no value.
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    tracked=$(git ls-files secrets secrets-test | grep -v -e '^secrets/[A-Z_]*\.example$' -e '^secrets/README\.md$' || true)
    if [ -z "$tracked" ]; then pass "git tracks only placeholders in secrets/"; else fail "git tracks secret files: $tracked"; fi
    for name in $SECRET_NAMES; do
        expect "git ignores secrets/$name and secrets-test/$name" \
            test "$(git check-ignore "secrets/$name" "secrets-test/$name" | wc -l)" -eq 2
        expect "secrets/$name.example is a placeholder, not a value" is_placeholder "secrets/$name.example"
    done
    # The same marker rule as the app's (case-insensitive "replace"); a real-looking value fails it.
    realistic=$(mktemp)
    printf '%s' "$test_key" >"$realistic"
    check "a real-looking value isn't taken for a placeholder" is_not_placeholder "$realistic"
    rm -f "$realistic"
fi

[ "$failures" -eq 0 ] || { echo "check-compose: $failures check(s) failed." >&2; exit 1; }
echo "check-compose: all checks passed."
