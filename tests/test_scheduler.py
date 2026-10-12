"""Exercise launchd management without touching the user's LaunchAgents."""

import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from vault.scheduler import BUNDLE_ID, LABEL, LAUNCHCTL, Scheduler


class FakeLaunchctl:
    def __init__(self):
        self.loaded = None
        self.calls = []
        self.fail_bootstrap = False
        self.bootstrap_failures_remaining = 0
        self.print_error = False

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        action = command[1]
        if action == "print":
            if self.print_error:
                return subprocess.CompletedProcess(command, 1, "", "Permission denied")
            if self.loaded is None:
                return subprocess.CompletedProcess(command, 113, "", "Could not find service")
            output = "job = {\n\targuments = {\n" + "\n".join("\t\t" + arg for arg in self.loaded) + "\n\t}\n}\n"
            return subprocess.CompletedProcess(command, 0, output, "")
        if action == "bootstrap":
            if self.fail_bootstrap or self.bootstrap_failures_remaining:
                self.bootstrap_failures_remaining = max(0, self.bootstrap_failures_remaining - 1)
                return subprocess.CompletedProcess(command, 5, "", "Bootstrap failed")
            self.loaded = plistlib.loads(Path(command[3]).read_bytes())["ProgramArguments"]
        elif action == "bootout":
            self.loaded = None
        return subprocess.CompletedProcess(command, 0, "", "")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name).resolve()
        self.project = root / "Project With Spaces"
        self.project.mkdir()
        (self.project / "app.py").write_text("", encoding="utf-8")
        self.state = root / "Private State"
        self.agents = root / "Test LaunchAgents"
        self.scheduler = Scheduler(self.state, self.project, agents_dir=self.agents)
        self.launchctl = FakeLaunchctl()
        patcher = patch("vault.scheduler.subprocess.run", side_effect=self.launchctl)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_configuration(self, arguments=None):
        self.agents.mkdir(exist_ok=True)
        configuration = self.scheduler._configuration()
        if arguments is not None:
            configuration["ProgramArguments"] = arguments
        self.scheduler.plist_path.write_bytes(plistlib.dumps(configuration))
        return configuration

    def test_install_private_plist_and_idempotence(self):
        self.assertTrue(self.scheduler.install()["installed"])
        configuration = plistlib.loads(self.scheduler.plist_path.read_bytes())
        self.assertEqual(configuration["Label"], LABEL)
        self.assertEqual(configuration["ProgramArguments"], [sys.executable, str(self.project / "app.py"), "--scheduled", "--state-dir", str(self.state)])
        self.assertEqual(configuration["StartInterval"], 900)
        self.assertTrue(configuration["RunAtLoad"])
        self.assertEqual(configuration["Umask"], 0o077)
        self.assertEqual(self.scheduler.plist_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / "logs").stat().st_mode & 0o777, 0o700)
        self.assertTrue(self.scheduler.status()["installed"])
        self.assertTrue(self.scheduler.install()["installed"])
        actions = [command[1] for command, _ in self.launchctl.calls]
        self.assertEqual(actions.count("bootstrap"), 1)
        self.assertEqual(actions.count("bootout"), 0)
        for command, kwargs in self.launchctl.calls:
            self.assertEqual(command[0], LAUNCHCTL)
            self.assertFalse(kwargs.get("shell", False))
        self.assertIn([LAUNCHCTL, "bootstrap", f"gui/{os.getuid()}", str(self.scheduler.plist_path)], [command for command, _ in self.launchctl.calls])

    def test_uninstall_is_idempotent(self):
        self.scheduler.install()
        self.assertFalse(self.scheduler.uninstall()["installed"])
        self.assertFalse(self.scheduler.plist_path.exists())
        self.assertIsNone(self.launchctl.loaded)
        self.assertFalse(self.scheduler.uninstall()["installed"])
        self.assertEqual([command[1] for command, _ in self.launchctl.calls].count("bootout"), 1)

    def test_foreign_plist_is_never_replaced(self):
        foreign = self.scheduler._arguments()
        foreign[4] = str(self.state.parent / "Someone Else")
        self.write_configuration(foreign)
        original = self.scheduler.plist_path.read_bytes()
        self.assertFalse(self.scheduler.status()["installed"])
        for operation in (self.scheduler.install, self.scheduler.uninstall):
            with self.assertRaisesRegex(RuntimeError, "Another project"):
                operation()
        self.assertEqual(self.scheduler.plist_path.read_bytes(), original)
        self.assertFalse(self.launchctl.calls)

    def test_foreign_loaded_job_is_never_stopped(self):
        self.write_configuration()
        foreign = self.scheduler._arguments()
        foreign[1] = str(self.project.parent / "Foreign" / "app.py")
        self.launchctl.loaded = foreign
        self.assertFalse(self.scheduler.status()["installed"])
        for operation in (self.scheduler.install, self.scheduler.uninstall):
            with self.assertRaisesRegex(RuntimeError, "loaded scheduler"):
                operation()
        self.assertTrue(self.scheduler.plist_path.exists())
        self.assertTrue(all(command[1] == "print" for command, _ in self.launchctl.calls))

    def test_bootstrap_failure_removes_new_plist(self):
        self.launchctl.fail_bootstrap = True
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            self.scheduler.install()
        self.assertFalse(self.scheduler.plist_path.exists())
        self.assertFalse(self.scheduler.status()["installed"])

    def test_bootstrap_failure_restores_previous_plist(self):
        configuration = self.write_configuration()
        configuration["StartInterval"] = 1800
        previous_raw = plistlib.dumps(configuration)
        self.scheduler.plist_path.write_bytes(previous_raw)
        self.launchctl.fail_bootstrap = True
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            self.scheduler.install()
        self.assertEqual(self.scheduler.plist_path.read_bytes(), previous_raw)

    def test_unloaded_plist_is_reported_and_repaired(self):
        self.write_configuration()
        status = self.scheduler.status()
        self.assertFalse(status["installed"])
        self.assertIn("not loaded", status["error"])
        self.assertTrue(self.scheduler.install()["installed"])

    def test_reinstall_only_stops_the_owned_service(self):
        configuration = self.write_configuration()
        configuration["StartInterval"] = 1800
        self.scheduler.plist_path.write_bytes(plistlib.dumps(configuration))
        self.launchctl.loaded = configuration["ProgramArguments"]
        self.assertTrue(self.scheduler.install()["installed"])
        bootouts = [command for command, _ in self.launchctl.calls if command[1] == "bootout"]
        self.assertEqual(bootouts, [[LAUNCHCTL, "bootout", f"gui/{os.getuid()}/{LABEL}"]])
        self.assertEqual(plistlib.loads(self.scheduler.plist_path.read_bytes())["StartInterval"], 900)

    def test_failed_reinstall_restores_the_previous_loaded_job(self):
        configuration = self.write_configuration()
        configuration["StartInterval"] = 1800
        previous_raw = plistlib.dumps(configuration)
        self.scheduler.plist_path.write_bytes(previous_raw)
        self.launchctl.loaded = configuration["ProgramArguments"]
        self.launchctl.bootstrap_failures_remaining = 1
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            self.scheduler.install()
        self.assertEqual(self.scheduler.plist_path.read_bytes(), previous_raw)
        self.assertEqual(self.launchctl.loaded, configuration["ProgramArguments"])

    def test_unknown_launchctl_failure_is_not_treated_as_missing(self):
        self.launchctl.print_error = True
        self.assertIn("Permission denied", self.scheduler.status()["error"])
        with self.assertRaisesRegex(RuntimeError, "Permission denied"):
            self.scheduler.install()
        self.assertFalse(self.scheduler.plist_path.exists())

    def test_symlink_plist_is_rejected(self):
        self.agents.mkdir()
        target = self.project / "target.plist"
        target.write_bytes(plistlib.dumps(self.scheduler._configuration()))
        self.scheduler.plist_path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "regular"):
            self.scheduler.install()
        self.assertTrue(self.scheduler.plist_path.is_symlink())

    def test_invalid_plist_is_rejected(self):
        self.agents.mkdir()
        self.scheduler.plist_path.write_bytes(b"not a plist")
        self.assertIn("cannot be verified", self.scheduler.status()["error"])
        with self.assertRaisesRegex(RuntimeError, "cannot be verified"):
            self.scheduler.uninstall()

    def native_bundle(self):
        bundle = self.project / "Synthetic App.app"
        self.project = bundle / "Contents/Resources/app"
        self.project.mkdir(parents=True)
        (self.project / "app.py").write_text("", encoding="utf-8")
        launcher = bundle / "Contents/MacOS/Shiliao"
        launcher.parent.mkdir()
        launcher.write_bytes(b"\xcf\xfa\xed\xfe" + b"Synthetic Mach-O fixture")
        launcher.chmod(0o755)
        (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": BUNDLE_ID, "CFBundleExecutable": "Shiliao"}))
        self.scheduler = Scheduler(self.state, self.project, agents_dir=self.agents)
        return launcher

    def test_native_bundle_uses_same_executable_and_bundle_association(self):
        launcher = self.native_bundle()
        self.scheduler.install()
        configuration = plistlib.loads(self.scheduler.plist_path.read_bytes())
        self.assertEqual(configuration["ProgramArguments"],
                         [str(launcher), "--scheduled", "--state-dir", str(self.state)])
        self.assertEqual(configuration["AssociatedBundleIdentifiers"], [BUNDLE_ID])
        self.assertEqual(self.launchctl.loaded, configuration["ProgramArguments"])
        self.assertIsNone(self.scheduler.status()["error"])

    def test_legacy_owned_python_job_is_migrated_only_on_install(self):
        launcher = self.native_bundle()
        previous = self.write_configuration([sys.executable, str(self.project / "app.py"),
                                             "--scheduled", "--state-dir", str(self.state)])
        previous.pop("AssociatedBundleIdentifiers")
        raw = plistlib.dumps(previous)
        self.scheduler.plist_path.write_bytes(raw)
        self.launchctl.loaded = previous["ProgramArguments"]
        self.assertIn("older application launcher", self.scheduler.status()["error"])
        self.assertEqual(self.scheduler.plist_path.read_bytes(), raw)
        self.assertTrue(all(command[1] == "print" for command, _ in self.launchctl.calls))
        self.scheduler.install()
        self.assertEqual(self.launchctl.loaded[0], str(launcher))
        self.assertIsNone(self.scheduler.status()["error"])

    def test_failed_native_migration_restores_legacy_job(self):
        self.native_bundle()
        previous = self.write_configuration([sys.executable, str(self.project / "app.py"),
                                             "--scheduled", "--state-dir", str(self.state)])
        previous.pop("AssociatedBundleIdentifiers")
        raw = plistlib.dumps(previous)
        self.scheduler.plist_path.write_bytes(raw)
        self.launchctl.loaded = previous["ProgramArguments"]
        self.launchctl.bootstrap_failures_remaining = 1
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            self.scheduler.install()
        self.assertEqual(self.scheduler.plist_path.read_bytes(), raw)
        self.assertEqual(self.launchctl.loaded, previous["ProgramArguments"])

    def test_script_or_foreign_native_launchers_are_not_owned(self):
        launcher = self.native_bundle()
        arguments = self.scheduler._arguments()
        arguments[0] = str(launcher.parent / "Foreign")
        self.assertFalse(self.scheduler._owns_arguments(arguments))
        launcher.write_text("#!/bin/sh\nexit 0\n")
        self.assertIsNone(self.scheduler._bundle_launcher())
        self.assertEqual(self.scheduler._arguments()[0], sys.executable)
        self.assertNotIn("AssociatedBundleIdentifiers", self.scheduler._configuration())


if __name__ == "__main__":
    unittest.main()
