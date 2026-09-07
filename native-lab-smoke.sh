#!/usr/bin/env bash
set -euo pipefail

ROOT=$(dirname -- "$(realpath -- "$0")")
NATIVE_LAB="$ROOT/native-lab"
TEST_RUNTIME=$(mktemp -d /tmp/native-lab-smoke.XXXXXX)
chmod 700 "$TEST_RUNTIME"
export XDG_RUNTIME_DIR="$TEST_RUNTIME"

INTERNAL_PORT=$((20000 + $$ % 10000))
HOST_PORT=$((INTERNAL_PORT + 1))
HOST_SERVER_PID=
INTERNAL_CLIENT_PID=

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

pass() {
    printf 'ok - %s\n' "$*"
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM HUP
    set +e
    [[ -n "$INTERNAL_CLIENT_PID" ]] && kill "$INTERNAL_CLIENT_PID" 2>/dev/null
    [[ -n "$HOST_SERVER_PID" ]] && kill "$HOST_SERVER_PID" 2>/dev/null
    "$NATIVE_LAB" stop >/dev/null 2>&1
    wait "$INTERNAL_CLIENT_PID" 2>/dev/null
    wait "$HOST_SERVER_PID" 2>/dev/null
    rm -rf -- "$TEST_RUNTIME"
    exit "$status"
}
trap cleanup EXIT INT TERM HUP

for dependency in bwrap socat ssh sshd ssh-keygen flock curl python3; do
    command -v "$dependency" >/dev/null 2>&1 || fail "missing dependency: $dependency"
done

printf 'native-lab smoke test\n'
printf '  workspace: %s\n' "$ROOT"
printf '  runtime:   %s\n' "$TEST_RUNTIME"

cd "$ROOT"

pids=()
for _ in 1 2 3 4 5 6; do
    "$NATIVE_LAB" run true &
    pids+=("$!")
done
for pid in "${pids[@]}"; do
    wait "$pid" || fail 'a concurrent first run failed'
done
pass 'concurrent startup creates a usable session'

status_output=$("$NATIVE_LAB" status) || fail 'status did not report an alive session'
grep -q '^SSH socket:      alive$' <<<"$status_output" || fail 'status is not alive'
holder_before=$(awk '/^Holder PID:/ {print $3}' <<<"$status_output")
[[ "$holder_before" =~ ^[0-9]+$ ]] || fail 'status returned an invalid holder PID'
pass 'status reports functional liveness and holder metadata'

session_dir=$(awk '/^Runtime:/ {sub(/^Runtime:[[:space:]]*/, ""); print}' <<<"$status_output")
[[ "$(stat -Lc '%a' "$XDG_RUNTIME_DIR/native-lab")" == 700 ]] || fail 'runtime root mode is not 0700'
[[ "$(stat -Lc '%a' "$session_dir")" == 700 ]] || fail 'session directory mode is not 0700'
[[ "$(stat -Lc '%a' "$session_dir/control")" == 700 ]] || fail 'control directory mode is not 0700'
[[ "$(stat -Lc '%a' "$session_dir/control/lab-ssh.sock")" == 600 ]] || fail 'socket mode is not 0600'
[[ "$(stat -Lc '%a' "$session_dir/control/client_ed25519")" == 600 ]] || fail 'client key mode is not 0600'
[[ "$(stat -Lc '%a' "$session_dir/control/ssh_host_ed25519_key")" == 600 ]] || fail 'host key mode is not 0600'
pass 'runtime directories, socket and private keys use restrictive modes'

host_netns=$(readlink /proc/self/ns/net)
host_mountns=$(readlink /proc/self/ns/mnt)
host_pidns=$(readlink /proc/self/ns/pid)
remote_netns=$("$NATIVE_LAB" run readlink /proc/self/ns/net)
remote_mountns=$("$NATIVE_LAB" run readlink /proc/self/ns/mnt)
remote_pidns=$("$NATIVE_LAB" run readlink /proc/self/ns/pid)
[[ "$host_netns" != "$remote_netns" ]] || fail 'network namespace matches the host'
[[ "$host_mountns" != "$remote_mountns" ]] || fail 'mount namespace matches the host'
[[ "$host_pidns" != "$remote_pidns" ]] || fail 'PID namespace matches the host'
pass 'network, mount and PID namespaces are private'

cap_eff=$("$NATIVE_LAB" run sh -c "awk '/CapEff/ {print \$2}' /proc/self/status")
[[ "$cap_eff" == 0000000000000000 ]] || fail "effective capabilities are not empty: $cap_eff"
pass 'policy 1 drops all effective capabilities'

"$NATIVE_LAB" run sh -c '
    test ! -e "/run/user/$(id -u)/bus"
    test ! -e /tmp/.X11-unix
    test -d /run/native-lab-control
    test "$(find /run -mindepth 1 -maxdepth 1 | wc -l)" -eq 1
' || fail '/run exposes more than the control directory'
pass '/run and host desktop sockets are private'

"$NATIVE_LAB" run sh -c '
    workspace_probe="$PWD/.native-lab-write-probe.$$"
    : >"$workspace_probe"
    rm -f -- "$workspace_probe"
    if touch "$HOME/.native-lab-ro-probe.$$" 2>/dev/null; then
        rm -f -- "$HOME/.native-lab-ro-probe.$$"
        exit 1
    fi
' || fail 'filesystem read-only boundary is incorrect'
pass 'workspace is writable while the user home outside it is read-only'

if curl --silent --show-error --max-time 0.3 "http://127.0.0.1:$INTERNAL_PORT" >/dev/null 2>&1; then
    fail "host port $INTERNAL_PORT is already occupied"
