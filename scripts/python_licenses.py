#!/usr/bin/env python3
"""Collect pinned Python standalone dependency notices without runtime files."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import subprocess
import tarfile
import tempfile


PYTHON_RELEASE = "20261009"
PYTHON_VERSION = "3.11.17"
FULL_ARCHIVES = {
    "arm64": ("aarch64", "a9c2933a3846c44daa7168dc3ee31a6c6b254964451f113bbff1e87cfa8146af"),
    "x86_64": ("x86_64", "f058edc651a4bd233bc3c38499fefd74faf7cc4c804bfb0afce0f0349065bc37"),
}
MAX_MEMBERS = 100_000
MAX_TAR_BYTES = 2 * 1024 ** 3
MAX_METADATA_BYTES = 4 * 1024 ** 2
MAX_LICENSE_BYTES = 2 * 1024 ** 2
MAX_LICENSE_TOTAL = 32 * 1024 ** 2


def source_info(architecture):
    if architecture not in FULL_ARCHIVES:
        raise ValueError("Unsupported Python architecture")
    target, expected = FULL_ARCHIVES[architecture]
    filename = f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-{target}-apple-darwin-pgo+lto-full.tar.zst"
    url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PYTHON_RELEASE}/{filename.replace('+', '%2B')}"
    return {"filename": filename, "url": url, "sha256": expected}


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_full_archive(architecture, cache):
    info = source_info(architecture)
    cache = Path(cache)
    if cache.is_symlink():
        raise ValueError("Python archive cache cannot be a symbolic link")
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / info["filename"]
    if path.is_symlink():
        raise ValueError("Cached Python archive cannot be a symbolic link")
    if path.exists():
        if not path.is_file() or _digest(path) != info["sha256"]:
            raise ValueError("Cached Python full archive failed its checksum")
        return path
    descriptor, name = tempfile.mkstemp(prefix=".python-license-download-", dir=cache)
    os.close(descriptor)
    partial = Path(name)
    try:
        result = subprocess.run([
            "/usr/bin/curl", "--fail", "--location", "--retry", "3", "--silent", "--show-error",
            "--proto", "=https", "--proto-redir", "=https", "--max-filesize", str(128 * 1024 ** 2),
            info["url"], "--output", str(partial),
        ], capture_output=True)
        if result.returncode:
            raise RuntimeError("Python full archive download failed")
        if _digest(partial) != info["sha256"]:
            raise ValueError("Downloaded Python full archive failed its checksum")
        os.link(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    return path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Python metadata contains duplicate keys")
        result[key] = value
    return result


def _relative_path(value, *, license_only=False):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Python metadata contains an unsafe path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Python metadata contains an unsafe path")
    if license_only and (len(path.parts) < 2 or path.parts[0] != "licenses"):
        raise ValueError("Python metadata license path is outside the license directory")
    return path.as_posix()


def _license_references(metadata, architecture):
    target = FULL_ARCHIVES[architecture][0] + "-apple-darwin"
    if (not isinstance(metadata, dict) or str(metadata.get("version")) != "8"
            or metadata.get("python_version") != PYTHON_VERSION
            or metadata.get("target_triple") != target
            or metadata.get("build_options") != "pgo+lto"):
        raise ValueError("Python license metadata version or architecture differs from the pinned runtime")
    root_license = metadata.get("license_path")
    if not isinstance(root_license, str):
        raise ValueError("Python metadata is missing its root license")
    build = metadata.get("build_info")
    extensions = build.get("extensions") if isinstance(build, dict) else None
    if not isinstance(extensions, dict):
        raise ValueError("Python metadata is missing extension licenses")
    for name in ("_ssl", "_ctypes", "_tkinter"):
        variants = extensions.get(name)
        if (not isinstance(variants, list) or not variants
                or any(not isinstance(row, dict) or not row.get("license_paths", row.get("license_path"))
                       for row in variants)):
            raise ValueError("Python metadata is missing a required extension license")
    references = {}

    def walk(item):
        if isinstance(item, dict):
            for key, value in item.items():
                if key in {"license_path", "license_paths"}:
                    values = [value] if isinstance(value, str) else value
                    if not isinstance(values, list) or not values:
                        raise ValueError("Python metadata contains invalid license references")
                    for name in values:
                        name = _relative_path(name, license_only=True)
                        references.setdefault(name, []).append(item)
                else:
                    walk(value)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(metadata)
    return references


def _system_zlib_reference(path, owners):
    # PBS 20261009 advertises both zlib variants although macOS uses system zlib.
    # The full macOS archive contains no zlib-ng binary or corresponding notice.
    return path == "licenses/LICENSE.zlib-ng.txt" and all(
        owner.get("init_fn") == "PyInit_zlib"
        and owner.get("links") == [{"name": "z", "system": True}]
        and "licenses/LICENSE.zlib.txt" in owner.get("license_paths", [])
        for owner in owners
    )


def extract_license_tar(stream, destination, architecture):
    """Validate a decompressed full tar stream and publish only declared notices."""
    source_info(architecture)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Python license destination already exists")
    seen, candidates = set(), {}
    metadata_bytes = None
    total = license_total = 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            name = _relative_path(member.name.rstrip("/"))
            if name in seen:
                raise ValueError("Python full archive contains duplicate members")
            seen.add(name)
            if len(seen) > MAX_MEMBERS or not name.startswith("python/"):
                raise ValueError("Python full archive contains an unsafe member")
            if member.size < 0:
                raise ValueError("Python full archive contains an invalid member size")
            total += member.size
            if total > MAX_TAR_BYTES:
                raise ValueError("Python full archive is too large")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError("Python full archive contains a special member")
            if member.issym() or member.islnk():
                target = (posixpath.join(posixpath.dirname(name), member.linkname)
                          if member.issym() else member.linkname)
                normalized = posixpath.normpath(target)
                if member.linkname.startswith("/") or not normalized.startswith("python/"):
                    raise ValueError("Python full archive link escapes its root")
            is_metadata = name == "python/PYTHON.json"
            is_license = name.startswith("python/licenses/") and not member.isdir()
            if not (is_metadata or is_license):
                continue
            if not member.isfile():
                raise ValueError("Python metadata and license members must be regular files")
            limit = MAX_METADATA_BYTES if is_metadata else MAX_LICENSE_BYTES
            if member.size <= 0 or member.size > limit:
                raise ValueError("Python metadata or license member is empty or too large")
            contents = archive.extractfile(member).read(limit + 1)
            if len(contents) != member.size:
                raise ValueError("Python metadata or license member is truncated")
            if is_metadata:
                metadata_bytes = contents
            else:
                license_total += len(contents)
                if license_total > MAX_LICENSE_TOTAL:
                    raise ValueError("Python license collection is too large")
                candidates[name.removeprefix("python/")] = contents
    if metadata_bytes is None:
        raise ValueError("Python full archive is missing its metadata")
    try:
        metadata = json.loads(metadata_bytes, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Python full archive metadata is invalid") from exc
    references = _license_references(metadata, architecture)
    omitted = []
    selected = {"PYTHON.json": metadata_bytes}
    for name, owners in sorted(references.items()):
        if name not in candidates:
            if _system_zlib_reference(name, owners):
                omitted.append({"path": name, "reason": "Not distributed: metadata identifies only Apple's system zlib; the pinned macOS archive contains no zlib-ng component."})
                continue
            raise ValueError("Python full archive is missing a declared license")
        selected[name] = candidates[name]
    info = source_info(architecture)
    report = {"version": 1, "release": PYTHON_RELEASE, "python_version": PYTHON_VERSION,
              "architecture": architecture, "source": {"url": info["url"], "sha256": info["sha256"]},
              "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                        for name, data in sorted(selected.items())], "omitted_system_references": omitted}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".python-licenses-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        for name, contents in selected.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
            path.chmod(0o644)
        (stage / "LICENSES.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        destination.mkdir(mode=0o755)
        try:
            for path in stage.iterdir():
                path.rename(destination / path.name)
        except BaseException:
            shutil.rmtree(destination)
            raise
    return report


def gather_python_licenses(architecture, destination, cache):
    """Fetch the pinned full distribution and collect notices using zstandard."""
    if Path(destination).exists() or Path(destination).is_symlink():
        raise ValueError("Python license destination already exists")
    archive = download_full_archive(architecture, cache)
    # This helper runs in the bundled runtime after its dependencies are installed.
    import zstandard
    with archive.open("rb") as compressed, zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
        return extract_license_tar(stream, destination, architecture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=sorted(FULL_ARCHIVES), required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    arguments = parser.parse_args()
    result = gather_python_licenses(arguments.arch, arguments.destination, arguments.cache)
    print(json.dumps({"ok": True, "license_files": len(result["files"]) - 1,
                      "system_references": len(result["omitted_system_references"])}))


if __name__ == "__main__":
    main()
