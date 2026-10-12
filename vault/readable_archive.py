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
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
import zipfile
import zlib

from vault.library import Library
from vault.media_store import MediaStore, MIMES, signature


CHUNK = 1024 * 1024
MAX_LIBRARY = 4 * 1024 ** 3
MAX_OBJECT = 8 * 1024 ** 3
OBJECT_PATH = re.compile(r"media/objects/([a-f0-9]{2})/([a-f0-9]{64})\.(bin|gz)\Z")


def _media_path(kind, mime, digest, encoding, byte_size, stored_size):
    if (mime not in MIMES.get(kind, set()) or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest) or encoding not in {"raw", "gzip"}
            or type(byte_size) is not int or not 0 < byte_size <= MAX_OBJECT
            or type(stored_size) is not int or not 0 < stored_size <= MAX_OBJECT
            or encoding == "raw" and byte_size != stored_size):
        raise ValueError("Archived media metadata is invalid")
    return f"media/objects/{digest[:2]}/{digest}" + (".gz" if encoding == "gzip" else ".bin")


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
            relative = _media_path(kind, mime, digest, encoding, byte_size, stored_size)
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


def _status_counts(rows):
    counts = {}
    for kind, status, count in rows:
        kind = kind if kind in MIMES else "other"
        status = status if status in {"ready", "missing", "locked", "unsupported", "error"} else "error"
        key = kind, status
        counts[key] = counts.get(key, 0) + count
    return [{"kind": kind, "status": status, "count": counts[kind, status]} for kind, status in sorted(counts)]


def _inventory(db):
    """Count all saved records and objects from one consistent database view."""
    objects = {}
    for kind, mime, digest, encoding, byte_size, stored_size in db.execute(
            "SELECT kind,mime_type,object_hash,encoding,byte_size,stored_size FROM media_assets WHERE status='ready'"):
        relative = _media_path(kind, mime, digest, encoding, byte_size, stored_size)
        value = {"relative": relative, "object_hash": digest, "encoding": encoding,
                 "byte_size": byte_size, "stored_size": stored_size}
        if relative in objects and objects[relative] != value:
            raise ValueError("Shared media objects have inconsistent metadata")
        objects[relative] = value
    summary = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
               for table in ("conversations", "messages")}
    avatars = {"total": 0, "ready": 0, "unavailable": 0}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if ("avatar_assets" in tables and {"identity_key", "asset_id"}
            <= {row[1] for row in db.execute("PRAGMA table_info(avatar_assets)")}):
        for row in db.execute("""SELECT v.identity_key,a.* FROM avatar_assets v
                LEFT JOIN media_assets a ON a.id=v.asset_id"""):
            avatars["total"] += 1
            if (isinstance(row["identity_key"], str) and re.fullmatch(r"[a-f0-9]{64}", row["identity_key"])
                    and Library._valid_avatar_asset(row)):
                avatars["ready"] += 1
        avatars["unavailable"] = avatars["total"] - avatars["ready"]
    versioned = {"wechat_source_messages", "media_import_revisions"} <= tables
    revision_join = (" LEFT JOIN wechat_source_messages w ON w.message_id=m.message_id "
                     "LEFT JOIN media_import_revisions r ON r.message_id=m.message_id AND r.kind=m.kind "
                     if versioned else "")
    revision_filter = " AND (w.message_id IS NULL OR r.source_digest=w.source_digest)" if versioned else ""
    items = _status_counts(db.execute("""SELECT m.kind,
            CASE WHEN a.id IS NULL THEN 'missing' WHEN a.kind<>m.kind THEN 'error' ELSE a.status END AS status,
            COUNT(*) FROM message_media m JOIN messages t ON t.id=m.message_id
            LEFT JOIN media_assets a ON a.id=m.asset_id """ + revision_join + " WHERE 1=1 " + revision_filter + " GROUP BY 1,2"))
    stale_links = (db.execute("""SELECT COUNT(*) FROM message_media m JOIN messages t ON t.id=m.message_id """
        + revision_join + " WHERE w.message_id IS NOT NULL AND (r.source_digest IS NULL OR r.source_digest<>w.source_digest)").fetchone()[0]
        if versioned else 0)
    assets = _status_counts(db.execute("SELECT kind,status,COUNT(*) FROM media_assets GROUP BY kind,status"))
    values = list(objects.values())
    summary.update(avatars=avatars, unavailable_media=sum(item["count"] for item in items if item["status"] != "ready"),
        media={"items": items, "assets": assets, "stale_links": stale_links, "unique_objects": len(values),
               "original_bytes": sum(item["byte_size"] for item in values),
               "stored_bytes": sum(item["stored_size"] for item in values)})
    return values, summary


def _snapshot_inventory(snapshot):
    with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        return _inventory(db)


def export_summary(library):
    """Preview the complete export using one read transaction and no media I/O."""
    with library.connect() as db:
        db.execute("BEGIN")
        return _inventory(db)[1]


def _verify_gzip(inp, expected_hash, expected_size):
    """Validate decoded bytes in bounded memory without materializing them."""
    digest, size = hashlib.sha256(), 0
    inp.seek(0)
    try:
        with gzip.GzipFile(fileobj=inp, mode="rb") as decoded:
            while chunk := decoded.read(CHUNK):
                size += len(chunk)
                if size > expected_size:
                    raise ValueError("A compressed media object exceeds its declared size")
                digest.update(chunk)
    except (gzip.BadGzipFile, EOFError, zlib.error):
        raise ValueError("A compressed media object failed its content integrity check") from None
    if size != expected_size or digest.hexdigest() != expected_hash:
        raise ValueError("A compressed media object failed its content integrity check")