fi
"$NATIVE_LAB" run python3 -u -m http.server "$INTERNAL_PORT" --bind 127.0.0.1 \
    >"$TEST_RUNTIME/internal-server.log" 2>&1 &
INTERNAL_CLIENT_PID=$!

internal_ready=0
for ((attempt = 0; attempt < 30; attempt++)); do
    if "$NATIVE_LAB" run curl --silent --fail --max-time 0.3 \
        "http://127.0.0.1:$INTERNAL_PORT" >/dev/null 2>&1; then
        internal_ready=1
        break
    fi
    sleep 0.1
done
[[ "$internal_ready" -eq 1 ]] || fail 'internal localhost server did not become reachable'
if curl --silent --max-time 0.5 "http://127.0.0.1:$INTERNAL_PORT" >/dev/null 2>&1; then
    fail 'sandbox localhost server is visible from the host'
fi
pass 'long-lived commands share internal localhost, which is hidden from the host'

python3 -u -m http.server "$HOST_PORT" --bind 127.0.0.1 \
    >"$TEST_RUNTIME/host-server.log" 2>&1 &
HOST_SERVER_PID=$!
host_ready=0
for ((attempt = 0; attempt < 20; attempt++)); do
    if curl --silent --fail --max-time 0.3 "http://127.0.0.1:$HOST_PORT" >/dev/null 2>&1; then
        host_ready=1
        break
    fi
    sleep 0.1
done
[[ "$host_ready" -eq 1 ]] || fail 'host-only test server did not start'
if "$NATIVE_LAB" run curl --silent --max-time 0.5 \
    "http://127.0.0.1:$HOST_PORT" >/dev/null 2>&1; then
    fail 'host localhost is visible from the sandbox'
fi
pass 'host localhost is not visible in the sandbox'

"$NATIVE_LAB" run python3 -c '
import socket
s = socket.socket()
s.settimeout(0.5)
try:
    s.connect(("1.1.1.1", 443))
except OSError:
    raise SystemExit(0)
raise SystemExit(1)
' || fail 'sandbox can connect to the Internet'
pass 'Internet access is unavailable'

stream_file="$TEST_RUNTIME/stream.out"
"$NATIVE_LAB" run python3 -u -c \
    'import time; print("start", flush=True); time.sleep(1); print("end", flush=True)' \
    >"$stream_file" &
stream_pid=$!
stream_seen=0
for ((attempt = 0; attempt < 7; attempt++)); do
    if grep -q '^start$' "$stream_file" 2>/dev/null; then
        stream_seen=1
        break
    fi
    sleep 0.1
done
[[ "$stream_seen" -eq 1 ]] || fail 'first output was buffered'
wait "$stream_pid" || fail 'streaming command failed'
grep -q '^end$' "$stream_file" || fail 'streaming command lost final output'
pass 'stdout streams before process exit'

stdio_out="$TEST_RUNTIME/stdio.out"
stdio_err="$TEST_RUNTIME/stdio.err"
printf 'hello-through-stdin\n' | "$NATIVE_LAB" run sh -c \
    'IFS= read -r line; printf "stdout:%s\n" "$line"; printf "stderr:separate\n" >&2' \
    >"$stdio_out" 2>"$stdio_err"
[[ "$(<"$stdio_out")" == 'stdout:hello-through-stdin' ]] || fail 'stdin/stdout mismatch'
[[ "$(<"$stdio_err")" == 'stderr:separate' ]] || fail 'stderr was not separate'
pass 'stdin, stdout and stderr retain SSH channel semantics'

set +e
"$NATIVE_LAB" run sh -c 'exit 37'
remote_status=$?
set -e
[[ "$remote_status" -eq 37 ]] || fail "expected exit 37, got $remote_status"
pass 'remote exit status is preserved'

quoted=$("$NATIVE_LAB" run python3 -c \
    'import sys; print("|".join(value.encode().hex() for value in sys.argv[1:]))' \
    '' 'a b' "single'quote" '$HOME' '*' $'line\nbreak')
expected='|612062|73696e676c652771756f7465|24484f4d45|2a|6c696e650a627265616b'
[[ "$quoted" == "$expected" ]] || fail "argv quoting mismatch: $quoted"
pass 'remote argv preserves empty and metacharacter-containing arguments'

host_npm=$(command -v npm 2>/dev/null || true)
if [[ -n "$host_npm" ]]; then
    remote_npm=$("$NATIVE_LAB" run sh -c 'command -v npm')
    [[ "$remote_npm" == "$host_npm" ]] || fail "remote PATH did not preserve npm: $remote_npm"
    pass 'remote command lookup preserves the caller PATH'
fi

kill -KILL "$holder_before"
for ((attempt = 0; attempt < 20; attempt++)); do
    kill -0 "$holder_before" 2>/dev/null || break
    sleep 0.05
done
"$NATIVE_LAB" run true || fail 'session did not recover after abrupt holder death'
holder_after=$("$NATIVE_LAB" status | awk '/^Holder PID:/ {print $3}')
[[ "$holder_after" =~ ^[0-9]+$ && "$holder_after" != "$holder_before" ]] ||
    fail 'stale recovery did not create a new holder'
pass 'stale socket and metadata recover after abrupt death'

"$NATIVE_LAB" stop >/dev/null
set +e
stopped_status=$("$NATIVE_LAB" status 2>&1)
status_rc=$?
set -e
[[ "$status_rc" -eq 1 ]] || fail 'stopped status should return 1'
grep -q '^SSH socket:      stopped$' <<<"$stopped_status" || fail 'status did not report stopped'
"$NATIVE_LAB" stop >/dev/null || fail 'second stop was not idempotent'
pass 'stop is complete and idempotent'

printf 'All smoke tests passed.\n'
