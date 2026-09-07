#!/bin/bash
set -euo pipefail

ROOT=$(dirname -- "$(realpath -- "$0")")
NATIVE_LAB="$ROOT/native-lab"
TEST_ROOT=$(mktemp -d -p "$ROOT" .native-lab-policy-smoke.XXXXXXXX)
TEST_RUNTIME=$(mktemp -d /tmp/native-lab-policy-runtime.XXXXXXXX)
TEST_CONFIG=$(mktemp -d /tmp/native-lab-policy-config.XXXXXXXX)
CURRENT="$TEST_ROOT/current"
TRUSTED="$TEST_ROOT/trusted"
UNTRUSTED="$TEST_ROOT/untrusted"
EXTRA_RO="$TEST_ROOT/extra-ro"
EXTRA_RO_2="$TEST_ROOT/extra-ro-2"
CODEX_CONFIG="$TEST_CONFIG/codex.toml"
NATIVE_CONFIG="$TEST_CONFIG/native-lab/config.toml"
REAL_HOME=$(getent passwd "$(id -u)" | cut -d: -f6)
NPM_GLOBAL="$REAL_HOME/.npm-global"
NPM_CACHE="$REAL_HOME/.npm"
NPM_BIN="$NPM_GLOBAL/bin"
NPM_EXEC="$NPM_BIN/native-lab-policy-probe-$$"
NPM_CACHE_PROBE="$NPM_CACHE/native-lab-policy-probe-$$"
CREATED_NPM_GLOBAL=0
CREATED_NPM_BIN=0
CREATED_NPM_CACHE=0

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

pass() {
    printf 'ok - %s\n' "$*"
}

in_current() {
    (cd "$CURRENT" && "$NATIVE_LAB" "$@")
}

write_native_config() {
    local second_extra=${1-}
    {
        printf '%s\n' \
            'version = 1' \
            '' \
            '[codex]' \
            'import_trusted_projects = true'
        printf 'config_path = "%s"\n' "$CODEX_CONFIG"
        printf '%s\n' '' '[filesystem]'
        if [[ -n "$second_extra" ]]; then
            printf 'extra_read_only = ["%s", "%s"]\n' "$EXTRA_RO" "$second_extra"
        else
            printf 'extra_read_only = ["%s"]\n' "$EXTRA_RO"
        fi
        printf '%s\n' 'extra_trusted_project_deny_globs = ["**/*.secret"]'
    } >"$NATIVE_CONFIG"
    chmod 600 "$NATIVE_CONFIG"
}

write_codex_config() {
    local trusted_level=$1
    {
        printf '[projects."%s"]\ntrust_level = "%s"\n\n' "$TRUSTED" "$trusted_level"
        printf '[projects."%s"]\ntrust_level = "untrusted"\n\n' "$UNTRUSTED"
        printf '[projects."%s"]\ntrust_level = "trusted"\n\n' "$CURRENT"
        printf '[projects."%s"]\ntrust_level = "trusted"\n' "$REAL_HOME"
    } >"$CODEX_CONFIG"
    chmod 600 "$CODEX_CONFIG"
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM HUP
    set +e
    in_current stop >/dev/null 2>&1
    rm -f -- "$NPM_EXEC" "$NPM_CACHE_PROBE"
    (( CREATED_NPM_BIN )) && rmdir -- "$NPM_BIN" 2>/dev/null
    (( CREATED_NPM_GLOBAL )) && rmdir -- "$NPM_GLOBAL" 2>/dev/null
    (( CREATED_NPM_CACHE )) && rmdir -- "$NPM_CACHE" 2>/dev/null
    rm -rf -- "$TEST_ROOT" "$TEST_RUNTIME" "$TEST_CONFIG"
    exit "$status"
}
trap cleanup EXIT INT TERM HUP

for dependency in bwrap socat ssh sshd ssh-keygen flock rg python3 getent; do
    command -v "$dependency" >/dev/null 2>&1 || fail "missing dependency: $dependency"
