#!/usr/bin/env bash
set -euo pipefail

# Execute INSIDE the bubblewrap sandbox.
ROOT="${NATIVE_LAB_ROOT:-$PWD}"
RUN_DIR="$ROOT/run"
SOCK="$RUN_DIR/lab-ssh.sock"
STATE_DIR="$RUN_DIR/lab-ssh-state"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing dependency: $1" >&2; exit 127; }; }
need socat
need sshd
need ssh-keygen
need readlink

SSHD_BIN="$(command -v sshd)"
mkdir -p "$RUN_DIR" "$STATE_DIR"
chmod 700 "$STATE_DIR"

HOST_KEY="$STATE_DIR/ssh_host_ed25519_key"
CLIENT_KEY="$STATE_DIR/client_ed25519"
AUTHORIZED_KEYS="$STATE_DIR/authorized_keys"
KNOWN_HOSTS="$STATE_DIR/known_hosts"
CONFIG="$STATE_DIR/sshd_config"
INETD_WRAPPER="$STATE_DIR/sshd-inetd.sh"
USER_FILE="$STATE_DIR/user"

[[ -s "$HOST_KEY" ]] || ssh-keygen -q -t ed25519 -N '' -f "$HOST_KEY"
[[ -s "$CLIENT_KEY" ]] || ssh-keygen -q -t ed25519 -N '' -f "$CLIENT_KEY"

LOGIN_USER="$(id -un)"
printf '%s\n' "$LOGIN_USER" >"$USER_FILE"
cat "$CLIENT_KEY.pub" >"$AUTHORIZED_KEYS"
chmod 600 "$HOST_KEY" "$CLIENT_KEY" "$AUTHORIZED_KEYS"
chmod 644 "$HOST_KEY.pub" "$CLIENT_KEY.pub" "$USER_FILE"

{
    printf 'native-lab '
    awk '{print $1, $2}' "$HOST_KEY.pub"
} >"$KNOWN_HOSTS"
chmod 600 "$KNOWN_HOSTS"

cat >"$CONFIG" <<EOF
HostKey $HOST_KEY
PidFile none

PubkeyAuthentication yes
AuthenticationMethods publickey
AuthorizedKeysFile $AUTHORIZED_KEYS
StrictModes no

PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
UsePAM no

AllowUsers $LOGIN_USER
PermitRootLogin prohibit-password

PermitTTY no
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
PermitTunnel no
PermitUserEnvironment no
PermitUserRC no

PrintMotd no
PrintLastLog no
UseDNS no
LogLevel VERBOSE
EOF

cat >"$INETD_WRAPPER" <<EOF
#!/usr/bin/env sh
exec "$SSHD_BIN" -i -e -f "$CONFIG"
EOF
chmod 700 "$INETD_WRAPPER"

rm -f "$SOCK"
cleanup() { rm -f "$SOCK"; }
trap cleanup EXIT INT TERM HUP

echo "native-lab SSH bridge"
echo "  user:     $LOGIN_USER"
echo "  socket:   $SOCK"
echo "  state:    $STATE_DIR"
echo "  netns:    $(readlink /proc/self/ns/net)"
echo "  mountns:  $(readlink /proc/self/ns/mnt)"
echo
echo "Waiting for SSH connections over AF_UNIX..."
echo

# Do NOT add socat's 'stderr' option here: sshd diagnostics must stay out
# of the SSH byte stream.
socat "UNIX-LISTEN:$SOCK,fork,mode=0600" "EXEC:$INETD_WRAPPER"
