"""Hardware-independent tests for the trusted GPU compiler and session identity."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("policy", ROOT / "native-lab-policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
gpu = policy.gpu


class FakeHost(gpu.Host):
    def __init__(self, count=1):
        self.files = {"/sys/devices/virtual/kfd/kfd/dev": "235:0"}
        self.links = {"/sys/dev/char/235:0": "/sys/devices/virtual/kfd/kfd"}
        self.nodes = {"/dev/kfd": dict(path="/dev/kfd", dev=7, ino=300, major=235, minor=0)}
        self.render_paths = []
        for index in range(count):
            self.add_render(f"0000:{11 + index:02x}:00.0", 128 + index)

    def add_render(self, pci, minor):
        physical = "/sys/devices/pci0000:00/" + pci
        render = f"/sys/class/drm/renderD{minor}"
        self.render_paths.append(render)
        self.links[render] = physical + f"/drm/renderD{minor}"
        self.links[render + "/device"] = physical
        self.links[physical + "/driver"] = "/sys/bus/pci/drivers/amdgpu"
        self.links[f"/sys/dev/char/226:{minor}"] = self.links[render]
        self.files[physical + "/vendor"] = "0x1002"
        self.files[physical + "/device"] = "0x744c"
        self.files[render + "/dev"] = f"226:{minor}"
        path = f"/dev/dri/renderD{minor}"
        self.nodes[path] = dict(path=path, dev=7, ino=minor, major=226, minor=minor)

    def read(self, path):
        return self.files[path]

    def realpath(self, path):
        return self.links.get(path, path)

    def renders(self):
        return sorted(self.render_paths)

    def pci_gpus(self):
        return []

    def node(self, path, check_open=True):
        if path not in self.nodes:
            raise FileNotFoundError(path)
        return self.nodes[path].copy()


def request(features=None, devices="auto", provider="amd", roots=None):
    return dict(provider=provider, features=features if features is not None else ["graphics"], devices=devices, readonly_roots=roots or [])


def resolve(req=None, host=None):
    return gpu.resolve(req or request(), "/project", "/home/test", ["/usr", "/etc", "/lib"], host or FakeHost())


class GPUResolutionTests(unittest.TestCase):
    def test_disabled_has_no_authority(self):
        self.assertEqual(gpu.resolve(None, "/project", "/home/test", []), gpu.disabled())
        self.assertEqual(resolve(request(features=[], roots=["/missing"])), gpu.disabled())

    def test_graphics_only_render_and_sysfs(self):
        resources = resolve()
        self.assertEqual([n["path"] for n in resources["nodes"]], ["/dev/dri/renderD128"])
        self.assertEqual(resources["readonly_mounts"], [dict(source="/sys", destination="/sys")])
        self.assertEqual(resources["environment"], {})

    def test_compute_requires_render_and_kfd(self):
        resources = resolve(request(["compute"]))
        self.assertEqual({n["path"] for n in resources["nodes"]}, {"/dev/kfd", "/dev/dri/renderD128"})
        host = FakeHost()
        del host.nodes["/dev/kfd"]
        with self.assertRaises(FileNotFoundError):
            resolve(request(["compute"]), host)

    def test_no_gpu_and_auto_ambiguity_fail(self):
        for count in (0, 2):
            with self.subTest(count=count), self.assertRaisesRegex(gpu.GPUError, "exactly one"):
                resolve(host=FakeHost(count))

    def test_all_and_pci_selection(self):
        self.assertEqual(len(resolve(request(devices="all"), FakeHost(2))["devices"]), 2)
        r = resolve(request(devices=["0000:0c:00.0"]), FakeHost(2))
        self.assertEqual([n["path"] for n in r["nodes"]], ["/dev/dri/renderD129"])
        with self.assertRaisesRegex(gpu.GPUError, "not found"):
            resolve(request(devices=["0000:ff:00.0"]))

    def test_partitions_grouped_by_physical_pci(self):
        host = FakeHost()
        host.add_render("0000:0b:00.0", 129)
        result = resolve(host=host)
        self.assertEqual(len(result["devices"]), 1)
        self.assertEqual(len(result["nodes"]), 2)

    def test_vendor_driver_and_syschar_mismatch(self):
        cases = [
            ("files", "/sys/devices/pci0000:00/0000:0b:00.0/vendor", "0x8086"),
            ("links", "/sys/devices/pci0000:00/0000:0b:00.0/driver", "/sys/bus/pci/drivers/vfio-pci"),
            ("files", "/sys/class/drm/renderD128/dev", "226:129"),
            ("links", "/sys/dev/char/226:128", "/sys/other"),
        ]
        for field, key, value in cases:
            host = FakeHost()
            getattr(host, field)[key] = value
            with self.subTest(key=key), self.assertRaises(gpu.GPUError):
                resolve(host=host)

    def test_kfd_identity_not_hardcoded_major(self):
        host = FakeHost()
        host.nodes["/dev/kfd"]["major"] = 240
        host.links["/sys/dev/char/240:0"] = "/sys/devices/virtual/kfd/kfd"
        resolve(request(["compute"]), host)
        host.links["/sys/dev/char/240:0"] = "/sys/devices/other"
        with self.assertRaises(gpu.GPUError):
            resolve(request(["compute"]), host)

    def test_revalidation_catches_inode_pci_and_driver_changes(self):
        for field in ("ino", "driver", "device"):
            host = FakeHost()
            resources = resolve(host=host)
            if field == "ino":
                host.nodes["/dev/dri/renderD128"]["ino"] += 1
            elif field == "driver":
                host.links["/sys/devices/pci0000:00/0000:0b:00.0/driver"] = "/sys/bus/pci/drivers/radeon"
            else:
                host.files["/sys/devices/pci0000:00/0000:0b:00.0/device"] = "0xffff"
            with self.subTest(field=field), self.assertRaisesRegex(gpu.GPUError, "identity changed"):
                gpu.revalidate(resources, host)

    def test_gpu_runtime_roots_explicit_and_deduplicated(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            (root / "runtime").mkdir()
            (root / "alias").symlink_to(root / "runtime", target_is_directory=True)
            req = request(roots=[str(root / "alias")])
            result = resolve(req)
            self.assertIn(dict(source=str(root / "runtime"), destination=str(root / "runtime")), result["readonly_mounts"])
            self.assertEqual(result["symlinks"], [dict(path=str(root / "alias"), target=str(root / "runtime"))])
            result = gpu.resolve(req, "/project", "/home/test", [str(root)], FakeHost())
            self.assertEqual(result["readonly_mounts"], [dict(source="/sys", destination="/sys")])

    def test_unsafe_runtime_roots_fail(self):
        for root in ("/", "/opt", "/dev", "/sys", "/run/udev", "/project", "/home/test", "/missing"):
            with self.subTest(root=root), self.assertRaises(gpu.GPUError):
                resolve(request(roots=[root]))

    def test_nvidia_and_unknown_fail_without_host_discovery(self):
        for name, message in (("nvidia", "not yet implemented"), ("other", "unknown")):
            with self.subTest(name=name), self.assertRaisesRegex(gpu.GPUError, message):
                resolve(request(provider=name))

    def test_manifest_cannot_smuggle_extra_devices_or_env(self):
        for key, value in (("environment", {"LD_PRELOAD": "/project/x"}),
                           ("nodes", [dict(path="/dev/mem", dev=7, ino=1, major=1, minor=1)])):
            r = resolve()
            r[key] = value
            with self.assertRaises(gpu.GPUError):
                gpu.validate_schema(r)


class DeviceValidationTests(unittest.TestCase):
    def metadata(self, mode=stat.S_IFCHR | 0o660, ino=1):
        return os.stat_result((mode, ino, 7, 1, 0, 109, 0, 0, 0, 0), {"st_rdev": os.makedev(226, 128)})

    def test_arbitrary_path_never_opened(self):
        with patch.object(gpu.os, "open") as opened:
            with self.assertRaises(gpu.GPUError):
                gpu.Host().node("/dev/mem")
            opened.assert_not_called()

    def test_symlink_or_regular_device_rejected(self):
        for mode in (stat.S_IFLNK, stat.S_IFREG):
            with patch.object(gpu.os, "lstat", return_value=self.metadata(mode)), patch.object(gpu.os, "open") as opened:
                with self.assertRaises(gpu.GPUError):
                    gpu.Host().node("/dev/dri/renderD128")
                opened.assert_not_called()

    def test_effective_open_permission_required(self):
        with patch.object(gpu.os, "lstat", return_value=self.metadata()), patch.object(gpu.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(gpu.GPUError, "permission insufficient"):
                gpu.Host().node("/dev/dri/renderD128")

    def test_race_between_lstat_and_open_fails(self):
        with patch.object(gpu.os, "lstat", return_value=self.metadata()), patch.object(gpu.os, "open", return_value=99), patch.object(gpu.os, "close"), patch.object(gpu.os, "fstat", return_value=self.metadata(ino=2)):
            with self.assertRaisesRegex(gpu.GPUError, "changed while opening"):
                gpu.Host().node("/dev/dri/renderD128")


class ConfigAndPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.workspace = root / "project"
        self.workspace.mkdir()
        for name in (".git", ".agents", ".codex"):
            (self.workspace / name).mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.config_root = root / "config"
        (self.config_root / "native-lab").mkdir(parents=True)
        self.config = self.config_root / "native-lab/config.toml"
        self.runtime = root / "runtime"
        self.runtime.mkdir()
        self.host = FakeHost()
        for context in (patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config_root)}),
                        patch.object(policy, "real_home", return_value=(str(self.home), str(self.home))),
                        patch.object(gpu, "Host", return_value=self.host)):
            context.start()
            self.addCleanup(context.stop)

    def compile(self, extra="", version=2):
        self.config.write_text(f'version = {version}\n[codex]\nimport_trusted_projects = false\n' + extra)
        self.config.chmod(0o600)
        return policy.compile_policy(str(self.workspace), "/usr/bin/rg", str(self.runtime))

    def gpu_table(self, workspace=None, features='["graphics"]', devices='"auto"'):
        return f'\n[gpu.workspaces."{workspace or self.workspace}"]\nprovider="amd"\nfeatures={features}\ndevices={devices}\n'

    def test_v1_v2_and_other_workspace_disabled(self):
        for extra, version in (("", 1), ("", 2), (self.gpu_table("/other"), 2)):
            self.assertEqual(self.compile(extra, version)["gpu"], gpu.disabled())

    def test_digest_authority_and_irrelevant_changes(self):
        a = self.compile(self.gpu_table())
        self.assertEqual(a["policy_digest"], self.compile(self.gpu_table() + "# comment\n" + self.gpu_table("/other"))["policy_digest"])
        self.assertNotEqual(a["policy_digest"], self.compile(self.gpu_table(features='["graphics", "compute"]'))["policy_digest"])
        b = self.compile(self.gpu_table(features='["compute", "graphics"]'))
        self.assertEqual(b["policy_digest"], self.compile(self.gpu_table(features='["graphics", "compute", "graphics"]'))["policy_digest"])
        self.host.nodes["/dev/dri/renderD128"]["ino"] += 1
        self.assertNotEqual(a["policy_digest"], self.compile(self.gpu_table())["policy_digest"])

    def test_strict_config(self):
        invalid = [dict(provider="intel", features=[]), dict(provider="amd", features="graphics"),
                   dict(provider="amd", features=["utility"]), dict(provider="amd", features=[], devices="/dev/dri/renderD128"),
                   dict(provider="amd", features=[], devices=["../dev/kfd"]), dict(provider="amd", features=[], surprise=True)]
        for req in invalid:
            with self.subTest(req=req), self.assertRaises(gpu.GPUError):
                gpu.parse_config({"workspaces": {"/project": req}}, 2)
        with self.assertRaises(gpu.GPUError):
            gpu.parse_config({"default": "amd"}, 2)
        with self.assertRaises(gpu.GPUError):
            self.compile(self.gpu_table(), 1)

    def test_config_in_workspace_and_symlink_refused(self):
        f = self.workspace / "config.toml"
        f.write_text("version=2")
        with self.assertRaises(policy.PolicyError):
            policy.checked_config_path(str(f), str(self.workspace), "test")
        self.config.symlink_to(f)
        with self.assertRaises(policy.PolicyError):
            policy.checked_config_path(str(self.config), str(self.workspace), "test")

    def test_nul_arguments_and_exact_device_allowlist(self):
        manifest = self.compile(self.gpu_table())
        encoded, _ = policy.emit_bwrap(manifest, "/mask")
        self.assertTrue(encoded.endswith(b"\0"))
        args = encoded.decode().split("\0")[:-1]
        index = args.index("--dev-bind")
        self.assertEqual(args[index + 1:index + 3], ["/dev/dri/renderD128"] * 2)
        self.assertLess(args.index("--dev"), index)
        self.assertNotIn("/dev/kfd", args)
        self.assertNotIn("/dev/dri/card0", args)
        disabled_args, _ = policy.emit_bwrap(self.compile(), "/mask")
        self.assertNotIn(b"/sys\0", disabled_args)
        self.assertNotIn(b"--dev-bind\0", disabled_args)

    def test_old_manifest_only_inspectable(self):
        m = self.compile()
        m.pop("gpu")
        m.update(policy_version=2, manifest_version=1)
        with self.assertRaisesRegex(policy.PolicyError, "only be inspected"):
            policy.emit_bwrap(m, "/mask")

    def test_redundant_roots_pruned_but_masks_and_workspace_preserved(self):
        m = self.compile()
        m["roots"]["user_read_only"] = [
            dict(source="/deps", destination="/deps"),
            dict(source="/deps", destination="/deps"),
        ]
        m["roots"]["trusted_projects"] = ["/deps/nested", "/deps-other"]
        m["masks"] = [dict(path="/deps/nested/secret.key", kind="file"),
                      dict(path="/deps/nested/.git", kind="directory")]
        encoded, _ = policy.emit_bwrap(m, "/mask")
        self.assertEqual(encoded.count(b"--ro-bind\0/deps\0/deps\0"), 1)
        self.assertNotIn(b"--ro-bind\0/deps/nested\0", encoded)
        self.assertIn(b"--ro-bind\0/deps-other\0/deps-other\0", encoded)
        self.assertIn(b"--ro-bind\0/mask\0/deps/nested/secret.key\0", encoded)
        self.assertIn(b"--tmpfs\0/deps/nested/.git\0--remount-ro\0", encoded)
        self.assertIn(("--bind\0" + str(self.workspace) + "\0").encode(), encoded)

    def test_collapse_masks_keeps_siblings_and_file_descendants(self):
        masks = [dict(path=path, kind=kind) for path, kind in [
            ("/deps/.git/config", "file"), ("/deps/.git", "directory"),
            ("/deps/.git-other/key", "file"), ("/deps/file", "file"),
            ("/deps/file/child", "file"), ("/deps/missing", "missing-directory"),
            ("/deps/missing/child", "file"), ("/deps/file", "file"),
        ]]
        self.assertEqual({m["path"] for m in policy.collapse_masks(masks)},
                         {"/deps/.git", "/deps/.git-other/key", "/deps/file",
                          "/deps/file/child", "/deps/missing"})

    def test_explicit_exceptions_preserve_other_secrets_and_metadata(self):
        trusted = self.home / "trusted"
        (trusted / "fixtures").mkdir(parents=True)
        (trusted / ".git").mkdir()
        for name in ("fixtures/test.key", "public.pem", "private.key", "public.pem.key"):
            (trusted / name).touch()
        config = ('\n[filesystem]\ntrusted_project_allow_paths = '
                  + json.dumps([str(trusted / "fixtures"), str(trusted / "public.pem")])
                  + '\nextra_trusted_project_deny_globs = ["**/*.key", "**/*.pem"]\n')
        with patch.object(policy, "collect_trusted_projects", return_value=[str(trusted)]):
            original = self.compile()
            self.assertNotIn(str(trusted / "private.key"), {m["path"] for m in original["masks"]})
            resolved = self.compile(config)
            paths = {m["path"] for m in resolved["masks"]}
            self.assertNotIn(str(trusted / "fixtures/test.key"), paths)
            self.assertNotIn(str(trusted / "public.pem"), paths)
            self.assertIn(str(trusted / "private.key"), paths)
            self.assertIn(str(trusted / "public.pem.key"), paths)
            self.assertIn(str(trusted / ".git"), paths)
            self.assertNotEqual(original["policy_digest"], resolved["policy_digest"])
            whole = self.compile('\n[filesystem]\ntrusted_project_allow_paths = '
                                 + json.dumps([str(trusted)]) + '\n')
            self.assertIn(str(trusted / ".git"), {m["path"] for m in whole["masks"]})

    def test_invalid_exception_paths(self):
        for path in ("relative/path", "/", str(self.home), "/tmp"):
            with self.subTest(path=path), self.assertRaises(policy.PolicyError):
                self.compile('\n[filesystem]\ntrusted_project_allow_paths = '
                             + json.dumps([path]) + '\n')

    def test_no_default_deny_scan(self):
        self.assertEqual(policy.DEFAULT_DENY_GLOBS, ())
        with patch.object(policy.subprocess, "run", side_effect=AssertionError("unexpected scan")):
            self.assertEqual(policy.run_rg("rg", "/project", []), [])

    def test_invalid_manifest_rehashed_still_rejected(self):
        m = self.compile(self.gpu_table())
        m["gpu"]["nodes"][0]["path"] = "/dev/mem"
        m.pop("policy_digest")
        m["policy_digest"] = hashlib.sha256(json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        path = self.runtime / "manifest.json"
        path.write_text(json.dumps(m))
        with self.assertRaises(gpu.GPUError):
            policy.load_manifest(path)

    def test_doctor_fingerprint_does_not_clean_stale_markers(self):
        target = self.workspace / ".git"
        directory = policy.synthetic_marker_dir(str(self.runtime), str(target))
        directory.mkdir(parents=True)
        s = target.stat()
        marker = directory / "old-session"
        marker.write_text(json.dumps(dict(path=str(target), pid=99999999, start_ticks="1", target_dev=s.st_dev, target_ino=s.st_ino)))
        self.compile()
        # Normal resolution recovered the stale marker. Recreate it for the
        # read-only fingerprint used by doctor --session.
        target.mkdir(exist_ok=True)
        directory.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(dict(path=str(target), pid=99999999, start_ticks="1", target_dev=target.stat().st_dev, target_ino=target.stat().st_ino)))
        before = marker.read_bytes()
        result = policy.compile_policy(str(self.workspace), "/usr/bin/rg", str(self.runtime), read_only=True)
        self.assertEqual(marker.read_bytes(), before)
        self.assertTrue(target.exists())
        self.assertEqual(next(m for m in result["masks"] if m["path"] == str(target))["kind"], "missing-directory")


class SessionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = "/work/project"
        self.daemon = "/trusted/native-labd"
        self.pid = 12345
        self.start = "100"
        self.make_session(3, 2)

    def make_session(self, policy_version, manifest_version):
        manifest = dict(workspace=self.workspace, profile="default", policy_version=policy_version, manifest_version=manifest_version)
        if policy_version == 3:
            manifest["gpu"] = gpu.disabled()
        self.digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        manifest["policy_digest"] = self.digest
        self.session_id = hashlib.sha256("\0".join((self.workspace, "default", str(policy_version), self.digest)).encode()).hexdigest()[:24]
        self.directory = self.root / self.session_id
        self.directory.mkdir(mode=0o700)
        data = {
            "resolved-policy.json": json.dumps(manifest),
            "session.info": f"workspace={self.workspace}\nprofile=default\npolicy_version={policy_version}\npolicy_digest={self.digest}\nholder_pid={self.pid}\n",
            "holder.pid": f"pid={self.pid}\nstart_ticks={self.start}\nsession_id={self.session_id}\n",
        }
        for name, content in data.items():
            path = self.directory / name
            path.write_text(content)
            path.chmod(0o600)
        self.argv = ["/bin/bash", self.daemon, "--workspace", self.workspace, "--session-dir", str(self.directory),
                     "--session-id", self.session_id, "--profile", "default", "--policy-version", str(policy_version),
                     "--policy-digest", self.digest, "--policy-file", str(self.directory / "resolved-policy.json"),
                     "--policy-helper", "/trusted/native-lab-policy.py", "--runtime-root", str(self.root)]

    def read_record(self, argv=None):
        with patch.object(policy, "process_identity_matches", return_value=True), patch.object(Path, "read_bytes", return_value="\0".join(argv or self.argv).encode() + b"\0"):
            return policy.session_record(self.directory, self.workspace, self.daemon)

    def test_active_holder_requires_exact_argv_pairings(self):
        self.assertEqual(self.read_record()["state"], "active")
        args = self.argv.copy()
        args[3], args[5] = args[5], args[3]
        with self.assertRaisesRegex(policy.PolicyError, "identity mismatch"):
            self.read_record(args)
        with self.assertRaises(policy.PolicyError):
            self.read_record(["sleep", *self.argv])

    def test_another_installation_cannot_validate_holder(self):
        args = self.argv.copy()
        args[1] = "/other/native-labd"
        with self.assertRaisesRegex(policy.PolicyError, "check CLI/MCP installation paths"):
            self.read_record(args)

    def test_process_diagnostics_distinguish_failure_causes(self):
        for error, expected in [(FileNotFoundError(), "PID not visible"),
                                (PermissionError(), "permission denied")]:
            with patch.object(Path, "read_text", side_effect=error):
                self.assertIn(expected, policy.process_identity_diagnostic(self.pid, self.start))
        with patch.object(Path, "read_text", return_value="123 (name with spaces) " + " ".join(["S"] + ["0"] * 18 + ["999"])):
            self.assertIn("start_ticks mismatch", policy.process_identity_diagnostic(self.pid, self.start))

    def test_reused_pid_not_active(self):
        with patch.object(policy, "process_identity_matches", return_value=False):
            self.assertEqual(policy.session_record(self.directory, self.workspace, self.daemon)["state"], "stale")

    def test_other_workspace_is_never_selected(self):
        self.assertIsNone(policy.session_record(self.directory, "/work/project-copy", self.daemon))
        self.assertEqual(policy.list_sessions(str(self.root), "/work/project-copy", self.daemon), [])

    def test_old_policy_holder_can_be_verified(self):
        self.make_session(2, 1)
        self.assertEqual(self.read_record()["policy_version"], "2")

    def test_metadata_symlink_and_duplicate_identity_fail(self):
        path = self.directory / "holder.pid"
        path.write_text(path.read_text() + "pid=999\n")
        with self.assertRaisesRegex(policy.PolicyError, "duplicate"):
            self.read_record()
        path.unlink()
        path.symlink_to(self.directory / "session.info")
        with self.assertRaisesRegex(policy.PolicyError, "unsafe session file"):
            self.read_record()

    def test_digest_directory_and_manifest_must_agree(self):
        info = self.directory / "session.info"
        info.write_text(info.read_text().replace(self.digest, "0" * 64))
        with self.assertRaisesRegex(policy.PolicyError, "inconsistent"):
            self.read_record()


if __name__ == "__main__":
    unittest.main()
