"""Synthetic SQLite fixtures for the read-only WeChat 4.x importer."""

from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import secrets
import tempfile
import unittest
from unittest.mock import patch

from vault.library import Library
from vault.wechat_reader import WeChatImportError, _open_source, import_decrypted

try:
    import zstandard
except ImportError:
    zstandard = None

try:
    from compression import zstd as stdlib_zstd
except ImportError:
    stdlib_zstd = None


def compress_text(data, known_size=True):
    if zstandard is not None:
        return zstandard.ZstdCompressor(write_content_size=known_size).compress(data)
    return stdlib_zstd.compress(data, options={stdlib_zstd.CompressionParameter.content_size_flag: int(known_size)})


def table_for(username):
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


class WeChatReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name).resolve()
        self.root = self.work / "decrypted" / "db_storage"
        (self.root / "contact").mkdir(parents=True)
        (self.root / "message").mkdir()
        self.library = Library(self.work / "library")
        self.alice = "wxid_alice"
        self.me = "wxid_myself"
        self.group = "synthetic_group@chatroom"
        self.create_contacts([
            (self.alice, "Alice nickname", "Alice remark"),
            (self.me, "My nickname", ""),
            (self.group, "Weekend group", ""),
        ])

    def tearDown(self):
        self.temporary.cleanup()

    def create_contacts(self, rows):
        path = self.root / "contact/contact.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)")
            db.executemany("INSERT INTO contact VALUES(?,?,?)", rows)

    def create_messages(self, database_name="message_0.db", conversations=None, names=None):
        path = self.root / "message" / database_name
        if names is None:
            names = [(3, self.alice), (7, self.me), (9, self.group)]
        if conversations is None:
            conversations = {self.alice: [(1, 1001, 1, 1791612000, 3, b"Synthetic text")]}
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE Name2Id(user_name TEXT)")
            db.executemany("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", names)
            for username, rows in conversations.items():
                table = table_for(username)
                db.execute(f'''CREATE TABLE "{table}" (
                    local_id INTEGER, server_id INTEGER, local_type INTEGER,
                    create_time INTEGER, real_sender_id INTEGER, message_content BLOB
                )''')
                db.executemany(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?)', rows)
        return path

    def all_messages(self):
        with self.library.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM messages ORDER BY id")]

    def test_contact_sender_group_and_media_mapping(self):
        group_rows = [
            (1, 1001, 1, 1791612000, 3, (self.alice + ":\n群聊正文").encode()),
            (2, 1002, 1, 1791612060, 3, b"someone_else:\nKeep this prefix"),
            (3, 1003, 49, 1791612120, 7, b"<xml>Private media metadata</xml>"),
            (4, 1004, 1, 1791612180, 7, "自己发送的消息".encode()),
        ]
        self.create_messages(conversations={
            self.group: group_rows,
            self.alice: [(1, 2001, 1, 1791612000, 3, (self.alice + ":\nDirect message").encode())],
        })
        result = import_decrypted(self.root, self.library, self_username=self.me)
        self.assertEqual(result, {"conversations": 2, "messages": 5, "updated_messages": 0})
        conversations = self.library.conversations()["conversations"]
        self.assertEqual({row["title"] for row in conversations}, {"Weekend group", "Alice remark"})
        group_id = next(row["id"] for row in conversations if row["title"] == "Weekend group")
        messages = self.library.messages(group_id)["messages"]
        self.assertEqual(messages[0]["sender"], "My nickname")
        self.assertEqual(messages[0]["text"], "自己发送的消息")
        self.assertEqual(messages[1]["text"], "[类型49消息]")
        self.assertTrue(messages[1]["is_self"])
        self.assertEqual(messages[2]["text"], "someone_else:\nKeep this prefix")
        self.assertEqual(messages[3]["sender"], "Alice remark")
        self.assertEqual(messages[3]["text"], "群聊正文")
        self.assertFalse(messages[3]["is_self"])
        direct = next(row for row in self.all_messages() if row["conversation_id"] != group_id)
        self.assertEqual(direct["text"], self.alice + ":\nDirect message")

    def test_cross_database_local_ids_do_not_collide(self):
        self.create_messages("message_0.db")
        self.create_messages("message_1.db", {
            self.alice: [(1, 2001, 1, 1791612060, 7, b"A message from the second shard")],
        })
        result = import_decrypted(self.root, self.library, self_username=self.me)
        self.assertEqual(result, {"conversations": 1, "messages": 2, "updated_messages": 0})
        self.assertEqual(len({row["conversation_id"] for row in self.all_messages()}), 1)
        self.assertEqual([row["is_self"] for row in self.all_messages()], [0, 1])

    def test_search_and_resource_databases_are_not_message_shards(self):
        self.create_messages("message_0.db")
        for name in ("message_fts.db", "message_resource.db", "message_backup.db"):
            (self.root / "message" / name).write_bytes(b"Synthetic non-message database")
        result = import_decrypted(self.root, self.library)
        self.assertEqual(result["messages"], 1)
        self.assertEqual(self.library.stats(), {"conversations": 1, "messages": 1})

    def test_only_auxiliary_databases_do_not_count_as_message_shards(self):
        for name in ("message_fts.db", "message_resource.db"):
            (self.root / "message" / name).write_bytes(b"Synthetic non-message database")
        with self.assertRaises(WeChatImportError) as failure:
            import_decrypted(self.root, self.library)
        self.assertEqual(failure.exception.code, "message_databases_missing")
        self.assertEqual(self.library.stats(), {"conversations": 0, "messages": 0})

    def test_repeated_import_deduplicates_and_updates_existing_local_id(self):
        path = self.create_messages()
        first = import_decrypted(self.root, self.library)
        self.assertEqual(first["messages"], 1)
        original_id = self.all_messages()[0]["id"]
        self.assertEqual(import_decrypted(self.root, self.library),
                         {"conversations": 0, "messages": 0, "updated_messages": 0})
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(f'UPDATE "{table_for(self.alice)}" SET message_content=? WHERE local_id=1',
                       (b"Corrected content",))
        self.assertEqual(import_decrypted(self.root, self.library),
                         {"conversations": 0, "messages": 0, "updated_messages": 1})
        self.assertEqual(self.all_messages()[0]["id"], original_id)
        self.assertEqual(self.all_messages()[0]["text"], "Corrected content")

    def test_stable_account_identity_survives_temporary_directory_changes(self):
        self.create_messages()
        import_decrypted(self.root, self.library, account_id="stable-original-account")
        other = self.work / "second-decrypted"
        shutil.copytree(self.root, other)
        self.assertEqual(import_decrypted(other, self.library, account_id="stable-original-account"),
                         {"conversations": 0, "messages": 0, "updated_messages": 0})
        self.assertEqual(import_decrypted(other, self.library, account_id="another-account")["messages"], 1)
        self.assertEqual(self.library.stats(), {"conversations": 2, "messages": 2})

    def test_read_only_connections_and_source_files_remain_unchanged(self):
        path = self.create_messages()
        before = {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in self.root.rglob("*.db")}
        with _open_source(path) as source:
            with self.assertRaises(sqlite3.OperationalError):
                source.execute("CREATE TABLE should_not_exist(value)")
        import_decrypted(self.root, self.library)
        for file, (content, mtime) in before.items():
            self.assertEqual(file.read_bytes(), content)
            self.assertEqual(file.stat().st_mtime_ns, mtime)
        self.assertEqual(len(list(self.root.rglob("*"))), 4)

    def test_nonempty_wal_is_rejected_before_import(self):
        path = self.create_messages()
        path.with_name(path.name + "-wal").write_bytes(b"Synthetic pending WAL")
        with self.assertRaises(WeChatImportError) as error:
            import_decrypted(self.root, self.library)
        self.assertEqual(error.exception.code, "source_wal_present")
        self.assertEqual(self.library.stats(), {"conversations": 0, "messages": 0})

    def test_bad_later_schema_rolls_back_earlier_messages(self):
        self.create_messages("message_0.db")
        bad = self.create_messages("message_1.db")
        with closing(sqlite3.connect(bad)) as source, source:
            table = table_for(self.alice)
            source.execute(f'DROP TABLE "{table}"')
            source.execute(f'CREATE TABLE "{table}" (local_id INTEGER)')
        saw_messages = []
        with self.assertRaises(WeChatImportError) as error:
            import_decrypted(self.root, self.library,
                             progress=lambda update: saw_messages.append(update["done_messages"]))
        self.assertEqual(error.exception.code, "unsupported_schema")
        self.assertIn(1, saw_messages)
        self.assertEqual(self.library.stats(), {"conversations": 0, "messages": 0})
        with self.library.connect() as destination:
            self.assertEqual(destination.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 0)
            self.assertIsNone(destination.execute(
                "SELECT name FROM sqlite_master WHERE name='wechat_source_messages'").fetchone())

    def test_corrupt_utf8_rolls_back_all_rows(self):
        self.create_messages(conversations={self.alice: [
            (1, 1001, 1, 1791612000, 3, b"Valid message"),
            (2, 1002, 1, 1791612060, 3, b"\xff\xfe"),
        ]})
        with self.assertRaises(WeChatImportError) as error:
            import_decrypted(self.root, self.library)
        self.assertEqual(error.exception.code, "invalid_text")
        self.assertEqual(self.library.stats(), {"conversations": 0, "messages": 0})

    @unittest.skipIf(zstandard is None and stdlib_zstd is None, "Zstandard support is not installed")
    def test_zstd_text_is_decoded_with_unicode_intact(self):
        raw = (self.alice + ":\n压缩的群聊文字。" * 10).encode()
        compressed = compress_text(raw)
        self.create_messages(conversations={self.group: [(1, 1001, 1, 1791612000, 3, compressed)]})
        import_decrypted(self.root, self.library)
        self.assertEqual(self.all_messages()[0]["text"], raw.decode()[len(self.alice) + 2:])

    @unittest.skipIf(zstandard is None and stdlib_zstd is None, "Zstandard support is not installed")
    def test_known_and_unknown_size_zstd_frames_are_bounded(self):
        for known in (True, False):
            with self.subTest(known=known):
                compressed = compress_text(b"x" * 10000, known_size=known)
                path = self.root / "message/message_0.db"
                path.unlink(missing_ok=True)
                self.create_messages(conversations={self.alice: [(1, 1001, 1, 1791612000, 3, compressed)]})
                with patch("vault.wechat_reader.MAX_MESSAGE_BYTES", 128):
                    with self.assertRaises(WeChatImportError):
                        import_decrypted(self.root, self.library)
                self.assertEqual(self.library.stats()["messages"], 0)

    def test_unresolved_chat_and_sender_have_honest_fallbacks(self):
        unknown = "not-in-any-name-table"
        self.create_messages(conversations={unknown: [(1, 1001, 1, 1791612000, 999, b"Unknown sender text")]})
        import_decrypted(self.root, self.library)
        conversation = self.library.conversations()["conversations"][0]
        self.assertEqual(conversation["title"], "未命名会话 " + hashlib.md5(unknown.encode()).hexdigest()[:8])
        self.assertEqual(self.all_messages()[0]["sender"], "未知发送者")
        self.assertEqual(self.all_messages()[0]["is_self"], 0)

    def test_chat_hash_can_resolve_using_another_shards_name_table(self):
        unknown = "cross_shard_room@chatroom"
        self.create_messages("message_0.db", {unknown: [
            (1, 1001, 1, 1791612000, 3, (self.alice + ":\nCross-shard group text").encode()),
        ]}, names=[(3, self.alice)])
        self.create_messages("message_1.db", {}, names=[(1, unknown)])
        import_decrypted(self.root, self.library)
        self.assertEqual(self.library.conversations()["conversations"][0]["title"], unknown)
        self.assertEqual(self.all_messages()[0]["text"], "Cross-shard group text")

    def test_source_mutation_during_import_rolls_back_target(self):
        path = self.create_messages()
        mutated = False

        def mutate(update):
            nonlocal mutated
            if update["done_messages"] and not mutated:
                mutated = True
                with closing(sqlite3.connect(path)) as source, source:
                    source.execute(f'UPDATE "{table_for(self.alice)}" SET message_content=?', (b"Changed",))

        with self.assertRaises(WeChatImportError) as error:
            import_decrypted(self.root, self.library, progress=mutate)
        self.assertEqual(error.exception.code, "source_changed")
        self.assertEqual(self.library.stats()["messages"], 0)

    def test_batches_report_counts_without_printing_message_content(self):
        self.create_messages(conversations={self.alice: [
            (index, 1000 + index, 1, 1791612000 + index, 3, b"Synthetic private text")
            for index in range(65)
        ]})
        updates = []
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = import_decrypted(self.root, self.library, progress=updates.append)
        self.assertEqual(result["messages"], 65)
        self.assertGreater(len(updates), 3)
        self.assertEqual(updates[-1]["done_databases"], 1)
        self.assertEqual(updates[-1]["done_messages"], 65)
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(all(isinstance(value, int) for update in updates for value in update.values()))

    def test_encrypted_account_keys_import_and_library_search_end_to_end(self):
        # This uses an independent encryptor, real per-page authentication,
        # real decryption, and the real reader. No reader functions are mocked.
        from test_wechat_crypto import Cipher, encrypt_fixture, make_sqlite
        from vault.wechat_access import import_account, save_verified_keys

        if Cipher is None:
            self.skipTest("Independent encrypted fixtures require cryptography")
        encrypted_root = self.work / "encrypted-account" / "db_storage"
        key_state = self.work / "key-state"
        expected_text = "加密到阅读库的端到端验证"
        key_records = {}
        encrypted_inputs = {}
        for relative in ("contact/contact.db", "message/message_0.db"):
            plaintext_path = self.work / (Path(relative).name + ".plaintext")
            make_sqlite(plaintext_path)
            with closing(sqlite3.connect(plaintext_path)) as database, database:
                database.execute("DROP TABLE messages")
                if relative.startswith("contact/"):
                    database.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)")
                    database.executemany("INSERT INTO contact VALUES(?,?,?)", [
                        (self.alice, "Alice nickname", "Alice remark"),
                        (self.group, "Weekend group", ""),
                    ])
                else:
                    database.execute("CREATE TABLE Name2Id(user_name TEXT)")
                    database.executemany("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)",
                                         [(3, self.alice), (9, self.group)])
                    database.execute(f'''CREATE TABLE "{table_for(self.group)}" (
                        local_id INTEGER, server_id INTEGER, local_type INTEGER,
                        create_time INTEGER, real_sender_id INTEGER, message_content BLOB
                    )''')
                    database.execute(f'INSERT INTO "{table_for(self.group)}" VALUES(?,?,?,?,?,?)',
                                     (1, 7001, 1, 1791612000, 3,
                                      (self.alice + ":\n" + expected_text).encode("utf-8")))
            plaintext = plaintext_path.read_bytes()
            self.assertEqual(plaintext[20], 80, "Fixture page reserve must match SQLCipher 4.")
            self.assertEqual(len(plaintext) % 4096, 0)
            key = secrets.token_bytes(32)
            ciphertext = encrypt_fixture(plaintext, key)
            target = encrypted_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(ciphertext)
            key_records[relative] = {"enc_key": key.hex()}
            encrypted_inputs[target] = ciphertext
            plaintext_path.unlink()

        checked_closed = []
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            key_result = save_verified_keys(json.dumps({"keys": key_records}).encode(), encrypted_root, key_state)
            imported = import_account(encrypted_root, key_state, self.library,
                                      assert_closed=lambda: checked_closed.append(True))
            repeated = import_account(encrypted_root, key_state, self.library,
                                      assert_closed=lambda: checked_closed.append(True))
        self.assertEqual(key_result, {"verified_databases": 2})
        self.assertEqual(imported, {"conversations": 1, "messages": 1, "updated_messages": 0})
        self.assertEqual(repeated, {"conversations": 0, "messages": 0, "updated_messages": 0})
        matches = self.library.conversations("端到端")["conversations"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["title"], "Weekend group")
        messages = self.library.messages(matches[0]["id"], "端到端")["messages"]
        self.assertEqual(messages[0]["text"], expected_text)
        self.assertEqual(messages[0]["sender"], "Alice remark")
        self.assertEqual(output.getvalue(), "")
        self.assertGreaterEqual(len(checked_closed), 8)
        self.assertEqual((key_state / "wechat-keys.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(key_state.glob("wechat-read-*")), [])
        for path, ciphertext in encrypted_inputs.items():
            self.assertEqual(path.read_bytes(), ciphertext)


if __name__ == "__main__":
    unittest.main()