def _write_entry(archive, relative, inp, size, media=None, expected_entry_hash=None):
    info = zipfile.ZipInfo(relative)
    info.compress_type = zipfile.ZIP_DEFLATED if relative == "library.sqlite3" else zipfile.ZIP_STORED
    info.external_attr = 0o100600 << 16
    digest = hashlib.sha256()
    with archive.open(info, "w", force_zip64=True) as out:
        _copy(inp, out, size, digest)
    stored_hash = digest.hexdigest()
    if expected_entry_hash is not None and stored_hash != expected_entry_hash:
        raise ValueError("Readable archive entry failed its integrity check")
    if media is not None:
        if media["encoding"] == "raw" and stored_hash != media["object_hash"]:
            raise ValueError("A media object failed its content integrity check")
        if media["encoding"] == "gzip":
            _verify_gzip(inp, media["object_hash"], media["byte_size"])
    return {"size": size, "sha256": stored_hash}


@contextmanager
def _local_input(source, size, root=None):
    before = source.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_size != size
            or root is not None and root.resolve() not in source.resolve().parents):
        raise ValueError("An archive source is not a regular private file of the expected size")
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as inp:
        if signature(os.fstat(inp.fileno())) != signature(before):
            raise ValueError("An archive source changed while opening")
        yield inp
        if signature(os.fstat(inp.fileno())) != signature(before) or signature(source.lstat()) != signature(before):
            raise ValueError("An archive source changed while copying")


def create_archive(library, destination, progress=None):
    """Export every record and ready object from a live or opened library.

    Opened archives are streamed from one verified ZIP handle, preserving
    existing raw/gzip objects without extracting all media into a cache.
    Unavailable media keeps its status; missing or corrupt ready objects fail
    the complete export. Publication never overwrites an existing archive.
    """
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    name = "readable-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(3).hex() + ".zip"
    target = destination / name
    with tempfile.TemporaryDirectory(prefix=".readable-", dir=destination) as folder:
        folder = Path(folder)
        snapshot = folder / "library.sqlite3"
        temporary = folder / "archive.zip"
        manifest = {"format": "shiliao-readable", "version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(), "files": {}}
        archived_source = getattr(library.media, "archive_source", None)
        with archived_source.read_session() if archived_source is not None else nullcontext(None) as input_archive:
            if input_archive is not None:
                item = archived_source.files["library.sqlite3"]
                digest = hashlib.sha256()
                with input_archive.open("library.sqlite3") as inp, snapshot.open("xb") as out:
                    os.fchmod(out.fileno(), 0o600)
                    _copy(inp, out, item["size"], digest)
                if digest.hexdigest() != item["sha256"]:
                    raise ValueError("Archived conversation library failed its integrity check")
            else:
                with closing(sqlite3.connect(snapshot)) as connection:
                    snapshot.chmod(0o600)
                    with library.connect() as source:
                        source.backup(connection)
            objects, summary = _snapshot_inventory(snapshot)
            manifest["summary"] = summary
            total_files = len(objects) + 1
            if progress:
                progress({"done_files": 0, "total_files": total_files, "current_file": "正在保存可离线打开的压缩归档"})
            with zipfile.ZipFile(temporary, "w", allowZip64=True, compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                with _local_input(snapshot, snapshot.stat().st_size) as inp:
                    manifest["files"]["library.sqlite3"] = _write_entry(archive, "library.sqlite3", inp, snapshot.stat().st_size)
                for index, media in enumerate(objects, 2):
                    relative, size = media["relative"], media["stored_size"]
                    if input_archive is not None:
                        item = archived_source.files.get(relative)
                        if item is None or item["size"] != size:
                            raise ValueError("Readable archive is missing a referenced media object")
                        with input_archive.open(relative) as inp:
                            entry = _write_entry(archive, relative, inp, size, media, item["sha256"])
                    else:
                        with _local_input(library.state_dir / relative, size, library.state_dir) as inp:
                            entry = _write_entry(archive, relative, inp, size, media)
                    manifest["files"][relative] = entry
                    if progress and (index % 100 == 0 or index == total_files):
                        progress({"done_files": index, "total_files": total_files, "current_file": "正在保存可离线打开的压缩归档"})
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
            if progress and not objects:
                progress({"done_files": 1, "total_files": 1, "current_file": "正在保存可离线打开的压缩归档"})
        temporary.chmod(0o600)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(temporary, target)
    return {"path": str(target), "filename": name, "byte_size": target.stat().st_size,
            "objects": len(objects), "original_bytes": sum(item["size"] for item in manifest["files"].values()),
            "summary": summary}


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

    @contextmanager
    def read_session(self):
        """Keep one central directory and descriptor for a bounded-memory export."""
        self._check()
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if signature(os.fstat(stream.fileno())) != self.signature:
                raise ValueError("The readable archive changed while opening")
            with zipfile.ZipFile(stream) as archive:
                yield archive
            if signature(os.fstat(stream.fileno())) != self.signature:
                raise ValueError("The readable archive changed during export")
        self._check()

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
