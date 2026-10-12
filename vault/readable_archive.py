"""Portable readable archives with lazy extraction of private media objects."""

import hashlib
import gzip
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import zipfile

from vault.library import Library
from vault.media_store import MediaStore, MIMES, signature


CHUNK = 1024 * 1024
MAX_LIBRARY = 4 * 1024 ** 3
MAX_OBJECT = 8 * 1024 ** 3
OBJECT_PATH = re.compile(r"media/objects/([a-f0-9]{2})/([a-f0-9]{64})\.(bin|gz)\Z")


class ArchivedLibrary(Library):
    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            yield connection
        finally:
            connection.close()


def _validate_library(path, files):
    required = {
        "imports": {"digest", "filename", "imported_at"},
        "conversations": {"id", "title", "import_digest"},
        "messages": {"id", "conversation_id", "sender", "text", "timestamp", "is_self"},
        "media_assets": {"id", "kind", "status", "mime_type", "filename", "object_hash", "encoding", "byte_size", "stored_size", "detail"},
        "message_media": {"message_id", "kind", "asset_id"},
    }
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        schema = connection.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        if any(kind in {"trigger", "view"} or (sql and re.match(r"CREATE\s+VIRTUAL\s+TABLE", sql, re.I)) for kind, name, sql in schema):
            raise ValueError("Archived libraries cannot contain executable views, triggers, or virtual tables")
        tables = {name for kind, name, sql in schema if kind == "table"}
        for name, columns in required.items():
            if name not in tables or not columns <= {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}:
                raise ValueError("Archived conversation library has an unsupported schema")
        avatar_tables = {
            "avatar_assets": {"identity_key": ("TEXT", 1), "asset_id": ("TEXT", 0)},
            "conversation_avatars": {"conversation_id": ("TEXT", 1), "identity_key": ("TEXT", 0)},
            "message_avatars": {"message_id": ("INTEGER", 1), "identity_key": ("TEXT", 0)},
        }
        if tables & avatar_tables.keys():
            if not avatar_tables.keys() <= tables:
                raise ValueError("Archived avatar mappings have an incomplete schema")
            for name, columns in avatar_tables.items():
                actual = {row[1]: (row[2].upper(), row[5])
                          for row in connection.execute(f'PRAGMA table_info("{name}")')}
                if any(actual.get(column) != specification for column, specification in columns.items()):
                    raise ValueError("Archived avatar mappings have an unsupported schema")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Archived conversation library is damaged")
        for kind, mime, digest, encoding, byte_size, stored_size in connection.execute(
                "SELECT kind,mime_type,object_hash,encoding,byte_size,stored_size FROM media_assets WHERE status='ready'"):
            if (mime not in MIMES.get(kind, set()) or not isinstance(digest, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", digest) or encoding not in {"raw", "gzip"}
                    or type(byte_size) is not int or not 0 < byte_size <= MAX_OBJECT
                    or type(stored_size) is not int or not 0 < stored_size <= MAX_OBJECT):
                raise ValueError("Archived media metadata is invalid")
            relative = f"media/objects/{digest[:2]}/{digest}" + (".gz" if encoding == "gzip" else ".bin")
            if relative not in files or files[relative]["size"] != stored_size:
                raise ValueError("Readable archive is missing a referenced media object")


def _copy(inp, out, expected, digest=None):
    total = 0
    while chunk := inp.read(CHUNK):
        total += len(chunk)
        if total > expected:
            raise ValueError("Archive entry exceeds its declared size")
        if digest:
            digest.update(chunk)
        out.write(chunk)
    if total != expected:
        raise ValueError("Archive entry is incomplete")


def create_archive(library, destination, progress=None):
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    name = "readable-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(3).hex() + ".zip"
    target = destination / name
    with tempfile.TemporaryDirectory(prefix=".readable-", dir=destination) as folder:
        folder = Path(folder)
        snapshot = folder / "library.sqlite3"
        connection = sqlite3.connect(snapshot)
        try:
            with library.connect() as source:
                source.backup(connection)
            objects = connection.execute("SELECT DISTINCT object_hash,encoding,stored_size FROM media_assets WHERE status='ready'").fetchall()
        finally:
            connection.close()
        snapshot.chmod(0o600)
        entries = [("library.sqlite3", snapshot)]
        seen = set()
        for digest, encoding, size in objects:
            if not re.fullmatch(r"[a-f0-9]{64}", digest or "") or encoding not in {"raw", "gzip"}:
                raise ValueError("Media metadata is invalid")
            relative = "media/objects/" + digest[:2] + "/" + digest + (".gz" if encoding == "gzip" else ".bin")
            if relative in seen:
                continue
            seen.add(relative)
            source = library.state_dir / relative
            if source.is_symlink() or not source.is_file() or source.stat().st_size != size:
                raise ValueError("A media object is missing; the readable archive was not created")
            entries.append((relative, source))
        temporary = folder / "archive.zip"
        manifest = {"format": "shiliao-readable", "version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(), "files": {}}
        with zipfile.ZipFile(temporary, "w", allowZip64=True, compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for index, (relative, source) in enumerate(entries):
                before = source.lstat()
                if (not stat.S_ISREG(before.st_mode)
                        or (relative != "library.sqlite3" and library.state_dir.resolve() not in source.resolve().parents)):
                    raise ValueError("An archive source is not a regular private file")
                info = zipfile.ZipInfo(relative)
                info.compress_type = zipfile.ZIP_DEFLATED if relative == "library.sqlite3" else zipfile.ZIP_STORED
                info.external_attr = 0o100600 << 16
                digest = hashlib.sha256()
                descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as inp, archive.open(info, "w", force_zip64=True) as out:
                    if signature(os.fstat(inp.fileno())) != signature(before):
                        raise ValueError("An archive source changed while opening")
                    _copy(inp, out, before.st_size, digest)
                    if signature(os.fstat(inp.fileno())) != signature(before):
                        raise ValueError("An archive source changed while copying")
                matched = OBJECT_PATH.fullmatch(relative)
                if matched:
                    if matched[3] == "bin" and digest.hexdigest() != matched[2]:
                        raise ValueError("A media object failed its content integrity check")
                    if matched[3] == "gz":
                        decoded = hashlib.sha256()
                        total = 0
                        with gzip.open(source, "rb") as inp:
                            while chunk := inp.read(CHUNK):
                                total += len(chunk)
                                if total > MAX_OBJECT:
                                    raise ValueError("A compressed media object exceeds its size limit")
                                decoded.update(chunk)
                        if decoded.hexdigest() != matched[2]:
                            raise ValueError("A media object failed its content integrity check")
                after = source.lstat()
                if signature(before) != signature(after):
                    raise ValueError("A media object changed while archiving")
                manifest["files"][relative] = {"size": before.st_size, "sha256": digest.hexdigest()}
                if progress and (index % 100 == 0 or index + 1 == len(entries)):
                    progress({"done_files": index + 1, "total_files": len(entries), "current_file": "正在保存可离线打开的压缩归档"})
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
        temporary.chmod(0o600)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(temporary, target)
    return {"path": str(target), "filename": name, "byte_size": target.stat().st_size,
            "objects": len(entries) - 1, "original_bytes": sum(item["size"] for item in manifest["files"].values())}


class ArchiveSource:
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("Select a regular readable ZIP archive")
        value = self.path.stat()
        self.signature = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        with zipfile.ZipFile(self.path) as archive:
            info = archive.getinfo("manifest.json")
            if info.file_size > 128 * CHUNK:
                raise ValueError("Readable archive manifest is too large")
            manifest = json.loads(archive.read(info))
            if manifest.get("format") != "shiliao-readable" or manifest.get("version") != 1:
                raise ValueError("This ZIP is not a supported readable archive")
            self.files = manifest.get("files")
            if not isinstance(self.files, dict) or "library.sqlite3" not in self.files or len(self.files) > 600000:
                raise ValueError("Readable archive manifest is invalid")
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Readable archive contains duplicate entries")
            if set(names) != set(self.files) | {"manifest.json"}:
                raise ValueError("Readable archive contains unexpected entries")
            for name, item in self.files.items():
                matched = OBJECT_PATH.fullmatch(name)
                if name != "library.sqlite3" and (not matched or matched[1] != matched[2][:2]):
                    raise ValueError("Readable archive contains an invalid object path")
                if not isinstance(item, dict) or type(item.get("size")) is not int or not 0 < item["size"] <= (MAX_LIBRARY if name == "library.sqlite3" else MAX_OBJECT):
                    raise ValueError("Readable archive entry has an invalid size")
                member = archive.getinfo(name)
                if stat.S_IFMT(member.external_attr >> 16) not in (0, stat.S_IFREG):
                    raise ValueError("Readable archive entries must be regular files")
                if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) or member.file_size != item["size"]:
                    raise ValueError("Readable archive entry metadata is invalid")
        self._check()

    def _check(self):
        value = self.path.lstat()
        if not stat.S_ISREG(value.st_mode) or signature(value) != self.signature:
            raise ValueError("The readable archive changed after opening")

    def extract(self, relative, target):
        self._check()
        item = self.files.get(relative)
        if item is None:
            raise KeyError("Archived media is not available")
        target = Path(target)
        if os.path.lexists(target):
            raise ValueError("The extraction destination already exists and was not changed")
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".extract-", dir=target.parent)
        try:
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "wb") as out:
                archive_fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(archive_fd, "rb") as archived_file:
                    if signature(os.fstat(archived_file.fileno())) != self.signature:
                        raise ValueError("The readable archive changed after opening")
                    with zipfile.ZipFile(archived_file) as archive, archive.open(relative) as inp:
                        _copy(inp, out, item["size"], digest)
                    out.flush()
                    os.fsync(out.fileno())
                    if signature(os.fstat(archived_file.fileno())) != self.signature:
                        raise ValueError("The readable archive changed during extraction")
            if digest.hexdigest() != item["sha256"]:
                raise ValueError("Readable archive entry failed its integrity check")
            self._check()
            os.link(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)


def open_archive(path, state_dir):
    source = ArchiveSource(path)
    cache_root = Path(state_dir) / "opened-archives"
    cache_root.mkdir(mode=0o700, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="reader-", dir=cache_root))
    try:
        source.extract("library.sqlite3", folder / "library.sqlite3")
        _validate_library(folder / "library.sqlite3", source.files)
        library = object.__new__(ArchivedLibrary)
        library.state_dir, library.path = folder, folder / "library.sqlite3"
        library.media = MediaStore(library, read_only=True)
        library.media.archive_source = source
        return library
    except Exception:
        import shutil
        shutil.rmtree(folder)
        raise
