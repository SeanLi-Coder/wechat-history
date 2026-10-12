#!/usr/bin/env python3
"""Retain missing native dependency notices for the pinned portable wheels.

This complements, rather than replaces, the upstream wheel license files.
Only reviewed license members are read; source archives are never extracted.
"""

import argparse
import email.parser
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import tomllib


EXPECTED = {"cryptography": "44.0.2", "zstandard": "0.23.0", "pilk": "0.2.4"}
SOURCES = {
    "zstandard": (
        "https://files.pythonhosted.org/packages/ed/f6/2ac0287b442160a89d726b17a9184a4c615bb5237db763791a7fd16d9df1/zstandard-0.23.0.tar.gz",
        "b2d8c62d08e7255f68f7a740bae85b3c9b8e5466baa9cbf7f57f1cde0ac6bc09",
    ),
    "pilk": (
        "https://files.pythonhosted.org/packages/66/bb/938dd697b6bc2d851ffec4ffe82ed20078e58bd0ef049e84f4d038d6f991/pilk-0.2.4.tar.gz",
        "d4a1bcf93dc6ef5e95e0cfd728ed4ef4d49f9c0476d70816fecbe456cc762e7f",
    ),
    "cryptography": (
        "https://files.pythonhosted.org/packages/cd/25/4ce80c78963834b8a9fd1cc1266be5ed8d1840785c0f2e1b73b8d128d505/cryptography-44.0.2.tar.gz",
        "c63454aa261a0cf0c5b4718349629793e9e634993538db841165b3df74f37ec0",
    ),
    "openssl": (
        "https://raw.githubusercontent.com/openssl/openssl/openssl-3.4.1/LICENSE.txt",
        "7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a",
    ),
}
LOCK_SHA256 = "c31afaadf604050f60cad3086bec5394f46851c6fd992ee4447bc655c82b06e9"
# Runtime crates reachable in cryptography 44.0.2 on the two supported macOS
# architectures. Build-only tools/proc macros and non-64-bit portable-atomic
# are excluded. Version and crate hashes come from the pinned Cargo.lock.
RUST_LICENSES = {
    "asn1": ("LICENSE",),
    "base64": ("LICENSE-APACHE", "LICENSE-MIT"),
    "bitflags": ("LICENSE-APACHE", "LICENSE-MIT"),
    "cfg-if": ("LICENSE-APACHE", "LICENSE-MIT"),
    "foreign-types": ("LICENSE-APACHE", "LICENSE-MIT"),
    "foreign-types-shared": ("LICENSE-APACHE", "LICENSE-MIT"),
    "itoa": ("LICENSE-APACHE", "LICENSE-MIT"),
    "libc": ("LICENSE-APACHE", "LICENSE-MIT"),
    "memoffset": ("LICENSE",),
    "once_cell": ("LICENSE-APACHE", "LICENSE-MIT"),
    "openssl": ("LICENSE",),
    "openssl-sys": ("LICENSE-MIT",),
    "pem": ("LICENSE.md",),
    "pyo3": ("LICENSE-APACHE", "LICENSE-MIT"),
    "pyo3-ffi": ("LICENSE-APACHE", "LICENSE-MIT"),
    "self_cell": ("LICENSE",),
    "unindent": ("LICENSE-APACHE", "LICENSE-MIT"),
}
MAX_TEXT_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024


