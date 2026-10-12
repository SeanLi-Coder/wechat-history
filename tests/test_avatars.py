"""Verify private identity mappings and portable avatars using synthetic data."""

from contextlib import contextmanager
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from app import Handler
from vault.library import Library
from vault.readable_archive import create_archive, open_archive


class AvatarTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-avatars-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "reader").mkdir()
        self.library = Library(self.root / "library")
        self.library.import_file(json.dumps({"conversations": [
            {"title": "Shared name", "messages": [{"sender": "Shared sender", "text": f"needle-{index}",
                "timestamp": 1700000000 + index}]} for index in range(3)]}).encode(), "synthetic.json")
        with self.library.connect() as db:
            self.messages = [dict(row) for row in db.execute("SELECT id,conversation_id FROM messages ORDER BY id")]
        self.identities = [hashlib.sha256(f"synthetic-account-{index}:synthetic-user".encode()).hexdigest()
                           for index in range(3)]
        self.payloads = []
        self.assets = []
        for index in range(3):
            source = self.root / f"synthetic-avatar-{index}.png"
            Image.new("RGB", (12, 12), (30 + index * 50, 90, 170)).save(source, format="PNG")
            self.payloads.append(source.read_bytes())
            self.assets.append(self.library.media.store_file(source, "image", "image/png"))
            self.library.set_avatar(self.identities[index], self.assets[index])
        with self.library.connect() as db:
            db.executemany("INSERT INTO conversation_avatars VALUES(?,?)",
                [(row["conversation_id"], identity) for row, identity in zip(self.messages, self.identities)])
            db.executemany("INSERT INTO message_avatars VALUES(?,?)",
                [(row["id"], identity) for row, identity in zip(self.messages, self.identities)])

    def assert_urls(self, library):
        expected_conversations = {row["conversation_id"]: "/api/media/" + asset
                                  for row, asset in zip(self.messages, self.assets)}
        expected_messages = {row["id"]: "/api/media/" + asset
                             for row, asset in zip(self.messages, self.assets)}
        conversations = library.conversations()["conversations"]
        self.assertEqual({row["id"]: row["avatar_url"] for row in conversations}, expected_conversations)
        results = library.search("Shared")
        self.assertEqual({row["id"]: row["avatar_url"] for row in results["conversations"]}, expected_conversations)
        self.assertEqual({row["id"]: row["avatar_url"] for row in results["messages"]}, expected_messages)
        for row in self.messages:
            for result in (library.messages(row["conversation_id"]),
                           library.context(row["conversation_id"], row["id"])):
                self.assertEqual(result["conversation"]["avatar_url"], expected_conversations[row["conversation_id"]])
                self.assertEqual(result["messages"][0]["avatar_url"], expected_messages[row["id"]])
                self.assertEqual(result["messages"][0]["media"], [])

    def test_same_display_names_are_resolved_by_hashed_identity_in_all_read_paths(self):
        self.assert_urls(self.library)
        with self.library.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_media").fetchone()[0], 0)

    def test_updating_an_identity_replaces_only_its_avatar(self):
        self.library.set_avatar(self.identities[0], self.assets[1])
        self.assets[0] = self.assets[1]
        self.assert_urls(self.library)
        with self.library.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM avatar_assets").fetchone()[0], 3)

    def test_invalid_identity_missing_asset_and_wrong_media_kind_are_rejected(self):
        source = self.root / "synthetic.wav"
        source.write_bytes(b"Synthetic audio bytes")
        audio = self.library.media.store_file(source, "audio", "audio/wav")
        for identity, asset in (("invalid", self.assets[0]), (self.identities[0], "../outside"),
                                (self.identities[0], "f" * 64), (self.identities[0], audio),
                                (self.identities[0].upper(), self.assets[0])):
            with self.subTest(valid_identity=identity == self.identities[0]):
                with self.assertRaises(ValueError):
                    self.library.set_avatar(identity, asset)
        self.assert_urls(self.library)

    def test_set_avatar_checks_object_content_hash_before_replacing_mapping(self):
        asset = self.assets[1]
        with self.library.connect() as db:
            row = db.execute("SELECT object_hash,encoding FROM media_assets WHERE id=?", (asset,)).fetchone()
        self.assertEqual(row["encoding"], "raw")
        path = self.library.media.objects / row["object_hash"][:2] / (row["object_hash"] + ".bin")
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.library.set_avatar(self.identities[0], asset)
        with self.library.connect() as db:
            self.assertEqual(db.execute("SELECT asset_id FROM avatar_assets WHERE identity_key=?",
                (self.identities[0],)).fetchone()[0], self.assets[0])

    def test_missing_and_invalid_references_return_null_without_name_fallback(self):
        mutations = (
            ("DELETE FROM message_avatars", ()),
            ("UPDATE avatar_assets SET asset_id=?", ("missing",)),
            ("UPDATE media_assets SET status='missing'", ()),
            ("UPDATE media_assets SET kind='audio'", ()),
            ("UPDATE media_assets SET mime_type='text/html'", ()),
            ("UPDATE media_assets SET object_hash='../outside'", ()),
            ("UPDATE media_assets SET id='invalid-'||id; UPDATE avatar_assets SET asset_id='invalid-'||asset_id", ()),
            ("UPDATE avatar_assets SET identity_key='invalid-'||identity_key; "
             "UPDATE message_avatars SET identity_key='invalid-'||identity_key", ()),
        )
        for statement, params in mutations:
            with self.subTest(statement=statement):
                with self.library.connect() as db:
                    backup = sqlite3.connect(":memory:")
                    db.backup(backup)
                    for command in statement.split(";"):
                        db.execute(command, params)
                try:
                    result = self.library.search("Shared")
                    self.assertTrue(all(row["avatar_url"] is None for row in result["messages"]))
                finally:
                    with self.library.connect() as db:
                        backup.backup(db)
                    backup.close()

    def test_page_avatar_resolution_uses_batch_queries(self):
        self.library.import_file(json.dumps({"title": "Bulk", "messages": [
            {"sender": "Shared sender", "text": f"bulk-{index}", "timestamp": 1700000000 + index}
            for index in range(500)]}).encode(), "bulk.json")
        cid = self.library.conversations("Bulk")["conversations"][0]["id"]
        with self.library.connect() as db:
            db.execute("INSERT INTO message_avatars SELECT id,? FROM messages WHERE conversation_id=?",
                       (self.identities[0], cid))
        traced = []
        original = self.library.connect

        @contextmanager
        def measured():
            with original() as db:
                db.set_trace_callback(traced.append)
                yield db

        with patch.object(self.library, "connect", measured):
            result = self.library.messages(cid, limit=500)
        self.assertEqual(len(result["messages"]), 500)
        self.assertTrue(all(row["avatar_url"] == "/api/media/" + self.assets[0] for row in result["messages"]))
        self.assertEqual(sum("FROM message_avatars m JOIN avatar_assets" in sql for sql in traced), 1)

    def test_new_archive_contains_unlinked_avatars_and_survives_source_deletion(self):
        archived = create_archive(self.library, self.root / "archives")
        archive = Path(archived["path"])
        self.assertEqual(archived["objects"], 3)
        with zipfile.ZipFile(archive) as packaged:
            self.assertEqual(sum(name.startswith("media/") for name in packaged.namelist()), 3)
        before = hashlib.sha256(archive.read_bytes()).digest()
        shutil.rmtree(self.library.state_dir)
        for source in self.root.glob("synthetic-avatar-*.png"):
            source.unlink()
        opened = open_archive(archive, self.root / "reader")
        self.assert_urls(opened)
        with opened.connect() as db:
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
        for asset, payload in zip(self.assets, self.payloads):
            with opened.media.open(asset) as (stream, info):
                self.assertEqual(stream.read(), payload)
                self.assertEqual(info["mime_type"], "image/png")
                stream.seek(0)
                with Image.open(stream) as image:
                    image.load()
                    self.assertEqual(image.size, (12, 12))
        with self.assertRaisesRegex(ValueError, "read-only"):
            opened.set_avatar(self.identities[0], self.assets[0])
        self.assertEqual(hashlib.sha256(archive.read_bytes()).digest(), before)

    def test_old_archive_without_avatar_tables_stays_read_only_and_returns_null(self):
        with self.library.connect() as db:
            for table in ("avatar_assets", "conversation_avatars", "message_avatars"):
                db.execute(f"DROP TABLE {table}")
        archive = Path(create_archive(self.library, self.root / "archives")["path"])
        before = hashlib.sha256(archive.read_bytes()).digest()
        opened = open_archive(archive, self.root / "reader")
        results = [opened.conversations()["conversations"], opened.search("Shared")["conversations"],
                   opened.search("Shared")["messages"]]
        row = self.messages[0]
        for response in (opened.messages(row["conversation_id"]), opened.context(row["conversation_id"], row["id"])):
            results.extend([response["messages"], [response["conversation"]]])
        self.assertTrue(all(item["avatar_url"] is None for result in results for item in result))
        with opened.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%avatar%'").fetchone()[0], 0)
        self.assertEqual(hashlib.sha256(archive.read_bytes()).digest(), before)

    def test_archive_rejects_partial_or_malformed_optional_avatar_schema(self):
        statements = ("DROP TABLE message_avatars",
            "DROP TABLE avatar_assets; CREATE TABLE avatar_assets(identity_key TEXT, asset_id TEXT)",
            "DROP TABLE message_avatars; CREATE TABLE message_avatars(message_id TEXT PRIMARY KEY, identity_key TEXT)",
            "DROP TABLE conversation_avatars; CREATE TABLE conversation_avatars(conversation_id TEXT PRIMARY KEY)")
        for index, statement in enumerate(statements):
            with self.subTest(index=index):
                library = Library(self.root / f"malformed-{index}")
                with library.connect() as db:
                    db.executescript(statement)
                archive = create_archive(library, self.root / "archives")
                with self.assertRaisesRegex(ValueError, "avatar mappings.*schema"):
                    open_archive(archive["path"], self.root / "reader")

    def test_http_conversation_messages_search_and_context_expose_local_avatar_urls(self):
        application = SimpleNamespace(token="synthetic-avatar-token", reader_library=self.library)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.app = application
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        row = self.messages[0]
        routes = (("/api/conversations", "conversations"),
                  (f"/api/conversations/{row['conversation_id']}/messages", "messages"),
                  ("/api/search?q=Shared", "messages"),
                  (f"/api/conversations/{row['conversation_id']}/context?message_id={row['id']}", "messages"))
        try:
            for path, field in routes:
                with self.subTest(path=path):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    try:
                        connection.request("GET", path, headers={"Authorization": "Bearer " + application.token})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        result = json.loads(response.read())
                        self.assertTrue(all(item["avatar_url"].startswith("/api/media/") for item in result[field]))
                    finally:
                        connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
