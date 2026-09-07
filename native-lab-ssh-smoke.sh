#!/usr/bin/env bash
set -euo pipefail

# Execute OUTSIDE the bubblewrap sandbox.
ROOT="${NATIVE_LAB_ROOT:-$PWD}"
RUN_DIR="$ROOT/run"
SOCK="$RUN_DIR/lab-ssh.sock"
STATE_DIR="$RUN_DIR/lab-ssh-state"

CLIENT_KEY="$STATE_DIR/client_ed25519"
KNOWN_HOSTS="$STATE_DIR/known_hosts"
USER_FILE="$STATE_DIR/user"

for path in "$SOCK" "$CLIENT_KEY" "$KNOWN_HOSTS" "$USER_FILE"; do
    [[ -e "$path" ]] || {
        echo "missing: $path" >&2
        echo "Is native-lab-sshd-server.sh running inside the bwrap sandbox?" >&2
        exit 1
    }
done

LOGIN_USER="$(cat "$USER_FILE")"

SSH_OPTS=(
    -T
    -o "ProxyCommand=socat STDIO UNIX-CONNECT:$SOCK"
    -o "HostKeyAlias=native-lab"
    -o "UserKnownHostsFile=$KNOWN_HOSTS"
    -o "StrictHostKeyChecking=yes"
    -o "IdentitiesOnly=yes"
    -o "PasswordAuthentication=no"
    -o "KbdInteractiveAuthentication=no"
    -o "ConnectTimeout=5"
    -i "$CLIENT_KEY"
)

printf 'host netns:    %s\n' "$(readlink /proc/self/ns/net)"
printf 'host mountns:  %s\n' "$(readlink /proc/self/ns/mnt)"
printf 'socket:        %s\n\n' "$SOCK"

printf -v ROOT_Q '%q' "$ROOT"

REMOTE_SMOKE="
set -eu
cd $ROOT_Q
echo '--- process / namespace probe ---'
printf 'pid=%s ppid=%s\n' \"\$\$\" \"\$PPID\"
date --iso-8601=seconds
printf 'netns='; readlink /proc/self/ns/net
printf 'mntns='; readlink /proc/self/ns/mnt
printf 'pidns='; readlink /proc/self/ns/pid
echo
if [ -x ./sandboxscape.sh ]; then
    echo '--- sandboxscape.sh ---'
    ./sandboxscape.sh
else
    echo '(./sandboxscape.sh not found/executable; skipping network probe)'
fi
"

echo "=== SSH exec smoke test ==="
ssh "${SSH_OPTS[@]}" "$LOGIN_USER@native-lab" "$REMOTE_SMOKE"

echo
echo "=== raw stdin/stdout/stderr smoke test ==="
printf 'hello-through-stdin\n' |
    ssh "${SSH_OPTS[@]}" "$LOGIN_USER@native-lab" \
        'IFS= read -r line; printf "stdout: got <%s>\n" "$line"; printf "stderr: separate channel works\n" >&2'

echo
echo "Smoke test complete."
