"""Consistent, local-only ZIP backups of a closed WeChat data directory."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time
from typing import Callable, Iterator
import uuid
import zipfile
import zlib


CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MANIFEST_VERSION = 1
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")


class BackupError(RuntimeError):
    """An actionable backup failure with a stable, machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _Entry:
    path: str
    kind: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int

    @classmethod
    def from_stat(cls, path: str, value: os.stat_result) -> "_Entry":
        if stat.S_ISREG(value.st_mode):
            kind = "file"
        elif stat.S_ISDIR(value.st_mode):
            kind = "directory"
        elif stat.S_ISLNK(value.st_mode):
            kind = "symlink"
        else:
            kind = "special"
        return cls(path, kind, value.st_dev, value.st_ino, value.st_size,
                   value.st_mtime_ns, value.st_ctime_ns, value.st_mode)


def _is_wechat_source(source: Path) -> bool:
    parts = [part.casefold() for part in source.parts]
    return any("com.tencent.xinwechat" in part
               or "com.tencent.wechat" in part
               or part in {"xwechat_files", "wechat files", "wechat", "weixin"}
               for part in parts)


def _assert_wechat_closed(source: Path, allow_running: bool) -> None:
    if allow_running or not _is_wechat_source(source):
        return
    for process in ("WeChat", "Weixin", "WeChatAppEx"):
        try:
            result = subprocess.run(
                ["pgrep", "-x", process], capture_output=True, text=True,
                timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackupError("process_check_failed",
                              "Cannot confirm that WeChat is closed.") from exc
        if result.returncode == 0:
            raise BackupError("wechat_running",
                              "Quit WeChat before backing up its data directory.")
        if result.returncode != 1:
            raise BackupError("process_check_failed",
                              "Cannot confirm that WeChat is closed.")


def discover_sources() -> list[dict]:
    """Return known macOS locations without opening any conversation files."""
    home = Path.home()
    container = home / "Library/Containers/com.tencent.xinWeChat/Data"
    candidates: list[tuple[Path, str]] = [
        (container / "Documents/xwechat_files", "WeChat 4.x data"),
        (container / "Library/Application Support/com.tencent.xinWeChat",
         "WeChat legacy data"),
        (container / "Documents", "WeChat container documents"),
    ]
    groups = home / "Library/Group Containers"
    try:
        with os.scandir(groups) as entries:
            for entry in entries:
                if ("wechat" in entry.name.casefold()
                        or "weixin" in entry.name.casefold()):
                    if entry.is_dir(follow_symlinks=False):
                        candidates.append((Path(entry.path), "WeChat shared data"))
    except OSError:
        pass
    result = []
    seen: set[str] = set()
    for candidate, label in candidates:
        path = str(candidate)
        if path in seen:
            continue
        seen.add(path)
        exists = candidate.is_dir() and not candidate.is_symlink()
        result.append({"path": path, "label": label, "exists": exists,
                       "kind": "wechat", "readable": exists and os.access(
                           candidate, os.R_OK | os.X_OK)})
    return sorted(result, key=lambda item: not item["exists"])


def _safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (not path.is_absolute() and str(path) == value
            and not any(part in {".", ".."} for part in path.parts))


def _scan(root_fd: int) -> dict[str, _Entry]:
    """Scan through directory descriptors so a replaced symlink is never followed."""
    result: dict[str, _Entry] = {}

    def visit(directory_fd: int, prefix: str) -> None:
        before = os.fstat(directory_fd)
        result[prefix] = _Entry.from_stat(prefix, before)
        with os.scandir(directory_fd) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if not _safe_relative(relative):
                raise BackupError("unsupported_path", "A file name is unsafe for ZIP storage.")
            metadata = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
            item = _Entry.from_stat(relative, metadata)
            if item.kind == "special":
                raise BackupError("unsupported_file",
                                  f"Unsupported file type: {relative}")
            result[relative] = item
            if item.kind == "directory":
                child_fd = os.open(entry.name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                                   dir_fd=directory_fd)
                try:
                    if _Entry.from_stat(relative, os.fstat(child_fd)) != item:
                        raise BackupError("source_changed", "The source changed during scanning.")
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
        if _Entry.from_stat(prefix, os.fstat(directory_fd)) != result[prefix]:
            raise BackupError("source_changed", "The source changed during scanning.")

    visit(root_fd, "")
    return result


@contextmanager
def _open_relative(root_fd: int, relative: str) -> Iterator[int]:
    directory_fd = os.dup(root_fd)
    file_fd: int | None = None
    try:
        parts = relative.split("/")
        for component in parts[:-1]:
            next_fd = os.open(component, os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                              dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | _NOFOLLOW, dir_fd=directory_fd)
        yield file_fd
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _validate_manifest(archive: zipfile.ZipFile) -> dict:
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if len(names) != len(set(names)) or "manifest.json" not in names:
        raise BackupError("corrupt_archive", "Duplicate ZIP entries or missing manifest.")
    manifest_info = archive.getinfo("manifest.json")
    if manifest_info.file_size > MAX_MANIFEST_BYTES:
        raise BackupError("corrupt_archive", "The backup manifest is too large.")
    try:
        manifest = json.loads(archive.read(manifest_info))
    except (ValueError, UnicodeError) as exc:
        raise BackupError("corrupt_archive", "The backup manifest is not valid JSON.") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise BackupError("corrupt_archive", "Unsupported backup manifest version.")
    required = ("id", "created_at", "source", "file_count", "original_bytes", "files")
    if any(field not in manifest for field in required):
        raise BackupError("corrupt_archive", "The backup manifest is incomplete.")
    if (not isinstance(manifest["id"], str)
            or not _SNAPSHOT_ID.fullmatch(manifest["id"])
            or not isinstance(manifest["created_at"], str)
            or not isinstance(manifest["source"], str)
            or not isinstance(manifest["files"], list)
            or type(manifest["file_count"]) is not int
            or type(manifest["original_bytes"]) is not int):
        raise BackupError("corrupt_archive", "The backup manifest has invalid fields.")
    try:
        created = datetime.fromisoformat(manifest["created_at"])
        if created.tzinfo is None:
            raise ValueError("A timezone is required.")
    except ValueError as exc:
        raise BackupError("corrupt_archive", "The backup timestamp is invalid.") from exc
    for field in ("directories", "skipped_symlinks"):
        values = manifest.get(field, [])
        if (not isinstance(values, list) or not all(_safe_relative(value) for value in values)
                or len(values) != len(set(values))):
            raise BackupError("corrupt_archive", "The backup directory metadata is invalid.")
    expected = {"manifest.json"}
    total_bytes = 0
    for item in manifest["files"]:
        if (not isinstance(item, dict) or not _safe_relative(item.get("path"))
                or type(item.get("size")) is not int or item["size"] < 0
                or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise BackupError("corrupt_archive", "An invalid file record was found.")
        name = f"data/{item['path']}"
        if name in expected:
            raise BackupError("corrupt_archive", "Duplicate manifest file records.")
        expected.add(name)
        try:
            info = archive.getinfo(name)
        except KeyError as exc:
            raise BackupError("corrupt_archive", "A backup file is missing.") from exc
        if (info.file_size != item["size"] or info.is_dir() or info.flag_bits & 1
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or stat.S_ISLNK(info.external_attr >> 16)):
            raise BackupError("corrupt_archive", "A ZIP entry does not match its manifest.")
        total_bytes += item["size"]
    if (set(names) != expected or manifest["file_count"] != len(manifest["files"])
            or manifest["original_bytes"] != total_bytes):
        raise BackupError("corrupt_archive", "The backup inventory is inconsistent.")
    return manifest


class BackupEngine:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).expanduser().resolve()

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.state_dir / "backup.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupError("backup_busy", "Another backup is already running.") from exc
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _validate_paths(source: str, destination: str) -> tuple[Path, Path]:
        if not source or not destination:
            raise BackupError("invalid_path", "Source and destination paths are required.")
        source_input = Path(source).expanduser().absolute()
        destination_input = Path(destination).expanduser().absolute()
        if source_input.is_symlink():
            raise BackupError("symlink_source", "The source must not be a symbolic link.")
        if destination_input.is_symlink():
            raise BackupError("symlink_destination", "The destination must not be a symbolic link.")
        source_path = source_input.resolve(strict=True)
        destination_path = destination_input.resolve()
        if not source_path.is_dir():
            raise BackupError("invalid_source", "The source must be a directory.")
        if source_path == Path(source_path.anchor):
            raise BackupError("unsafe_source", "The filesystem root cannot be backed up.")
        if destination_path == source_path or source_path in destination_path.parents:
            raise BackupError("nested_destination", "The destination must be outside the source.")
        if destination_path.exists() and not destination_path.is_dir():
            raise BackupError("invalid_destination", "The destination must be a directory.")
        return source_path, destination_path

    def create(self, source: str, destination: str,
               progress: Callable[[dict], None] | None = None,
               allow_running: bool = False) -> dict:
        """Create a verified-consistent archive; never alter the source directory.

        ``allow_running`` exists for synthetic tests only. Applications should
        leave it false when dealing with live WeChat data.

        Every invocation produces an independent full archive. This method
        never deletes prior archives and does not perform incremental backup.
        """
        partial: Path | None = None
        try:
            source_path, destination_path = self._validate_paths(source, destination)
            with self._lock():
                _assert_wechat_closed(source_path, allow_running)
                last_process_check = time.monotonic()
                destination_path.mkdir(parents=True, exist_ok=True, mode=0o700)
                root_fd = os.open(source_path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
                try:
                    inventory = _scan(root_fd)
                    files = [item for item in inventory.values() if item.kind == "file"]
                    files.sort(key=lambda item: item.path)
                    original_bytes = sum(item.size for item in files)
                    required_space = original_bytes + max(original_bytes // 100,
                                                          len(files) * 1024) + CHUNK_SIZE
                    if shutil.disk_usage(destination_path).free < required_space:
                        raise BackupError("insufficient_space",
                                          "Not enough free disk space for a safe backup.")
                    now = datetime.now(timezone.utc)
                    source_key = hashlib.sha256(str(source_path).encode()).hexdigest()[:12]
                    snapshot_id = (f"wechat-{now.strftime('%Y%m%dT%H%M%S%fZ')}-"
                                   f"{source_key}-{uuid.uuid4().hex[:12]}")
                    final_path = destination_path / f"{snapshot_id}.zip"
                    partial = destination_path / f".{snapshot_id}.partial"
                    manifest = {
                        "version": MANIFEST_VERSION, "id": snapshot_id,
                        "created_at": now.isoformat(), "source": str(source_path),
                        "file_count": len(files), "original_bytes": original_bytes,
                        "files": [],
                        "directories": sorted(item.path for item in inventory.values()
                                              if item.kind == "directory" and item.path),
                        "skipped_symlinks": sorted(item.path for item in inventory.values()
                                                   if item.kind == "symlink"),
                    }
                    done_files = 0
                    done_bytes = 0

                    def report(current_file: str = "") -> None:
                        nonlocal last_process_check
                        if time.monotonic() - last_process_check >= 2:
                            _assert_wechat_closed(source_path, allow_running)
                            last_process_check = time.monotonic()
                        if progress:
                            progress({"done_files": done_files, "total_files": len(files),
                                      "done_bytes": done_bytes, "total_bytes": original_bytes,
                                      "current_file": current_file})

                    fd = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                                 0o600)
                    with os.fdopen(fd, "w+b") as output:
                        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                                             compresslevel=6, allowZip64=True) as archive:
                            report()
                            for item in files:
                                with _open_relative(root_fd, item.path) as input_fd:
                                    if _Entry.from_stat(item.path, os.fstat(input_fd)) != item:
                                        raise BackupError("source_changed",
                                                          "The source changed before copying.")
                                    checksum = hashlib.sha256()
                                    copied = 0
                                    info = zipfile.ZipInfo(f"data/{item.path}")
                                    modified = datetime.fromtimestamp(item.mtime_ns / 1e9,
                                                                      tz=timezone.utc)
                                    if 1980 <= modified.year <= 2107:
                                        info.date_time = modified.timetuple()[:6]
                                    info.compress_type = zipfile.ZIP_DEFLATED
                                    info.external_attr = (item.mode & 0xFFFF) << 16
                                    info.create_system = 3
                                    info.file_size = item.size
                                    with archive.open(info, "w", force_zip64=True) as target:
                                        while True:
                                            chunk = os.read(input_fd, CHUNK_SIZE)
                                            if not chunk:
                                                break
                                            target.write(chunk)
                                            checksum.update(chunk)
                                            copied += len(chunk)
                                            done_bytes += len(chunk)
                                            if copied > item.size:
                                                raise BackupError("source_changed",
                                                                  "The source grew while copying.")
                                            report(item.path)
                                    if (copied != item.size or
                                            _Entry.from_stat(item.path, os.fstat(input_fd)) != item):
                                        raise BackupError("source_changed",
                                                          "The source changed while copying.")
                                manifest["files"].append({
                                    "path": item.path, "size": item.size,
                                    "sha256": checksum.hexdigest(), "mtime_ns": item.mtime_ns,
                                })
                                done_files += 1
                                report(item.path)
                            if inventory != _scan(root_fd):
                                raise BackupError("source_changed",
                                                  "The source inventory changed during backup.")
                            if _Entry.from_stat("", os.stat(source_path, follow_symlinks=False)) != inventory[""]:
                                raise BackupError("source_changed", "The source directory was replaced.")
                            _assert_wechat_closed(source_path, allow_running)
                            archive.writestr("manifest.json", json.dumps(
                                manifest, ensure_ascii=False, separators=(",", ":")
                            ).encode("utf-8"))
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(partial, final_path)
                    partial = None
                    directory_fd = os.open(destination_path, os.O_RDONLY | _DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    return self._snapshot(final_path, manifest)
                finally:
                    os.close(root_fd)
        except BackupError:
            raise
        except PermissionError as exc:
            raise BackupError("permission_denied", "Permission denied while reading or writing backup files.") from exc
        except FileNotFoundError as exc:
            raise BackupError("source_missing", "A required file or directory no longer exists.") from exc
        except OSError as exc:
            code = "insufficient_space" if exc.errno == errno.ENOSPC else "backup_failed"
            raise BackupError(code, f"Backup failed: {exc.strerror or str(exc)}") from exc
        finally:
            if partial is not None:
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _snapshot(path: Path, manifest: dict) -> dict:
        return {"id": manifest["id"], "name": path.name,
                "created_at": manifest["created_at"], "source": manifest["source"],
                "file_count": manifest["file_count"],
                "original_bytes": manifest["original_bytes"],
                "archive_bytes": path.stat().st_size, "path": str(path),
                "status": "ready", "verification": "not_checked",
                "skipped_symlinks": len(manifest.get("skipped_symlinks", []))}

    @staticmethod
    def _archive_path(snapshot_id: str, destination: str) -> Path:
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
            raise BackupError("invalid_snapshot", "Invalid snapshot identifier.")
        return Path(destination).expanduser().resolve() / f"{snapshot_id}.zip"

    @contextmanager
    def _read_archive(self, snapshot_id: str, destination: str) -> Iterator[tuple[zipfile.ZipFile, dict]]:
        path = self._archive_path(snapshot_id, destination)
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            with zipfile.ZipFile(handle, "r") as archive:
                manifest = _validate_manifest(archive)
                if manifest["id"] != snapshot_id:
                    raise BackupError("corrupt_archive", "The snapshot identifier does not match its archive.")
                yield archive, manifest

    def read_manifest(self, snapshot_id: str, destination: str) -> dict:
        try:
            with self._read_archive(snapshot_id, destination) as (_, manifest):
                return manifest
        except BackupError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error) as exc:
            raise BackupError("corrupt_archive", "The backup archive cannot be read.") from exc

    def list_snapshots(self, destination: str) -> list[dict]:
        directory = Path(destination).expanduser().resolve()
        if not directory.exists():
            return []
        snapshots = []
        for path in sorted(directory.glob("wechat-*.zip")):
            try:
                manifest = self.read_manifest(path.stem, str(directory))
                snapshots.append(self._snapshot(path, manifest))
            except (BackupError, OSError) as exc:
                try:
                    archive_bytes = path.lstat().st_size
                except OSError:
                    archive_bytes = 0
                snapshots.append({"id": path.stem, "name": path.name,
                                  "created_at": "", "file_count": 0,
                                  "original_bytes": 0, "archive_bytes": archive_bytes,
                                  "path": str(path), "status": "corrupt", "error": str(exc)})
        return sorted(snapshots, key=lambda item: (item["created_at"], item["name"]), reverse=True)

    def verify(self, snapshot_id: str, destination: str) -> dict:
        checked_files = 0
        checked_bytes = 0
        errors = []
        try:
            with self._read_archive(snapshot_id, destination) as (archive, manifest):
                for item in manifest["files"]:
                    checksum = hashlib.sha256()
                    size = 0
                    with archive.open(f"data/{item['path']}") as handle:
                        while True:
                            chunk = handle.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            checksum.update(chunk)
                            size += len(chunk)
                    if size != item["size"] or checksum.hexdigest() != item["sha256"]:
                        errors.append(f"Checksum mismatch: {item['path']}")
                    checked_files += 1
                    checked_bytes += size
        except (BackupError, OSError, zipfile.BadZipFile, RuntimeError,
                ValueError, NotImplementedError, zlib.error) as exc:
            errors.append(str(exc))
        return {"ok": not errors, "snapshot_id": snapshot_id,
                "checked_files": checked_files, "checked_bytes": checked_bytes,
                "errors": errors}
