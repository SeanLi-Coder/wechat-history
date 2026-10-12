#!/usr/bin/env python3
"""Shiliao: a loopback-only WeChat archive and readable export library."""

import argparse
import fcntl
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

from vault import __version__
from vault.backup import BackupEngine, discover_sources
from vault.library import Library, MAX_IMPORT_BYTES
from vault.scheduler import Scheduler
from vault.wechat_access import import_account, key_status, save_verified_keys


PROJECT = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / "Library/Application Support/Shiliao"
TIMEZONE = ZoneInfo("Asia/Shanghai")


class OperationBusy(ValueError):
    code = "operation_busy"


def serialized_operation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.operation_lock():
            return method(self, *args, **kwargs)
    return wrapped


def now():
    return datetime.now(TIMEZONE)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def wechat_running():
    if sys.platform != "darwin":
        return False
    for name in ("WeChat", "Weixin", "WeChatAppEx"):
        result = subprocess.run(["/usr/bin/pgrep", "-x", name], capture_output=True, timeout=5)
        if result.returncode == 0:
            return True
        if result.returncode not in (0, 1):
            raise RuntimeError("Cannot determine whether WeChat is running")
    return False


def default_config():
    sources = [source for source in discover_sources() if source.get("exists")]
    return {"source": sources[0]["path"] if sources else "",
            "destination": str(Path.home() / "Documents/拾聊备份"),
            "schedule_enabled": False, "schedule_time": "03:00", "interval": "daily",
            "include_media": True, "timezone": "Asia/Shanghai"}


def schedule_due(config, last_success, current=None):
    current = current or now()
    if not config.get("schedule_enabled"):
        return False
    current = current.replace(tzinfo=TIMEZONE) if current.tzinfo is None else current.astimezone(TIMEZONE)

    def local_time(value):
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=TIMEZONE) if parsed.tzinfo is None else parsed.astimezone(TIMEZONE)

    previous = None
    if last_success and last_success.get("source") == config["source"] and last_success.get("destination") == config["destination"]:
        try:
            previous = local_time(last_success["finished_at"])
        except (KeyError, TypeError, ValueError):
            previous = None
    if config.get("interval") == "hourly":
        return previous is None or (current - previous).total_seconds() >= 3600
    try:
        activated = local_time(config["schedule_activated_at"])
    except (KeyError, TypeError, ValueError):
        activated = None
    hour, minute = map(int, config["schedule_time"].split(":"))
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if current < target:
        if activated is None and previous is None:
            return False
        target -= timedelta(days=1)
    if activated is not None and activated > target:
        return False
    return previous is None or previous < target


