#!/usr/bin/env python3
"""Sign an arm64 release feed and create a real changed-file delta package."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import sys
import tempfile
import urllib.parse
import zipfile

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from vault.updater import APP_ID, PUBLIC_KEY_B64, canonical, file_hash, tree_digest, verify_release, version_tuple, zip_inventory


def _info(archive, root):
    with zipfile.ZipFile(archive, metadata_encoding="utf-8") as source:
        info = plistlib.loads(source.read(root + "/Contents/Info.plist"))
    if info.get("CFBundleIdentifier") != APP_ID:
        raise ValueError("Update archive has the wrong application identity")
    version_tuple(info.get("CFBundleShortVersionString"))
    return info["CFBundleShortVersionString"]


def build_update(base_zip, target_zip, output_dir, private_key, release_url, sequence, *, expires_days=90):
    """Use a caller-supplied Ed25519 key object; private key generation is external."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("An Ed25519 signing key is required")
    if type(sequence) is not int or sequence < 1 or type(expires_days) is not int or not 1 <= expires_days <= 366:
        raise ValueError("Invalid update sequence or expiry interval")
    base_zip, target_zip, output_dir = Path(base_zip), Path(target_zip), Path(output_dir)
    if base_zip.is_symlink() or target_zip.is_symlink() or output_dir.is_symlink():
        raise ValueError("Release inputs and outputs cannot be symbolic links")
    base_root, before = zip_inventory(base_zip)
    target_root, after = zip_inventory(target_zip)
    old_version, version = _info(base_zip, base_root), _info(target_zip, target_root)
    if version_tuple(version) <= version_tuple(old_version):
        raise ValueError("Target version must be newer than the base")
    old = {row["path"]: row for row in before}
    changed = [row for row in after if row["type"] == "file" and old.get(row["path"]) != row]
    output_dir.mkdir(parents=True, exist_ok=True)
    delta_name = f"shiliao-{old_version}-to-{version}-arm64.delta.zip"
    names = [delta_name, "update-arm64.json", delta_name + ".sha256", "update-arm64.json.sha256"]
    if any((output_dir / name).exists() or (output_dir / name).is_symlink() for name in names):
        raise ValueError("Update release outputs already exist")
    prefix = release_url.rstrip("/") + "/"
    with tempfile.TemporaryDirectory(prefix=".update-release-", dir=output_dir) as temporary:
        temporary = Path(temporary)
        patch = temporary / delta_name
        with zipfile.ZipFile(target_zip, metadata_encoding="utf-8") as source, zipfile.ZipFile(patch, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for row in changed:
                member = zipfile.ZipInfo("payload/" + row["path"], (1980, 1, 1, 0, 0, 0))
                member.create_system = 3
                member.external_attr = (stat.S_IFREG | row["mode"]) << 16
                member.compress_type = zipfile.ZIP_DEFLATED
                with source.open(target_root + "/" + row["path"]) as inp, archive.open(member, "w") as out:
                    import shutil
                    shutil.copyfileobj(inp, out, 1024 * 1024)
        asset = lambda path: {"url": prefix + urllib.parse.quote(path.name), "bytes": path.stat().st_size, "sha256": file_hash(path)}
        signed = {"schema": 1, "app_id": APP_ID, "architecture": "arm64", "version": version,
                  "sequence": sequence, "expires": (datetime.now(timezone.utc) + timedelta(days=expires_days)).replace(microsecond=0).isoformat(),
                  "full": asset(target_zip), "target": {"files": after, "tree_sha256": tree_digest(after)},
                  "deltas": [{"base_version": old_version, "base_tree_sha256": tree_digest(before), "asset": asset(patch)}]}
        envelope = {"signed": signed, "signature": base64.b64encode(private_key.sign(canonical(signed))).decode("ascii")}
        public = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        verify_release(envelope, base64.b64encode(public).decode("ascii"))
        (temporary / "update-arm64.json").write_bytes(canonical(envelope) + b"\n")
        for name in (delta_name, "update-arm64.json"):
            (temporary / (name + ".sha256")).write_text(file_hash(temporary / name) + "  " + name + "\n")
        published = []
        try:
            for name in names:
                os.link(temporary / name, output_dir / name)
                published.append(output_dir / name)
        except BaseException:
            for path in published:
                path.unlink()
            raise
    return {"base_version": old_version, "version": version, "changed_files": len(changed),
            "target_files": len(after), "delta_bytes": signed["deltas"][0]["asset"]["bytes"],
            "full_bytes": signed["full"]["bytes"], "delta_smaller": signed["deltas"][0]["asset"]["bytes"] < signed["full"]["bytes"]}


def load_private_key(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.resolve().is_relative_to(PROJECT):
        raise ValueError("The signing key must be a regular file outside the source repository")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("The signing key must be readable only by its owner")
    data = path.read_bytes()
    try:
        key = Ed25519PrivateKey.from_private_bytes(data) if len(data) == 32 else serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid Ed25519 signing key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Invalid Ed25519 signing key")
    return key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--release-url", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--expires-days", type=int, default=90)
    args = parser.parse_args()
    from cryptography.hazmat.primitives import serialization
    key = load_private_key(args.private_key)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if public != base64.b64decode(PUBLIC_KEY_B64, validate=True):
        raise ValueError("The release signing key does not match the embedded update public key")
    result = build_update(args.base, args.target, args.output, key,
                          args.release_url, args.sequence, expires_days=args.expires_days)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
