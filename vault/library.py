"""Import readable chat exports into a private, searchable SQLite library."""

import csv
import gzip
import hashlib
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


MAX_IMPORT_BYTES = 100 * 1024 * 1024
MAX_MESSAGES = 500_000
MAX_SEARCH_QUERY = 200
MAX_SEARCH_OFFSET = 10_000_000
CONVERSATION_SEARCH_LIMIT = 50


def normalize_time(value):
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
        number = float(value)
        if number > 1e12:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            raise ValueError("Message timestamp is out of range") from None
    text = str(value).strip().replace("/", "-")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise ValueError("Unsupported timestamp; use ISO 8601 or Unix seconds") from None


def decode_text(data):
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    raise ValueError("Cannot decode export; save it as UTF-8")


def normalize_message(row, names):
    if not isinstance(row, dict):
        raise ValueError("Every message must be an object")
    content = row.get("text", row.get("content", row.get("Content")))
    if content is None and "type" in row:
        content = "[Media message: {}]".format(row["type"])
    if content is None:
        raise ValueError("Message is missing text/content")
    if isinstance(content, (list, dict)):
        content = json.dumps(content, ensure_ascii=False)
    sender_id = str(row.get("sender", row.get("Sender", row.get("from", "Unknown"))))
    sender = row.get("groupNickname") or row.get("accountName") or names.get(sender_id) or sender_id
    is_self = row.get("is_self", row.get("isSelf", row.get("IsSender", False)))
    return {
        "sender": str(sender), "text": str(content),
        "timestamp": normalize_time(row.get("timestamp", row.get("time", row.get("CreateTime")))),
        "is_self": is_self is True or str(is_self).lower() in ("1", "true"),
    }


def parse_export(data, filename):
    if not data or len(data) > MAX_IMPORT_BYTES:
        raise ValueError("Import must contain 1 byte to 100 MB")
    extension = Path(filename).suffix.lower()
    text = decode_text(data)
    title = Path(filename).stem or "Imported conversation"
    groups = []
    if extension in (".json", ".jsonl"):
        if extension == ".jsonl":
            lines = [json.loads(line) for line in text.splitlines() if line.strip() and not line.startswith("#")]
            if not all(isinstance(line, dict) for line in lines):
                raise ValueError("Every JSONL line must be an object")
            if not lines or not isinstance(lines[0], dict) or lines[0].get("_type") != "header":
                raise ValueError("JSONL requires a ChatLab header")
            document = {**lines[0], "members": [x for x in lines if x.get("_type") == "member"],
                        "messages": [x for x in lines if x.get("_type") == "message"]}
        else:
            document = json.loads(text)
        if isinstance(document, list):
            document = {"messages": document}
        if not isinstance(document, dict):
            raise ValueError("JSON must be a message array or conversation object")
        conversations = document.get("conversations", [document])
        if not isinstance(conversations, list):
            raise ValueError("conversations must be an array")
        for conversation in conversations:
            if not isinstance(conversation, dict):
                raise ValueError("Every conversation must be an object")
            meta = conversation.get("meta") or {}
            if not isinstance(meta, dict) or not isinstance(conversation.get("members", []), list):
                raise ValueError("Invalid conversation metadata or members")
            names = {str(m.get("platformId", m.get("id"))): m.get("groupNickname") or m.get("accountName") or m.get("name")
                     for m in conversation.get("members", []) if isinstance(m, dict)}
            rows = conversation.get("messages")
            if not isinstance(rows, list):
                raise ValueError("JSON requires a messages array")
            groups.append((str(conversation.get("title") or meta.get("name") or conversation.get("name") or title),
                           [normalize_message(row, names) for row in rows]))
    elif extension == ".csv":
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or not any(k in reader.fieldnames for k in ("text", "content", "Content")):
            raise ValueError("CSV requires text/content, sender and timestamp columns")
        buckets = {}
        for row in reader:
            buckets.setdefault(row.get("conversation") or title, []).append(normalize_message(row, {}))
        groups = list(buckets.items())
    elif extension == ".txt":
        pattern = re.compile(r"^\[?(\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\]?\s+([^:：\n]+)[:：]\s?(.*)$")
        messages = []
        for line in text.splitlines():
            match = pattern.match(line)
            if match:
                timestamp, sender, content = match.groups()
                messages.append(normalize_message({"timestamp": timestamp, "sender": sender, "text": content}, {}))
            elif messages:
                messages[-1]["text"] += "\n" + line
            elif line.strip():
                messages.append(normalize_message({"sender": "Imported text", "text": line}, {}))
        groups = [(title, messages)]
    else:
        raise ValueError("Supported formats: JSON, ChatLab JSONL, CSV, TXT. Encrypted databases cannot be imported.")
    count = sum(len(rows) for _, rows in groups)
    if count == 0:
        raise ValueError("The export contains no messages")
    if count > MAX_MESSAGES:
        raise ValueError("Import exceeds 500,000 messages; split the export first")
    return groups


