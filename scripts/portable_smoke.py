#!/usr/bin/env python3
"""Verify a relocated portable app using synthetic data and isolated state."""

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


CHECK_NAMES = (
    "portable_layout", "relocated_copy", "isolated_runtime", "python_dependencies",
    "bundled_tools", "silk_conversion", "sqlcipher_export", "archive_roundtrip",
    "offline_media", "scheduler_launcher", "native_startup", "http_status",
    "http_static", "http_search", "http_media", "scheduled_disabled",
)
CORE_CHECKS = CHECK_NAMES[2:10]
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class SmokeFailure(RuntimeError):
    """A failure carrying only a fixed public check name."""

    def __init__(self, check):
        self.check = check
        super().__init__(check)


def _within(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def _require(condition, check):
    if not condition:
        raise SmokeFailure(check)


def _environment(home, temporary):
    return {
        "HOME": str(home), "PATH": SYSTEM_PATH, "TMPDIR": str(temporary),
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }


def _stop(process):
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def _command(arguments, environment, cwd, timeout=180, input_data=None):
    """Capture subprocess diagnostics without exposing paths or credentials."""
    process = subprocess.Popen(arguments, cwd=cwd, env=environment, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, _ = process.communicate(input_data, timeout=timeout)
        return process.returncode, stdout
    finally:
        _stop(process)


def _core_checks(resources, workspace):
    """Run inside the copied app's real CPython, not the host interpreter."""
    checks = {name: False for name in CORE_CHECKS}
    counts = {"messages": 0, "objects": 0}
    current = "isolated_runtime"
    try:
        resources, workspace = resources.resolve(), workspace.resolve()
        python_root = resources / "python"
        _require(_within(sys.executable, python_root) and _within(sys.prefix, python_root)
                 and _within(sys.exec_prefix, python_root), current)
        _require(Path.home().resolve() == (workspace / "home").resolve(), current)
        _require(os.environ.get("PATH") == SYSTEM_PATH and not os.environ.get("PYTHONPATH"), current)
        sys.path.insert(0, str(resources / "app"))
        checks[current] = True

        current = "python_dependencies"
        dependencies = ("PIL.Image", "PIL._imaging", "cryptography.hazmat.primitives.ciphers",
                        "cryptography.hazmat.bindings._rust", "zstandard", "pilk")
        for name in dependencies:
            module = importlib.import_module(name)
            _require(_within(module.__file__, python_root), current)
        from PIL import Image
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        import pilk
        import zstandard
        sample = b"Portable synthetic validation"
        _require(zstandard.ZstdDecompressor().decompress(zstandard.ZstdCompressor().compress(sample)) == sample, current)
        cipher = Cipher(algorithms.AES(bytes(16)), modes.ECB())
        encrypted = cipher.encryptor().update(bytes(16))
        _require(cipher.decryptor().update(encrypted) == bytes(16), current)
        checks[current] = True

        current = "bundled_tools"
        from vault.runtime import find_tool
        tools = {name: find_tool(name) for name in ("ffmpeg", "ffprobe", "sqlcipher")}
        for name, executable in tools.items():
            _require(executable is not None and _within(executable, resources / "tools"), current)
            result = subprocess.run([executable, "-version"], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False, timeout=20)
            _require(result.returncode == 0 and bool(result.stdout), current)
        checks[current] = True

        current = "silk_conversion"
        import math
        import struct
        from vault.media_codecs import convert_audio, prepare_video
        fixtures = workspace / "fixtures"
        fixtures.mkdir(mode=0o700)
        pcm, silk, flac = (fixtures / name for name in ("samples.pcm", "voice.silk", "voice.flac"))
        samples = b"".join(struct.pack("<h", int(4000 * math.sin(index * .1))) for index in range(4800))
        pcm.write_bytes(samples)
        pilk.encode(str(pcm), str(silk), pcm_rate=24000, tencent=True)
        info = convert_audio(silk, flac)
        _require(info["source_silk"] and info["lossless"] and flac.stat().st_size > 0, current)
        probed = subprocess.run([tools["ffprobe"], "-v", "error", "-show_entries", "stream=codec_name",
                                 "-of", "json", str(flac)], capture_output=True, timeout=20, check=True)
        _require(json.loads(probed.stdout)["streams"][0]["codec_name"] == "flac", current)
        checks[current] = True

        current = "sqlcipher_export"
        import secrets
        import sqlite3
        from vault.sqlcipher_export import export_database
        encrypted_db, plaintext_db = fixtures / "encrypted.db", fixtures / "plain.db"
        key = secrets.token_hex(32)
        sql = (f'''PRAGMA key="x'{key}'";\nPRAGMA cipher_compatibility=4;\n'''
               "CREATE TABLE samples(id INTEGER PRIMARY KEY,value TEXT);\n"
               "INSERT INTO samples VALUES(1,'Synthetic SQLite fixture');\n")
        result = subprocess.run([tools["sqlcipher"], "-batch", "-bail", "-init", os.devnull, str(encrypted_db)],
                                input=sql.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        _require(result.returncode == 0, current)
        before = hashlib.sha256(encrypted_db.read_bytes()).digest()
        export_database(encrypted_db, plaintext_db, key)
        with sqlite3.connect(plaintext_db) as database:
            _require(database.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1, current)
        _require(hashlib.sha256(encrypted_db.read_bytes()).digest() == before, current)
        checks[current] = True

        current = "archive_roundtrip"
        from vault.library import Library
        from vault.readable_archive import create_archive
        from vault.archive_import import archive_import_info, import_archive
        original = Library(workspace / "original-state")
        original.import_file(json.dumps({"title": "Portable synthetic conversation", "messages": [
            {"sender": "Synthetic person", "text": "Portable keyword 中文", "timestamp": "2024-06-01T12:00:00Z"},
            {"sender": "Synthetic self", "text": "Portable media", "timestamp": "2024-06-01T12:01:00Z", "is_self": True},
        ]}, ensure_ascii=False).encode(), "synthetic.json")
        with original.connect() as database:
            conversation_id = database.execute("SELECT id FROM conversations").fetchone()[0]
            message_ids = [row[0] for row in database.execute("SELECT id FROM messages ORDER BY id")]
        picture, avatar, video_source, video = (fixtures / name for name in ("image.png", "avatar.png", "video-source.mp4", "video.mp4"))
        Image.new("RGB", (32, 24), (30, 90, 160)).save(picture)
        Image.new("RGB", (16, 16), (90, 160, 30)).save(avatar)
        generated = subprocess.run([tools["ffmpeg"], "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                                    "color=blue:size=32x24:rate=10", "-t", "0.3", "-c:v", "mpeg4",
                                    "-pix_fmt", "yuv420p", "-y", str(video_source)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        _require(generated.returncode == 0, current)
        _require(prepare_video(video_source, video)["lossless"], current)
        assets = {}
        for kind, mime, path in (("image", "image/png", picture), ("audio", "audio/flac", flac),
                                 ("video", "video/mp4", video), ("original", "audio/silk", silk)):
            asset = original.media.store_file(path, kind, mime)
            original.media.link(message_ids[1], kind, asset)
            assets[asset] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}
        avatar_id = original.media.store_file(avatar, "image", "image/png")
        identity = hashlib.sha256(b"portable:synthetic-person").hexdigest()
        original.set_avatar(identity, avatar_id)
        assets[avatar_id] = {"sha256": hashlib.sha256(avatar.read_bytes()).hexdigest(), "size": avatar.stat().st_size}
        with original.connect() as database:
            database.execute("INSERT INTO conversation_avatars VALUES(?,?)", (conversation_id, identity))
            database.execute("INSERT INTO message_avatars VALUES(?,?)", (message_ids[0], identity))
        archive = Path(create_archive(original, workspace / "archives")["path"])
        target = Library(workspace / "state")
        preview = archive_import_info(archive, target)
        imported = import_archive(target, archive)
        _require(preview["summary"]["messages"] == 2 and imported["added"]["messages"] == 2, current)
        _require(import_archive(target, archive)["duplicate"], current)
        checks[current] = True

        current = "offline_media"
        archive.unlink()
        shutil.rmtree(original.state_dir)
        shutil.rmtree(fixtures)
        for asset, expected in assets.items():
            with target.media.open(asset) as (stream, metadata):
                data = stream.read()
                _require(len(data) == expected["size"] and hashlib.sha256(data).hexdigest() == expected["sha256"], current)
        messages = target.messages(conversation_id)["messages"]
        _require(any(row["avatar_url"] == "/api/media/" + avatar_id for row in messages), current)
        _require(target.search("keyword")["total"] == 1, current)
        checks[current] = True

        current = "scheduler_launcher"
        from vault.scheduler import Scheduler
        launcher = resources.parent / "MacOS/Shiliao"
        scheduler = Scheduler(target.state_dir, resources / "app", agents_dir=workspace / "home/Library/LaunchAgents")
        _require(scheduler._arguments() == [str(launcher), "--scheduled", "--state-dir", str(target.state_dir)], current)
        _require(not scheduler.plist_path.exists(), current)
        checks[current] = True
        counts = {"messages": target.stats()["messages"], "objects": len(assets)}
        handoff = {"assets": assets, "conversation_id": conversation_id, "avatar_id": avatar_id}
        (workspace / "synthetic-handoff.json").write_text(json.dumps(handoff))
        os.chmod(workspace / "synthetic-handoff.json", 0o600)
        return {"ok": True, "checks": checks, "counts": counts}
    except Exception:
        return {"ok": False, "checks": checks, "counts": counts, "failed_check": current}


def _request(runtime, path, *, authorized=True):
    parsed = urlsplit(runtime["url"])
    _require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1", "native_startup")
    token = parse_qs(parsed.fragment)["token"][0]
    headers = {"Authorization": "Bearer " + token} if authorized else {}
    request = Request(f'http://127.0.0.1:{runtime["port"]}' + path, headers=headers)
    with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
        return response.read(), dict(response.headers)


def run_smoke(bundle):
    """Return safe check results; never operate on the installed app or state."""
    checks = {name: False for name in CHECK_NAMES}
    counts = {"messages": 0, "objects": 0}
    current, server = "portable_layout", None
    try:
        bundle = Path(bundle).expanduser().resolve(strict=True)
        with (bundle / "Contents/Info.plist").open("rb") as source:
            original_info = plistlib.load(source)
        _require(original_info.get("ShiliaoBundledPython") == "python/bin/python3", current)
        _require((bundle / "Contents/Resources/python/bin/python3").is_file(), current)
        checks[current] = True
        with tempfile.TemporaryDirectory(prefix="shiliao-portable-smoke-") as temporary:
            workspace = Path(temporary).resolve()
            current = "relocated_copy"
            copied = workspace / "带空格的 test folder" / "拾聊 Portable.app"
            copied.parent.mkdir()
            shutil.copytree(bundle, copied, symlinks=True)
            resources = copied / "Contents/Resources"
            python = resources / "python/bin/python3"
            launcher = copied / "Contents/MacOS/Shiliao"
            _require(_within(python, resources / "python") and os.access(python, os.X_OK), current)
            _require(launcher.is_file() and os.access(launcher, os.X_OK), current)
            home, scratch = workspace / "home", workspace / "tmp"
            home.mkdir(mode=0o700)
            scratch.mkdir(mode=0o700)
            environment = _environment(home, scratch)
            checks[current] = True
            current = "isolated_runtime"
            # The harness stays outside the app and imports only the copied payload.
            harness = workspace / "portable-harness.py"
            shutil.copyfile(Path(__file__).resolve(), harness)
            code, output = _command([str(python), "-s", str(harness), "--internal-core", str(resources), str(workspace)],
                                    environment, workspace, timeout=300)
            result = json.loads(output.splitlines()[-1])
            for name in CORE_CHECKS:
                checks[name] = result.get("checks", {}).get(name) is True
            counts = {name: int(result.get("counts", {}).get(name, 0)) for name in counts}
            if code or not result.get("ok"):
                raise SmokeFailure(result.get("failed_check") if result.get("failed_check") in CORE_CHECKS else current)

            current = "native_startup"
            state = workspace / "state"
            server = subprocess.Popen([str(launcher), "--no-browser", "--state-dir", str(state), "--port", "0"],
                                      cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            runtime = None
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and server.poll() is None:
                try:
                    candidate = json.loads((state / "runtime.json").read_text())
                    _require(candidate["pid"] == server.pid, current)
                    raw, _ = _request(candidate, "/api/config")
                    _require(json.loads(raw)["schedule_enabled"] is False, current)
                    runtime = candidate
                    break
                except (OSError, ValueError, KeyError, URLError):
                    time.sleep(.2)
            _require(runtime is not None and server.poll() is None, current)
            checks[current] = True

            current = "http_status"
            raw, _ = _request(runtime, "/api/status")
            status = json.loads(raw)
            _require(status["version"] == original_info["CFBundleShortVersionString"], current)
            _require(status["library"]["messages"] == 2 and status["config"]["schedule_enabled"] is False, current)
            _require(status["wechat_reader"]["configured"] is False, current)
            checks[current] = True

            current = "http_static"
            for path, marker in (("/", b"<!DOCTYPE html>"), ("/app.js", b""), ("/app.css", b"")):
                raw, headers = _request(runtime, path, authorized=False)
                _require(bool(raw) and marker.lower() in raw.lower(), current)
                policy = headers.get("Content-Security-Policy", "")
                _require("script-src 'self'" in policy and "'unsafe-inline'" not in policy, current)
            checks[current] = True

            current = "http_search"
            raw, _ = _request(runtime, "/api/search?" + urlencode({"q": "keyword"}))
            result = json.loads(raw)
            _require(result["total"] == 1 and len(result["messages"]) == 1 and result["messages"][0]["avatar_url"], current)
            checks[current] = True

            current = "http_media"
            handoff = json.loads((workspace / "synthetic-handoff.json").read_text())
            for asset, expected in handoff["assets"].items():
                data, _ = _request(runtime, "/api/media/" + asset)
                _require(len(data) == expected["size"] and hashlib.sha256(data).hexdigest() == expected["sha256"], current)
            checks[current] = True
            _stop(server)
            server = None

            current = "scheduled_disabled"
            code, output = _command([str(launcher), "--scheduled", "--state-dir", str(state)], environment, workspace, timeout=40)
            _require(code == 0 and json.loads(output.splitlines()[-1])["status"] == "not_due", current)
            _require(not (home / "Library/LaunchAgents/com.shiliao.backup.plist").exists(), current)
            _require(not (state / "schedule-result.json").exists(), current)
            checks[current] = True
        return {"ok": True, "checks": checks, "counts": counts}
    except Exception as error:
        failed = error.check if isinstance(error, SmokeFailure) else current
        return {"ok": False, "checks": checks, "counts": counts, "failed_check": failed}
    finally:
        _stop(server)


def main():
    parser = argparse.ArgumentParser(description="Validate a portable app using isolated synthetic data")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--internal-core", nargs=2, metavar=("RESOURCES", "WORKSPACE"), help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.internal_core:
        result = _core_checks(*(Path(value) for value in arguments.internal_core))
    elif arguments.bundle:
        result = run_smoke(arguments.bundle)
    else:
        parser.error("--bundle is required")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
