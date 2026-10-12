"""Import already-decrypted WeChat 4.x SQLite snapshots without altering them."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
from typing import Callable, Iterator

from vault.library import Library, normalize_time
from vault.wechat_avatars import avatar_identity


MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MESSAGE_BATCH_SIZE = 8
METADATA_BATCH_SIZE = 512
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
MESSAGE_TABLE = re.compile(r"Msg_([0-9a-f]{32})\Z", re.IGNORECASE)
MESSAGE_COLUMNS = {"local_id", "server_id", "local_type", "create_time",
                   "real_sender_id", "message_content"}


class WeChatImportError(ValueError):
    """An import failure whose message never includes conversation content."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _file_state(path: Path) -> tuple:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise WeChatImportError("unsafe_source", "Source databases must be regular files.")
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _assert_no_pending_journal(path: Path) -> None:
    for suffix in ("-wal", "-WAL", "-journal"):
        journal = path.with_name(path.name + suffix)
        try:
            value = journal.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(value.st_mode) or value.st_size:
            raise WeChatImportError(
                "source_wal_present" if suffix.lower() == "-wal" else "source_journal_present",
                "Source databases have a pending journal; use a fully checkpointed snapshot.",
            )


@contextmanager
def _open_source(path: Path) -> Iterator[sqlite3.Connection]:
    _file_state(path)
    _assert_no_pending_journal(path)
    # immutable prevents SQLite from creating SHM or journal sidecars. The
    # caller also verifies every input file's metadata before committing.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        yield connection
    finally:
        connection.close()


def _require_columns(connection: sqlite3.Connection, table: str, required: set[str]) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({_identifier(table)})")}
    if not required.issubset(columns):
        raise WeChatImportError("unsupported_schema", "A source database has an unsupported schema.")


def _metadata_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeError:
            pass
    raise WeChatImportError("invalid_metadata", "A source identifier or display name is invalid.")


def _integer(value: object) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    raise WeChatImportError("invalid_message", "A message contains an invalid numeric field.")


def _decompress(raw: bytes) -> bytes:
    try:
        import zstandard
    except ImportError:
        try:
            from compression import zstd
        except ImportError as exc:
            raise WeChatImportError("zstd_unavailable", "Zstandard support is required to read compressed text.") from exc
        try:
            decoder = zstd.ZstdDecompressor(options={zstd.DecompressionParameter.window_log_max: 24})
            result = decoder.decompress(raw, max_length=MAX_MESSAGE_BYTES + 1)
            if len(result) > MAX_MESSAGE_BYTES:
                raise WeChatImportError("message_too_large", "A decompressed message exceeds the 16 MB limit.")
            if not decoder.eof or decoder.unused_data:
                raise WeChatImportError("invalid_compressed_text", "A compressed text message is incomplete or unsupported.")
            return result
        except zstd.ZstdError as exc:
            raise WeChatImportError("invalid_compressed_text", "A compressed text message cannot be decoded.") from exc
    try:
        parameters = zstandard.get_frame_parameters(raw)
        if (parameters.window_size > MAX_MESSAGE_BYTES or
                (parameters.content_size != zstandard.CONTENTSIZE_UNKNOWN
                 and parameters.content_size > MAX_MESSAGE_BYTES)):
            raise WeChatImportError("message_too_large", "A decompressed message exceeds the 16 MB limit.")
        # max_output_size alone does not constrain frames that advertise their
        # size, so the frame header must be checked before allocating output.
        result = zstandard.ZstdDecompressor().decompress(
            raw, max_output_size=MAX_MESSAGE_BYTES, allow_extra_data=False,
        )
        if len(result) > MAX_MESSAGE_BYTES:
            raise WeChatImportError("message_too_large", "A decompressed message exceeds the 16 MB limit.")
        return result
    except zstandard.ZstdError as exc:
        raise WeChatImportError("invalid_compressed_text", "A compressed text message cannot be decoded.") from exc


