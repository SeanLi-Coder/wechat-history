"""Keep independent, compressed and deduplicated local media objects."""

from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
import zlib


CHUNK = 1024 * 1024
MAX_OBJECT = 8 * 1024 ** 3
MIMES = {"image": {"image/jpeg", "image/png", "image/gif", "image/webp", "image/avif"},
         "audio": {"audio/flac", "audio/wav", "audio/mpeg", "audio/mp4", "audio/ogg"},
         "video": {"video/mp4", "video/webm", "video/quicktime"},
         "original": {"application/octet-stream", "audio/silk", "image/x-wxgf"}}


def signature(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


class MediaStore:
    def __init__(self, library, read_only=False):
        self.library = library
        self.root = library.state_dir / "media"
        self.objects = self.root / "objects"
        self.cache = self.root / "cache"
        for folder in (self.root, self.objects, self.cache):
            if folder.is_symlink():
                raise ValueError("Media storage folders must not be symbolic links")
            folder.mkdir(parents=True, mode=0o700, exist_ok=True)
            folder.chmod(0o700)
        self.cache_lock = threading.Lock()
        self.verified = {}
        self.batch_state = threading.local()
        self.read_only = read_only
        if read_only:
            return
        with library.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS media_assets (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    mime_type TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL DEFAULT '',
                    object_hash TEXT, encoding TEXT, byte_size INTEGER NOT NULL DEFAULT 0,
                    stored_size INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS message_media (
                    message_id INTEGER NOT NULL, kind TEXT NOT NULL, asset_id TEXT NOT NULL,
                    PRIMARY KEY (message_id, kind)
                );
                CREATE INDEX IF NOT EXISTS media_message_asset ON message_media(asset_id);
                CREATE TABLE IF NOT EXISTS media_import_revisions (
                    message_id INTEGER NOT NULL, kind TEXT NOT NULL, source_digest TEXT NOT NULL,
                    PRIMARY KEY (message_id, kind)
                );
            """)

    @contextmanager
    def batch(self):
        if self.read_only:
            raise ValueError("An opened archive is read-only")
        if getattr(self.batch_state, "connection", None) is not None:
            raise ValueError("Nested media batches are not supported")
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.batch_state.connection = db
            try:
                yield self
            finally:
                self.batch_state.connection = None

    @contextmanager
    def _write(self):
        if self.read_only:
            raise ValueError("An opened archive is read-only")
        connection = getattr(self.batch_state, "connection", None)
        if connection is not None:
            yield connection
        else:
            with self.library.connect() as connection:
                yield connection

    def _verify_stream(self, stream, digest, expected):
        before = signature(os.fstat(stream.fileno()))
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or before[2] != expected:
            raise ValueError("Stored media size is invalid")
        with self.cache_lock:
            if self.verified.get((digest, before)) is None:
                actual = hashlib.sha256()
                while chunk := stream.read(CHUNK):
                    actual.update(chunk)
                if signature(os.fstat(stream.fileno())) != before or actual.hexdigest() != digest:
                    raise ValueError("Stored media failed its integrity check")
                self.verified[(digest, before)] = True
        stream.seek(0)

    def store_file(self, source, kind, mime_type, filename="", detail=""):
        if self.read_only:
            raise ValueError("An opened archive is read-only")
        if mime_type not in MIMES.get(kind, set()):
            raise ValueError("Unsupported media type")
        source = Path(source)
        before = source.lstat()
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_OBJECT:
            raise ValueError("Media source must be a regular file within the size limit")
        with tempfile.TemporaryDirectory(prefix=".object-", dir=self.root) as temporary:
            temporary = Path(temporary)
            raw = temporary / "raw"
            digest = hashlib.sha256()
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as inp, raw.open("xb") as out:
                os.fchmod(out.fileno(), 0o600)
                if signature(os.fstat(inp.fileno())) != signature(before):
                    raise ValueError("Media source changed while opening")
                copied = 0
                sample = b""
                while chunk := inp.read(CHUNK):
                    if not sample:
                        sample = chunk
                    copied += len(chunk)
                    if copied > before.st_size:
                        raise ValueError("Media source grew while copying")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
                if copied != before.st_size or signature(os.fstat(inp.fileno())) != signature(before):
                    raise ValueError("Media source changed while copying")
            if signature(source.lstat()) != signature(before):
                raise ValueError("Media source changed while copying")
            object_hash = digest.hexdigest()
            selected, encoding = raw, "raw"
            if len(zlib.compress(sample, 3)) < len(sample) * .96:
                compressed = temporary / "compressed"
                with raw.open("rb") as inp, compressed.open("xb") as out:
                    os.fchmod(out.fileno(), 0o600)
                    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6, mtime=0, filename="") as archive:
                        shutil.copyfileobj(inp, archive, CHUNK)
                if compressed.stat().st_size < copied * .98:
                    selected, encoding = compressed, "gzip"
            target = self.objects / object_hash[:2] / (object_hash + (".gz" if encoding == "gzip" else ".bin"))
            if target.parent.is_symlink():
                raise ValueError("Media object folders must not be symbolic links")
            target.parent.mkdir(mode=0o700, exist_ok=True)
            try:
                os.link(selected, target)
            except FileExistsError:
                if target.is_symlink() or target.stat().st_size != selected.stat().st_size:
                    raise ValueError("Existing media object is invalid")
                with target.open("rb") as existing, selected.open("rb") as incoming:
                    while chunk := incoming.read(CHUNK):
                        if existing.read(len(chunk)) != chunk:
                            raise ValueError("Existing media object failed its integrity check")
            asset_id = hashlib.sha256((kind + ":" + object_hash + ":" + detail).encode()).hexdigest()
            with self._write() as db:
                db.execute("""INSERT OR IGNORE INTO media_assets
                    (id,kind,status,mime_type,filename,object_hash,encoding,byte_size,stored_size,detail)
                    VALUES(?,?,'ready',?,?,?,?,?,?,?)""",
                    (asset_id, kind, mime_type, Path(filename).name[:200], object_hash,
                     encoding, copied, selected.stat().st_size, detail))
            return asset_id

    def link(self, message_id, kind, asset_id):
        with self._write() as db:
            if not db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone():
                raise ValueError("Media message does not exist")
            if not db.execute("SELECT 1 FROM media_assets WHERE id=? AND kind=?", (asset_id, kind)).fetchone():
                raise ValueError("Media asset does not match its message")
            db.execute("INSERT INTO message_media VALUES(?,?,?) ON CONFLICT(message_id,kind) DO UPDATE SET asset_id=excluded.asset_id",
                       (message_id, kind, asset_id))

    def unavailable(self, message_id, kind, status="missing", detail=""):
        if kind not in MIMES or status not in {"missing", "locked", "unsupported", "error"}:
            raise ValueError("Invalid media status")
        asset_id = hashlib.sha256(f"unavailable:{message_id}:{kind}".encode()).hexdigest()
        with self._write() as db:
            db.execute("""INSERT INTO media_assets(id,kind,status,detail) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,detail=excluded.detail""",
                (asset_id, kind, status, detail))
        self.link(message_id, kind, asset_id)
        return asset_id

    def for_messages(self, ids):
        if not ids:
            return {}
        with self.library.connect() as db:
            # A new text import commits before media processing starts. Do
            # not display stale links during that gap. Immutable older archive
            # formats retain their original read behavior for compatibility.
            versioned = db.execute("""SELECT COUNT(*) FROM sqlite_master WHERE type='table'
                AND name IN ('wechat_source_messages','media_import_revisions')""").fetchone()[0] == 2
            revision_join = (" LEFT JOIN wechat_source_messages w ON w.message_id=m.message_id "
                             "LEFT JOIN media_import_revisions r ON r.message_id=m.message_id AND r.kind=m.kind "
                             if versioned else "")
            revision_filter = " AND (w.message_id IS NULL OR r.source_digest=w.source_digest)" if versioned else ""
            rows = db.execute("""SELECT m.message_id,a.id,a.kind,a.status,a.mime_type,a.filename,a.byte_size,a.detail
                FROM message_media m JOIN media_assets a ON a.id=m.asset_id
                """ + revision_join + " WHERE m.message_id IN (" + ",".join("?" for _ in ids) + ")" +
                revision_filter, list(ids)).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            message_id = item.pop("message_id")
            if item["status"] == "ready":
                item["url"] = "/api/media/" + item["id"]
            result.setdefault(message_id, []).append(item)
        return result

    def ready_messages(self):
        with self.library.connect() as db:
            return {(row[0], row[1]) for row in db.execute("""SELECT m.message_id,m.kind FROM message_media m
                JOIN media_assets a ON a.id=m.asset_id WHERE a.status='ready'""")}

    def begin_import(self, account_key):
        """Drop stale associations and return reusable states for one account.

        A source message can change to text and disappear from the media
        iterator, so invalidation must cover every prior link before iterating.
        Objects remain intact; only associations without a matching imported
        revision are removed. Older unversioned associations are retried once.
        """
        with self._write() as db:
            db.execute("""DELETE FROM message_media
                WHERE EXISTS (SELECT 1 FROM wechat_source_messages w
                    WHERE w.message_id=message_media.message_id AND w.account_key=?
                    AND NOT EXISTS (SELECT 1 FROM media_import_revisions r
                        WHERE r.message_id=message_media.message_id AND r.kind=message_media.kind
                        AND r.source_digest=w.source_digest))""", (account_key,))
            rows = db.execute("""SELECT m.message_id,m.kind,r.source_digest,a.detail FROM message_media m
                JOIN media_assets a ON a.id=m.asset_id
                JOIN media_import_revisions r ON r.message_id=m.message_id AND r.kind=m.kind
                JOIN wechat_source_messages w ON w.message_id=m.message_id
                WHERE w.account_key=? AND a.status='ready' AND r.source_digest=w.source_digest""",
                (account_key,)).fetchall()
            return {(row[0], row[1]): {"source_digest": row[2], "detail": row[3]} for row in rows}

    def record_import_revision(self, message_id, kind, source_digest):
        """Record the source revision inside the same transaction as its link."""
        if kind not in {"image", "audio", "video"} or not re.fullmatch(r"[a-f0-9]{64}", source_digest or ""):
            raise ValueError("Invalid media source revision")
        with self._write() as db:
            if not db.execute("SELECT 1 FROM wechat_source_messages WHERE message_id=? AND source_digest=?",
                              (message_id, source_digest)).fetchone():
                raise ValueError("The source message changed during media import")
            db.execute("""INSERT INTO media_import_revisions VALUES(?,?,?)
                ON CONFLICT(message_id,kind) DO UPDATE SET source_digest=excluded.source_digest""",
                (message_id, kind, source_digest))
            db.execute("DELETE FROM message_media WHERE message_id=? AND kind<>? AND kind IN ('image','audio','video')",
                       (message_id, kind))
            db.execute("DELETE FROM media_import_revisions WHERE message_id=? AND kind<>?",
                       (message_id, kind))

    def stats(self):
        with self.library.connect() as db:
            rows = db.execute("""SELECT a.kind,a.status,COUNT(*) AS count FROM message_media m
                JOIN media_assets a ON a.id=m.asset_id GROUP BY a.kind,a.status""").fetchall()
            sizes = db.execute("""SELECT COALESCE(SUM(byte_size),0),COALESCE(SUM(stored_size),0),COUNT(*)
                FROM (SELECT object_hash,MAX(byte_size) byte_size,MAX(stored_size) stored_size
                      FROM media_assets WHERE status='ready' GROUP BY object_hash)""").fetchone()
            linked = db.execute("""SELECT COALESCE(SUM(a.byte_size),0) FROM message_media m
                JOIN media_assets a ON a.id=m.asset_id WHERE a.status='ready'""").fetchone()[0]
        return {"items": [dict(row) for row in rows], "unique_objects": sizes[2],
                "original_bytes": sizes[0], "stored_bytes": sizes[1],
                "deduplicated_bytes": max(0, linked - sizes[0])}

    @contextmanager
    def open(self, asset_id):
        if not re.fullmatch(r"[a-f0-9]{64}", asset_id):
            raise KeyError("Media not found")
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM media_assets WHERE id=? AND status='ready'", (asset_id,)).fetchone()
        if row is None:
            raise KeyError("Media not found")
        info = dict(row)
        digest = info["object_hash"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest or "") or info["encoding"] not in {"raw", "gzip"}:
            raise ValueError("Media metadata is invalid")
        if (info["mime_type"] not in MIMES.get(info["kind"], set())
                or type(info["byte_size"]) is not int or not 0 < info["byte_size"] <= MAX_OBJECT
                or type(info["stored_size"]) is not int or not 0 < info["stored_size"] <= MAX_OBJECT):
            raise ValueError("Media metadata is invalid")
        path = self.objects / digest[:2] / (digest + (".gz" if info["encoding"] == "gzip" else ".bin"))
        if path.parent.is_symlink() or self.objects.resolve() not in path.resolve().parents:
            raise ValueError("Stored media path is invalid")
        if not path.exists() and getattr(self, "archive_source", None) is not None:
            with self.cache_lock:
                if not path.exists():
                    self.archive_source.extract(str(path.relative_to(self.library.state_dir)), path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != info["stored_size"]:
            raise ValueError("Stored media object is missing or damaged")
        if info["encoding"] == "gzip":
            with self.cache_lock:
                cached = self.cache / digest
                if not cached.exists():
                    descriptor, temporary = tempfile.mkstemp(prefix=".decode-", dir=self.cache)
                    try:
                        actual = hashlib.sha256()
                        total = 0
                        with os.fdopen(descriptor, "wb") as output, gzip.open(path, "rb") as inp:
                            while chunk := inp.read(CHUNK):
                                total += len(chunk)
                                if total > info["byte_size"] or total > MAX_OBJECT:
                                    raise ValueError("Stored media exceeds its expected size")
                                actual.update(chunk)
                                output.write(chunk)
                        if total != info["byte_size"] or actual.hexdigest() != digest:
                            raise ValueError("Stored media failed its integrity check")
                        os.replace(temporary, cached)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
                path = cached
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            self._verify_stream(stream, digest, info["byte_size"])
            yield stream, info
