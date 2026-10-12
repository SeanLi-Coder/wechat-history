"""Compile a disposable native bundle and verify its process/argument contract."""

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_app


@unittest.skipUnless(sys.platform == "darwin", "The native launcher requires macOS")
class NativeLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="shiliao-native-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        project = cls.root / "Synthetic Source"
        for folder in ("scripts", "vault", "static"):
            (project / folder).mkdir(parents=True, exist_ok=True)
        shutil.copy2(build_app.PROJECT / "scripts/native_launcher.m", project / "scripts/native_launcher.m")
        (project / "app.py").write_text(
            "import json,os,sys\n"
            "mask=os.umask(0o077)\n"
            "print(json.dumps({'pid':os.getpid(),'args':sys.argv[1:],'cwd':os.getcwd(),'umask':mask,'no_bytecode':sys.dont_write_bytecode}),flush=True)\n"
            "raise SystemExit(int(os.environ.get('SHILIAO_TEST_EXIT','0')))\n", encoding="utf-8")
        cls.bundle = cls.root / "Synthetic App.app"
        with patch.object(build_app, "PROJECT", project):
            build_app.build_app(cls.bundle)
        cls.launcher = cls.bundle / "Contents/MacOS/Shiliao"

    def test_bundle_is_macho_signed_and_describes_app_data_access(self):
        output = subprocess.check_output(["/usr/bin/file", str(self.launcher)], text=True)
        self.assertIn("Mach-O", output)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(self.bundle)], check=True)
        info = plistlib.loads((self.bundle / "Contents/Info.plist").read_bytes())
        self.assertEqual(info["CFBundleIdentifier"], "local.shiliao.archive")
        self.assertTrue(info["NSAppDataUsageDescription"])
        self.assertTrue(Path(info["ShiliaoPythonExecutable"]).is_absolute())

    def test_arguments_spaces_and_process_identity_survive_native_exec(self):
        arguments = ["--scheduled", "--state-dir", str(self.root / "Private State"),
                     "literal;$(not-a-command)", "Unicode-测试"]
        process = subprocess.Popen([str(self.launcher), *arguments], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, error)
        report = json.loads(output)
        self.assertEqual(report["pid"], process.pid)
        self.assertEqual(report["args"], arguments)
        self.assertEqual(report["umask"], 0o077)
        self.assertTrue(report["no_bytecode"])
        self.assertEqual(report["cwd"], str(self.bundle / "Contents/Resources/app"))

    def test_scheduled_exit_status_is_not_hidden_by_background_launch(self):
        result = subprocess.run([str(self.launcher), "--scheduled"], capture_output=True, text=True,
                                env={**os.environ, "SHILIAO_TEST_EXIT": "19"}, timeout=10)
        self.assertEqual(result.returncode, 19)
        self.assertEqual(json.loads(result.stdout)["args"], ["--scheduled"])

    def test_relocated_bundle_resolves_its_own_payload(self):
        moved = self.root / "Moved App.app"
        shutil.copytree(self.bundle, moved)
        result = subprocess.run([str(moved / "Contents/MacOS/Shiliao"), "--no-browser"],
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout)["cwd"], str(moved / "Contents/Resources/app"))


if __name__ == "__main__":
    unittest.main()
