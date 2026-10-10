#!/bin/bash
set -euo pipefail
umask 077

ROOT=$(dirname -- "$(realpath -- "$0")")
LAB="$ROOT/native-lab"
REQUIRE=0
ROOTS=()
while (($#)); do
    case "$1" in
        --require-hardware) REQUIRE=1; shift ;;
        --readonly-root) ROOTS+=("${2:?missing runtime root}"); shift 2 ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done
if ! compgen -G '/dev/dri/renderD*' >/dev/null; then
    printf 'SKIP: GPU hardware/device nodes unavailable; no hardware test passed\n'
    (( REQUIRE )) && exit 1
    exit 77
fi
TEST_ROOT=$(mktemp -d -p "$ROOT" .native-lab-gpu-smoke.XXXXXXXX)
TEST_RUNTIME=$(mktemp -d /tmp/native-lab-gpu-runtime.XXXXXXXX)
TEST_CONFIG=$(mktemp -d /tmp/native-lab-gpu-config.XXXXXXXX)
export XDG_RUNTIME_DIR="$TEST_RUNTIME" XDG_CONFIG_HOME="$TEST_CONFIG"
mkdir "$TEST_ROOT/current" "$TEST_ROOT/other" "$TEST_CONFIG/native-lab"
CURRENT="$TEST_ROOT/current"
OTHER="$TEST_ROOT/other"
CONFIG="$TEST_CONFIG/native-lab/config.toml"
cleanup() {
    local result=$?
    trap - EXIT INT TERM HUP
    set +e
    (cd "$CURRENT" && "$LAB" stop --all) >/dev/null 2>&1
    (cd "$OTHER" && "$LAB" stop --all) >/dev/null 2>&1
    rm -rf -- "$TEST_ROOT" "$TEST_RUNTIME" "$TEST_CONFIG"
    exit "$result"
}
trap cleanup EXIT INT TERM HUP
current() { (cd "$CURRENT" && "$LAB" "$@"); }
other() { (cd "$OTHER" && "$LAB" "$@"); }
config() {
    /usr/bin/python3 -I - "$CONFIG" "$CURRENT" "$1" "${ROOTS[@]}" <<'PY'
import json,sys
from pathlib import Path
path,workspace,mode,*roots=sys.argv[1:]
features={'none':[], 'graphics':['graphics'], 'compute':['graphics','compute']}[mode]
Path(path).write_text('version=2\n[codex]\nimport_trusted_projects=false\n[gpu]\ndefault="none"\n'
    + '[gpu.workspaces.'+json.dumps(workspace)+']\nprovider="amd"\ndevices="auto"\nfeatures='+json.dumps(features)
    + '\nreadonly_roots='+json.dumps(roots)+'\n')
PY
}

config none
current run /usr/bin/python3 -I -c 'import pathlib; assert not pathlib.Path("/sys").exists(); assert not pathlib.Path("/dev/kfd").exists(); assert not pathlib.Path("/dev/dri").exists()'
disabled_status=$(current status)
disabled_id=$(awk '/^Session:/ {print $2}' <<<"$disabled_status")
other run true
other_id=$(other status | awk '/^Session:/ {print $2}')
printf 'ok - GPU disabled and unrelated workspace baseline\n'

config graphics
current run /usr/bin/python3 -I -c '
import json, os, pathlib
p=json.loads(pathlib.Path("/run/native-lab-bootstrap/resolved-policy.json").read_text())
assert set(pathlib.Path("/dev/dri").iterdir()) == {pathlib.Path(n["path"]) for n in p["gpu"]["nodes"]}
assert not pathlib.Path("/dev/kfd").exists()
assert not pathlib.Path("/tmp/.X11-unix").exists()
assert not list(pathlib.Path(os.environ["XDG_RUNTIME_DIR"]).glob("wayland-*"))
assert not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
assert not pathlib.Path("/run/udev/data").exists()
for n in p["gpu"]["nodes"]: os.close(os.open(n["path"],os.O_RDWR))
assert any(" /sys " in line and "ro" in line.split()[5].split(",") for line in pathlib.Path("/proc/self/mountinfo").read_text().splitlines())
print("graphics nodes, O_RDWR, sysfs RO, private display and no udev: PASS")
'
[[ "$(readlink /proc/self/ns/net)" != "$(current run readlink /proc/self/ns/net)" ]]
current doctor gpu --session
# Optional windowed diagnostics use only an internal Xvfb display. A software
# GLX renderer is reported separately from the required EGL/Vulkan checks.
if command -v xvfb-run >/dev/null && command -v glxinfo >/dev/null && command -v vkcube >/dev/null; then
    # Variables below belong to the shell running inside NativeLab.
    # shellcheck disable=SC2016
    current run xvfb-run -a -s '-screen 0 640x480x24 -nolisten tcp' sh -c '
        glxinfo -B
        timeout 15 vkcube --c 60
        cube_status=$?
        printf "Xvfb vkcube exit: %s\n" "$cube_status"
        timeout 3 stdbuf -oL glxgears -info
        gears_status=$?
        printf "Xvfb glxgears exit: %s (124 means the animation ran until timeout)\n" "$gears_status"
    '
fi
graphics_status=$(current status)
graphics_id=$(awk '/^Session:/ {print $2}' <<<"$graphics_status")
[[ "$graphics_id" != "$disabled_id" ]]
[[ "$graphics_status" == *"Other session:   $disabled_id active"* ]]
printf 'ok - real graphics and changed policy identity\n'

if [[ -e /dev/kfd ]]; then
    config compute
    current run true
    current doctor gpu --session
    cp "$ROOT/tests/hip_vector_add.cpp" "$CURRENT/hip_vector_add.cpp"
    # Tool lookup and all compilation/execution happen inside NativeLab.
    current run /usr/bin/python3 -I -c '
import json, os, pathlib, subprocess
p=json.loads(pathlib.Path("/run/native-lab-bootstrap/resolved-policy.json").read_text())
roots=[m["destination"] for m in p["gpu"]["readonly_mounts"]]+["/usr"]
hipcc=next((str(pathlib.Path(r,"bin/hipcc")) for r in roots if os.access(pathlib.Path(r,"bin/hipcc"),os.X_OK)),None)
if not hipcc:
    print("SKIP: HIP SDK compiler absent; kernel test NOT PASSED")
    raise SystemExit(77)
subprocess.run([hipcc,"hip_vector_add.cpp","-o","hip-vector-add"],check=True)
subprocess.run(["./hip-vector-add"],check=True)
'
    printf 'ok - HIP allocation, kernel dispatch and validated results\n'
else
    printf 'SKIP: KFD absent; compute NOT PASSED\n'
    (( REQUIRE )) && exit 1
fi

# Revocation must work even when no current policy can be compiled.
printf 'invalid = [\n' >"$CONFIG"
current stop --all
config none
[[ "$(other status | awk '/^Session:/ {print $2}')" == "$other_id" ]]
current run /usr/bin/python3 -I -c 'import pathlib; assert not pathlib.Path("/sys").exists(); assert not pathlib.Path("/dev/kfd").exists(); assert not pathlib.Path("/dev/dri").exists()'
printf 'ok - invalid-config revocation preserves unrelated workspace; GPU disabled again\n'
printf 'Core GPU hardware checks passed; optional Xvfb results are reported separately above.\n'
