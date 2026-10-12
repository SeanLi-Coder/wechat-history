#!/usr/bin/env python3
"""Build a reviewed source-only ZIP and checksum without reading user state."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import zipfile


PROJECT = Path(__file__).resolve().parent.parent
MAX_SOURCE_BYTES = 8 * 1024 * 1024
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
RELEASE_FILES = (
    ".github/workflows/ci.yml",
    ".github/workflows/portable.yml",
    ".gitignore",
    "README.md",
    "Start.command",
    "THIRD_PARTY_NOTICES.md",
    "app.py",
    "assets/AppIcon.icns",
    "docs/RELEASING.md",
    "requirements-dev.txt",
    "requirements-portable.txt",
    "requirements.txt",
    "scripts/browser_smoke.py",
    "scripts/build_app.py",
    "scripts/build_portable.py",
    "scripts/build_tools.py",
    "scripts/build_update.py",
    "scripts/bundle_tools.py",
    "scripts/native_launcher.m",
    "scripts/package_release.py",
    "scripts/portable_smoke.py",
    "scripts/python_licenses.py",
    "scripts/search_smoke.py",
    "scripts/test_key_capture.py",
    "scripts/wechat_initialize.py",
    "scripts/wheel_licenses.py",
    "static/app.css",
    "static/app.js",
    "static/index.html",
    "tests/test_app.py",
    "tests/test_archive_import.py",
    "tests/test_avatars.py",
    "tests/test_backup.py",
    "tests/test_bundle_tools.py",
    "tests/test_library.py",
    "tests/test_media_codecs.py",
    "tests/test_media_http.py",
    "tests/test_media_import.py",
    "tests/test_media_store.py",
    "tests/test_native_launcher.py",
    "tests/test_package_release.py",
    "tests/test_portable_build.py",
    "tests/test_python_licenses.py",
    "tests/test_raw_archive.py",
    "tests/test_readable_archive.py",
    "tests/test_runtime.py",
    "tests/test_scheduler.py",
    "tests/test_search.py",
    "tests/test_sqlcipher_export.py",
    "tests/test_update_routes.py",
    "tests/test_updater.py",
    "tests/test_wechat_access.py",
    "tests/test_wechat_avatars.py",
    "tests/test_wechat_crypto.py",
    "tests/test_wechat_media.py",
    "tests/test_wechat_reader.py",
    "tests/test_wheel_licenses.py",
    "vault/__init__.py",
    "vault/archive_import.py",
    "vault/backup.py",
    "vault/library.py",
    "vault/media_codecs.py",
    "vault/media_import.py",
    "vault/media_store.py",
    "vault/raw_archive.py",
    "vault/readable_archive.py",
    "vault/runtime.py",
    "vault/scheduler.py",
    "vault/sqlcipher_export.py",
    "vault/updater.py",
    "vault/wechat_access.py",
    "vault/wechat_avatars.py",
    "vault/wechat_crypto.py",
    "vault/wechat_media.py",
    "vault/wechat_reader.py",
    "vendor/wechat-access/LICENSE",
    "vendor/wechat-access/README.md",
    "vendor/wechat-access/capture_keys.py",
)
SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("service_token", re.compile(r"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-(?:proj-)?[A-Za-z0-9_-]{24,})")),
    ("personal_home", re.compile(r"/(?:Users|home)/[^/\s'\"`<>]+")),
    ("database_key", re.compile(r"[\"']enc_key[\"']\s*:\s*[\"'][0-9a-fA-F]{64}[\"']")),
    ("credential_assignment", re.compile(r"[\"'](?:api_key|access_token|refresh_token|password)[\"']\s*:\s*[\"'][A-Za-z0-9_+/=-]{24,}[\"']")),
    ("bearer_token", re.compile(r"[\"']Bearer [A-Za-z0-9_-]{32,}[\"']")),
)
SYNTHETIC_ACCOUNTS = {"wxid_alice", "wxid_myself"}


class ReleaseError(ValueError):
    """Report an allowlisted filename and fixed rule, never a matched value."""


def _signature(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _source_path(project, name):
    relative = PurePosixPath(name)
    if (relative.is_absolute() or relative.as_posix() != name
            or any(part in {"", ".", ".."} for part in relative.parts)):
        raise ReleaseError("The release allowlist contains an unsafe path")
    current = project
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            value = current.lstat()
        except FileNotFoundError:
            raise ReleaseError(f"Missing release source: {name}") from None
        if stat.S_ISLNK(value.st_mode):
            raise ReleaseError(f"Symbolic links cannot be released: {name}")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(value.st_mode):
            raise ReleaseError(f"Invalid release source directory: {name}")
    if not stat.S_ISREG(value.st_mode) or not 0 < value.st_size <= MAX_SOURCE_BYTES:
        raise ReleaseError(f"Invalid release source file: {name}")
    return current, value


def _audit_content(name, data):
    if name == "assets/AppIcon.icns":
        if len(data) < 8 or data[:4] != b"icns" or int.from_bytes(data[4:8], "big") != len(data):
            raise ReleaseError(f"Invalid icon container: {name}")
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ReleaseError(f"Release source is not UTF-8 text: {name}") from None
    if "\x00" in text:
        raise ReleaseError(f"Binary content in release source: {name}")
    for rule, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            raise ReleaseError(f"Release audit rejected {name}: {rule}")
    for match in re.finditer(r"\bwxid_[A-Za-z0-9_]{4,}", text):
        account = match.group()
        if account not in SYNTHETIC_ACCOUNTS and not account.startswith(("wxid_synthetic", "wxid_example", "wxid_test")):
            raise ReleaseError(f"Release audit rejected {name}: account_identifier")


def collect_sources(project):
    project = Path(project).expanduser().resolve(strict=True)
    if len(set(RELEASE_FILES)) != len(RELEASE_FILES):
        raise ReleaseError("The release allowlist contains duplicate paths")
    collected = []
    for name in sorted(RELEASE_FILES):
        path, before = _source_path(project, name)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                if _signature(os.fstat(stream.fileno())) != _signature(before):
                    raise ReleaseError(f"Release source changed: {name}")
                data = stream.read(MAX_SOURCE_BYTES + 1)
                if (len(data) != before.st_size or _signature(os.fstat(stream.fileno())) != _signature(before)
                        or _signature(path.lstat()) != _signature(before)):
                    raise ReleaseError(f"Release source changed: {name}")
        except OSError:
            raise ReleaseError(f"Release source could not be read: {name}") from None
        _audit_content(name, data)
        collected.append((name, data, _signature(before)))
    return project, collected


def _version(collected, requested):
    source = next(data for name, data, _ in collected if name == "vault/__init__.py")
    try:
        module = ast.parse(source.decode("utf-8"))
        declared = next(node.value.value for node in module.body
                        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets))
    except (SyntaxError, StopIteration, AttributeError):
        raise ReleaseError("The source version must be an explicit __version__ string") from None
    if not isinstance(declared, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.-][A-Za-z0-9]+)*", declared):
        raise ReleaseError("The source version is invalid")
    if requested is not None and requested.removeprefix("v") != declared:
        raise ReleaseError("The requested release version differs from vault/__init__.py")
    return declared


def _manifest(collected, version):
    return {"format": "shiliao-source-release", "version": version,
            "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                      for name, data, _ in collected]}


def _entry(name, executable=False):
    entry = zipfile.ZipInfo(name, ZIP_TIME)
    entry.create_system = 3
    entry.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
    entry.compress_type = zipfile.ZIP_DEFLATED
    return entry


def build_release(project=PROJECT, output_dir=None, version=None):
    project, collected = collect_sources(project)
    version = _version(collected, version)
    manifest = _manifest(collected, version)
    output_dir = Path(output_dir or project / "dist/releases").expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    name = f"shiliao-{version}-source.zip"
    archive_path, checksum_path = output_dir / name, output_dir / (name + ".sha256")
    if os.path.lexists(archive_path) or os.path.lexists(checksum_path):
        raise ReleaseError("Release outputs already exist; choose an empty output directory")
    with tempfile.TemporaryDirectory(prefix=".shiliao-source-", dir=output_dir) as temporary:
        temporary = Path(temporary)
        archive, checksum = temporary / name, temporary / (name + ".sha256")
        prefix = f"shiliao-{version}/"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
            for relative, data, _ in collected:
                executable = relative == "Start.command" or relative.startswith("scripts/") and relative.endswith(".py")
                package.writestr(_entry(prefix + relative, executable), data, compresslevel=9)
            package.writestr(_entry(prefix + "RELEASE-MANIFEST.json"),
                             (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(), compresslevel=9)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum.write_text(f"{digest}  {name}\n", encoding="ascii")
        for target in (archive, checksum):
            target.chmod(0o644)
            with target.open("rb") as stream:
                os.fsync(stream.fileno())
        for relative, _, signature in collected:
            _, current = _source_path(project, relative)
            if _signature(current) != signature:
                raise ReleaseError(f"Release source changed before publication: {relative}")
        published = []
        try:
            for source, target in ((archive, archive_path), (checksum, checksum_path)):
                os.link(source, target)
                published.append((target, source.stat().st_ino))
        except OSError:
            for target, inode in published:
                if target.exists() and target.stat().st_ino == inode:
                    target.unlink()
            raise ReleaseError("Release outputs could not be published without overwriting files") from None
    return {"version": version, "file_count": len(collected), "source_bytes": sum(len(data) for _, data, _ in collected),
            "archive": str(archive_path), "checksum": str(checksum_path), "sha256": digest,
            "archive_bytes": archive_path.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--version")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Audit all allowed sources without creating files")
    mode.add_argument("--list", action="store_true", help="Print only the reviewed relative source paths")
    args = parser.parse_args()
    try:
        if args.check or args.list:
            _, collected = collect_sources(args.project)
            version = _version(collected, args.version)
            if args.list:
                for name, _, _ in collected:
                    print(name)
            else:
                print(json.dumps({"ok": True, "version": version, "file_count": len(collected),
                                  "source_bytes": sum(len(data) for _, data, _ in collected)}))
        else:
            print(json.dumps(build_release(args.project, args.output_dir, args.version)))
    except (OSError, ReleaseError) as error:
        message = str(error) if isinstance(error, ReleaseError) else "Release filesystem operation failed"
        print(json.dumps({"ok": False, "error": message}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
