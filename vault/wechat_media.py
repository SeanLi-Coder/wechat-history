"""Resolve local WeChat media from immutable decrypted database snapshots.

No network fetch, source modification, image decoding, or audio transcoding is
performed here. Resource identities come from the resource database, never
from an XML body or from a filename guessed from message text.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess


KINDS = {3: "image", 34: "audio", 43: "video"}
TYPES = {value: key for key, value in KINDS.items()}
MESSAGE_TABLE = re.compile(r"Msg_([a-f0-9]{32})\Z", re.I)
HASH_BYTES = re.compile(rb"(?<![a-fA-F0-9])[a-fA-F0-9]{32}(?![a-fA-F0-9])")
FILE_HASH = re.compile(r"([a-fA-F0-9]{32})(?:[_.].*|\Z)")
MAX_PACKED_BYTES = 1024 * 1024
MAX_VOICE_BYTES = 16 * 1024 * 1024
BATCH_SIZE = 512


class MediaLocatorError(ValueError):
    """A source failure with no private identifiers or values in its message."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _signature(value: os.stat_result) -> tuple:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _safe_path(base: Path, relative: Path, directory: bool = False) -> Path:
    if relative.is_absolute() or any(part in ("..", ".") for part in relative.parts):
        raise MediaLocatorError("unsafe_source", "A media source path is unsafe.")
    path = base
    for part in relative.parts:
        path = path / part
        value = path.lstat()
        if stat.S_ISLNK(value.st_mode):
            raise MediaLocatorError("unsafe_source", "Media source paths cannot contain symbolic links.")
    value = path.lstat()
    valid = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if not valid:
        raise MediaLocatorError("unsafe_source", "A media source has an unsupported file type.")
    return path


