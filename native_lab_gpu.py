"""Trusted GPU resource resolution. No workspace code or discovery commands run here."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any


class GPUError(ValueError):
    pass


def fail(message: str) -> None:
    raise GPUError(message)


def within(path: str, root: str) -> bool:
    return os.path.commonpath((path, root)) == root


def overlaps(a: str, b: str) -> bool:
    return within(a, b) or within(b, a)


def string(value: Any, label: str) -> str:
    if not isinstance(value, str) or any(c in value for c in "\0\r\n"):
        fail(f"{label} must be a string without NUL/newlines")
    return value


def strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        fail(f"{label} must be an array of strings")
    return [string(v, label) for v in value]


def table(value: Any, allowed: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        fail(f"{label} must be a table")
    unknown = set(value) - allowed
    if unknown:
        fail(f"unknown {label} keys: {', '.join(sorted(unknown))}")
    return value


PCI = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]\Z")
RENDER = re.compile(r"renderD[0-9]+\Z")


def parse_config(raw: Any, version: int) -> dict[str, dict]:
    if version == 1 and raw is not None:
        fail("[gpu] requires NativeLab config version = 2")
    gpu = table(raw if raw is not None else {}, {"default", "workspaces"}, "[gpu]")
    if gpu.get("default", "none") != "none":
        fail('gpu.default must be "none"; GPU authorization must be per workspace')
    workspaces = gpu.get("workspaces", {})
    if not isinstance(workspaces, dict):
        fail("gpu.workspaces must be a table")
    result = {}
    for path, raw_request in workspaces.items():
        string(path, "GPU workspace")
        if not os.path.isabs(path):
            fail("GPU workspace must be an absolute path")
        canonical = os.path.realpath(path)
        if canonical in result:
            fail(f"conflicting GPU workspace aliases: {canonical}")
        request = table(raw_request, {"provider", "features", "devices", "readonly_roots"}, "GPU workspace")
        provider = string(request.get("provider"), "GPU provider")
        if provider not in {"amd", "nvidia"}:
            fail(f"unknown GPU provider: {provider}")
        features = strings(request.get("features"), "GPU features")
        if set(features) - {"graphics", "compute"}:
            fail("GPU features must contain only graphics and/or compute")
        devices = request.get("devices", "auto")
        if isinstance(devices, str):
            if devices not in {"auto", "all"}:
                fail("GPU devices must be auto, all, or a nonempty list of PCI addresses; device paths are forbidden")
        else:
            devices = sorted(set(v.lower() for v in strings(devices, "GPU devices")))
            if not devices or any(not PCI.fullmatch(v) for v in devices):
                fail("GPU devices must be a nonempty list of full PCI addresses (dddd:bb:ss.f)")
        roots = sorted(set(strings(request.get("readonly_roots", []), "GPU readonly_roots")))
        for root in roots:
            if not (os.path.isabs(root) or root.startswith("~/")):
                fail("GPU readonly_roots must be absolute or start with ~/")
        result[canonical] = dict(provider=provider, features=sorted(set(features)), devices=devices, readonly_roots=roots)
    return result


def disabled() -> dict:
    return dict(provider="none", features=[], selection=None, devices=[], nodes=[], readonly_mounts=[], symlinks=[], environment={})


class Host:
    """Inject this interface in tests; production always reads the real host."""

    def read(self, path: str) -> str:
        return Path(path).read_text().strip()

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def renders(self) -> list[str]:
        return sorted(str(p) for p in Path("/sys/class/drm").glob("renderD*"))

    def pci_gpus(self) -> list[str]:
        result = []
        for path in Path("/sys/bus/pci/devices").glob("*"):
            if self.read(str(path / "vendor")) == "0x1002" and int(self.read(str(path / "class")), 16) >> 16 in {3, 0x12}:
                result.append(f"{path.name} driver={os.path.basename(self.realpath(str(path / 'driver')))}")
        return sorted(result)

    def node(self, path: str, check_open: bool = True) -> dict:
        # Only provider-generated device paths reach this method.
        if path != "/dev/kfd" and not (path.startswith("/dev/dri/") and RENDER.fullmatch(path[9:])):
            fail(f"GPU device path is not permitted: {path}")
        for parent in ("/dev", "/dev/dri") if path.startswith("/dev/dri/") else ("/dev",):
            if stat.S_ISLNK(os.lstat(parent).st_mode):
                fail(f"GPU device parent is a symlink: {parent}")
        s = os.lstat(path)
        if not stat.S_ISCHR(s.st_mode):
            fail(f"GPU device must be a non-symlink character device: {path}")
        expected = (s.st_dev, s.st_ino, s.st_rdev)
        if check_open:
            try:
                fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            except PermissionError as error:
                fail(f"GPU permission insufficient opening {path}: {error}")
            try:
                opened = os.fstat(fd)
                current = os.lstat(path)
                if any((x.st_dev, x.st_ino, x.st_rdev) != expected for x in (opened, current)):
                    fail(f"GPU device changed while opening: {path}")
            finally:
                os.close(fd)
        return dict(path=path, dev=s.st_dev, ino=s.st_ino, major=os.major(s.st_rdev), minor=os.minor(s.st_rdev))


class AMDProvider:
    name = "amd"

    def discover(self, host: Host) -> list[dict]:
        devices: dict[str, dict] = {}
        for render in host.renders():
            name = os.path.basename(render)
            if not RENDER.fullmatch(name):
                continue
            physical = host.realpath(render + "/device")
            vendor = host.read(physical + "/vendor").lower()
            if vendor != "0x1002":
                continue
            pci = os.path.basename(physical).lower()
            if not PCI.fullmatch(pci):
                fail(f"AMD GPU has no supported PCI identity: {physical}")
            driver_path = host.realpath(physical + "/driver")
            driver = os.path.basename(driver_path)
            entry = devices.setdefault(pci, dict(pci=pci, vendor=vendor,
                device=host.read(physical + "/device").lower(), driver=driver,
                sysfs=physical, render_nodes=[]))
            entry["render_nodes"].append("/dev/dri/" + name)
        return sorted(devices.values(), key=lambda d: d["pci"])

    def validate_node(self, host: Host, path: str, device: dict | None = None, check_open: bool = True) -> dict:
        node = host.node(path, check_open)
        syschar = f"/sys/dev/char/{node['major']}:{node['minor']}"
        if path == "/dev/kfd":
            if host.realpath(syschar) != "/sys/devices/virtual/kfd/kfd":
                fail("/dev/kfd identity does not match kernel KFD sysfs")
        else:
            render = "/sys/class/drm/" + os.path.basename(path)
            if host.read(render + "/dev") != f"{node['major']}:{node['minor']}":
                fail(f"GPU major/minor does not match sysfs: {path}")
            if host.realpath(syschar) != host.realpath(render):
                fail(f"GPU device does not match DRM sysfs: {path}")
            if device is None or host.realpath(render + "/device") != device["sysfs"]:
                fail(f"GPU render node belongs to another PCI device: {path}")
        return node

    def resolve(self, request: dict, host: Host) -> tuple[list[dict], list[dict]]:
        discovered = self.discover(host)
        selection = request["devices"]
        if selection == "auto":
            if len(discovered) != 1:
                detail = ""
                if not discovered:
                    physical = host.pci_gpus()
                    detail = (f"; physical AMD GPU found but driver/render node unavailable: {', '.join(physical)}"
                              if physical else "; physical AMD GPU not found")
                fail(f"GPU auto requires exactly one AMD GPU; found {len(discovered)}{detail}")
            selected = discovered
        elif selection == "all":
            selected = discovered
        else:
            selected = [d for d in discovered if d["pci"] in selection]
            missing = set(selection) - {d["pci"] for d in selected}
            if missing:
                fail(f"AMD GPU PCI not found: {', '.join(sorted(missing))}")
        if not selected:
            fail("AMD GPU physical device/render node not found")
        nodes = []
        for device in selected:
            allowed = {"amdgpu"} if "compute" in request["features"] else {"amdgpu", "radeon"}
            if device["driver"] not in allowed:
                fail(f"AMD GPU driver unavailable/incompatible: {device['pci']} ({device['driver']})")
            nodes.extend(self.validate_node(host, path, device) for path in device["render_nodes"])
        if "compute" in request["features"]:
            nodes.append(self.validate_node(host, "/dev/kfd"))
        return selected, sorted(nodes, key=lambda n: n["path"])


def provider(name: str) -> AMDProvider:
    if name == "nvidia":
        fail("GPU provider nvidia is not yet implemented")
    if name != "amd":
        fail(f"unknown GPU provider: {name}")
    return AMDProvider()


def resolve(request: dict | None, workspace: str, home: str, covered: list[str], host: Host | None = None) -> dict:
    if request is None:
        return disabled()
    backend = provider(request["provider"])
    if not request["features"]:
        return disabled()
    host = host or Host()
    devices, nodes = backend.resolve(request, host)
    mounts = [{"source": "/sys", "destination": "/sys"}]
    symlinks = []
    candidates = []
    for raw in request["readonly_roots"]:
        logical = os.path.normpath(home + raw[1:] if raw.startswith("~/") else raw)
        source = os.path.realpath(logical)
        for p in (logical, source):
            if p in {"/", "/opt"} or within(os.path.realpath(home), p) or overlaps(p, workspace):
                fail(f"unsafe GPU readonly root: {logical}")
            if any(overlaps(p, root) for root in ("/dev", "/proc", "/run", "/tmp", "/sys")):
                fail(f"GPU readonly root conflicts with private/provider mounts: {logical}")
        if not os.path.isdir(source):
            fail(f"GPU readonly root is not an existing directory: {logical}")
        candidates.append(source)
        if logical != source and not any(within(logical, root) for root in covered):
            symlinks.append(dict(path=logical, target=source))
    for source in sorted(set(candidates), key=lambda p: (p.count("/"), p)):
        if not any(within(source, root) for root in covered + [m["destination"] for m in mounts]):
            mounts.append(dict(source=source, destination=source))
    # Links already inside mounted trees retain their original host contents.
    symlinks = [s for s in symlinks if not any(within(s["path"], m["destination"]) for m in mounts)]
    return dict(provider=backend.name, features=request["features"], selection=request["devices"],
                devices=devices, nodes=nodes, readonly_mounts=mounts,
                symlinks=sorted(symlinks, key=lambda s: s["path"]), environment={})


def validate_schema(resources: Any) -> None:
    fields = set(disabled())
    if not isinstance(resources, dict) or set(resources) != fields:
        fail("invalid GPU resource manifest schema")
    if resources["provider"] == "none":
        if resources != disabled():
            fail("disabled GPU manifest contains resources")
        return
    provider(resources["provider"])
    features = resources["features"]
    if not isinstance(features, list) or not features or any(f not in {"graphics", "compute"} for f in features):
        fail("invalid GPU manifest features")
    if resources["environment"] != {}:
        fail("GPU environment is not supported in this manifest version")
    if not isinstance(resources["devices"], list) or not resources["devices"]:
        fail("GPU manifest has no physical devices")
    expected = set()
    for d in resources["devices"]:
        if not isinstance(d, dict) or set(d) != {"pci", "vendor", "device", "driver", "sysfs", "render_nodes"}:
            fail("invalid GPU physical device schema")
        if not isinstance(d["pci"], str) or not PCI.fullmatch(d["pci"]) or d["vendor"] != "0x1002":
            fail("invalid AMD GPU identity")
        if not isinstance(d["sysfs"], str) or not d["sysfs"].startswith("/sys/devices/"):
            fail("invalid GPU sysfs path")
        if not isinstance(d["render_nodes"], list) or not d["render_nodes"]:
            fail("GPU manifest has no render nodes")
        for p in d["render_nodes"]:
            if not isinstance(p, str) or not p.startswith("/dev/dri/") or not RENDER.fullmatch(p[9:]):
                fail("arbitrary device path in GPU manifest")
            if p in expected:
                fail("duplicate GPU render node")
            expected.add(p)
    if "compute" in features:
        expected.add("/dev/kfd")
    if not isinstance(resources["nodes"], list):
        fail("invalid GPU nodes schema")
    actual = set()
    for node in resources["nodes"]:
        if not isinstance(node, dict) or set(node) != {"path", "dev", "ino", "major", "minor"}:
            fail("invalid GPU node schema")
        if not isinstance(node["path"], str) or node["path"] not in expected or node["path"] in actual:
            fail("unexpected or duplicate GPU node")
        if any(type(node[k]) is not int or node[k] < 0 for k in ("dev", "ino", "major", "minor")):
            fail("invalid GPU node identity")
        actual.add(node["path"])
    if actual != expected:
        fail("GPU manifest node set does not match authorization")
    mounts = resources["readonly_mounts"]
    if not isinstance(mounts, list) or {"source": "/sys", "destination": "/sys"} not in mounts:
        fail("GPU manifest must include read-only sysfs")
    for m in mounts:
        if not isinstance(m, dict) or set(m) != {"source", "destination"}:
            fail("invalid GPU mount schema")
        for p in m.values():
            string(p, "GPU mount")
            if not os.path.isabs(p) or os.path.normpath(p) != p or p in {"/", "/opt"}:
                fail("unsafe GPU mount path")
            if any(overlaps(p, root) for root in ("/dev", "/proc", "/run", "/tmp")):
                fail("unsafe GPU mount path")
        if m["source"] != m["destination"]:
            fail("GPU mounts must use canonical destinations")
    if not isinstance(resources["symlinks"], list):
        fail("invalid GPU symlinks")
    for link in resources["symlinks"]:
        if not isinstance(link, dict) or set(link) != {"path", "target"}:
            fail("invalid GPU symlink schema")
        for p in link.values():
            string(p, "GPU symlink")
            if not os.path.isabs(p) or os.path.normpath(p) != p or any(overlaps(p, r) for r in ("/dev", "/proc", "/run", "/tmp", "/sys")):
                fail("unsafe GPU symlink path")


def revalidate(resources: dict, host: Host | None = None, check_open: bool = True) -> None:
    validate_schema(resources)
    if resources["provider"] == "none":
        return
    host = host or Host()
    backend = provider(resources["provider"])
    discovered = {d["pci"]: d for d in backend.discover(host)}
    by_path = {}
    for device in resources["devices"]:
        if discovered.get(device["pci"]) != device:
            fail(f"GPU physical identity changed: {device['pci']}")
        by_path.update({path: device for path in device["render_nodes"]})
    for expected in resources["nodes"]:
        actual = backend.validate_node(host, expected["path"], by_path.get(expected["path"]), check_open)
        if actual != expected:
            fail(f"GPU device identity changed: {expected['path']}")


def summary(resources: dict) -> list[str]:
    lines = [f"GPU:             {'disabled' if resources['provider'] == 'none' else 'enabled'}",
             f"GPU provider:    {resources['provider']}", f"GPU features:    {', '.join(resources['features']) or '-'}"]
    lines += [f"GPU PCI:         {d['pci']} ({d['driver']})" for d in resources["devices"]]
    lines += [f"GPU node:        {n['path']} ({n['major']}:{n['minor']})" for n in resources["nodes"]]
    lines += [f"GPU RO mount:    {m['destination']}" for m in resources["readonly_mounts"]]
    lines += [f"GPU symlink:     {s['path']} -> {s['target']}" for s in resources["symlinks"]]
    lines.append(f"GPU KFD:         {'yes' if any(n['path'] == '/dev/kfd' for n in resources['nodes']) else 'no'}")
    return lines
