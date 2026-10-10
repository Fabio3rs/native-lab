#!/usr/bin/python3
"""GPU diagnostics executed only in an existing NativeLab session over SSH."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess


def main() -> int:
    root = Path("/run/native-lab-bootstrap")
    spec = importlib.util.spec_from_file_location("gpu", root / "native_lab_gpu.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resources = json.loads((root / "resolved-policy.json").read_text())["gpu"]
    print(f"SSH process: uid={os.getuid()} gid={os.getgid()} groups={os.getgroups()}", flush=True)
    for name in ("uid_map", "gid_map"):
        print(f"{name}: {(Path('/proc/self') / name).read_text().strip()}", flush=True)
    try:
        module.revalidate(resources)
    except (module.GPUError, OSError) as error:
        print(f"GPU device/mount validation FAILED: {error}")
        return 1
    if resources["provider"] == "none":
        if Path("/dev/kfd").exists() or list(Path("/dev/dri").glob("renderD*")):
            print("FAILED: unexpected GPU devices in disabled session")
            return 1
        print("GPU disabled: no render nodes or KFD")
        return 0
    print("Device identity and O_RDWR after SSH: OK", flush=True)
    directories = ["/usr/bin", "/usr/sbin"]
    directories += [m["destination"] + "/bin" for m in resources["readonly_mounts"] if m["destination"] != "/sys"]
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": os.environ["HOME"],
                   "XDG_RUNTIME_DIR": os.environ["XDG_RUNTIME_DIR"], "LANG": "C.UTF-8",
                   "EGL_PLATFORM": "surfaceless", "MESA_SHADER_CACHE_DISABLE": "true"}
    result = 0
    for name, arguments in (("vulkaninfo", ["--summary"]), ("eglinfo", ["-B", "-p", "surfaceless"]), ("rocminfo", [])):
        if (name == "rocminfo" and "compute" not in resources["features"]) or (name != "rocminfo" and "graphics" not in resources["features"]):
            continue
        binary = next((str(Path(d, name)) for d in directories if os.access(Path(d, name), os.X_OK)), None)
        if binary is None:
            print(f"{name}: UNTESTED (tool absent from mounted runtime)", flush=True)
            result = max(result, 2)
            continue
        try:
            completed = subprocess.run([binary, *arguments], env=environment, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"{name}: FAILED: {error}", flush=True)
            result = 1
            continue
        output = completed.stdout + completed.stderr
        print(f"--- {name} (exit {completed.returncode}) ---\n{output}", flush=True)
        if name == "vulkaninfo":
            hardware = any("vendorID" in block and "0x1002" in block and
                           re.search(r"deviceType\s*=\s*PHYSICAL_DEVICE_TYPE_(DISCRETE|INTEGRATED)_GPU", block)
                           for block in re.split(r"GPU\d+:\s*", output))
        elif name == "eglinfo":
            hardware = any("renderer:" in line and ("radeon" in line.lower() or "amd" in line.lower())
                           and not any(s in line.lower() for s in ("llvmpipe", "softpipe", "lavapipe")) for line in output.splitlines())
        else:
            hardware = bool(re.search(r"Device Type:\s+GPU", output) and "KERNEL_DISPATCH" in output)
        if completed.returncode or not hardware:
            print(f"{name}: FAILED (runtime error or no AMD hardware; software rendering is not success)", flush=True)
            result = 1
        else:
            print(f"{name}: AMD hardware identified", flush=True)
    if "compute" in resources["features"]:
        print("HIP kernel execution: not tested by doctor; run the hardware smoke test")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