done
[[ "$REAL_HOME" == /* && "$REAL_HOME" != / ]] || fail 'passwd returned an unsafe HOME'

chmod 700 "$TEST_RUNTIME" "$TEST_CONFIG"
export XDG_RUNTIME_DIR="$TEST_RUNTIME"
export XDG_CONFIG_HOME="$TEST_CONFIG"
export NATIVE_LAB_TEST_TOKEN=native-lab-secret-probe
mkdir -m 700 "$TEST_CONFIG/native-lab"
mkdir -p "$CURRENT/.agents" "$CURRENT/.codex"
mkdir -p "$TRUSTED/.git" "$TRUSTED/.agents" "$TRUSTED/.codex" "$TRUSTED/nested"
mkdir -p "$UNTRUSTED" "$EXTRA_RO" "$EXTRA_RO_2"

printf 'current\n' >"$CURRENT/normal.txt"
printf 'current-env\n' >"$CURRENT/.env"
printf 'current-pem\n' >"$CURRENT/current.pem"
printf 'hidden\n' >"$CURRENT/.agents/private"
printf 'hidden\n' >"$CURRENT/.codex/private"
printf 'trusted\n' >"$TRUSTED/source.txt"
printf 'secret\n' >"$TRUSTED/.env"
printf 'secret\n' >"$TRUSTED/.env.local"
printf 'secret\n' >"$TRUSTED/cert.pem"
printf 'secret\n' >"$TRUSTED/private.key"
printf 'secret\n' >"$TRUSTED/nested/archive.p12"
printf 'secret\n' >"$TRUSTED/nested/custom.secret"
printf 'metadata\n' >"$TRUSTED/.git/config"
printf 'metadata\n' >"$TRUSTED/.agents/private"
printf 'metadata\n' >"$TRUSTED/.codex/private"
printf 'untrusted\n' >"$UNTRUSTED/source.txt"
printf 'extra\n' >"$EXTRA_RO/reference.txt"
printf 'extra-2\n' >"$EXTRA_RO_2/reference.txt"

if [[ ! -d "$NPM_GLOBAL" ]]; then
    mkdir -- "$NPM_GLOBAL"
    CREATED_NPM_GLOBAL=1
fi
if [[ ! -d "$NPM_BIN" ]]; then
    mkdir -- "$NPM_BIN"
    CREATED_NPM_BIN=1
fi
if [[ ! -d "$NPM_CACHE" ]]; then
    mkdir -- "$NPM_CACHE"
    CREATED_NPM_CACHE=1
fi
printf '#!/bin/sh\nprintf "npm-global-probe\\n"\n' >"$NPM_EXEC"
chmod 700 "$NPM_EXEC"
printf 'npm-cache-probe\n' >"$NPM_CACHE_PROBE"

write_codex_config trusted
write_native_config

printf 'native-lab filesystem policy smoke test\n'
printf '  current: %s\n' "$CURRENT"
printf '  trusted: %s\n' "$TRUSTED"
printf '  runtime: %s\n' "$TEST_RUNTIME"

# Expansions in this program intentionally happen in the remote shell.
# shellcheck disable=SC2016
in_current run sh -c '
    trusted=$1
    untrusted=$2
    extra=$3
    npm_exec=$4
    npm_cache_probe=$5

    grep -q current normal.txt
    printf "write\n" >>normal.txt
    grep -q current-env .env
    printf "write\n" >>.env
    grep -q current-pem current.pem

    test -d .git
    test ! -r .git
    ! touch .git/probe 2>/dev/null
    test ! -r .agents
    ! touch .agents/probe 2>/dev/null
    test ! -r .codex
    ! touch .codex/probe 2>/dev/null

    grep -q trusted "$trusted/source.txt"
    ! sh -c "printf write >>\"$trusted/source.txt\"" 2>/dev/null
    for denied in .env .env.local cert.pem private.key nested/archive.p12 nested/custom.secret; do
        test ! -r "$trusted/$denied"
    done
    for denied in .git .agents .codex; do
        test ! -r "$trusted/$denied"
    done
    test ! -e "$untrusted"

    grep -q extra "$extra/reference.txt"
    ! sh -c "printf write >>\"$extra/reference.txt\"" 2>/dev/null
    test "$("$npm_exec")" = npm-global-probe
    grep -q npm-cache-probe "$npm_cache_probe"
    ! sh -c "printf write >>\"$npm_cache_probe\"" 2>/dev/null

    test ! -e "$HOME/.ssh"
    test ! -e "$HOME/.config/chromium"
    test ! -e "$HOME/.config/google-chrome"
    test ! -e "$HOME/.mozilla"
    test ! -e "/run/user/$(id -u)/bus"
    test "$XDG_RUNTIME_DIR" = "/run/user/$(id -u)"
    test -d "$XDG_RUNTIME_DIR"
    : >/dev/shm/native-lab-policy-probe
    test -e /dev/shm/native-lab-policy-probe
    test ! -e /var
    test -z "${NATIVE_LAB_TEST_TOKEN-}"
' _ "$TRUSTED" "$UNTRUSTED" "$EXTRA_RO" "$NPM_EXEC" "$NPM_CACHE_PROBE" ||
    fail 'resolved filesystem policy does not match the v2 contract'
pass 'workspace RW, trusted/extra RO, secret masks and private HOME/runtime work'

grep -q write "$CURRENT/normal.txt" || fail 'workspace normal-file write did not persist'
grep -q write "$CURRENT/.env" || fail 'workspace .env write did not persist'
[[ "$(<"$TRUSTED/source.txt")" == trusted ]] || fail 'trusted project was modified'
[[ "$(<"$NPM_CACHE_PROBE")" == npm-cache-probe ]] || fail 'npm cache was modified'
[[ -d "$CURRENT/.git" ]] || fail 'missing .git placeholder was not created'
[[ -z "$(find "$CURRENT/.git" -mindepth 1 -print -quit)" ]] ||
    fail 'missing .git placeholder gained content'
pass 'only current-workspace writes persist and missing metadata is held empty'

status_before=$(in_current status) || fail 'policy session is not alive'
session_before=$(awk '/^Session:/ {print $2}' <<<"$status_before")
digest_before=$(awk '/^Policy digest:/ {print $3}' <<<"$status_before")
grep -q '^Trusted projects: 1$' <<<"$status_before" || fail 'trusted project summary is wrong'
grep -Fq "  - $TRUSTED" <<<"$status_before" || fail 'trusted project is absent from status'
[[ "$digest_before" =~ ^[0-9a-f]{64}$ ]] || fail 'status policy digest is invalid'
pass 'status exposes policy digest and trusted-project summary'

in_current stop >/dev/null || fail 'policy session did not stop'
[[ ! -e "$CURRENT/.git" ]] || fail 'synthetic .git placeholder survived stop'
[[ -d "$CURRENT/.agents" && -d "$CURRENT/.codex" ]] ||
    fail 'pre-existing protected metadata was removed'
pass 'stop removes only NativeLab-created metadata placeholders'

ln -s -- "$TRUSTED" "$CURRENT/.git"
set +e
symlink_error=$(in_current status 2>&1)
symlink_status=$?
set -e
rm -f -- "$CURRENT/.git"
[[ "$symlink_status" -ne 0 ]] || fail 'protected metadata symlink did not fail closed'
grep -q 'cannot mask symlink fail-closed' <<<"$symlink_error" ||
    fail 'protected metadata symlink failed without a clear diagnostic'
pass 'protected metadata symlinks fail closed before sandbox startup'

write_codex_config untrusted
status_without_trust=$(in_current status 2>&1 || true)
session_without_trust=$(awk '/^Session:/ {print $2}' <<<"$status_without_trust")
digest_without_trust=$(awk '/^Policy digest:/ {print $3}' <<<"$status_without_trust")
[[ "$session_without_trust" != "$session_before" && "$digest_without_trust" != "$digest_before" ]] ||
    fail 'removing Codex trust did not change session identity and policy digest'
pass 'Codex trust changes invalidate persistent-session identity'

write_native_config "$EXTRA_RO_2"
status_extra_changed=$(in_current status 2>&1 || true)
session_extra_changed=$(awk '/^Session:/ {print $2}' <<<"$status_extra_changed")
digest_extra_changed=$(awk '/^Policy digest:/ {print $3}' <<<"$status_extra_changed")
[[ "$session_extra_changed" != "$session_without_trust" &&
   "$digest_extra_changed" != "$digest_without_trust" ]] ||
    fail 'changing NativeLab extra_read_only did not change policy identity'
pass 'NativeLab config changes invalidate persistent-session identity'

fake_bin="$CURRENT/fake-bin"
mkdir "$fake_bin"
printf '#!/bin/sh\n: >"%s"\nexit 99\n' "$CURRENT/fake-rg-ran" >"$fake_bin/rg"
chmod 700 "$fake_bin/rg"
(
    cd "$CURRENT"
    PATH="$fake_bin:$PATH" "$NATIVE_LAB" status >/dev/null 2>&1 || true
)
[[ ! -e "$CURRENT/fake-rg-ran" ]] || fail 'host-side policy resolution used workspace PATH'
pass 'host-side TCB ignores workspace-supplied executables'

printf 'All filesystem policy smoke tests passed.\n'
