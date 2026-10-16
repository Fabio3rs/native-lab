"""Exercise daemon failure diagnostics without requiring user namespaces."""
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class StartupLoggingTests(unittest.TestCase):
    def run_daemon(self, bwrap_body, fail_validation=False):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            runtime = root / "runtime"
            session = runtime / "session"
            (session / "control").mkdir(parents=True)
            binary = root / "bin"
            binary.mkdir()
            bwrap = binary / "bwrap"
            bwrap.write_text("#!/bin/bash\n" + bwrap_body + "\n")
            bwrap.chmod(0o700)
            daemon = root / "native-labd"
            source = (ROOT / "native-labd").read_text()
            # Keep production tool resolution intact; inject only the test PATH.
            source = source.replace("readonly HOST_PATH=", f"readonly HOST_PATH={binary}:")
            daemon.write_text(source)
            bootstrap = root / "native-lab-session"
            bootstrap.write_text("#!/bin/bash\nexit 0\n")
            bootstrap.chmod(0o700)
            helper = root / "policy.py"
            helper.write_text('''import pathlib, sys
command = sys.argv[1]
if command == 'summary':
    print('home=/home/test')
elif command == 'emit-bwrap':
    pathlib.Path(sys.argv[sys.argv.index('--args-output') + 1]).write_bytes(
        b'--bind\\0/source with spaces\\0/destination\\0')
    pathlib.Path(sys.argv[sys.argv.index('--synthetic-output') + 1]).touch()
elif command == 'validate-gpu':
    sys.exit(23 if FAIL else 0)
'''.replace("FAIL", repr(fail_validation)))
            policy = root / "policy.json"
            policy.write_text("{}")
            args = ["bash", str(daemon)]
            for name, value in {
                "workspace": workspace, "session-dir": session,
                "session-id": "test", "profile": "default", "policy-version": "3",
                "policy-digest": "0" * 64, "policy-file": policy,
                "policy-helper": helper, "runtime-root": runtime,
            }.items():
                args.extend(["--" + name, str(value)])
            result = subprocess.run(args, capture_output=True, text=True, timeout=17)
            plan = session / "bwrap-plan.txt"
            self.assertTrue(plan.is_file())
            self.assertIn(r"/source\ with\ spaces", plan.read_text())
            return result

    def test_early_exit_reports_status_without_timeout(self):
        result = self.run_daemon("exit 42")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exited before bootstrap acknowledgement (status=42)", result.stderr)
        self.assertNotIn("acknowledgement missing after", result.stderr)
        self.assertIn("full plan:", result.stderr)
        self.assertNotIn(r"/source\ with\ spaces", result.stderr)

    def test_timeout_reports_phase_and_process_state(self):
        result = self.run_daemon("exec sleep 30")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bootstrap acknowledgement missing after 10s", result.stderr)
        self.assertIn("waiting for bootstrap acknowledgement", result.stderr)
        self.assertIn("State:", result.stderr)
        self.assertIn("wchan:", result.stderr)

    def test_helper_failure_reports_stage_line_and_status(self):
        result = self.run_daemon("exit 0", fail_validation=True)
        self.assertEqual(result.returncode, 23)
        self.assertRegex(result.stderr, r"error phase=validating GPU policy on host before launch line=\d+ status=23")
        self.assertNotIn("bubblewrap pid=", result.stderr)


if __name__ == "__main__":
    unittest.main()