class Library:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state_dir / "library.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS imports (digest TEXT PRIMARY KEY, filename TEXT, imported_at TEXT);
                CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, title TEXT, import_digest TEXT);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL,
                    sender TEXT NOT NULL, text TEXT NOT NULL, timestamp TEXT NOT NULL, is_self INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, timestamp, id);
            """)
        self.path.chmod(0o600)
        from vault.media_store import MediaStore
        self.media = MediaStore(self)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def stats(self):
        with self.connect() as db:
            return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("conversations", "messages")}

    def import_file(self, data, filename):
        digest = hashlib.sha256(data).hexdigest()
        with self.connect() as db:
            if db.execute("SELECT 1 FROM imports WHERE digest=?", (digest,)).fetchone():
                return {"conversations": 0, "messages": 0, "duplicate": True}
        groups = parse_export(data, filename)
        imported = 0
        archive_dir = self.state_dir / "imports"
        archive_dir.mkdir(mode=0o700, exist_ok=True)
        archive = archive_dir / (digest + ".gz")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM imports WHERE digest=?", (digest,)).fetchone():
                return {"conversations": 0, "messages": 0, "duplicate": True}
            db.execute("INSERT INTO imports VALUES(?,?,?)", (digest, Path(filename).name, datetime.now(timezone.utc).isoformat()))
            for index, (title, messages) in enumerate(groups):
                conversation_id = digest[:24] + "-" + str(index)
                db.execute("INSERT INTO conversations VALUES(?,?,?)", (conversation_id, title, digest))
                db.executemany("INSERT INTO messages(conversation_id,sender,text,timestamp,is_self) VALUES(?,?,?,?,?)",
                               [(conversation_id, row["sender"], row["text"], row["timestamp"], int(row["is_self"])) for row in messages])
                imported += len(messages)
            with gzip.open(archive, "wb") as output:
                output.write(data)
            archive.chmod(0o600)
        return {"conversations": len(groups), "messages": imported, "duplicate": False}

    @staticmethod
    def search_pattern(query):
        return "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    @staticmethod
    def _search_query(query):
        if not isinstance(query, str) or len(query) > MAX_SEARCH_QUERY or "\x00" in query:
            raise ValueError("Search query must contain at most 200 characters and no null bytes")
        return query.strip()

    @staticmethod
    def _page(offset, limit, maximum=100):
        if type(offset) is not int or not 0 <= offset <= MAX_SEARCH_OFFSET:
            raise ValueError("Search offset must be an integer between 0 and 10000000")
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise ValueError(f"Search limit must be an integer between 1 and {maximum}")
        return offset, limit

    def _matching_conversations(self, db, query, limit, include_text=False):
        # Find matching conversation IDs once, before joining their message
        # summaries. A correlated LIKE search repeated for every joined
        # message becomes quadratic in large conversations.
        params = []
        if query:
            pattern = self.search_pattern(query)
            match_text = " OR text LIKE ? ESCAPE '\\'" if include_text else ""
            matching = ("SELECT id FROM conversations WHERE title LIKE ? ESCAPE '\\' "
                        "UNION SELECT conversation_id FROM messages WHERE sender LIKE ? ESCAPE '\\'" + match_text)
            params = [pattern, pattern] + ([pattern] if include_text else [])
        else:
            matching = "SELECT id FROM conversations"
        rows = db.execute("""WITH matching AS (""" + matching + """), summaries AS (
                SELECT c.id,c.title,COUNT(m.id) AS message_count,MAX(m.timestamp) AS last_message_at
                FROM matching h JOIN conversations c ON c.id=h.id
                JOIN messages m ON m.conversation_id=c.id GROUP BY c.id
            ), selected AS (
                SELECT *,COUNT(*) OVER() AS matching_total FROM summaries
                ORDER BY last_message_at DESC,id LIMIT ?
            )
            SELECT s.*,(SELECT SUBSTR(text,1,160) FROM messages
                WHERE conversation_id=s.id ORDER BY timestamp DESC,id DESC LIMIT 1) AS preview
            FROM selected s ORDER BY last_message_at DESC,id""", (*params, limit)).fetchall()
        total = rows[0]["matching_total"] if rows else 0
        result = []
        for row in rows:
            item = dict(row)
            item.pop("matching_total")
            result.append(item)
        return result, total

    def conversations(self, query=""):
        query = self._search_query(query)
        with self.connect() as db:
            rows, _ = self._matching_conversations(db, query, 1000, include_text=True)
        return {"conversations": rows}

    def search(self, query="", offset=0, limit=50):
        """Search titles/senders and message text/senders without changing SQLite.

        Literal substring matching uses SQLite LIKE's ASCII case folding;
        Chinese characters and escaped percent/underscore characters remain
        literal. Existing immutable archives use the same read-only queries.
        Global and conversation-local messages are newest first, with IDs
        descending for equal timestamps. LIMIT is applied to IDs before
        loading full message bodies.
        """
        query = self._search_query(query)
        offset, limit = self._page(offset, limit)
        result = {"query": query, "conversations": [], "conversation_total": 0,
                  "conversation_limit": CONVERSATION_SEARCH_LIMIT, "conversations_has_more": False,
                  "messages": [], "total": 0, "offset": offset, "limit": limit, "has_more": False}
        if not query:
            return result
        pattern = self.search_pattern(query)
        with self.connect() as db:
            db.execute("BEGIN")
            result["conversations"], result["conversation_total"] = self._matching_conversations(
                db, query, CONVERSATION_SEARCH_LIMIT)
            result["conversations_has_more"] = result["conversation_total"] > len(result["conversations"])
            result["total"] = db.execute("""SELECT COUNT(*) FROM messages
                WHERE text LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\'""", (pattern, pattern)).fetchone()[0]
            if offset < result["total"]:
                rows = db.execute("""SELECT m.id,m.conversation_id,c.title AS conversation_title,
                        m.sender,m.text,m.timestamp,m.is_self
                    FROM (SELECT id FROM messages WHERE text LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\'
                          ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?) page
                    JOIN messages m ON m.id=page.id JOIN conversations c ON c.id=m.conversation_id
                    ORDER BY m.timestamp DESC,m.id DESC""", (pattern, pattern, limit, offset)).fetchall()
                result["messages"] = [{**dict(row), "is_self": bool(row["is_self"])} for row in rows]
        result["has_more"] = offset + len(result["messages"]) < result["total"]
        return result

    def messages(self, conversation_id, query="", offset=0, limit=100):
        """Return a newest-first page, breaking timestamp ties by descending ID."""
        query = self._search_query(query)
        offset, limit = self._page(offset, limit, 500)
        with self.connect() as db:
            conversation = db.execute("SELECT id,title FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not conversation:
                raise KeyError("Conversation not found")
            params = (conversation_id, query, self.search_pattern(query), self.search_pattern(query))
            clause = "conversation_id=? AND (?='' OR text LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\')"
            total = db.execute("SELECT COUNT(*) FROM messages WHERE " + clause, params).fetchone()[0]
            rows = db.execute("SELECT id,sender,text,timestamp,is_self FROM messages WHERE " + clause +
                              " ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
            media = self.media.for_messages([row["id"] for row in rows])
            return {"messages": [{**dict(row), "is_self": bool(row["is_self"]), "media": media.get(row["id"], [])} for row in rows],
                    "total": total, "conversation": dict(conversation)}

    def context(self, conversation_id, message_id, limit=100):
        """Return an indexed newest-first window containing one exact message.

        Offsets and before/after flags refer to positions in that same order.
        """
        _, limit = self._page(0, limit, 200)
        if type(message_id) is not int or not 1 <= message_id <= 2 ** 63 - 1:
            raise ValueError("Message ID must be a positive SQLite integer")
        with self.connect() as db:
            db.execute("BEGIN")
            conversation = db.execute("SELECT id,title FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            target = db.execute("SELECT timestamp FROM messages WHERE id=? AND conversation_id=?",
                                (message_id, conversation_id)).fetchone()
            if conversation is None or target is None:
                raise KeyError("Conversation message not found")
            total = db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            preceding = db.execute("""SELECT COUNT(*) FROM messages WHERE conversation_id=?
                AND (timestamp,id)>(?,?)""", (conversation_id, target["timestamp"], message_id)).fetchone()[0]
            offset = max(0, min(preceding - limit // 2, total - limit))
            rows = db.execute("""SELECT id,sender,text,timestamp,is_self FROM messages
                WHERE conversation_id=? ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?""",
                (conversation_id, limit, offset)).fetchall()
        media = self.media.for_messages([row["id"] for row in rows])
        messages = [{**dict(row), "is_self": bool(row["is_self"]), "media": media.get(row["id"], [])} for row in rows]
        return {"conversation": dict(conversation), "messages": messages, "target_message_id": message_id,
                "total": total, "offset": offset, "limit": limit,
                "has_before": offset > 0, "has_after": offset + len(messages) < total}

    def demo(self):
        return self.import_file(json.dumps({"title": "使用示例 · 不是你的聊天记录", "messages": [
            {"sender": "拾聊", "text": "欢迎来到拾聊。这是一段示例对话，用来体验离线阅读和搜索。", "timestamp": "2026-01-01T10:00:00+08:00"},
            {"sender": "我", "text": "备份后就能直接读微信聊天吗？", "is_self": True, "timestamp": "2026-01-01T10:01:00+08:00"},
            {"sender": "拾聊", "text": "微信原始数据库已加密。压缩备份会完整保留原文件；阅读器需要另行导入可读的 JSON、CSV 或 TXT 记录。", "timestamp": "2026-01-01T10:02:00+08:00"},
            {"sender": "拾聊", "text": "你可以在搜索框试试“备份”。导入的记录只存放在这台电脑。", "timestamp": "2026-01-01T10:03:00+08:00"}
        ]}, ensure_ascii=False).encode(), "demo.json")