def _message_text(value: object, local_type: int, content_bytes: object,
                  chat_username: str | None, sender_username: str | None) -> str:
    if local_type != 1:
        return f"[类型{local_type}消息]"
    if content_bytes is not None and _integer(content_bytes) > MAX_MESSAGE_BYTES:
        raise WeChatImportError("message_too_large", "A text message exceeds the 16 MB limit.")
    if value is None:
        text = ""
    elif isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise WeChatImportError("message_too_large", "A text message exceeds the 16 MB limit.")
        text = value
    elif isinstance(value, bytes):
        raw = _decompress(value) if value.startswith(ZSTD_MAGIC) else value
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise WeChatImportError("invalid_text", "A text message is not valid UTF-8.") from exc
    else:
        raise WeChatImportError("invalid_text", "A text message uses an unsupported storage type.")
    if chat_username and chat_username.endswith("@chatroom") and sender_username:
        prefix = sender_username + ":\n"
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def _source_paths(root: Path) -> tuple[Path | None, list[Path]]:
    message_dir, contact_dir = root / "message", root / "contact"
    if message_dir.is_symlink() or contact_dir.is_symlink():
        raise WeChatImportError("unsafe_source", "Source database directories must not be symbolic links.")
    paths = sorted(path for path in message_dir.glob("message_*.db")
                   if re.fullmatch(r"message_[0-9]+\.db", path.name))
    if not paths:
        raise WeChatImportError("message_databases_missing", "No decrypted message databases were found.")
    contact = contact_dir / "contact.db"
    if not contact.exists() and not contact.is_symlink():
        contact = None
    return contact, paths