def _sha(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _fetch(url, expected, cache, filename=None):
    """Use system curl so the build does not require Python CA packages."""
    if not url.startswith("https://"):
        raise ValueError("License sources must use HTTPS")
    name = filename or url.rsplit("/", 1)[-1]
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("Invalid license cache filename")
    path = cache / name
    if path.is_symlink():
        raise ValueError("License cache cannot contain symbolic links")
    if path.exists():
        if not path.is_file() or _sha(path) != expected:
            raise ValueError("Cached license source checksum differs from the pinned value")
        return path
    fd, partial = tempfile.mkstemp(prefix=".wheel-license-", dir=cache)
    os.close(fd)
    partial = Path(partial)
    try:
        subprocess.run(
            ["/usr/bin/curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https", "--retry", "3", "--silent",
             "--show-error", "--max-time", "120", "--max-filesize", str(MAX_DOWNLOAD_BYTES),
             url, "--output", str(partial)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if partial.stat().st_size > MAX_DOWNLOAD_BYTES or _sha(partial) != expected:
            raise ValueError("Downloaded license source failed its pinned checksum")
        os.link(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    return path


def _member(archive, name):
    with tarfile.open(archive, "r:gz") as source:
        matches = [item for item in source if item.name == name]
        if len(matches) != 1 or not matches[0].isfile():
            raise ValueError("Required license source member is missing or unsafe")
        if not 0 < matches[0].size <= MAX_TEXT_BYTES:
            raise ValueError("License text exceeds the allowed size")
        with source.extractfile(matches[0]) as stream:
            content = stream.read(MAX_TEXT_BYTES + 1)
    content.decode("utf-8")
    return content


def _installed_versions(python_root):
    result = {}
    for metadata in python_root.glob("lib/python*/site-packages/*.dist-info/METADATA"):
        if metadata.is_symlink() or not metadata.resolve().is_relative_to(python_root):
            raise ValueError("Wheel metadata escapes the Python runtime")
        info = email.parser.Parser().parsestr(metadata.read_text(encoding="utf-8"))
        name = (info.get("Name") or "").lower().replace("_", "-")
        if name in EXPECTED:
            if name in result:
                raise ValueError("Duplicate installed wheel metadata")
            result[name] = info.get("Version")
    if result != EXPECTED:
        raise ValueError("Installed wheel versions differ from the reviewed license sources")
    return result


def _collect_materials(cache):
    """Return text plus provenance; checksums are transitive through Cargo.lock."""
    result = []

    def add(content, relative_path, component, version, url, source_sha256, member=None):
        content.decode("utf-8")
        if not 0 < len(content) <= MAX_TEXT_BYTES:
            raise ValueError("Invalid license text size")
        result.append((content, {
            "path": relative_path, "component": component, "version": version,
            "source_url": url, "source_sha256": source_sha256,
            "source_member": member, "sha256": hashlib.sha256(content).hexdigest(),
        }))

    url, checksum = SOURCES["zstandard"]
    archive = _fetch(url, checksum, cache)
    member = "zstandard-0.23.0/zstd/LICENSE"
    add(_member(archive, member), "zstandard-0.23.0/zstd-LICENSE", "zstd", "1.5.6",
        url, checksum, member)

    url, checksum = SOURCES["pilk"]
    archive = _fetch(url, checksum, cache)
    member = "pilk-0.2.4/src/interface/SKP_Silk_SDK_API.h"
    header = _member(archive, member)
    end = header.find(b"*/")
    if not header.startswith(b"/*") or end < 0:
        raise ValueError("Pinned SILK header does not contain the expected license")
    notice = header[:end + 2] + b"\n"
    if b"Skype Limited" not in notice or b"PATENT RIGHTS" not in notice:
        raise ValueError("Pinned SILK license is incomplete")
    add(notice, "pilk-0.2.4/SILK-LICENSE", "SILK SDK", "bundled-with-pilk-0.2.4",
        url, checksum, member)

    url, checksum = SOURCES["openssl"]
    path = _fetch(url, checksum, cache, "openssl-3.4.1-LICENSE.txt")
    add(path.read_bytes(), "cryptography-44.0.2/OpenSSL-3.4.1-LICENSE.txt",
        "OpenSSL", "3.4.1", url, checksum)

    url, checksum = SOURCES["cryptography"]
    archive = _fetch(url, checksum, cache)
    member = "cryptography-44.0.2/Cargo.lock"
    lock = _member(archive, member)
    if hashlib.sha256(lock).hexdigest() != LOCK_SHA256:
        raise ValueError("Cryptography dependency lock differs from the reviewed version")
    packages = {item["name"]: item for item in tomllib.loads(lock.decode())["package"]
                if item.get("source") == "registry+https://github.com/rust-lang/crates.io-index"}
    add(lock, "cryptography-44.0.2/Cargo.lock", "cryptography Rust dependencies", "44.0.2",
        url, checksum, member)
    for name, licenses in RUST_LICENSES.items():
        package = packages[name]
        version, checksum = package["version"], package["checksum"]
        crate = f"{name}-{version}"
        url = f"https://static.crates.io/crates/{name}/{crate}.crate"
        archive = _fetch(url, checksum, cache)
        for filename in licenses:
            member = f"{crate}/{filename}"
            add(_member(archive, member), f"cryptography-44.0.2/rust/{crate}/{filename}",
                name, version, url, checksum, member)
    return result


def collect_wheel_licenses(python_root, cache_dir):
    """Publish reviewed notices under python_root/licenses/wheels atomically.

    Returns the same provenance dictionary written to LICENSES.json. Repeating
    an identical collection is harmless; existing differing material is refused.
    No installed package or dist-info license is modified.
    """
    python_root, cache = Path(python_root), Path(cache_dir)
    if python_root.is_symlink() or cache.is_symlink():
        raise ValueError("Runtime and license cache must be real directories")
    python_root = python_root.resolve(strict=True)
    versions = _installed_versions(python_root)
    cache.mkdir(parents=True, exist_ok=True)
    parent = python_root / "licenses"
    destination = parent / "wheels"
    if parent.is_symlink() or destination.is_symlink():
        raise ValueError("License output cannot be a symbolic link")
    parent.mkdir(exist_ok=True)
    materials = _collect_materials(cache)
    manifest = {"version": 1, "packages": versions, "files": [entry for _, entry in materials]}
    with tempfile.TemporaryDirectory(prefix=".wheel-licenses-", dir=parent) as temporary:
        staged = Path(temporary) / "wheels"
        staged.mkdir()
        for content, entry in materials:
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid license output path")
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content)
        (staged / "LICENSES.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if destination.exists():
            expected = {path.relative_to(staged): _sha(path) for path in staged.rglob("*") if path.is_file()}
            actual = {}
            for path in destination.rglob("*"):
                if path.is_symlink():
                    raise ValueError("Existing license output contains a symbolic link")
                if path.is_file():
                    actual[path.relative_to(destination)] = _sha(path)
            if actual != expected:
                raise ValueError("Existing wheel licenses differ; rebuild in a clean staging directory")
        else:
            os.rename(staged, destination)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-root", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    manifest = collect_wheel_licenses(args.python_root, args.cache)
    print(json.dumps({"ok": True, "packages": len(manifest["packages"]),
                      "license_files": len(manifest["files"])}, sort_keys=True))


if __name__ == "__main__":
    main()
