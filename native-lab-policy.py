#!/usr/bin/python3
"""Compile and validate NativeLab filesystem policy manifests."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any


POLICY_VERSION = 2
MANIFEST_VERSION = 1
MAX_DENY_MATCHES = 8192
SYSTEM_READ_ONLY = (
    "/bin",
    "/sbin",
    "/usr",
    "/etc",
    "/lib",
    "/lib64",
    "/nix/store",
    "/run/current-system/sw",
)
PROTECTED_METADATA = (".git", ".agents", ".codex")
DEFAULT_DENY_GLOBS = (
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
)
PRIVATE_MOUNT_ROOTS = ("/tmp", "/run", "/proc", "/dev")


class PolicyError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise PolicyError(message)


def warn(message: str) -> None:
    print(f"native-lab-policy: warning: {message}", file=sys.stderr)


def reject_control_characters(value: str, label: str) -> None:
    if "\x00" in value or "\n" in value or "\r" in value:
        fail(f"{label} contains unsupported NUL or newline characters")


def is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def overlaps(left: str, right: str) -> bool:
    return is_within(left, right) or is_within(right, left)


def expand_tilde(value: str, home: str, label: str) -> str:
    reject_control_characters(value, label)
    if value == "~":
        return home
    if value.startswith("~/"):
        return os.path.join(home, value[2:])
    if value.startswith("~"):
        fail(f"{label} uses unsupported user expansion: {value}")
    if not os.path.isabs(value):
        fail(f"{label} must be absolute or start with ~/: {value}")
    return os.path.normpath(value)


def real_home() -> tuple[str, str]:
    logical = pwd.getpwuid(os.getuid()).pw_dir
    if not logical or not os.path.isabs(logical):
        fail("cannot determine an absolute home directory from passwd")
    logical = os.path.normpath(logical)
    reject_control_characters(logical, "passwd home")
    canonical = os.path.realpath(logical)
    if logical == "/" or canonical == "/":
        fail("refusing to use / as the sandbox home")
    return logical, canonical


def checked_config_path(path: str, workspace: str, label: str) -> Path | None:
    reject_control_characters(path, label)
    if not os.path.isabs(path):
        fail(f"{label} must be absolute: {path}")
    normalized = os.path.normpath(path)
    if is_within(normalized, workspace):
        fail(f"{label} must not be inside the current workspace: {normalized}")
    try:
        metadata = os.lstat(normalized)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        fail(f"{label} must not be a symlink: {normalized}")
    if not stat.S_ISREG(metadata.st_mode):
        fail(f"{label} is not a regular file: {normalized}")
    if metadata.st_uid != os.getuid():
        fail(f"{label} is not owned by the current user: {normalized}")
    if metadata.st_mode & 0o022:
        fail(f"{label} is writable by group or others: {normalized}")
    canonical = os.path.realpath(normalized)
    if is_within(canonical, workspace):
        fail(f"{label} resolves inside the current workspace: {canonical}")
    return Path(canonical)


def load_toml(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        fail(f"cannot parse {label} {path}: {error}")
    if not isinstance(value, dict):
        fail(f"{label} root must be a TOML table")
    return value


def expect_table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{label} must be a TOML table")
    return value


def expect_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        fail(f"{label} must be a boolean")
    return value


def expect_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        fail(f"{label} must be a string")
    reject_control_characters(value, label)
    return value


def expect_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        fail(f"{label} must be an array of strings")
    result = []
    for item in value:
        reject_control_characters(item, label)
        result.append(item)
    return result


def parse_native_config(
    path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    defaults: dict[str, Any] = {
        "codex": {
            "import_trusted_projects": True,
            "config_path": "~/.codex/config.toml",
        },
        "filesystem": {
            "extra_read_only": [],
            "extra_trusted_project_deny_globs": [],
        },
    }
    if path is None:
        return defaults, {"version": 1}

    raw = load_toml(path, "NativeLab config")
    allowed_root = {"version", "codex", "filesystem"}
    unknown = sorted(set(raw) - allowed_root)
    if unknown:
        fail(f"unknown NativeLab config keys: {', '.join(unknown)}")
    version = raw.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        fail("NativeLab config version must be integer 1")

    codex = expect_table(raw.get("codex", {}), "[codex]")
    unknown = sorted(set(codex) - {"import_trusted_projects", "config_path"})
    if unknown:
        fail(f"unknown [codex] keys: {', '.join(unknown)}")
    filesystem = expect_table(raw.get("filesystem", {}), "[filesystem]")
    unknown = sorted(
        set(filesystem) - {"extra_read_only", "extra_trusted_project_deny_globs"}
    )
    if unknown:
        fail(f"unknown [filesystem] keys: {', '.join(unknown)}")

    resolved = {
        "codex": {
            "import_trusted_projects": expect_bool(
                codex.get("import_trusted_projects", True),
                "codex.import_trusted_projects",
            ),
            "config_path": expect_string(
                codex.get("config_path", "~/.codex/config.toml"),
                "codex.config_path",
            ),
        },
        "filesystem": {
            "extra_read_only": expect_string_list(
                filesystem.get("extra_read_only", []),
                "filesystem.extra_read_only",
            ),
            "extra_trusted_project_deny_globs": expect_string_list(
                filesystem.get("extra_trusted_project_deny_globs", []),
                "filesystem.extra_trusted_project_deny_globs",
            ),
        },
    }
    return resolved, {"version": version}


def canonical_existing(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    return os.path.realpath(path)


def forbidden_home_root(path: str, home: str) -> bool:
    return path == "/" or is_within(home, path)


def conflicts_private_mount(path: str) -> bool:
    return any(overlaps(path, private) for private in PRIVATE_MOUNT_ROOTS)


def validate_deny_glob(pattern: str) -> str:
    reject_control_characters(pattern, "trusted-project deny glob")
    if not pattern:
        fail("trusted-project deny glob must not be empty")
    if pattern.startswith("!"):
        fail(f"trusted-project deny glob must not be a negation: {pattern}")
    if pattern.startswith("/"):
        fail(f"trusted-project deny glob must be relative: {pattern}")
    if ".." in pattern.split("/"):
        fail(f"trusted-project deny glob must not contain ..: {pattern}")
    return pattern


def identity(path: str, metadata: os.stat_result) -> dict[str, Any]:
    return {
        "path": path,
        "dev": metadata.st_dev,
        "ino": metadata.st_ino,
    }


def mask_for_existing(path: str, origin: str) -> dict[str, Any]:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        fail(f"cannot mask symlink fail-closed: {path}")
    if stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
    elif stat.S_ISREG(metadata.st_mode):
        kind = "file"
    else:
        fail(f"cannot mask unsupported filesystem object: {path}")
    result = identity(path, metadata)
    result.update({"kind": kind, "origin": origin})
    return result


def synthetic_marker_dir(runtime_root: str, path: str) -> Path:
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()
    return Path(runtime_root, "synthetic-mounts", digest)


def process_identity_matches(pid: int, start_ticks: str) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    closing = raw.rfind(") ")
    if closing < 0:
        return False
    fields = raw[closing + 2 :].split()
    return len(fields) > 19 and fields[19] == start_ticks


def read_marker(path: Path, expected_target: str) -> tuple[bool, dict[str, Any] | None]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError):
        return False, None
    if not isinstance(marker, dict) or marker.get("path") != expected_target:
        return False, marker if isinstance(marker, dict) else None
    pid = marker.get("pid")
    start_ticks = marker.get("start_ticks")
    if not isinstance(pid, int) or not isinstance(start_ticks, str):
        return False, marker
    return process_identity_matches(pid, start_ticks), marker


def validate_marker_target(marker: dict[str, Any], metadata: os.stat_result, path: Path) -> None:
    if marker.get("target_dev") != metadata.st_dev or marker.get("target_ino") != metadata.st_ino:
        fail(f"synthetic mount target identity changed for marker {path}")


def directory_is_empty(path: str) -> bool:
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is None
    except OSError:
        return False


def inspect_synthetic_registry(runtime_root: str, target: str) -> bool:
    marker_dir = synthetic_marker_dir(runtime_root, target)
    try:
        entries = list(marker_dir.iterdir())
    except FileNotFoundError:
        return False
    except OSError as error:
        fail(f"cannot inspect synthetic mount registry for {target}: {error}")
    active_markers: list[tuple[Path, dict[str, Any]]] = []
    saw_marker = False
    for marker_path in entries:
        if not marker_path.is_file() or marker_path.is_symlink():
            fail(f"unsafe synthetic mount registry entry: {marker_path}")
        saw_marker = True
        marker_active, marker = read_marker(marker_path, target)
        if marker is None or marker.get("path") != target:
            fail(f"invalid synthetic mount marker: {marker_path}")
        if marker_active:
            active_markers.append((marker_path, marker))
        else:
            marker_path.unlink()
    if active_markers:
        if os.path.lexists(target):
            metadata = os.lstat(target)
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                fail(f"active synthetic mount target changed type: {target}")
            if not directory_is_empty(target):
                fail(f"active synthetic mount target gained content: {target}")
            for marker_path, marker in active_markers:
                validate_marker_target(marker, metadata, marker_path)
        else:
            fail(f"active synthetic mount target disappeared: {target}")
        return True
    if saw_marker and os.path.lexists(target):
        metadata = os.lstat(target)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            fail(f"stale synthetic mount target changed type: {target}")
        if not directory_is_empty(target):
            fail(f"stale synthetic mount target gained content: {target}")
        os.rmdir(target)
    try:
        marker_dir.rmdir()
    except FileNotFoundError:
        pass
    except OSError as error:
        fail(f"cannot remove stale synthetic marker directory {marker_dir}: {error}")
    return False


def collect_trusted_projects(
    codex: dict[str, Any],
    home: str,
    home_canonical: str,
    workspace: str,
) -> list[str]:
    projects = codex.get("projects", {})
    if not isinstance(projects, dict):
        return []
    result: set[str] = set()
    for raw_path, settings in projects.items():
        if not isinstance(raw_path, str) or not isinstance(settings, dict):
            continue
        if settings.get("trust_level") != "trusted":
            continue
        try:
            logical = expand_tilde(raw_path, home, "Codex trusted project")
        except PolicyError as error:
            warn(str(error))
            continue
        if not os.path.isdir(logical):
            continue
        canonical = os.path.realpath(logical)
        if forbidden_home_root(canonical, home_canonical):
            warn(f"ignoring unsafe Codex trusted project root: {logical}")
            continue
        if conflicts_private_mount(canonical):
            warn(f"ignoring trusted project conflicting with private mounts: {canonical}")
            continue
        if is_within(canonical, workspace):
            continue
        result.add(canonical)
    return sorted(result)


def collect_read_only_roots(
    home: str,
    home_canonical: str,
    workspace: str,
    extra: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    configured: list[tuple[str, str]] = [
        (os.path.join(home, ".npm-global"), "default-user"),
        (os.path.join(home, ".npm"), "default-user"),
    ]
    configured.extend((expand_tilde(value, home, "extra_read_only"), "extra") for value in extra)

    resolved: list[dict[str, Any]] = []
    configured_state: list[dict[str, Any]] = []
    seen_destinations: set[str] = set()
    for destination, origin in configured:
        destination = os.path.normpath(destination)
        if destination in seen_destinations:
            continue
        seen_destinations.add(destination)
        source = canonical_existing(destination)
        comparison = source or destination
        if source is not None and (
            forbidden_home_root(source, home_canonical) or conflicts_private_mount(source)
        ):
            fail(f"read-only user path resolves to an unsafe root: {destination} -> {source}")
        if origin == "extra":
            if forbidden_home_root(comparison, home_canonical):
                fail(f"extra_read_only must not expose /, HOME, or an ancestor of HOME: {destination}")
            if conflicts_private_mount(comparison):
                fail(f"extra_read_only conflicts with a private sandbox mount: {destination}")
            if overlaps(comparison, workspace) or overlaps(destination, workspace):
                fail(f"extra_read_only overlaps the current workspace: {destination}")
        entry = {
            "destination": destination,
            "origin": origin,
            "present": source is not None,
            "source": source,
        }
        configured_state.append(entry)
        if source is not None:
            resolved.append(entry)
    resolved.sort(key=lambda item: (item["destination"], item["source"]))
    configured_state.sort(key=lambda item: item["destination"])
    return resolved, configured_state


def run_rg(rg: str, root: str, globs: list[str]) -> list[str]:
    command = [rg, "--files", "--hidden", "--no-ignore", "--null"]
    for pattern in globs:
        command.extend(("--glob", pattern))
    command.extend(("--", root))
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as error:
        fail(f"cannot run ripgrep for trusted project {root}: {error}")
    if completed.returncode == 1 and not completed.stderr:
        return []
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        fail(f"ripgrep deny scan failed for {root}: {detail or f'exit {completed.returncode}'}")
    paths: list[str] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        path = os.fsdecode(raw)
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        path = os.path.normpath(path)
        if not is_within(path, root):
            fail(f"ripgrep returned a path outside trusted project {root}: {path}")
        paths.append(path)
    return paths


def collapse_masks(masks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {mask["path"]: mask for mask in masks}
    result: list[dict[str, Any]] = []
    for path in sorted(unique, key=lambda value: (value.count(os.sep), value)):
        if any(mask["kind"] in {"directory", "missing-directory"} and is_within(path, mask["path"]) for mask in result):
            continue
        result.append(unique[path])
    return result


def compile_policy(workspace: str, rg: str, runtime_root: str) -> dict[str, Any]:
    workspace = os.path.realpath(workspace)
    if not os.path.isdir(workspace):
        fail(f"workspace is not a directory: {workspace}")
    reject_control_characters(workspace, "workspace")
    home, home_canonical = real_home()

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        if not os.path.isabs(xdg_config_home):
            fail("XDG_CONFIG_HOME must be absolute when set")
        config_root = os.path.normpath(xdg_config_home)
        if overlaps(config_root, workspace) or overlaps(
            os.path.realpath(config_root), workspace
        ):
            fail("XDG_CONFIG_HOME must not overlap the current workspace")
    else:
        config_root = os.path.join(home, ".config")
    native_config_name = os.path.join(config_root, "native-lab", "config.toml")
    native_config_path = checked_config_path(native_config_name, workspace, "NativeLab config")
    native, native_meta = parse_native_config(native_config_path)

    codex_path_name = expand_tilde(native["codex"]["config_path"], home, "codex.config_path")
    codex_config_path = checked_config_path(codex_path_name, workspace, "Codex config")
    codex_config: dict[str, Any] = {}
    if native["codex"]["import_trusted_projects"] and codex_config_path is not None:
        codex_config = load_toml(codex_config_path, "Codex config")

    trusted_projects = collect_trusted_projects(
        codex_config, home, home_canonical, workspace
    )
    read_only, configured_read_only = collect_read_only_roots(
        home,
        home_canonical,
        workspace,
        native["filesystem"]["extra_read_only"],
    )
    deny_globs = sorted(
        {
            validate_deny_glob(pattern)
            for pattern in (
                *DEFAULT_DENY_GLOBS,
                *native["filesystem"]["extra_trusted_project_deny_globs"],
            )
        }
    )

    masks: list[dict[str, Any]] = []
    for name in PROTECTED_METADATA:
        path = os.path.join(workspace, name)
        if inspect_synthetic_registry(runtime_root, path):
            masks.append(
                {
                    "path": path,
                    "kind": "missing-directory",
                    "origin": "workspace-metadata",
                    "dev": None,
                    "ino": None,
                }
            )
            continue
        try:
            os.lstat(path)
        except FileNotFoundError:
            masks.append(
                {
                    "path": path,
                    "kind": "missing-directory",
                    "origin": "workspace-metadata",
                    "dev": None,
                    "ino": None,
                }
            )
        else:
            masks.append(mask_for_existing(path, "workspace-metadata"))

    match_count = 0
    for project in trusted_projects:
        for name in PROTECTED_METADATA:
            path = os.path.join(project, name)
            try:
                os.lstat(path)
            except FileNotFoundError:
                continue
            masks.append(mask_for_existing(path, "trusted-metadata"))
        for path in run_rg(rg, project, deny_globs):
            if is_within(path, workspace):
                continue
            match_count += 1
            if match_count > MAX_DENY_MATCHES:
                fail(f"trusted-project deny globs matched more than {MAX_DENY_MATCHES} paths")
            masks.append(mask_for_existing(path, "trusted-deny"))

    masks = collapse_masks(masks)
    system_read_only = [
        {"source": path, "destination": path}
        for path in SYSTEM_READ_ONLY
        if os.path.lexists(path)
    ]
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "policy_version": POLICY_VERSION,
        "profile": "default",
        "workspace": workspace,
        "home": {"logical": home, "canonical": home_canonical},
        "config": {
            "native_path": native_config_name,
            "native_present": native_config_path is not None,
            "native_version": native_meta["version"],
            "codex_path": codex_path_name,
            "codex_present": codex_config_path is not None,
            "import_trusted_projects": native["codex"]["import_trusted_projects"],
            "extra_read_only": sorted(native["filesystem"]["extra_read_only"]),
            "extra_trusted_project_deny_globs": sorted(
                native["filesystem"]["extra_trusted_project_deny_globs"]
            ),
        },
        "roots": {
            "read_write": [workspace],
            "system_read_only": system_read_only,
            "user_read_only": read_only,
            "configured_user_read_only": configured_read_only,
            "trusted_projects": trusted_projects,
        },
        "protected_metadata": sorted(PROTECTED_METADATA),
        "trusted_deny_globs": deny_globs,
        "masks": masks,
    }
    digest_input = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    manifest["policy_digest"] = hashlib.sha256(digest_input).hexdigest()
    return manifest


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as error:
        fail(f"cannot read policy manifest {path}: {error}")
    if not isinstance(manifest, dict):
        fail("policy manifest root is not an object")
    claimed = manifest.get("policy_digest")
    unsigned = dict(manifest)
    unsigned.pop("policy_digest", None)
    actual = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    if claimed != actual:
        fail("policy manifest digest mismatch")
    return manifest


def ensure_registry_root(runtime_root: str) -> Path:
    root = Path(runtime_root, "synthetic-mounts")
    try:
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
        metadata = root.lstat()
    except OSError as error:
        fail(f"cannot prepare synthetic mount registry {root}: {error}")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        fail(f"unsafe synthetic mount registry: {root}")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        fail(f"synthetic mount registry must be owned by the user with mode 0700: {root}")
    return root


def register_synthetics(
    manifest: dict[str, Any], runtime_root: str, session_id: str, pid: int, start_ticks: str
) -> None:
    if not session_id or "/" in session_id or session_id in {".", ".."}:
        fail("invalid session id for synthetic mount registry")
    registry_root = ensure_registry_root(runtime_root)
    for mask in manifest["masks"]:
        target = mask["path"]
        kind = mask["kind"]
        if kind != "missing-directory":
            metadata = os.lstat(target)
            if stat.S_ISLNK(metadata.st_mode):
                fail(f"protected path became a symlink before mount setup: {target}")
            if metadata.st_dev != mask["dev"] or metadata.st_ino != mask["ino"]:
                fail(f"protected path identity changed before mount setup: {target}")
            continue

        active = inspect_synthetic_registry(runtime_root, target)
        marker_dir = synthetic_marker_dir(runtime_root, target)
        marker_dir.mkdir(mode=0o700, exist_ok=True)
        marker_dir_metadata = marker_dir.lstat()
        if stat.S_ISLNK(marker_dir_metadata.st_mode) or not stat.S_ISDIR(
            marker_dir_metadata.st_mode
        ):
            fail(f"unsafe synthetic marker directory: {marker_dir}")
        if (
            marker_dir_metadata.st_uid != os.getuid()
            or stat.S_IMODE(marker_dir_metadata.st_mode) != 0o700
        ):
            fail(
                "synthetic marker directory must be owned by the user "
                f"with mode 0700: {marker_dir}"
            )
        marker_path = marker_dir / session_id
        marker = {
            "path": target,
            "pid": pid,
            "start_ticks": start_ticks,
            "session_id": session_id,
            "target_dev": None,
            "target_ino": None,
        }
        if not active:
            if os.path.lexists(target):
                fail(f"missing protected path appeared before mount setup: {target}")
            # A provisional marker makes a crash between mkdir and the final
            # marker recoverable on the next locked policy resolution.
            atomic_write(marker_path, canonical_manifest_bytes(marker))
            try:
                os.mkdir(target, mode=0o700)
            except OSError as error:
                fail(f"cannot create protected synthetic mountpoint {target}: {error}")
        metadata = os.lstat(target)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            fail(f"unsafe synthetic mount target: {target}")
        if not directory_is_empty(target):
            fail(f"synthetic mount target is not empty: {target}")

        target_metadata = os.lstat(target)
        marker["target_dev"] = target_metadata.st_dev
        marker["target_ino"] = target_metadata.st_ino
        atomic_write(marker_path, canonical_manifest_bytes(marker))

    try:
        registry_root.rmdir()
    except OSError:
        pass


def unregister_synthetics(
    manifest: dict[str, Any], runtime_root: str, session_id: str
) -> None:
    for mask in manifest["masks"]:
        if mask["kind"] != "missing-directory":
            continue
        target = mask["path"]
        marker_dir = synthetic_marker_dir(runtime_root, target)
        marker_path = marker_dir / session_id
        try:
            marker_path.unlink()
        except FileNotFoundError:
            pass
        try:
            remaining = list(marker_dir.iterdir())
        except FileNotFoundError:
            continue
        active_remaining = False
        for other in remaining:
            is_active, marker = read_marker(other, target)
            if marker is None or marker.get("path") != target:
                fail(f"invalid synthetic mount marker: {other}")
            if is_active:
                active_remaining = True
                if os.path.lexists(target):
                    validate_marker_target(marker, os.lstat(target), other)
            else:
                other.unlink()
        if active_remaining:
            continue
        if os.path.lexists(target):
            metadata = os.lstat(target)
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                fail(f"synthetic mount target changed type: {target}")
            if not directory_is_empty(target):
                fail(f"synthetic mount target gained content: {target}")
            os.rmdir(target)
        try:
            marker_dir.rmdir()
        except FileNotFoundError:
            pass
        except OSError as error:
            fail(f"cannot remove synthetic marker directory {marker_dir}: {error}")


def append_arg(buffer: bytearray, *values: object) -> None:
    for value in values:
        encoded = str(value).encode("utf-8")
        if b"\0" in encoded:
            fail("cannot encode a NUL byte in bwrap arguments")
        buffer.extend(encoded)
        buffer.append(0)


def emit_bwrap(manifest: dict[str, Any], mask_source: str) -> tuple[bytes, bytes]:
    args = bytearray()
    synthetic = bytearray()
    home = manifest["home"]["logical"]
    uid = os.getuid()
    append_arg(args, "--tmpfs", "/")
    append_arg(args, "--dev", "/dev")
    append_arg(args, "--perms", "1777", "--tmpfs", "/dev/shm")
    append_arg(args, "--perms", "1777", "--tmpfs", "/tmp")
    append_arg(args, "--perms", "755", "--tmpfs", "/run")
    append_arg(args, "--perms", "700", "--tmpfs", home)
    append_arg(args, "--dir", "/run/user", "--dir", f"/run/user/{uid}")
    append_arg(args, "--chmod", "0700", f"/run/user/{uid}")

    roots = manifest["roots"]
    for entry in roots["system_read_only"]:
        append_arg(args, "--ro-bind", entry["source"], entry["destination"])
    for entry in roots["user_read_only"]:
        append_arg(args, "--ro-bind", entry["source"], entry["destination"])
    for project in roots["trusted_projects"]:
        append_arg(args, "--ro-bind", project, project)
    workspace = manifest["workspace"]
    append_arg(args, "--bind", workspace, workspace)

    for mask in manifest["masks"]:
        path = mask["path"]
        if mask["kind"] in {"directory", "missing-directory"}:
            append_arg(args, "--perms", "000", "--tmpfs", path, "--remount-ro", path)
        elif mask["kind"] == "file":
            append_arg(args, "--ro-bind", mask_source, path)
        else:
            fail(f"unknown mask kind in manifest: {mask['kind']}")
        if mask["kind"] == "missing-directory":
            append_arg(synthetic, path)
    return bytes(args), bytes(synthetic)


def summary_lines(manifest: dict[str, Any]) -> str:
    roots = manifest["roots"]
    ro_roots = [
        *(entry["destination"] for entry in roots["system_read_only"]),
        *(entry["destination"] for entry in roots["user_read_only"]),
        *roots["trusted_projects"],
    ]
    lines = [
        f"policy_digest={manifest['policy_digest']}",
        f"home={manifest['home']['logical']}",
        f"read_write_root_count={len(roots['read_write'])}",
        f"read_only_root_count={len(ro_roots)}",
        f"trusted_project_count={len(roots['trusted_projects'])}",
    ]
    lines.extend(f"read_write_root={path}" for path in roots["read_write"])
    lines.extend(f"read_only_root={path}" for path in ro_roots)
    lines.extend(f"trusted_project={path}" for path in roots["trusted_projects"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--workspace", required=True)
    resolve.add_argument("--rg", required=True)
    resolve.add_argument("--runtime-root", required=True)
    resolve.add_argument("--output", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--policy", required=True)
    verify.add_argument("--workspace", required=True)
    verify.add_argument("--policy-version", required=True, type=int)
    verify.add_argument("--policy-digest", required=True)

    emit = subparsers.add_parser("emit-bwrap")
    emit.add_argument("--policy", required=True)
    emit.add_argument("--mask-source", required=True)
    emit.add_argument("--args-output", required=True)
    emit.add_argument("--synthetic-output", required=True)

    summary = subparsers.add_parser("summary")
    summary.add_argument("--policy", required=True)

    value = subparsers.add_parser("value")
    value.add_argument("--policy", required=True)
    value.add_argument("--key", required=True, choices=("home",))

    register = subparsers.add_parser("register-synthetics")
    register.add_argument("--policy", required=True)
    register.add_argument("--runtime-root", required=True)
    register.add_argument("--session-id", required=True)
    register.add_argument("--pid", required=True, type=int)
    register.add_argument("--start-ticks", required=True)

    unregister = subparsers.add_parser("unregister-synthetics")
    unregister.add_argument("--policy", required=True)
    unregister.add_argument("--runtime-root", required=True)
    unregister.add_argument("--session-id", required=True)

    args = parser.parse_args()
    if args.command == "resolve":
        lock_path = Path(args.runtime_root, "policy-mount.lock")
        with lock_path.open("a+b") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            manifest = compile_policy(args.workspace, args.rg, args.runtime_root)
            atomic_write(Path(args.output), canonical_manifest_bytes(manifest))
        print(manifest["policy_digest"])
        return 0

    manifest = load_manifest(Path(args.policy))
    if args.command == "verify":
        if manifest.get("workspace") != os.path.realpath(args.workspace):
            fail("policy manifest workspace mismatch")
        if manifest.get("policy_version") != args.policy_version:
            fail("policy manifest version mismatch")
        if manifest.get("policy_digest") != args.policy_digest:
            fail("policy manifest digest does not match daemon argument")
        return 0
    if args.command == "emit-bwrap":
        bwrap_args, synthetic = emit_bwrap(manifest, args.mask_source)
        atomic_write(Path(args.args_output), bwrap_args)
        atomic_write(Path(args.synthetic_output), synthetic)
        return 0
    if args.command == "summary":
        sys.stdout.write(summary_lines(manifest))
        return 0
    if args.command == "value":
        if args.key == "home":
            print(manifest["home"]["logical"])
            return 0
    if args.command == "register-synthetics":
        register_synthetics(
            manifest, args.runtime_root, args.session_id, args.pid, args.start_ticks
        )
        return 0
    if args.command == "unregister-synthetics":
        unregister_synthetics(manifest, args.runtime_root, args.session_id)
        return 0
    fail(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PolicyError as error:
        print(f"native-lab-policy: {error}", file=sys.stderr)
        raise SystemExit(1)
