#!/usr/bin/env python3
"""Prepare a private debug copy, then explicitly initialize local WeChat access."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from app import DEFAULT_STATE, default_config, read_json, wechat_running, write_json
from vault.wechat_access import database_root, save_verified_keys


def run(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"{Path(arguments[0]).name} failed; no original application files were changed")
    return result.stdout or result.stderr


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def prepare(state_dir):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Initialization currently supports native Apple Silicon macOS only")
    if os.geteuid() == 0:
        raise RuntimeError("Run as the logged-in user, without sudo")
    original = Path("/Applications/WeChat.app")
    core = original / "Contents/Resources/wechat.dylib"
    workspace = state_dir / "wechat-access"
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.chmod(0o700)
    metadata_file = workspace / "prepared.json"
    shadow = workspace / "WeChat-Read.app"
    if shadow.exists() or metadata_file.exists():
        raise RuntimeError("A prepared copy already exists; inspect it before preparing another")
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(original)])
    signature = run(["/usr/bin/codesign", "-dvv", str(original)])
    original_digest = digest(core)
    total_size = sum(path.stat().st_size for path in original.rglob("*") if path.is_file())
    if shutil.disk_usage(workspace).free < total_size + 100 * 1024 * 1024:
        raise RuntimeError("Not enough space for the temporary application copy")
    run(["/usr/bin/ditto", str(original), str(shadow)])
    run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(shadow)])
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(shadow)])
    if digest(core) != original_digest or run(["/usr/bin/codesign", "-dvv", str(original)]) != signature:
        raise RuntimeError("Original application identity changed during preparation; initialization stopped")
    metadata = {"original": str(original), "shadow": str(shadow), "core_digest": original_digest,
                "shadow_core_digest": digest(shadow / "Contents/Resources/wechat.dylib"),
                "signature": signature, "prepared": True}
    write_json(metadata_file, metadata)
    print(json.dumps({"status": "prepared", "original_unchanged": True, "temporary_copy": str(shadow)}))


def capture(state_dir, acknowledge):
    if not acknowledge:
        raise RuntimeError("Review the temporary-copy workflow and pass --acknowledge-debug-copy before starting it")
    if wechat_running():
        raise RuntimeError("Quit WeChat normally first. No existing process was stopped")
    workspace = state_dir / "wechat-access"
    metadata = read_json(workspace / "prepared.json")
    if not metadata or not metadata.get("prepared"):
        raise RuntimeError("Run the prepare command first")
    original = Path(metadata["original"])
    shadow = Path(metadata["shadow"])
    if original != Path("/Applications/WeChat.app") or shadow != workspace / "WeChat-Read.app":
        raise RuntimeError("Prepared application paths do not match this workspace")
    core = original / "Contents/Resources/wechat.dylib"
    if digest(core) != metadata["core_digest"]:
        raise RuntimeError("WeChat changed since preparation; initialize against the current version")
    if digest(shadow / "Contents/Resources/wechat.dylib") != metadata.get("shadow_core_digest"):
        raise RuntimeError("The prepared copy changed; initialization stopped")
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(shadow)])
    config = read_json(state_dir / "config.json", None) or default_config()
    root = database_root(config["source"])
    output = workspace / "keys.json"
    if output.exists():
        raise RuntimeError("Captured keys already exist; they will not be overwritten")
    lldb_path = run(["/usr/bin/lldb", "-P"]).strip()
    environment = dict(os.environ, PYTHONPATH=lldb_path)
    command = ["/usr/bin/python3", str(PROJECT / "vendor/wechat-access/capture_keys.py"),
               "--exe", str(shadow / "Contents/MacOS/WeChat"), "--db-root", str(root),
               "--out", str(output), "--timeout", "300"]
    print("The temporary WeChat window will open. Complete phone confirmation if requested. Do not chat in the temporary copy.", flush=True)
    result = None
    try:
        result = subprocess.run(command, env=environment, timeout=330)
    finally:
        unchanged = digest(core) == metadata["core_digest"] and run(["/usr/bin/codesign", "-dvv", str(original)]) == metadata["signature"]
        if not unchanged:
            raise RuntimeError("Original application identity changed unexpectedly; restore manually")
        if wechat_running():
            print("A WeChat process is still running. Quit the temporary copy before opening the official application.", flush=True)
        else:
            registration = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
            subprocess.run([registration, "-u", str(shadow)], capture_output=True, timeout=30)
            shadow.rename(workspace / "WeChat-Read.disabled")
            subprocess.Popen([str(original / "Contents/MacOS/WeChat")], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    if output.exists():
        saved = save_verified_keys(output.read_bytes(), str(root), state_dir)
        print(json.dumps({"status": "keys_saved_privately", **saved, "original_unchanged": True}))
    if result is None or result.returncode:
        raise RuntimeError("Initialization did not fully complete; check the local metadata output before retrying")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "capture"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--acknowledge-debug-copy", action="store_true")
    args = parser.parse_args()
    state_dir = args.state_dir.expanduser().resolve()
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.umask(0o077)
    if args.action == "prepare":
        prepare(state_dir)
    else:
        capture(state_dir, args.acknowledge_debug_copy)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Initialization stopped: {error}", file=sys.stderr)
        raise SystemExit(1)