class Application:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.config_path = self.state_dir / "config.json"
        if not self.config_path.exists():
            write_json(self.config_path, default_config())
        self.engine = BackupEngine(self.state_dir)
        self.library = Library(self.state_dir)
        self.archive_library = None
        self.reader_source = {"archived": False, "name": "本机记录"}
        self.scheduler = Scheduler(self.state_dir, PROJECT)
        self.job = None
        self.job_lock = threading.Lock()
        self.settings_lock = threading.Lock()
        self.operation_local = threading.local()
        self.token = secrets.token_urlsafe(32)

    @contextmanager
    def operation_lock(self):
        if getattr(self.operation_local, "active", False):
            yield
            return
        descriptor = os.open(self.state_dir / "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OperationBusy("另一项备份或导入正在进行，请等待完成后再试。") from None
            self.operation_local.active = True
            try:
                yield
            finally:
                self.operation_local.active = False
        finally:
            os.close(descriptor)

    def config(self):
        config = read_json(self.config_path)
        if not isinstance(config, dict):
            raise ValueError("Settings file is invalid")
        return config

    @property
    def reader_library(self):
        return self.archive_library or self.library

    def save_config(self, changes):
        with self.settings_lock:
            config = self.config()
            previous = dict(config)
            for key in ("source", "destination", "schedule_enabled", "schedule_time", "interval"):
                if key in changes:
                    config[key] = changes[key]
            if type(config["schedule_enabled"]) is not bool:
                raise ValueError("schedule_enabled must be boolean")
            if config["interval"] not in ("daily", "hourly"):
                raise ValueError("Choose a daily or hourly schedule")
            if not isinstance(config["schedule_time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", config["schedule_time"]):
                raise ValueError("Schedule time must be HH:MM")
            if not all(isinstance(config[k], str) and config[k].strip() for k in ("source", "destination")):
                raise ValueError("Choose source and destination folders")
            source = Path(config["source"]).expanduser()
            destination = Path(config["destination"]).expanduser()
            if not source.is_absolute() or not destination.is_absolute():
                raise ValueError("Folder paths must be absolute")
            require_existing_source = config["schedule_enabled"] or config["source"] != previous["source"]
            if source.is_symlink() or (require_existing_source and not source.is_dir()):
                raise ValueError("Source must be an existing folder, not a symbolic link")
            source, destination = source.resolve(), destination.resolve()
            if source == Path(source.anchor) or destination == source or source in destination.parents:
                raise ValueError("Backup destination cannot be inside the source folder")
            if destination.exists() and not destination.is_dir():
                raise ValueError("Destination must be a folder")
            if source == self.state_dir or source in self.state_dir.parents:
                raise ValueError("Source must not contain the application state folder")
            config.update(source=str(source), destination=str(destination))
            if config["schedule_enabled"] and (
                not previous.get("schedule_enabled")
                or any(config[key] != previous.get(key) for key in ("interval", "schedule_time"))
                or not config.get("schedule_activated_at")
            ):
                config["schedule_activated_at"] = now().astimezone(TIMEZONE).isoformat()
            write_json(self.config_path, config)
            try:
                if config["schedule_enabled"]:
                    self.scheduler.install()
                elif previous.get("schedule_enabled"):
                    self.scheduler.uninstall()
            except Exception:
                write_json(self.config_path, previous)
                raise
            return config

    def snapshots(self, config=None):
        config = config or self.config()
        return self.engine.list_snapshots(config["destination"])

    def status(self):
        config = self.config()
        with self.job_lock:
            job = json.loads(json.dumps(self.job))
        return {"app_name": "拾聊", "version": __version__, "config": config,
                "sources": discover_sources(), "snapshots": self.snapshots(config), "job": job,
                "schedule": self.scheduler.status(), "library": self.reader_library.stats(),
                "media": self.reader_library.media.stats(), "reader_source": self.reader_source,
                "wechat_reader": key_status(self.state_dir),
                "wechat_running": wechat_running(),
                "last_scheduled_result": read_json(self.state_dir / "schedule-result.json", None)}

    def record_success(self, config, result):
        write_json(self.state_dir / "last-success.json", {"source": config["source"], "destination": config["destination"],
                   "finished_at": now().isoformat(), "snapshot_id": result["id"]})

    @staticmethod
    def assert_wechat_closed():
        if wechat_running():
            raise ValueError("请从微信菜单正常退出微信后再读取，关闭窗口不等于退出。")

    @serialized_operation
    def read_wechat(self, config, progress=None):
        snapshot_ready = False
        def report(value):
            nonlocal snapshot_ready
            if not progress:
                return
            snapshot_ready = snapshot_ready or value.get("snapshot_ready", False)
            if "done_databases" in value:
                progress({"done_files": value["done_databases"], "total_files": value["total_databases"],
                          "snapshot_ready": snapshot_ready,
                          "current_file": f"已整理 {value.get('done_messages', 0)} 条消息"})
            else:
                progress({**value, "snapshot_ready": snapshot_ready})
        return import_account(config["source"], self.state_dir, self.library, self.assert_wechat_closed, report)

    @serialized_operation
    def backup_with_reader(self, config, progress=None):
        result = self.engine.create(config["source"], config["destination"], progress=progress)
        self.record_success(config, result)
        if key_status(self.state_dir)["configured"]:
            try:
                result["reader"] = self.read_wechat(config, progress)
            except Exception as error:
                result["reader_error"] = str(error)
        if self.library.stats()["messages"]:
            try:
                from vault.readable_archive import create_archive
                result["readable_archive"] = create_archive(self.library, config["destination"], progress)
            except Exception:
                result["reader_error"] = "原始备份已保存，可读压缩归档未能生成，请重试保存可读归档。"
        return result

    def start_job(self, kind, snapshot_id=None, archive_path=None):
        config = self.config()
        with self.job_lock:
            if self.job and self.job["status"] == "running":
                raise ValueError("Another operation is already running")
            self.job = {"id": secrets.token_hex(8), "type": kind, "status": "running", "progress": {}}
            job_id = self.job["id"]

        def progress(value):
            with self.job_lock:
                self.job["progress"] = value

        def worker():
            try:
                if kind == "backup":
                    result = self.backup_with_reader(config, progress)
                elif kind == "wechat_import":
                    result = self.read_wechat(config, progress)
                elif kind == "reader_export":
                    from vault.readable_archive import create_archive
                    with self.operation_lock():
                        result = create_archive(self.library, config["destination"], progress)
                elif kind == "reader_open":
                    from vault.readable_archive import open_archive
                    opened = open_archive(archive_path, self.state_dir)
                    self.archive_library = opened
                    self.reader_source = {"archived": True, "name": Path(archive_path).name}
                    result = opened.stats()
                else:
                    result = self.engine.verify(snapshot_id, config["destination"])
                    if not result.get("ok"):
                        with self.job_lock:
                            self.job.update(status="error", error="归档校验未通过，请查看文件完整性。", result=result)
                        return
                with self.job_lock:
                    self.job.update(status="done", result=result)
            except Exception as error:
                with self.job_lock:
                    self.job.update(status="error", error=str(error), code=getattr(error, "code", "operation_failed"))
        threading.Thread(target=worker, daemon=True, name="archive-worker").start()
        return {"job_id": job_id}

    def run_scheduled(self):
        config = self.config()
        last_success = read_json(self.state_dir / "last-success.json", None)
        if not schedule_due(config, last_success):
            return {"status": "not_due"}
        result = {"attempted_at": now().isoformat()}
        try:
            if wechat_running():
                result.update(status="deferred", reason="WeChat is running; retry in about 15 minutes")
            else:
                snapshot = self.backup_with_reader(config)
                result.update(status="done", snapshot_id=snapshot["id"])
                if snapshot.get("reader_error"):
                    result["reader_error"] = snapshot["reader_error"]
        except OperationBusy:
            result.update(status="deferred", reason="Another archive operation is running; retry in about 15 minutes")
        except Exception as error:
            result.update(status="error", error=str(error))
        write_json(self.state_dir / "schedule-result.json", result)
        return result


class Handler(BaseHTTPRequestHandler):
    server_version = "Shiliao"

    @property
    def app(self):
        return self.server.app

    def log_message(self, format, *args):
        pass

    def response(self, value, status=200, content_type="application/json; charset=utf-8"):
        data = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if getattr(self, "authenticated", False):
            self.send_header("Set-Cookie", f"shiliao_media={self.app.token}; HttpOnly; SameSite=Strict; Path=/api/media/")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def authorize(self, require_token=True):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            self.response({"error": "Invalid host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in hosts}:
            self.response({"error": "Cross-origin access is blocked"}, 403)
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.response({"error": "Cross-site access is blocked"}, 403)
            return False
        token = self.headers.get("Authorization", "")
        if require_token and not hmac.compare_digest(token.encode("utf-8"), ("Bearer " + self.app.token).encode("ascii")):
            self.response({"error": "请从拾聊应用重新打开页面。", "code": "unauthorized"}, 401)
            return False
        if require_token:
            self.authenticated = True
        return True

    def serve_media(self, path, head=False):
        if not self.authorize(False):
            return
        bearer = self.headers.get("Authorization", "")
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie["shiliao_media"].value if "shiliao_media" in cookie else ""
        except Exception:
            value = ""
        if not (hmac.compare_digest(bearer.encode(), ("Bearer " + self.app.token).encode())
                or hmac.compare_digest(value.encode(), self.app.token.encode())):
            return self.response({"error": "Media authorization is required"}, 401)
        asset_id = path.removeprefix("/api/media/")
        try:
            with self.app.reader_library.media.open(asset_id) as (stream, info):
                size = info["byte_size"]
                start, end, status = 0, size - 1, 200
                request_range = self.headers.get("Range")
                if request_range:
                    matched = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", request_range)
                    if matched and (matched[1] or matched[2]):
                        if not matched[1]:
                            start = max(0, size - int(matched[2]))
                        else:
                            start = int(matched[1])
                            end = min(size - 1, int(matched[2])) if matched[2] else size - 1
                    else:
                        start = size
                    if start >= size or end < start:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
                self.send_response(status)
                self.send_header("Content-Type", info["mime_type"])
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if not head:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (KeyError, OSError, ValueError):
            return self.response({"error": "Media is unavailable"}, 404)

    def do_HEAD(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/media/"):
            return self.serve_media(path, head=True)
        self.response({"error": "Not found"}, 404)

    def body(self, limit=64 * 1024, raw=False):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer encoding is not supported")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > limit:
            raise ValueError("Request is too large")
        self.connection.settimeout(120)
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("Incomplete request body")
        if raw:
            return data
        result = json.loads(data or b"{}")
        if not isinstance(result, dict):
            raise ValueError("Request body must be an object")
        return result

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/media/"):
            return self.serve_media(parsed.path)
        if not self.authorize(parsed.path.startswith("/api/")):
            return
        try:
            query = parse_qs(parsed.query)
            if parsed.path in ("/", "/index.html", "/app.css", "/app.js", "/static/app.css", "/static/app.js"):
                name = "index.html" if parsed.path == "/" else parsed.path.rsplit("/", 1)[-1]
                content_type = {"index.html": "text/html; charset=utf-8", "app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}[name]
                return self.response((PROJECT / "static" / name).read_bytes(), content_type=content_type)
            if parsed.path == "/api/status":
                return self.response(self.app.status())
            if parsed.path == "/api/config":
                return self.response(self.app.config())
            if parsed.path == "/api/conversations":
                return self.response(self.app.reader_library.conversations(query.get("q", [""])[0]))
            if parsed.path == "/api/search":
                return self.response(self.app.reader_library.search(query.get("q", [""])[0],
                    int(query.get("offset", ["0"])[0]), int(query.get("limit", ["50"])[0])))
            match = re.fullmatch(r"/api/conversations/([a-zA-Z0-9-]+)/context", parsed.path)
            if match:
                return self.response(self.app.reader_library.context(match[1],
                    int(query.get("message_id", ["0"])[0]), int(query.get("limit", ["100"])[0])))
            match = re.fullmatch(r"/api/conversations/([a-zA-Z0-9-]+)/messages", parsed.path)
            if match:
                return self.response(self.app.reader_library.messages(match[1], query.get("q", [""])[0],
                    int(query.get("offset", ["0"])[0]), int(query.get("limit", ["100"])[0])))
            match = re.fullmatch(r"/api/snapshots/([a-zA-Z0-9_.-]+)", parsed.path)
            if match:
                return self.response(self.app.engine.read_manifest(match[1], self.app.config()["destination"]))
            self.response({"error": "Not found"}, 404)
        except KeyError:
            self.response({"error": "Not found"}, 404)
        except (ValueError, OSError, RuntimeError) as error:
            self.response({"error": str(error)}, 400)

    def do_POST(self):
        if not self.authorize():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/import":
                filename = unquote(self.headers.get("X-Filename", "export.json"))
                data = self.body(MAX_IMPORT_BYTES, raw=True)
                with self.app.operation_lock():
                    return self.response(self.app.library.import_file(data, filename))
            if path == "/api/wechat/keys":
                data = self.body(4 * 1024 * 1024, raw=True)
                with self.app.operation_lock():
                    return self.response(save_verified_keys(data, self.app.config()["source"], self.app.state_dir))
            body = self.body()
            if path == "/api/config":
                return self.response(self.app.save_config(body))
            if path == "/api/backup":
                return self.response(self.app.start_job("backup"), 202)
            if path == "/api/wechat/import":
                return self.response(self.app.start_job("wechat_import"), 202)
            if path == "/api/reader/export":
                return self.response(self.app.start_job("reader_export"), 202)
            if path == "/api/reader/archive":
                archive_path = body.get("path")
                if not isinstance(archive_path, str) or not Path(archive_path).is_absolute():
                    raise ValueError("请选择本机的可读压缩归档。")
                return self.response(self.app.start_job("reader_open", archive_path=archive_path), 202)
            if path == "/api/reader/live":
                self.app.archive_library = None
                self.app.reader_source = {"archived": False, "name": "本机记录"}
                return self.response({"ok": True})
            if path == "/api/pick-archive":
                if sys.platform != "darwin":
                    raise ValueError("File selection is only available on macOS")
                chosen = subprocess.run(["/usr/bin/osascript", "-e",
                    'POSIX path of (choose file with prompt "选择拾聊可读压缩归档" of type {"zip"})'],
                    capture_output=True, text=True, timeout=120)
                if chosen.returncode:
                    return self.response({"path": None})
                return self.response({"path": chosen.stdout.strip()})
            if path == "/api/demo":
                with self.app.operation_lock():
                    return self.response(self.app.library.demo())
            if path == "/api/pick-folder":
                if sys.platform != "darwin":
                    raise ValueError("Enter a folder path manually on this platform")
                prompt = "选择微信数据文件夹" if body.get("purpose") == "source" else "选择备份保存文件夹"
                result = subprocess.run(["/usr/bin/osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")'],
                                        capture_output=True, text=True, timeout=180)
                if result.returncode:
                    return self.response({"path": None, "cancelled": True})
                return self.response({"path": result.stdout.strip()})
            match = re.fullmatch(r"/api/snapshots/([a-zA-Z0-9_.-]+)/(verify|reveal)", path)
            if match:
                snapshot_id, action = match.groups()
                if action == "verify":
                    return self.response(self.app.start_job("verify", snapshot_id), 202)
                snapshot = next((item for item in self.app.snapshots() if item["id"] == snapshot_id), None)
                if not snapshot:
                    raise KeyError("Snapshot not found")
                subprocess.run(["/usr/bin/open", "-R", snapshot["path"]], check=True, timeout=15, capture_output=True)
                return self.response({"ok": True})
            self.response({"error": "Not found"}, 404)
        except KeyError:
            self.response({"error": "Not found"}, 404)
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.response({"error": str(error), "code": getattr(error, "code", "operation_failed")}, 400)


def main():
    parser = argparse.ArgumentParser(description="Local WeChat archive and reader")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--scheduled", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    application = Application(args.state_dir)
    if args.scheduled:
        result = application.run_scheduled()
        print(json.dumps(result))
        return 1 if result["status"] == "error" else 0
    lock = (application.state_dir / "server.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        runtime = read_json(application.state_dir / "runtime.json", {})
        if runtime.get("url") and not args.no_browser:
            webbrowser.open(runtime["url"])
        print("Shiliao is already running.")
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.app = application
    url = f"http://127.0.0.1:{server.server_port}/#token={application.token}"
    write_json(application.state_dir / "runtime.json", {"pid": os.getpid(), "url": url, "port": server.server_port})
    print(f"Shiliao is listening on 127.0.0.1:{server.server_port}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        (application.state_dir / "runtime.json").unlink(missing_ok=True)
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