def import_decrypted(root: Path, library: Library, progress: Callable[[dict], None] | None = None,
                     account_id: str | None = None, self_username: str | None = None) -> dict:
    """Import a quiescent, already-decrypted ``db_storage`` directory.

    Counts describe newly inserted conversations/messages and changed existing
    messages. Repeating an unchanged import returns three zero counts. Supply a
    stable original account identity when the decrypted directory is temporary.
    ``self_username`` must be supplied explicitly; this reader never guesses it.
    This function does not decrypt databases or extract any WeChat keys.
    """
    root_input = Path(root).expanduser()
    if root_input.is_symlink():
        raise WeChatImportError("unsafe_source", "The source root must not be a symbolic link.")
    root = root_input.resolve(strict=True)
    if not root.is_dir():
        raise WeChatImportError("invalid_source", "The decrypted source must be a directory.")
    identity = str(root) if account_id is None else account_id
    if not isinstance(identity, str) or not identity.strip():
        raise WeChatImportError("invalid_account", "A nonempty account identity is required.")
    if self_username is not None and not isinstance(self_username, str):
        raise WeChatImportError("invalid_account", "The self username must be text.")
    account_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    import_digest = "wechat-local-" + account_key
    contact_path, message_paths = _source_paths(root)
    all_paths = ([contact_path] if contact_path is not None else []) + message_paths
    original_states = {path: _file_state(path) for path in all_paths}
    for path in all_paths:
        _assert_no_pending_journal(path)
    contacts: dict[str, str] = {}
    chat_names: dict[str, str | None] = {}

    def remember(username: str) -> None:
        if not username:
            return
        digest = hashlib.md5(username.encode("utf-8")).hexdigest()
        if digest in chat_names and chat_names[digest] != username:
            chat_names[digest] = None
        else:
            chat_names[digest] = username

    result = {"conversations": 0, "messages": 0, "updated_messages": 0}
    processed_messages = 0
    processed_databases = 0

    def report() -> None:
        if progress:
            progress({"done_databases": processed_databases, "total_databases": len(message_paths),
                      "done_messages": processed_messages, **result})

    try:
        if contact_path is not None:
            with _open_source(contact_path) as source:
                _require_columns(source, "contact", {"username", "nick_name", "remark"})
                cursor = source.execute("SELECT username, nick_name, remark FROM contact")
                while rows := cursor.fetchmany(METADATA_BATCH_SIZE):
                    for row in rows:
                        username = _metadata_text(row["username"])
                        if username:
                            contacts[username] = (_metadata_text(row["remark"]) or
                                                  _metadata_text(row["nick_name"]) or username)
                            remember(username)
        # A conversation's username may appear only in another message shard.
        # Build one reverse hash index without retaining every shard's row map.
        for path in message_paths:
            with _open_source(path) as source:
                _require_columns(source, "Name2Id", {"user_name"})
                cursor = source.execute("SELECT user_name FROM Name2Id")
                while rows := cursor.fetchmany(METADATA_BATCH_SIZE):
                    for row in rows:
                        remember(_metadata_text(row["user_name"]))

        with library.connect() as destination:
            destination.execute("BEGIN IMMEDIATE")
            destination.execute("""
                CREATE TABLE IF NOT EXISTS wechat_source_messages (
                    account_key TEXT NOT NULL,
                    database_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    local_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL UNIQUE,
                    source_digest TEXT NOT NULL,
                    PRIMARY KEY (account_key, database_name, table_name, local_id)
                )
            """)
            destination.execute("INSERT OR IGNORE INTO imports(digest, filename, imported_at) VALUES(?,?,?)",
                                (import_digest, "WeChat local databases", datetime.now(timezone.utc).isoformat()))
            report()
            for path in message_paths:
                with _open_source(path) as source:
                    senders: dict[int, str] = {}
                    sender_identities: dict[int, str] = {}
                    cursor = source.execute("SELECT rowid AS sender_id, user_name FROM Name2Id")
                    while rows := cursor.fetchmany(METADATA_BATCH_SIZE):
                        for row in rows:
                            sender_id = _integer(row["sender_id"])
                            username = _metadata_text(row["user_name"])
                            senders[sender_id] = username
                            if username:
                                sender_identities[sender_id] = avatar_identity(account_key, username)
                    tables = source.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                    table_names = [row["name"] for row in tables if row["name"].lower().startswith("msg_")]
                    for table in table_names:
                        matched = MESSAGE_TABLE.fullmatch(table)
                        if not matched:
                            raise WeChatImportError("unsupported_schema", "A message table has an unsupported name.")
                        _require_columns(source, table, MESSAGE_COLUMNS)
                        table_hash = matched.group(1).lower()
                        chat_username = chat_names.get(table_hash)
                        title = (contacts.get(chat_username, chat_username) if chat_username else
                                 "未命名会话 " + table_hash[:8])
                        conversation_id = "wechat-" + hashlib.sha256(
                            (account_key + ":" + table_hash).encode("ascii")
                        ).hexdigest()
                        # Do not materialize media/XML blobs or oversized text
                        # while fetching a batch from an untrusted source file.
                        cursor = source.execute(
                            "SELECT local_id, server_id, local_type, create_time, real_sender_id, "
                            "length(CAST(message_content AS BLOB)) AS content_bytes, "
                            "CASE WHEN local_type=1 AND length(CAST(message_content AS BLOB))<=? "
                            "THEN message_content ELSE NULL END AS message_content "
                            f"FROM {_identifier(table)} ORDER BY local_id", (MAX_MESSAGE_BYTES,),
                        )
                        conversation_ready = False
                        while rows := cursor.fetchmany(MESSAGE_BATCH_SIZE):
                            if not conversation_ready:
                                inserted = destination.execute(
                                    "INSERT OR IGNORE INTO conversations(id, title, import_digest) VALUES(?,?,?)",
                                    (conversation_id, title, import_digest),
                                ).rowcount
                                result["conversations"] += inserted
                                destination.execute("UPDATE conversations SET title=? WHERE id=?", (title, conversation_id))
                                if chat_username:
                                    destination.execute("""INSERT INTO conversation_avatars VALUES(?,?)
                                        ON CONFLICT(conversation_id) DO UPDATE SET identity_key=excluded.identity_key
                                        WHERE identity_key<>excluded.identity_key""",
                                        (conversation_id, avatar_identity(account_key, chat_username)))
                                else:
                                    destination.execute("DELETE FROM conversation_avatars WHERE conversation_id=?", (conversation_id,))
                                conversation_ready = True
                            for row in rows:
                                local_id = str(_integer(row["local_id"]))
                                local_type = _integer(row["local_type"])
                                sender_id = None if row["real_sender_id"] is None else _integer(row["real_sender_id"])
                                sender_username = senders.get(sender_id)
                                sender = contacts.get(sender_username, sender_username) or "未知发送者"
                                text = _message_text(row["message_content"], local_type, row["content_bytes"],
                                                     chat_username, sender_username)
                                timestamp = normalize_time(row["create_time"])
                                is_self = int(bool(self_username) and sender_username == self_username)
                                server_id = None if row["server_id"] is None else str(_integer(row["server_id"]))
                                values = (conversation_id, sender, text, timestamp, is_self)
                                digest = hashlib.sha256(json.dumps(
                                    [*values, server_id, local_type], ensure_ascii=False, separators=(",", ":")
                                ).encode("utf-8")).hexdigest()
                                key = (account_key, path.name, table.lower(), local_id)
                                existing = destination.execute("""
                                    SELECT w.source_digest, w.message_id, m.id AS target_id
                                    FROM wechat_source_messages w LEFT JOIN messages m ON m.id=w.message_id
                                    WHERE account_key=? AND database_name=? AND table_name=? AND local_id=?
                                """, key).fetchone()
                                if existing is not None and existing["target_id"] is not None:
                                    message_id = existing["message_id"]
                                    if existing["source_digest"] != digest:
                                        destination.execute("""
                                            UPDATE messages SET conversation_id=?, sender=?, text=?, timestamp=?, is_self=?
                                            WHERE id=?
                                        """, (*values, existing["message_id"]))
                                        destination.execute("""
                                            UPDATE wechat_source_messages SET source_digest=?
                                            WHERE account_key=? AND database_name=? AND table_name=? AND local_id=?
                                        """, (digest, *key))
                                        result["updated_messages"] += 1
                                else:
                                    message_id = destination.execute("""
                                        INSERT INTO messages(conversation_id, sender, text, timestamp, is_self)
                                        VALUES(?,?,?,?,?)
                                    """, values).lastrowid
                                    destination.execute("""
                                        INSERT INTO wechat_source_messages
                                        (account_key, database_name, table_name, local_id, message_id, source_digest)
                                        VALUES(?,?,?,?,?,?)
                                        ON CONFLICT(account_key, database_name, table_name, local_id)
                                        DO UPDATE SET message_id=excluded.message_id, source_digest=excluded.source_digest
                                    """, (*key, message_id, digest))
                                    result["messages"] += 1
                                sender_identity = sender_identities.get(sender_id)
                                if sender_identity:
                                    destination.execute("""INSERT INTO message_avatars VALUES(?,?)
                                        ON CONFLICT(message_id) DO UPDATE SET identity_key=excluded.identity_key
                                        WHERE identity_key<>excluded.identity_key""", (message_id, sender_identity))
                                else:
                                    destination.execute("DELETE FROM message_avatars WHERE message_id=?", (message_id,))
                                processed_messages += 1
                            report()
                processed_databases += 1
                report()
            if _source_paths(root) != (contact_path, message_paths):
                raise WeChatImportError("source_changed", "The source database inventory changed during import.")
            for path, previous in original_states.items():
                _assert_no_pending_journal(path)
                if _file_state(path) != previous:
                    raise WeChatImportError("source_changed", "A source database changed during import.")
        return result
    except WeChatImportError:
        raise
    except sqlite3.DatabaseError as exc:
        raise WeChatImportError("invalid_database", "A source database cannot be read or the import could not be committed.") from exc
    except UnicodeError as exc:
        raise WeChatImportError("invalid_text", "A source text field cannot be decoded.") from exc
