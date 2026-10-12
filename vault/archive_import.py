"""Merge portable readable archives without replacing existing local records."""

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import zipfile

from vault.library import Library
from vault.media_store import MIMES, signature
from vault.readable_archive import (
    ArchiveSource, CHUNK, _copy, _inventory, _local_input, _media_path,
    _validate_library, _verify_gzip,
)

HEX = re.compile(r"[a-f0-9]{64}\Z")
BATCH = 500
MESSAGE_FIELDS = ("conversation_id", "sender", "text", "timestamp", "is_self")
ASSET_FIELDS = ("id", "kind", "status", "mime_type", "filename", "object_hash",
                "encoding", "byte_size", "stored_size", "detail")
WECHAT_FIELDS = ("account_key", "database_name", "table_name", "local_id", "message_id", "source_digest")


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _columns(db, name):
    return {row[1] for row in db.execute(f'PRAGMA table_info("{name}")')}


def _optional_schema(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    optional = {
        "wechat_source_messages": set(WECHAT_FIELDS),
        "media_import_revisions": {"message_id", "kind", "source_digest"},
        "archive_message_origins": {"origin_key", "message_id", "payload_digest"},
        "archive_origin_lineage": {"origin_key", "canonical_key"},
    }
    for name, fields in optional.items():
        if name in tables and not fields <= _columns(db, name):
            raise ValueError("Archive import metadata has an unsupported schema")
    if "archive_origin_lineage" in tables:
        if db.execute("SELECT 1 FROM archive_origin_lineage GROUP BY origin_key HAVING COUNT(*)>1 LIMIT 1").fetchone():
            raise ValueError("Archive provenance lineage resolves ambiguously")
    if "archive_message_origins" in tables:
        if db.execute("SELECT 1 FROM archive_message_origins GROUP BY origin_key HAVING COUNT(*)>1 LIMIT 1").fetchone():
            raise ValueError("Archive message provenance resolves ambiguously")
    if "wechat_source_messages" in tables:
        if db.execute("""SELECT 1 FROM wechat_source_messages GROUP BY message_id HAVING COUNT(*)>1 LIMIT 1""").fetchone():
            raise ValueError("Archive source message identities are ambiguous")
        if db.execute("""SELECT 1 FROM wechat_source_messages
                GROUP BY account_key,database_name,table_name,local_id HAVING COUNT(*)>1 LIMIT 1""").fetchone():
            raise ValueError("Archive source message identities are ambiguous")
    return tables


@contextmanager
def _source_database(source, archive, folder):
    path = folder / "incoming.sqlite3"
    item = source.files["library.sqlite3"]
    digest = hashlib.sha256()
    with archive.open("library.sqlite3") as inp, path.open("xb") as out:
        os.fchmod(out.fileno(), 0o600)
        _copy(inp, out, item["size"], digest)
    if digest.hexdigest() != item["sha256"]:
        raise ValueError("Archived conversation library failed its integrity check")
    _validate_library(path, source.files)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        yield db, _optional_schema(db)


def _required_bytes(source, objects, library):
    # Include the SQLite snapshot, transaction growth and one object in flight.
    missing = sum(item["stored_size"] for item in objects
                  if library is None or not (library.state_dir / item["relative"]).exists())
    return missing + 3 * source.files["library.sqlite3"]["size"] + max(
        (item["stored_size"] for item in objects), default=0)


def _archive_import_info(path, library=None):
    """Inspect schemas and counts without importing records or caching media.

    Every media byte is checked during import; this inexpensive preview only
    verifies the database and declared object inventory. No manifest summary
    is trusted. Estimated space includes conservative transaction overhead.
    """
    source = ArchiveSource(path)
    with tempfile.TemporaryDirectory(prefix="shiliao-import-preview-") as temporary:
        with source.read_session() as archive:
            with _source_database(source, archive, Path(temporary)) as (db, tables):
                objects, summary = _inventory(db)
                required = _required_bytes(source, objects, library)
                unverified = _unknown_revisions(db, tables)
    result = {"filename": source.path.name, "summary": summary, "required_bytes": required, "unverified_media": unverified}
    if library is not None:
        result["available_bytes"] = shutil.disk_usage(library.state_dir).free
    return result


def archive_import_info(path, library=None):
    """Return verified database counts and a conservative space estimate."""
    try:
        return _archive_import_info(path, library)
    except (zipfile.BadZipFile, sqlite3.DatabaseError, KeyError, json.JSONDecodeError, UnicodeError, TypeError, AttributeError):
        raise ValueError("请选择有效的拾聊可读 ZIP 归档，原始微信备份不能作为聊天归档导入。") from None


def _schema(db):
    # Execute statements separately: executescript would commit the transaction.
    for statement in (
        """CREATE TABLE IF NOT EXISTS archive_message_origins (
            origin_key TEXT PRIMARY KEY, message_id INTEGER NOT NULL, payload_digest TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS archive_origins_message ON archive_message_origins(message_id)",
        "CREATE TABLE IF NOT EXISTS archive_origin_lineage (origin_key TEXT PRIMARY KEY, canonical_key TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS wechat_source_messages (
            account_key TEXT NOT NULL,database_name TEXT NOT NULL,table_name TEXT NOT NULL,
            local_id TEXT NOT NULL,message_id INTEGER NOT NULL UNIQUE,source_digest TEXT NOT NULL,
            PRIMARY KEY(account_key,database_name,table_name,local_id))""",
        "CREATE TEMP TABLE incoming_messages (source_id INTEGER PRIMARY KEY,target_id INTEGER NOT NULL)",
        "CREATE TEMP TABLE incoming_assets (source_id TEXT PRIMARY KEY,target_id TEXT NOT NULL)",
        "CREATE TEMP TABLE incoming_conversations (id TEXT PRIMARY KEY)",
    ):
        db.execute(statement)


def _verify_object(stream, item):
    if item["encoding"] == "gzip":
        _verify_gzip(stream, item["object_hash"], item["byte_size"])
    else:
        digest, size = hashlib.sha256(), 0
        while chunk := stream.read(CHUNK):
            size += len(chunk)
            if size > item["byte_size"]:
                raise ValueError("A media object exceeds its declared size")
            digest.update(chunk)
        if size != item["byte_size"] or digest.hexdigest() != item["object_hash"]:
            raise ValueError("A media object failed its content integrity check")


def _objects(library, source, archive, folder, objects, created, progress):
    sizes, added, skipped = {}, 0, 0
    for parent in (library.media.root, library.media.objects, library.media.cache):
        if parent.is_symlink():
            raise ValueError("Media storage folders must not be symbolic links")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for index, item in enumerate(objects, 1):
        relative = item["relative"]
        incoming = folder / "object"
        digest = hashlib.sha256()
        with archive.open(relative) as inp, incoming.open("xb") as out:
            os.fchmod(out.fileno(), 0o600)
            _copy(inp, out, item["stored_size"], digest)
            out.flush()
            os.fsync(out.fileno())
        if digest.hexdigest() != source.files[relative]["sha256"]:
            raise ValueError("Readable archive entry failed its integrity check")
        with incoming.open("rb") as inp:
            _verify_object(inp, item)
        target = library.state_dir / relative
        for parent in (library.media.root, library.media.objects, target.parent):
            if parent.is_symlink():
                raise ValueError("Media storage folders must not be symbolic links")
        target.parent.mkdir(mode=0o700, exist_ok=True)
        published = False
        try:
            os.link(incoming, target)
        except FileExistsError:
            existing_size = target.lstat().st_size
            with _local_input(target, existing_size, library.state_dir) as inp:
                _verify_object(inp, item)
            skipped += 1
        else:
            created.append((target, signature(target.lstat())))
            published = True
            added += 1
        sizes[relative] = target.stat().st_size
        incoming.unlink()
        if published:
            created[-1] = (target, signature(target.lstat()))
        if progress and (index % 50 == 0 or index == len(objects)):
            progress({"phase": "media", "done_files": index, "total_files": len(objects)})
    return sizes, added, skipped


def _message_values(row):
    values = tuple(row[name] for name in MESSAGE_FIELDS)
    if (type(row["id"]) is not int or row["id"] < 1
            or any(not isinstance(value, str) for value in values[:4])
            or type(values[4]) is not int or values[4] not in (0, 1)):
        raise ValueError("Archive message fields are invalid")
    return values


def _equal_message(db, message_id, values):
    row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    return row is not None and tuple(row[name] for name in MESSAGE_FIELDS) == values


def _source_metadata(source, tables, ids):
    placeholders = ",".join("?" for _ in ids)
    wechat, origins = {}, {}
    if "wechat_source_messages" in tables:
        for row in source.execute(f"SELECT * FROM wechat_source_messages WHERE message_id IN ({placeholders})", ids):
            if (any(not isinstance(row[name], str) for name in WECHAT_FIELDS if name != "message_id")
                    or not HEX.fullmatch(row["account_key"]) or not HEX.fullmatch(row["source_digest"])):
                raise ValueError("Archive source message identity is invalid")
            wechat[row["message_id"]] = row
    if "archive_message_origins" in tables:
        lineage = "archive_origin_lineage" in tables
        query = ("SELECT o.*,l.canonical_key FROM archive_message_origins o LEFT JOIN archive_origin_lineage l ON l.origin_key=o.origin_key"
                 if lineage else "SELECT *,NULL AS canonical_key FROM archive_message_origins")
        for row in source.execute(query + f" WHERE message_id IN ({placeholders})", ids):
            if not all(isinstance(row[name], str) and HEX.fullmatch(row[name])
                       for name in ("origin_key", "payload_digest")):
                raise ValueError("Archive message provenance is invalid")
            if row["canonical_key"] is not None and (not isinstance(row["canonical_key"], str) or not HEX.fullmatch(row["canonical_key"])):
                raise ValueError("Archive provenance lineage is invalid")
            origins.setdefault(row["message_id"], []).append(row)
            if len(origins[row["message_id"]]) > 64:
                raise ValueError("Archive message has too many provenance aliases")
    return wechat, origins


def _message_origin(row, values, wechat, inherited, ordinal):
    payload = _hash(values)
    origins = {value["origin_key"] for value in inherited if value["payload_digest"] == payload}
    origins.update(value["canonical_key"] for value in inherited
                   if value["payload_digest"] == payload and value["canonical_key"] is not None)
    if not origins:
        identity = ([wechat[name] for name in WECHAT_FIELDS if name != "message_id"]
                    if wechat is not None else [values[0], ordinal])
        origins.add(_hash(["wechat" if wechat is not None else "message", identity, payload]))
    return payload, sorted(origins)


def _available_origin(db, origin, payload, values):
    existing = db.execute("SELECT message_id,payload_digest FROM archive_message_origins WHERE origin_key=?", (origin,)).fetchone()
    if existing is None:
        return origin
    if existing["payload_digest"] != payload:
        raise ValueError("Archive message provenance conflicts with existing content")
    if _equal_message(db, existing["message_id"], values):
        return origin
    # Native WeChat updates can legitimately replace the row referenced by an
    # old alias. Preserve that alias and bind its historical payload separately.
    canonical = db.execute("SELECT canonical_key FROM archive_origin_lineage WHERE origin_key=?", (origin,)).fetchone()
    canonical = canonical[0] if canonical else origin
    alternate = _hash(["archive-historical-revision", canonical, payload])
    db.execute("INSERT OR IGNORE INTO archive_origin_lineage VALUES(?,?)", (alternate, canonical))
    existing = db.execute("SELECT message_id,payload_digest FROM archive_message_origins WHERE origin_key=?", (alternate,)).fetchone()
    if existing is not None and (existing["payload_digest"] != payload or not _equal_message(db, existing["message_id"], values)):
        raise ValueError("Archive historical provenance conflicts with existing content")
    return alternate


def _seed_origins(db):
    """Give existing native records portable identities before adding any rows."""
    cursor = db.execute("""SELECT m.* FROM messages m JOIN incoming_conversations c
        ON c.id=m.conversation_id ORDER BY m.id""")
    ordinals = {}
    while rows := cursor.fetchmany(BATCH):
        wechat, inherited = _source_metadata(db, {"wechat_source_messages", "archive_message_origins", "archive_origin_lineage"}, [row["id"] for row in rows])
        for row in rows:
            values = _message_values(row)
            ordinal = ordinals[values[0]] = ordinals.get(values[0], 0) + 1
            payload, origins = _message_origin(row, values, wechat.get(row["id"]), inherited.get(row["id"], []), ordinal)
            for origin in origins:
                db.execute("INSERT OR IGNORE INTO archive_message_origins VALUES(?,?,?)", (origin, row["id"], payload))


def _messages(source, tables, db, counts, progress):
    cursor = source.execute("SELECT * FROM messages ORDER BY id")
    done, ordinals = 0, {}
    while rows := cursor.fetchmany(BATCH):
        wechat_rows, inherited = _source_metadata(source, tables, [row["id"] for row in rows])
        for row in rows:
            values = _message_values(row)
            if not db.execute("SELECT 1 FROM conversations WHERE id=?", (values[0],)).fetchone():
                raise ValueError("Archive message references a missing conversation")
            wechat = wechat_rows.get(row["id"])
            ordinal = ordinals[values[0]] = ordinals.get(values[0], 0) + 1
            payload, origins = _message_origin(row, values, wechat, inherited.get(row["id"], []), ordinal)
            origins = sorted({_available_origin(db, origin, payload, values) for origin in origins})
            candidates = set()
            for origin in origins:
                found = db.execute("SELECT message_id,payload_digest FROM archive_message_origins WHERE origin_key=?", (origin,)).fetchone()
                if found and found["payload_digest"] == payload and _equal_message(db, found["message_id"], values):
                    candidates.add(found["message_id"])
            key = tuple(wechat[name] for name in WECHAT_FIELDS[:4]) if wechat is not None else None
            if not candidates and key is not None:
                found = db.execute("""SELECT message_id,source_digest FROM wechat_source_messages
                    WHERE account_key=? AND database_name=? AND table_name=? AND local_id=?""", key).fetchone()
                if found and found["source_digest"] == wechat["source_digest"] and _equal_message(db, found["message_id"], values):
                    candidates.add(found["message_id"])
            if len(candidates) > 1:
                raise ValueError("Archive message provenance resolves ambiguously")
            if candidates:
                target_id = candidates.pop()
                counts["skipped"]["messages"] += 1
            else:
                target_id = db.execute("""INSERT INTO messages(conversation_id,sender,text,timestamp,is_self)
                    VALUES(?,?,?,?,?)""", values).lastrowid
                counts["added"]["messages"] += 1
            db.execute("INSERT INTO incoming_messages VALUES(?,?)", (row["id"], target_id))
            for origin in origins:
                db.execute("INSERT OR IGNORE INTO archive_message_origins VALUES(?,?,?)", (origin, target_id, payload))
            if wechat is not None and not db.execute("SELECT 1 FROM wechat_source_messages WHERE message_id=?", (target_id,)).fetchone():
                if db.execute("""SELECT 1 FROM wechat_source_messages
                        WHERE account_key=? AND database_name=? AND table_name=? AND local_id=?""", key).fetchone():
                    key = (_hash(["archive-version", *key, payload, wechat["source_digest"]]), *key[1:])
                db.execute("INSERT INTO wechat_source_messages VALUES(?,?,?,?,?,?)", (*key, target_id, wechat["source_digest"]))
        done += len(rows)
        if progress:
            progress({"phase": "messages", "done_messages": done})


def _assets(source, db, sizes):
    for row in source.execute("SELECT * FROM media_assets"):
        value = dict(row)
        if (not isinstance(value["id"], str) or not HEX.fullmatch(value["id"])
                or value["kind"] not in MIMES or value["status"] not in {"ready", "missing", "locked", "unsupported", "error"}
                or any(not isinstance(value[name], str) for name in ("mime_type", "filename", "detail"))):
            raise ValueError("Archive media metadata is invalid")
        if value["status"] == "ready":
            relative = _media_path(value["kind"], value["mime_type"], value["object_hash"], value["encoding"], value["byte_size"], value["stored_size"])
            value["stored_size"] = sizes[relative]
        if value["status"] != "ready":
            # Native unavailable IDs include a local message integer. Keep
            # imported status assets immutable and independent of that ID.
            value["id"] = _hash(["archive-unavailable", *[value[name] for name in ASSET_FIELDS[1:]]])
        existing = db.execute("SELECT * FROM media_assets WHERE id=?", (value["id"],)).fetchone()
        comparable = [name for name in ASSET_FIELDS if name not in {"id", "filename"}]
        if existing is not None and any(existing[name] != value[name] for name in comparable):
            value["id"] = _hash(["archive-asset", *[value[name] for name in ASSET_FIELDS]])
            alternate = db.execute("SELECT * FROM media_assets WHERE id=?", (value["id"],)).fetchone()
            if alternate is not None and any(alternate[name] != value[name] for name in comparable):
                raise ValueError("Archive asset identity conflicts with an existing asset")
        db.execute("""INSERT OR IGNORE INTO media_assets
            (id,kind,status,mime_type,filename,object_hash,encoding,byte_size,stored_size,detail)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", tuple(value[name] for name in ASSET_FIELDS))
        db.execute("INSERT INTO incoming_assets VALUES(?,?)", (row["id"], value["id"]))


def _unknown_revisions(source, tables):
    if "wechat_source_messages" in tables and "media_import_revisions" not in tables:
        return source.execute("""SELECT COUNT(*) FROM message_media m JOIN wechat_source_messages w
            ON w.message_id=m.message_id""").fetchone()[0]
    return 0


def _links(source, tables, db):
    added = 0
    versioned = "wechat_source_messages" in tables
    has_revisions = "media_import_revisions" in tables
    joins = " LEFT JOIN wechat_source_messages w ON w.message_id=m.message_id" if versioned else ""
    if versioned and has_revisions:
        joins += " LEFT JOIN media_import_revisions r ON r.message_id=m.message_id AND r.kind=m.kind"
    extras = (",w.source_digest AS current_digest" if versioned else ",NULL AS current_digest")
    extras += (",r.source_digest AS media_digest" if versioned and has_revisions else ",NULL AS media_digest")
    for row in source.execute("SELECT m.*" + extras + " FROM message_media m" + joins):
        target = db.execute("SELECT target_id FROM incoming_messages WHERE source_id=?", (row["message_id"],)).fetchone()
        asset = db.execute("SELECT target_id FROM incoming_assets WHERE source_id=?", (row["asset_id"],)).fetchone()
        if target is None:
            raise ValueError("Archive media references a missing message")
        if row["kind"] not in MIMES:
            raise ValueError("Archive media kind is invalid")
        target_id = target[0]
        if asset is None:
            asset_id = _hash(["archive-missing", row["asset_id"], row["kind"]])
            db.execute("INSERT OR IGNORE INTO media_assets(id,kind,status) VALUES(?,?,'missing')", (asset_id, row["kind"]))
        else:
            asset_id = asset[0]
        incoming = db.execute("SELECT kind,status FROM media_assets WHERE id=?", (asset_id,)).fetchone()
        if incoming["kind"] != row["kind"]:
            raise ValueError("Archive media does not match its message kind")
        current = db.execute("""SELECT a.status,w.source_digest,r.source_digest AS media_digest
            FROM message_media m JOIN media_assets a ON a.id=m.asset_id
            LEFT JOIN wechat_source_messages w ON w.message_id=m.message_id
            LEFT JOIN media_import_revisions r ON r.message_id=m.message_id AND r.kind=m.kind
            WHERE m.message_id=? AND m.kind=?""", (target_id, row["kind"])).fetchone()
        source_valid = not versioned or row["current_digest"] is None or row["media_digest"] == row["current_digest"]
        current_valid = current is not None and (current["source_digest"] is None or current["media_digest"] == current["source_digest"])
        if current is not None and (current["status"] == "ready" and current_valid
                                  or incoming["status"] != "ready" or not source_valid):
            continue
        added += 1
        db.execute("""INSERT INTO message_media VALUES(?,?,?)
            ON CONFLICT(message_id,kind) DO UPDATE SET asset_id=excluded.asset_id""", (target_id, row["kind"], asset_id))
        revision = row["media_digest"] if versioned else None
        db.execute("DELETE FROM media_import_revisions WHERE message_id=? AND kind=?", (target_id, row["kind"]))
        if revision is not None:
            if not isinstance(revision, str) or not HEX.fullmatch(revision):
                raise ValueError("Archive media revision is invalid")
            db.execute("INSERT INTO media_import_revisions VALUES(?,?,?)", (target_id, row["kind"], revision))

    return added


def _avatars(source, tables, db):
    added = 0
    if "avatar_assets" not in tables:
        return added
    for row in source.execute("SELECT * FROM avatar_assets"):
        if not isinstance(row["identity_key"], str) or not HEX.fullmatch(row["identity_key"]):
            continue
        mapped = db.execute("SELECT target_id FROM incoming_assets WHERE source_id=?", (row["asset_id"],)).fetchone()
        asset = db.execute("SELECT * FROM media_assets WHERE id=?", (mapped[0],)).fetchone() if mapped else None
        if not Library._valid_avatar_asset(asset):
            continue
        current = db.execute("""SELECT a.* FROM avatar_assets v LEFT JOIN media_assets a ON a.id=v.asset_id
            WHERE v.identity_key=?""", (row["identity_key"],)).fetchone()
        if current is None or not Library._valid_avatar_asset(current):
            db.execute("""INSERT INTO avatar_assets VALUES(?,?) ON CONFLICT(identity_key)
                DO UPDATE SET asset_id=excluded.asset_id""", (row["identity_key"], mapped[0]))
            added += 1
    for row in source.execute("SELECT * FROM conversation_avatars"):
        if db.execute("SELECT 1 FROM conversations WHERE id=?", (row["conversation_id"],)).fetchone():
            db.execute("INSERT OR IGNORE INTO conversation_avatars VALUES(?,?)", (row["conversation_id"], row["identity_key"]))
    for row in source.execute("SELECT * FROM message_avatars"):
        target = db.execute("SELECT target_id FROM incoming_messages WHERE source_id=?", (row["message_id"],)).fetchone()
        if target:
            db.execute("INSERT OR IGNORE INTO message_avatars VALUES(?,?)", (target[0], row["identity_key"]))
    return added


def _import_archive(library, path, progress=None, *, expected_signature=None):
    """Atomically merge all saved records and verified compressed objects.

    The caller holds the application's operation lock. Message provenance is
    portable across subsequent exports. Conflicting historical revisions are
    preserved as separate records; existing local text and ready links win.
    New object files are removed on any pre-commit failure. Existing objects
    are never overwritten, even when their gzip representations differ.
    """
    if library.media.read_only:
        raise ValueError("An opened archive is read-only; select the local library")
    source = ArchiveSource(path)
    if expected_signature is not None and tuple(expected_signature) != source.signature:
        raise ValueError("The readable archive changed after preview")
    counts = {"added": {"conversations": 0, "messages": 0, "objects": 0, "avatars": 0},
              "skipped": {"messages": 0, "objects": 0}, "source": {"name": source.path.name}}
    created, committed = [], False
    try:
        with tempfile.TemporaryDirectory(prefix=".archive-import-", dir=library.state_dir) as temporary:
            folder = Path(temporary)
            with library.connect() as db:
                db.execute("PRAGMA temp_store=FILE")
                db.execute("BEGIN IMMEDIATE")
                _schema(db)
                with source.read_session() as archive:
                    with _source_database(source, archive, folder) as (incoming, tables):
                        objects, summary = _inventory(incoming)
                        if _required_bytes(source, objects, library) > shutil.disk_usage(library.state_dir).free:
                            raise ValueError("There is not enough free disk space to import this archive")
                        counts["summary"] = summary
                        counts["unverified_media"] = _unknown_revisions(incoming, tables)
                        sizes, counts["added"]["objects"], counts["skipped"]["objects"] = _objects(
                            library, source, archive, folder, objects, created, progress)
                        for row in incoming.execute("SELECT digest,filename,imported_at FROM imports"):
                            db.execute("INSERT OR IGNORE INTO imports VALUES(?,?,?)", tuple(row))
                        for row in incoming.execute("SELECT id,title,import_digest FROM conversations"):
                            if not all(isinstance(value, str) for value in row):
                                raise ValueError("Archive conversation fields are invalid")
                            counts["added"]["conversations"] += db.execute(
                                "INSERT OR IGNORE INTO conversations VALUES(?,?,?)", tuple(row)).rowcount
                            db.execute("INSERT INTO incoming_conversations VALUES(?)", (row["id"],))
                        if "archive_origin_lineage" in tables:
                            for row in incoming.execute("SELECT origin_key,canonical_key FROM archive_origin_lineage"):
                                if any(not isinstance(value, str) or not HEX.fullmatch(value) for value in row):
                                    raise ValueError("Archive provenance lineage is invalid")
                                previous = db.execute("SELECT canonical_key FROM archive_origin_lineage WHERE origin_key=?", (row["origin_key"],)).fetchone()
                                if previous is not None and previous[0] != row["canonical_key"]:
                                    raise ValueError("Archive provenance lineage conflicts with local records")
                                db.execute("INSERT OR IGNORE INTO archive_origin_lineage VALUES(?,?)", tuple(row))
                        _seed_origins(db)
                        _messages(incoming, tables, db, counts, progress)
                        _assets(incoming, db, sizes)
                        counts["added"]["media_links"] = _links(incoming, tables, db)
                        counts["added"]["avatars"] = _avatars(incoming, tables, db)
                    if progress:
                        progress({"phase": "committing", "done_messages": counts["added"]["messages"] + counts["skipped"]["messages"]})
            committed = True
        counts["duplicate"] = not any(counts["added"].values())
        return counts
    finally:
        if not committed:
            for target, previous in reversed(created):
                try:
                    if signature(target.lstat()) == previous:
                        target.unlink()
                except FileNotFoundError:
                    pass


def import_archive(library, path, progress=None, *, expected_signature=None):
    """Merge a verified ZIP under the caller's operation lock; never replace text."""
    try:
        return _import_archive(library, path, progress, expected_signature=expected_signature)
    except (zipfile.BadZipFile, sqlite3.DatabaseError, KeyError, json.JSONDecodeError, UnicodeError, TypeError, AttributeError):
        raise ValueError("拾聊归档的数据库或媒体内容无效，导入未完成，原有本机记录保持不变。") from None
