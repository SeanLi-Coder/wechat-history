#!/usr/bin/env python3
"""Build a native macOS trampoline without changing installed launch agents."""

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile


PROJECT = Path(__file__).resolve().parent.parent
BUNDLE_ID = "local.shiliao.archive"


def build_app(bundle=None, python_executable=None, *, bundled_python=False, minimum_macos=None):
    bundle = Path(bundle or PROJECT / "dist/拾聊.app").expanduser().absolute()
    runtime = Path(python_executable or sys.executable).expanduser().resolve(strict=True)
    if not runtime.is_file() or not os.access(runtime, os.X_OK):
        raise ValueError("The Python runtime must be an existing executable.")
    compiler = subprocess.run(["/usr/bin/xcrun", "--find", "clang"], capture_output=True,
                              text=True, check=True).stdout.strip()
    sdk = subprocess.run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"],
                         capture_output=True, text=True, check=True).stdout.strip()
    # Compile first so a compiler error cannot replace an existing launcher.
    with tempfile.TemporaryDirectory(prefix="shiliao-native-build-") as temporary:
        executable = Path(temporary) / "Shiliao"
        target = ["-mmacosx-version-min=" + minimum_macos] if minimum_macos else []
        subprocess.run([compiler, "-isysroot", sdk, *target, "-fobjc-arc", "-Os", "-Wall", "-Wextra", "-Werror",
                        "-framework", "Foundation", str(PROJECT / "scripts/native_launcher.m"),
                        "-o", str(executable)], check=True)
        macos = bundle / "Contents/MacOS"
        resources = bundle / "Contents/Resources"
        macos.mkdir(parents=True, exist_ok=True)
        resources.mkdir(parents=True, exist_ok=True)
        icon = PROJECT / "assets/AppIcon.icns"
        if icon.exists():
            shutil.copy2(icon, resources / "AppIcon.icns")
        payload = resources / "app"
        payload.mkdir(exist_ok=True)
        shutil.copy2(PROJECT / "app.py", payload / "app.py")
        for folder in ("vault", "static"):
            shutil.copytree(PROJECT / folder, payload / folder, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for cache in payload.rglob("__pycache__"):
            if cache.is_symlink():
                cache.unlink()
            elif cache.is_dir():
                shutil.rmtree(cache)
        for bytecode in payload.rglob("*.pyc"):
            bytecode.unlink()
        configuration = {
            "CFBundleName": "拾聊", "CFBundleDisplayName": "拾聊", "CFBundleExecutable": "Shiliao",
            "CFBundleIdentifier": BUNDLE_ID, "CFBundleVersion": "10",
            "CFBundleIconFile": "AppIcon", "CFBundleShortVersionString": "0.8.0",
            "CFBundlePackageType": "APPL", "LSUIElement": True, "NSHighResolutionCapable": True,
            "NSAppDataUsageDescription": "读取你选择的微信账号文件夹，在本机备份并阅读聊天与媒体；不会修改微信原始数据。",
            "NSDocumentsFolderUsageDescription": "Save local WeChat backups to the selected folder.",
            "NSAppleEventsUsageDescription": "Show a local folder picker for backup settings.",
        }
        if bundled_python:
            configuration["ShiliaoBundledPython"] = "python/bin/python3"
        else:
            configuration["ShiliaoPythonExecutable"] = str(runtime)
        if minimum_macos:
            configuration["LSMinimumSystemVersion"] = minimum_macos
        with (bundle / "Contents/Info.plist").open("wb") as output:
            plistlib.dump(configuration, output)
        launcher = macos / "Shiliao"
        staged = macos / ".Shiliao-new"
        shutil.copy2(executable, staged)
        staged.chmod(0o755)
        os.replace(staged, launcher)
    # Ad-hoc signing supports local attribution, but is not a persistent
    # distribution identity and does not grant privacy permissions.
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID,
                    str(bundle)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(bundle)], check=True)
    return bundle


def main():
    parser = argparse.ArgumentParser(description="Build the native Shiliao macOS launcher")
    parser.add_argument("--output", type=Path, help="Build into a separate .app directory")
    parser.add_argument("--python", type=Path, help="Use this existing Python executable")
    args = parser.parse_args()
    print(build_app(args.output, args.python))


if __name__ == "__main__":
    main()