def _root(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    _safe_path(Path(path.anchor), path.relative_to(path.anchor), directory=True)
    return path


def _columns(connection: sqlite3.Connection, table: str, required: set[str]) -> None:
    actual = {row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')}
    if not required.issubset(actual):
        raise MediaLocatorError("unsupported_schema", "A media source database has an unsupported schema.")


def _integer(value: object) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d{1,20}", value):
        return int(value)
    raise MediaLocatorError("invalid_metadata", "A media source identifier is invalid.")


def _hashes(value: object) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, bytes) or len(value) > MAX_PACKED_BYTES:
        raise MediaLocatorError("invalid_metadata", "A resource metadata field is invalid or too large.")
    return {item.decode("ascii").lower() for item in HASH_BYTES.findall(value)}


class MediaLocator:
    """Keep source connections and bounded directory indexes for a whole import.

    ``decrypted_root`` contains checkpointed contact/message SQLite files.
    ``account_root`` is the original account directory containing ``msg``.
    ``locate`` returns a status, a kind, candidates, and a fixed detail code.
    Paths and voice bytes are private data for the caller, not log messages.
    Use this as a context manager and copy candidates before closing it.
    """

    def __init__(self, decrypted_root: Path, account_root: Path):
        self.root = _root(decrypted_root)
        self.account_root = _root(account_root)
        self._connections: dict[Path, sqlite3.Connection] = {}
        self._states: dict[Path, tuple] = {}
        self._chat_names: dict[str, str | None] = {}
        self._resource = None
        self._resource_chats: dict[str, int] = {}
        self._voices: dict[tuple, list[tuple]] = {}
        self._voice_local: dict[tuple, list[tuple]] = {}
        self._folders: OrderedDict[Path, dict] = OrderedDict()
        self._video_probes: OrderedDict[tuple, dict | None] = OrderedDict()
        self._closed = False
        try:
            message_dir = _safe_path(self.root, Path("message"), directory=True)
            self._message_paths = sorted(path for path in message_dir.iterdir()
                                         if re.fullmatch(r"message_[0-9]+\.db", path.name))
            for path in self._message_paths:
                connection = self._open(path)
                _columns(connection, "Name2Id", {"user_name"})
                for row in connection.execute("SELECT user_name FROM Name2Id"):
                    self._remember(row[0])
            contact = self.root / "contact/contact.db"
            if contact.exists() or contact.is_symlink():
                connection = self._open(contact)
                _columns(connection, "contact", {"username"})
                for row in connection.execute("SELECT username FROM contact"):
                    self._remember(row[0])
            resource = message_dir / "message_resource.db"
            if resource.exists() or resource.is_symlink():
                self._load_resources(self._open(resource))
            voice_paths = sorted(path for path in message_dir.iterdir()
                                 if re.fullmatch(r"media_[0-9]+\.db", path.name))
            for path in voice_paths:
                self._load_voices(self._open(path))
        except Exception:
            self.close(check=False)
            raise

    def _open(self, path: Path) -> sqlite3.Connection:
        path = _safe_path(self.root, path.relative_to(self.root))
        self._check_journal(path)
        self._states[path] = _signature(path.lstat())
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        self._connections[path] = connection
        return connection

    @staticmethod
    def _check_journal(path: Path) -> None:
        for suffix in ("-wal", "-WAL", "-journal"):
            candidate = path.with_name(path.name + suffix)
            if candidate.exists() or candidate.is_symlink():
                value = candidate.lstat()
                if not stat.S_ISREG(value.st_mode) or value.st_size:
                    raise MediaLocatorError("pending_journal", "Media databases must be checkpointed snapshots.")

    def _remember(self, value: object) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        digest = hashlib.md5(value.encode("utf-8")).hexdigest()
        if digest in self._chat_names and self._chat_names[digest] != value:
            self._chat_names[digest] = None
        else:
            self._chat_names[digest] = value
        return value

    def _load_resources(self, connection: sqlite3.Connection) -> None:
        _columns(connection, "ChatName2Id", {"user_name"})
        _columns(connection, "MessageResourceInfo", {"message_id", "chat_id", "message_local_type",
                 "message_create_time", "message_local_id", "message_svr_id", "packed_info"})
        _columns(connection, "MessageResourceDetail", {"message_id", "size", "status", "packed_info"})
        for row in connection.execute("SELECT rowid,user_name FROM ChatName2Id"):
            username = self._remember(row["user_name"])
            if username:
                self._resource_chats[username] = row["rowid"]
        # The real database has indexes on (chat_id, message_svr_id) and on
        # detail.message_id. A compact identity map also supports snapshots
        # whose indexes were stripped by third-party exports.
        self._resource = connection
        self._resource_server: dict[tuple, list[int]] = {}
        self._resource_local: dict[tuple, list[int]] = {}
        for row in connection.execute("SELECT message_id,chat_id,message_svr_id,message_local_id,"
                                      "message_create_time,message_local_type FROM MessageResourceInfo "
                                      "WHERE message_local_type IN (3,43)"):
            value = row["message_id"]
            key = (row["chat_id"], row["message_svr_id"], row["message_local_type"])
            if row["message_svr_id"]:
                self._resource_server.setdefault(key, []).append(value)
            key = (row["chat_id"], row["message_local_id"], row["message_create_time"], row["message_local_type"])
            self._resource_local.setdefault(key, []).append(value)

    def _load_voices(self, connection: sqlite3.Connection) -> None:
        _columns(connection, "Name2Id", {"user_name"})
        _columns(connection, "VoiceInfo", {"chat_name_id", "create_time", "local_id", "svr_id", "voice_data"})
        names = {row["rowid"]: self._remember(row["user_name"])
                 for row in connection.execute("SELECT rowid,user_name FROM Name2Id")}
        cursor = connection.execute("SELECT rowid,chat_name_id,create_time,local_id,svr_id,length(voice_data) AS size FROM VoiceInfo")
        for row in cursor:
            chat = names.get(row["chat_name_id"])
            if not chat:
                continue
            record = (connection, row["rowid"], row["local_id"], row["create_time"], row["size"] or 0)
            if row["svr_id"]:
                self._voices.setdefault((chat, row["svr_id"]), []).append(record)
            self._voice_local.setdefault((chat, row["local_id"], row["create_time"]), []).append(record)

    def iter_media_messages(self):
        """Stream identities without loading message content or media blobs."""
        if self._closed:
            raise MediaLocatorError("closed", "The media locator is closed.")
        for path in self._message_paths:
            connection = self._connections[path]
            names = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            for row in names:
                table = row["name"]
                matched = MESSAGE_TABLE.fullmatch(table)
                if not matched:
                    continue
                _columns(connection, table, {"local_id", "server_id", "create_time", "local_type"})
                cursor = connection.execute('SELECT local_id,server_id,create_time,local_type FROM "' + table +
                                            '" WHERE local_type IN (3,34,43) ORDER BY local_id')
                while rows := cursor.fetchmany(BATCH_SIZE):
                    for message in rows:
                        yield {"database_name": path.name, "table_name": table.lower(),
                               "local_id": str(_integer(message["local_id"])),
                               "server_id": _integer(message["server_id"]) if message["server_id"] is not None else 0,
                               "create_time": _integer(message["create_time"]),
                               "chat_username": self._chat_names.get(matched[1].lower()),
                               "kind": KINDS[message["local_type"]]}
        self.assert_unchanged()

    @staticmethod
    def _result(ref: dict, status: str, detail: str, candidates: list | None = None) -> dict:
        return {"status": status, "kind": ref.get("kind"), "detail": detail, "candidates": candidates or []}

    def locate(self, ref: dict) -> dict:
        """Resolve exactly one message, preserving missing/ambiguous statuses."""
        if self._closed:
            raise MediaLocatorError("closed", "The media locator is closed.")
        if ref.get("kind") not in TYPES:
            return self._result(ref, "unsupported", "unsupported_message_type")
        chat = ref.get("chat_username")
        if not isinstance(chat, str) or not chat:
            return self._result(ref, "missing", "conversation_identity_missing")
        if ref.get("table_name") is not None:
            table = MESSAGE_TABLE.fullmatch(str(ref["table_name"]))
            if not table or table[1].lower() != hashlib.md5(chat.encode("utf-8")).hexdigest():
                return self._result(ref, "unsupported", "conversation_identity_mismatch")
        try:
            server = _integer(ref.get("server_id", 0) or 0)
            local = _integer(ref["local_id"])
            timestamp = _integer(ref["create_time"])
            if ref["kind"] == "audio":
                return self._locate_voice(ref, chat, server, local, timestamp)
            return self._locate_file(ref, chat, server, local, timestamp)
        except FileNotFoundError:
            return self._result(ref, "missing", "media_not_downloaded")
        except PermissionError:
            return self._result(ref, "missing", "media_access_denied")
        except MediaLocatorError as exc:
            return self._result(ref, "unsafe" if exc.code == "unsafe_source" else "unsupported", exc.code)
        except (KeyError, TypeError, ValueError, OverflowError, OSError, sqlite3.DatabaseError):
            return self._result(ref, "unsupported", "invalid_media_metadata")

    def _locate_voice(self, ref, chat, server, local, timestamp):
        records = self._voices.get((chat, server), []) if server else self._voice_local.get((chat, local, timestamp), [])
        if not records:
            return self._result(ref, "missing", "voice_not_downloaded")
        if len(records) != 1:
            return self._result(ref, "ambiguous", "multiple_voice_records")
        connection, rowid, _, _, size = records[0]
        if not 0 < size <= MAX_VOICE_BYTES:
            return self._result(ref, "unsupported" if size else "missing", "voice_size_unsupported" if size else "voice_not_downloaded")
        body = connection.execute("SELECT voice_data FROM VoiceInfo WHERE rowid=?", (rowid,)).fetchone()[0]
        if not isinstance(body, bytes) or len(body) != size:
            return self._result(ref, "unsupported", "invalid_voice_blob")
        return self._result(ref, "ready", "voice_blob_matched", [{"voice_data": body, "size": size}])

    def _locate_file(self, ref, chat, server, local, timestamp):
        if self._resource is None:
            return self._result(ref, "missing", "resource_database_missing")
        chat_id = self._resource_chats.get(chat)
        typ = TYPES[ref["kind"]]
        identities = (self._resource_server.get((chat_id, server, typ), []) if server else
                      self._resource_local.get((chat_id, local, timestamp, typ), []))
        if not identities:
            return self._result(ref, "missing", "resource_metadata_missing")
        rows = []
        for ident in identities:
            rows.append(self._resource.execute(
                "SELECT message_id,message_local_id,message_create_time,"
                "CASE WHEN length(packed_info)<=? THEN packed_info ELSE NULL END AS packed_info,"
                "length(packed_info) AS size FROM MessageResourceInfo WHERE message_id=?",
                (MAX_PACKED_BYTES, ident)).fetchone())
        if len(rows) > 1:
            rows = [row for row in rows if row["message_local_id"] == local and row["message_create_time"] == timestamp]
            if not rows:
                return self._result(ref, "ambiguous", "multiple_resource_records")
        hashes, sizes, previous_hashes = set(), set(), None
        for row in rows:
            if row["size"] and row["size"] > MAX_PACKED_BYTES:
                return self._result(ref, "unsupported", "resource_metadata_too_large")
            row_hashes = _hashes(row["packed_info"])
            for detail in self._resource.execute(
                    "SELECT size,status,CASE WHEN length(packed_info)<=? THEN packed_info ELSE NULL END AS packed_info "
                    "FROM MessageResourceDetail WHERE message_id=?", (MAX_PACKED_BYTES, row["message_id"])):
                row_hashes.update(_hashes(detail["packed_info"]))
                if detail["status"] == 1 and type(detail["size"]) is int and detail["size"] > 0:
                    sizes.add(detail["size"])
            # Mac WeChat can retain duplicate resource rows for the same
            # server/local/time identity. Merge only identical hash sets;
            # conflicting resource identities remain explicitly ambiguous.
            if previous_hashes is not None and previous_hashes != row_hashes:
                return self._result(ref, "ambiguous", "conflicting_resource_records")
            previous_hashes = row_hashes
            hashes.update(row_hashes)
        if not hashes or not sizes:
            return self._result(ref, "missing", "media_not_downloaded")
        # Both adjacent month interpretations are exact and bounded. WeChat
        # records may have been created before the Mac's timezone was changed.
        months = {datetime.fromtimestamp(timestamp).strftime("%Y-%m"),
                  datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m")}
        group_hash = hashlib.md5(chat.encode("utf-8")).hexdigest()
        candidates = {}
        for month in sorted(months):
            relative = Path("msg/attach") / group_hash / month / "Img" if typ == 3 else Path("msg/video") / month
            try:
                folder = self._folder(relative)
            except FileNotFoundError:
                continue
            for digest in hashes:
                for item in folder.get(digest, []):
                    if item["size"] in sizes:
                        candidates[item["path"]] = item
        if not candidates:
            return self._result(ref, "missing", "media_not_downloaded")
        values = sorted(candidates.values(), key=lambda item: (item["thumbnail"], -item["size"], str(item["path"])))
        if typ == 43:
            videos = []
            for item in values:
                with self.open_candidate(item) as stream:
                    header = stream.read(16)
                if len(header) >= 12 and header[4:8] == b"ftyp":
                    videos.append(item)
            values = videos
            if not values:
                return self._result(ref, "missing", "video_original_not_downloaded")
        if typ == 43 and len(values) != 1:
            # Exact duplicate content is safe to collapse regardless of its
            # filename. Different-sized encodings are not byte duplicates.
            if len({item["size"] for item in values}) == 1:
                digests = set()
                for item in values:
                    digest = hashlib.sha256()
                    with self.open_candidate(item) as stream:
                        while chunk := stream.read(1024 * 1024):
                            digest.update(chunk)
                    digests.add(digest.digest())
                    if len(digests) > 1:
                        break
                if len(digests) == 1:
                    return self._result(ref, "ready", "identical_video_files", [values[0]])
            selected = self._video_quality_variant(values)
            if selected is not None:
                return self._result(ref, "ready", "video_quality_variant", [selected])
            return self._result(ref, "ambiguous", "multiple_video_files")
        # Recheck the selected path, including parents, after consulting a
        # cached directory index. The consumer must also use open_candidate.
        for item in values:
            path = _safe_path(self.account_root, item["path"].relative_to(self.account_root))
            if _signature(path.lstat()) != item["signature"]:
                return self._result(ref, "missing", "media_source_changed")
        return self._result(ref, "ready", "resource_file_matched", values)

    def _video_quality_variant(self, candidates: list[dict]) -> dict | None:
        """Recognize only the verified standard/original two-file family."""
        if len(candidates) != 2:
            return None
        by_name = {item["path"].name.lower(): item for item in candidates}
        if len(by_name) != 2:
            return None
        families = {name[:32] for name in by_name}
        if len(families) != 1:
            return None
        family = next(iter(families))
        if set(by_name) != {family + ".mp4", family + "_raw.mp4"}:
            return None
        variants = []
        for name, candidate in by_name.items():
            metadata = self._video_properties(candidate)
            if metadata is None:
                return None
            variants.append({**candidate, **metadata, "quality_variant": True,
                             "variant_role": "original" if name.endswith("_raw.mp4") else "derived"})
        if abs(variants[0]["duration"] - variants[1]["duration"]) > 0.1 + 1e-9:
            return None
        return max(variants, key=lambda item: (item["width"] * item["height"], item["variant_role"] == "original"))

    def _video_properties(self, candidate: dict) -> dict | None:
        """Probe bounded public media metadata, caching by source signature."""
        key = (str(candidate["path"]), tuple(candidate["signature"]))
        if key in self._video_probes:
            self._video_probes.move_to_end(key)
            return self._video_probes[key]
        executable = next((path for path in ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe")
                           if os.path.isfile(path) and os.access(path, os.X_OK)), None) or shutil.which("ffprobe")
        metadata = None
        if executable:
            try:
                # Inherit a safely opened descriptor instead of making ffprobe
                # reopen an unchecked path. No private filename reaches argv.
                with self.open_candidate(candidate) as stream:
                    descriptor = stream.fileno()
                    result = subprocess.run(
                        [executable, "-v", "error", "-show_entries",
                         "format=duration:stream=codec_type,width,height", "-of", "json", f"/dev/fd/{descriptor}"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        timeout=20, check=False, pass_fds=(descriptor,),
                    )
                if result.returncode == 0 and len(result.stdout) <= 64 * 1024:
                    data = json.loads(result.stdout)
                    videos = [item for item in data.get("streams", []) if item.get("codec_type") == "video"]
                    duration = float(data.get("format", {}).get("duration", 0))
                    if len(videos) == 1 and math.isfinite(duration) and duration > 0:
                        width, height = videos[0].get("width"), videos[0].get("height")
                        if type(width) is int and type(height) is int and 0 < width <= 65536 and 0 < height <= 65536:
                            metadata = {"width": width, "height": height, "duration": duration}
            except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
                metadata = None
        self._video_probes[key] = metadata
        if len(self._video_probes) > 1024:
            self._video_probes.popitem(last=False)
        return metadata

    def _folder(self, relative: Path) -> dict:
        folder = _safe_path(self.account_root, relative, directory=True)
        if folder in self._folders:
            self._folders.move_to_end(folder)
            return self._folders[folder]
        result = {}
        with os.scandir(folder) as entries:
            for entry in entries:
                matched = FILE_HASH.fullmatch(entry.name)
                if not matched:
                    continue
                value = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(value.st_mode):
                    continue
                item = {"path": Path(entry.path), "size": value.st_size,
                        "thumbnail": entry.name.lower().endswith(("_t.dat", "_thumb.dat")),
                        "signature": _signature(value)}
                result.setdefault(matched[1].lower(), []).append(item)
        self._folders[folder] = result
        if len(self._folders) > 256:
            self._folders.popitem(last=False)
        return result

    @contextmanager
    def open_candidate(self, candidate: dict):
        """Open a selected regular file safely and verify it after consumption."""
        path = Path(candidate["path"])
        relative = path.relative_to(self.account_root)
        path = _safe_path(self.account_root, relative)
        # Open each ancestor relative to a held directory descriptor. Merely
        # checking lstat before open would allow a parent symlink replacement.
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(self.account_root, directory_flags)
        try:
            for part in relative.parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            fd = os.open(relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        with os.fdopen(fd, "rb") as stream:
            if _signature(os.fstat(stream.fileno())) != tuple(candidate["signature"]):
                raise MediaLocatorError("source_changed", "A media source changed before reading.")
            yield stream
            path = _safe_path(self.account_root, relative)
            if (_signature(os.fstat(stream.fileno())) != tuple(candidate["signature"]) or
                    _signature(path.lstat()) != tuple(candidate["signature"])):
                raise MediaLocatorError("source_changed", "A media source changed during reading.")

    def assert_unchanged(self) -> None:
        for path, expected in self._states.items():
            self._check_journal(path)
            _safe_path(self.root, path.relative_to(self.root))
            if _signature(path.lstat()) != expected:
                raise MediaLocatorError("source_changed", "A decrypted media snapshot changed during import.")

    def close(self, check: bool = True) -> None:
        if self._closed:
            return
        try:
            if check:
                self.assert_unchanged()
        finally:
            for connection in self._connections.values():
                connection.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close(check=exc_type is None)
