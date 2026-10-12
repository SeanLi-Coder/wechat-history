"""Open verified raw WeChat ZIP backups in an isolated, immutable reader cache.

The caller holds the application operation lock. The original ZIP and the live
library are never modified. First opening requires database keys; a completed
cache can subsequently be opened without keys or another full extraction.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import unicodedata
import uuid
import zipfile
import zlib

from vault.backup import BackupError, MAX_MANIFEST_BYTES, _validate_manifest
from vault.library import Library
from vault.media_store import MediaStore, signature
from vault.readable_archive import ArchivedLibrary, ArchiveSource, _snapshot_inventory, _validate_library
from vault.wechat_access import import_account, parse_keys


CHUNK = 1024 * 1024
CACHE_VERSION = 1
KEYS_LIMIT = 4 * CHUNK


class RawArchiveError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _signature(path):
    try:
        value = path.lstat()
    except OSError:
        raise RawArchiveError("archive_unavailable", "无法读取所选备份，请检查文件是否仍在本机。") from None
    if not stat.S_ISREG(value.st_mode):
        raise RawArchiveError("unsafe_archive", "请选择普通 ZIP 文件，不能使用符号链接。")
    return signature(value)


@contextmanager
def _zip_session(path, expected):
    if _signature(path) != expected:
        raise RawArchiveError("archive_changed", "备份文件在打开期间发生变化，请重新选择。")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if signature(os.fstat(stream.fileno())) != expected:
            raise RawArchiveError("archive_changed", "备份文件在打开期间发生变化，请重新选择。")
        try:
            with zipfile.ZipFile(stream) as archive:
                yield archive
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError):
            raise RawArchiveError("corrupt_archive", "备份 ZIP 已损坏或压缩格式无法读取，请重新选择完整备份。") from None
        if signature(os.fstat(stream.fileno())) != expected or _signature(path) != expected:
            raise RawArchiveError("archive_changed", "备份文件在打开期间发生变化，请重新选择。")


def _manifest_bytes(archive, limit=128 * CHUNK):
    matches = [item for item in archive.infolist() if item.filename == "manifest.json"]
    if len(matches) != 1:
        raise RawArchiveError("unsupported_archive", "此 ZIP 没有有效的拾聊备份清单，请选择拾聊生成的备份或可读归档。")
    info = matches[0]
    if (not 0 < info.file_size <= limit or info.flag_bits & 1
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or stat.S_IFMT(info.external_attr >> 16) not in {0, stat.S_IFREG}):
        raise RawArchiveError("corrupt_archive", "备份清单无效或过大。")
    return archive.read(info)


class RawArchiveSource:
    def __init__(self, path, expected_signature=None):
        self.path = Path(path).expanduser().absolute()
        self.signature = _signature(self.path)
        if expected_signature is not None and self.signature != tuple(expected_signature):
            raise RawArchiveError("archive_changed", "备份文件已变化，请重新选择后打开。")
        with _zip_session(self.path, self.signature) as archive:
            content = _manifest_bytes(archive, MAX_MANIFEST_BYTES)
            try:
                self.manifest = _validate_manifest(archive)
            except BackupError:
                raise RawArchiveError("corrupt_archive", "原始备份的清单、文件路径或大小校验失败。") from None
            self.manifest_sha256 = hashlib.sha256(content).hexdigest()
            # Refuse collisions on both case-sensitive and default macOS file
            # systems before creating any file or following any path component.
            paths = set()
            for item in self.manifest["files"]:
                name = unicodedata.normalize("NFC", item["path"]).casefold()
                if name in paths:
                    raise RawArchiveError("unsafe_paths", "备份含有在本机相互冲突的文件名，无法安全解包。")
                paths.add(name)
                info = archive.getinfo("data/" + item["path"])
                if stat.S_IFMT(info.external_attr >> 16) not in {0, stat.S_IFREG}:
                    raise RawArchiveError("unsafe_paths", "原始备份只能包含普通文件。")
            for name in paths:
                if any(str(parent) in paths for parent in PurePosixPath(name).parents if str(parent) != "."):
                    raise RawArchiveError("unsafe_paths", "备份的文件与目录路径发生冲突。")
        self.database_paths = self._database_paths()
        database_bytes = sum(item["size"] for item in self.manifest["files"]
                             if re.search(r"\.db(?:-wal|-journal)?$", item["path"]))
        self.required_bytes = self.manifest["original_bytes"] * 2 + database_bytes * 3 + 512 * CHUNK
        identity = json.dumps([CACHE_VERSION, self.signature, self.manifest_sha256], separators=(",", ":"))
        self.cache_id = hashlib.sha256(identity.encode()).hexdigest()

    def _database_paths(self):
        roots = set()
        for item in self.manifest["files"]:
            parts = PurePosixPath(item["path"]).parts
            if len(parts) >= 2 and parts[-2] == "message" and re.fullmatch(r"message_[0-9]+\.db", parts[-1]):
                prefix = parts[:-2]
                if not prefix or prefix[-1] == "db_storage":
                    roots.add("/".join(prefix))
        return sorted(roots)

    def check(self):
        if _signature(self.path) != self.signature:
            raise RawArchiveError("archive_changed", "原始备份已变化，请重新打开。")

    def extract(self, destination, progress=None):
        done_bytes = 0
        last_report = 0.0
        total_files = self.manifest["file_count"]
        with _zip_session(self.path, self.signature) as archive:
            for index, item in enumerate(self.manifest["files"]):
                target = destination / item["path"]
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with archive.open("data/" + item["path"]) as inp, target.open("xb") as out:
                    os.fchmod(out.fileno(), 0o600)
                    while chunk := inp.read(CHUNK):
                        size += len(chunk)
                        if size > item["size"]:
                            raise RawArchiveError("corrupt_archive", "备份文件超过清单声明的大小。")
                        out.write(chunk)
                        digest.update(chunk)
                        done_bytes += len(chunk)
                        now = time.monotonic()
                        if progress and now - last_report >= 0.25:
                            progress({"phase": "raw_extract", "done_files": index, "total_files": total_files,
                                      "done_bytes": done_bytes, "total_bytes": self.manifest["original_bytes"],
                                      "current_file": "正在校验并解包原始备份，首次打开可能需要较长时间"})
                            last_report = now
                if size != item["size"] or digest.hexdigest() != item["sha256"]:
                    raise RawArchiveError("corrupt_archive", "原始备份内容校验失败，未保存不完整的阅读库。")
            if progress:
                progress({"phase": "raw_extract", "done_files": total_files, "total_files": total_files,
                          "done_bytes": done_bytes, "total_bytes": self.manifest["original_bytes"],
                          "current_file": "原始备份校验完成，正在生成独立阅读库"})


def inspect_archive(path):
    """Identify our two ZIP formats without extracting or reading conversations."""
    path = Path(path).expanduser().absolute()
    expected = _signature(path)
    with _zip_session(path, expected) as archive:
        content = _manifest_bytes(archive)
        try:
            document = json.loads(content)
        except (ValueError, UnicodeError):
            raise RawArchiveError("corrupt_archive", "备份清单不是有效的 JSON。") from None
    if isinstance(document, dict) and document.get("format") == "shiliao-readable":
        source = ArchiveSource(path)
        if source.signature != expected:
            raise RawArchiveError("archive_changed", "备份文件已变化，请重新选择。")
        return {"format": "readable", "signature": expected}
    source = RawArchiveSource(path, expected_signature=expected)
    return {"format": "raw", "signature": source.signature, "raw_id": source.manifest["id"],
            "manifest_sha256": source.manifest_sha256, "required_bytes": source.required_bytes,
            "accounts": len(source.database_paths), "file_count": source.manifest["file_count"],
            "original_bytes": source.manifest["original_bytes"]}


def _hash_file(path):
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_cache(folder, source):
    if not folder.exists():
        return None
    if folder.is_symlink() or not folder.is_dir():
        raise RawArchiveError("unsafe_cache", "原始备份阅读缓存路径异常。")
    try:
        receipt_path = folder / "raw-reader.json"
        library_path = folder / "library.sqlite3"
        if (receipt_path.is_symlink() or library_path.is_symlink()
                or receipt_path.stat().st_size > CHUNK):
            return None
        receipt = json.loads(receipt_path.read_bytes())
        if (receipt.get("version") != CACHE_VERSION or receipt.get("cache_id") != source.cache_id
                or receipt.get("manifest_sha256") != source.manifest_sha256
                or receipt.get("library_sha256") != _hash_file(library_path)):
            return None
        objects, _ = _snapshot_inventory(library_path)
        files = {}
        for item in objects:
            target = folder / item["relative"]
            if target.is_symlink() or folder.resolve() not in target.resolve().parents:
                return None
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size != item["stored_size"]:
                return None
            files[item["relative"]] = {"size": item["stored_size"]}
        _validate_library(library_path, files)
        result = object.__new__(ArchivedLibrary)
        result.state_dir, result.path = folder, library_path
        result.media = MediaStore(result, read_only=True)
        result.raw_backup = {"id": source.manifest["id"], "manifest_sha256": source.manifest_sha256}
        result.raw_cache_hit = True
    except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError):
        return None
    source.check()
    return result


def _private_keys(state_dir, temporary):
    source = state_dir / "wechat-keys.json"
    if not source.exists():
        raise RawArchiveError("keys_required", "这是微信原始数据备份。请先在聊天页的管理区完成“本地读取初始化”或导入对应账号的数据库密钥，再重新打开；也可以直接选择可读归档 ZIP。")
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= KEYS_LIMIT:
        raise RawArchiveError("invalid_keys", "本机数据库密钥文件无效，请在聊天页管理区重新导入。")
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if signature(os.fstat(stream.fileno())) != signature(before):
            raise RawArchiveError("invalid_keys", "本机密钥文件正在变化，请稍后重试。")
        keys = parse_keys(stream.read(KEYS_LIMIT + 1))
        if signature(os.fstat(stream.fileno())) != signature(before):
            raise RawArchiveError("invalid_keys", "本机密钥文件正在变化，请稍后重试。")
    target = temporary / "wechat-keys.json"
    with target.open("x", encoding="utf-8") as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump({"keys": keys}, out)


def _extraction_root(work, source):
    # Retain the account directory basename: DAT image key derivation uses it.
    original = PurePosixPath(source.manifest["source"])
    name = original.name
    if not name or name in {".", ".."} or "\\" in name or "\x00" in name:
        name = "archived-account"
    if name == "db_storage":
        parent = original.parent.name
        if not parent or parent in {".", ".."} or "\\" in parent or "\x00" in parent:
            parent = "archived-account"
        return work / "snapshot" / parent / name
    return work / "snapshot" / name


def open_raw_archive(path, state_dir, progress=None, *, expected_signature=None):
    """Convert once and open only this raw snapshot, with independent media.

    Requires no running-WeChat check because all input is an immutable ZIP
    snapshot. Cache publication is atomic; failed conversions leave no keys,
    decrypted source databases, partial media, or partial conversation library.
    """
    source = RawArchiveSource(path, expected_signature)
    state_dir = Path(state_dir).expanduser().resolve(strict=True)
    cache_root = state_dir / "opened-raw-archives"
    if cache_root.is_symlink():
        raise RawArchiveError("unsafe_cache", "原始备份阅读缓存不能是符号链接。")
    cache_root.mkdir(mode=0o700, exist_ok=True)
    cached = cache_root / source.cache_id
    result = _read_cache(cached, source)
    if result is not None:
        if progress:
            progress({"phase": "raw_ready", "cache_hit": True, "current_file": "已打开原始备份的独立阅读缓存"})
        return result
    if len(source.database_paths) != 1:
        message = ("这个备份包含多个微信账号，暂时不能自动选择；请打开单个账号的备份或对应可读归档。"
                   if source.database_paths else "原始备份中没有找到微信 4.x 消息数据库，请选择单个账号的完整备份或可读归档。")
        raise RawArchiveError("multiple_accounts" if source.database_paths else "missing_databases", message)
    with tempfile.TemporaryDirectory(prefix=".raw-reader-", dir=cache_root) as temporary:
        work = Path(temporary)
        key_state = work / "private-state"
        key_state.mkdir(mode=0o700)
        _private_keys(state_dir, key_state)
        if shutil.disk_usage(cache_root).free < source.required_bytes:
            raise RawArchiveError("insufficient_space", "首次打开原始备份需要空间保存安全副本和独立阅读库。当前磁盘空间不足，请释放空间，或改为打开可读归档 ZIP。")
        extracted = _extraction_root(work, source)
        extracted.mkdir(parents=True, mode=0o700)
        source.extract(extracted, progress)
        database = extracted / source.database_paths[0]
        if not source.database_paths[0] and database.name != "db_storage":
            raise RawArchiveError("missing_databases", "备份的数据库目录结构不完整，请选择完整账号备份。")
        library = Library(work / "reader")

        def report(update):
            if progress:
                safe = {key: value for key, value in update.items()
                        if key != "snapshot_ready" and key != "current_file"}
                safe.update(phase="raw_import", current_file="正在从原始备份整理聊天和媒体，微信可以继续使用")
                progress(safe)

        identity = str(PurePosixPath(source.manifest["source"]) / source.database_paths[0])
        from vault.wechat_crypto import WeChatCryptoError
        try:
            import_account(database, key_state, library, assert_closed=source.check, progress=report,
                           account_id=identity, include_missing_media=True)
        except WeChatCryptoError as error:
            if error.code in {"key_mismatch", "invalid_key"}:
                raise RawArchiveError("keys_mismatch", "当前密钥与这个原始备份不匹配。请在聊天页管理区导入备份对应账号的数据库密钥，再重新打开。") from None
            raise
        source.check()
        receipt = {"version": CACHE_VERSION, "cache_id": source.cache_id,
                   "manifest_sha256": source.manifest_sha256, "library_sha256": _hash_file(library.path)}
        receipt_path = library.state_dir / "raw-reader.json"
        with receipt_path.open("x", encoding="utf-8") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(receipt, out, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        # Only fully imported, checked data survives the temporary directory.
        source.check()
        obsolete = None
        if cached.exists():
            obsolete = cache_root / (".obsolete-" + uuid.uuid4().hex)
            os.rename(cached, obsolete)
        try:
            os.rename(library.state_dir, cached)
        except BaseException:
            if obsolete is not None:
                os.rename(obsolete, cached)
            raise
        if obsolete is not None:
            shutil.rmtree(obsolete)
    result = _read_cache(cached, source)
    if result is None:
        shutil.rmtree(cached)
        raise RawArchiveError("cache_failed", "原始备份的独立阅读库校验失败，请重新打开。")
    result.raw_cache_hit = False
    if progress:
        progress({"phase": "raw_ready", "cache_hit": False, "current_file": "原始备份已转换为独立只读聊天，可直接阅读"})
    return result
