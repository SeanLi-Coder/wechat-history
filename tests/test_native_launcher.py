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


@unittest.skipUnless(sys.platform == "darwin", "The native launcher requires macOS")
class PortableNativeLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="shiliao-portable-native-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        project = cls.root / "Synthetic Source"
        for folder in ("scripts", "vault", "static"):
            (project / folder).mkdir(parents=True, exist_ok=True)
        shutil.copy2(build_app.PROJECT / "scripts/native_launcher.m", project / "scripts/native_launcher.m")
        (project / "app.py").write_text("# Synthetic payload; the test runtime reports arguments only.\n")
        cls.template = cls.root / "Portable Template.app"
        runtime = cls.template / "Contents/Resources/python/bin/python3"
        runtime.parent.mkdir(parents=True)
        source = cls.root / "synthetic_runtime.c"
        # This executable reports the launch contract; it is not a Python runtime.
        source.write_text(r'''
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

static void string_json(const char *text) {
    if (text == NULL) { fputs("null", stdout); return; }
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
        if (*p == '"' || *p == '\\') putchar('\\');
        if (*p < 32) printf("\\u%04x", *p); else putchar(*p);
    }
    putchar('"');
}

int main(int argc, char **argv) {
    char cwd[PATH_MAX];
    if (getcwd(cwd, sizeof(cwd)) == NULL) return 2;
    printf("{\"pid\":%ld,\"umask\":%u,\"cwd\":", (long)getpid(), (unsigned)umask(0077));
    string_json(cwd);
    const char *names[] = {"PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"};
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        putchar(','); string_json(names[i]); putchar(':'); string_json(getenv(names[i]));
    }
    fputs(",\"args\":[", stdout);
    for (int i = 0; i < argc; ++i) {
        if (i) putchar(',');
        string_json(argv[i]);
    }
    puts("]}");
    return 0;
}
''', encoding="utf-8")
        subprocess.run(["/usr/bin/xcrun", "clang", "-Wall", "-Wextra", "-Werror", str(source),
                        "-o", str(runtime)], check=True, capture_output=True)
        with patch.object(build_app, "PROJECT", project):
            build_app.build_app(cls.template, runtime, bundled_python=True, minimum_macos="15.0")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="case-", dir=self.root)
        self.addCleanup(temporary.cleanup)
        self.case_root = Path(temporary.name)
        self.bundle = self.case_root / "Portable App.app"
        shutil.copytree(self.template, self.bundle, symlinks=True)

    def launch(self, *arguments, environment=None):
        self.assertTrue(arguments, "Explicit CLI arguments avoid writing any user GUI log")
        return subprocess.run([str(self.bundle / "Contents/MacOS/Shiliao"), *arguments],
                              env=environment, capture_output=True, text=True, timeout=10)

    def test_portable_metadata_contains_no_host_runtime_path(self):
        info = plistlib.loads((self.bundle / "Contents/Info.plist").read_bytes())
        self.assertEqual(info["ShiliaoBundledPython"], "python/bin/python3")
        self.assertNotIn("ShiliaoPythonExecutable", info)
        self.assertEqual(info["LSMinimumSystemVersion"], "15.0")
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(self.bundle)],
                       check=True, capture_output=True)

    def test_native_exec_preserves_arguments_and_clears_external_python_environment(self):
        arguments = ["--scheduled", "--state-dir", str(self.case_root / "Synthetic State"),
                     "literal;$(not-a-command)", "Unicode-测试", "quoted\"argument"]
        process = subprocess.Popen([str(self.bundle / "Contents/MacOS/Shiliao"), *arguments],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, "PYTHONHOME": "/synthetic/external/runtime",
                                        "PYTHONPATH": "/synthetic/external/modules", "PYTHONNOUSERSITE": "0",
                                        "PYTHONDONTWRITEBYTECODE": "0"})
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, error)
        report = json.loads(output)
        self.assertEqual(report["pid"], process.pid)
        self.assertEqual(report["umask"], 0o077)
        self.assertIsNone(report["PYTHONHOME"])
        self.assertIsNone(report["PYTHONPATH"])
        self.assertEqual(report["PYTHONNOUSERSITE"], "1")
        self.assertEqual(report["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(report["args"][2:], arguments)
        self.assertEqual(Path(report["args"][0]).resolve(), self.bundle / "Contents/Resources/python/bin/python3")
        self.assertEqual(Path(report["args"][1]).resolve(), self.bundle / "Contents/Resources/app/app.py")

    def test_relocated_bundle_executes_only_its_internal_runtime(self):
        old = self.bundle
        self.bundle = self.case_root / "Moved Portable App.app"
        shutil.move(old, self.bundle)
        result = self.launch("--no-browser", environment={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["cwd"], str(self.bundle / "Contents/Resources/app"))
        self.assertEqual(Path(report["args"][0]).resolve(), self.bundle / "Contents/Resources/python/bin/python3")
        self.assertEqual(Path(report["args"][1]).resolve(), self.bundle / "Contents/Resources/app/app.py")
        self.assertFalse(old.exists())

    def test_internal_runtime_symlink_remains_valid_after_move(self):
        runtime = self.bundle / "Contents/Resources/python/bin/python3"
        runtime.rename(runtime.with_name("python3.11"))
        runtime.symlink_to("python3.11")
        old = self.bundle
        self.bundle = self.case_root / "Moved Linked Runtime.app"
        shutil.move(old, self.bundle)
        result = self.launch("--scheduled")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)["args"][0]).resolve(),
                         self.bundle / "Contents/Resources/python/bin/python3.11")

    def test_rejects_runtime_executable_symlink_to_external_file(self):
        runtime = self.bundle / "Contents/Resources/python/bin/python3"
        outside = self.case_root / "external-synthetic-runtime"
        shutil.move(runtime, outside)
        runtime.symlink_to(outside)
        result = self.launch("--scheduled")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime must remain inside the application", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_rejects_entire_runtime_directory_symlink_to_external_folder(self):
        runtime = self.bundle / "Contents/Resources/python"
        outside = self.case_root / "external-synthetic-runtime-folder"
        shutil.move(runtime, outside)
        runtime.symlink_to(outside, target_is_directory=True)
        result = self.launch("--scheduled")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime must remain inside the application", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_rejects_arbitrary_portable_runtime_location_in_metadata(self):
        path = self.bundle / "Contents/Info.plist"
        info = plistlib.loads(path.read_bytes())
        info["ShiliaoBundledPython"] = "../../../external-synthetic-runtime"
        path.write_bytes(plistlib.dumps(info))
        result = self.launch("--scheduled")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid bundled runtime location", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
