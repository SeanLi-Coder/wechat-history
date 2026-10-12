"""Exercise private key ingestion and all-or-nothing decryption staging."""

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from vault.wechat_access import import_account, parse_keys, save_verified_keys
from vault.wechat_crypto import WeChatCryptoError
from test_wechat_crypto import Cipher, encrypt_fixture, make_sqlite


class ParseKeysTests(unittest.TestCase):
    def test_normalizes_supported_key_values_without_changing_paths(self):
        data = json.dumps({"keys": {
            "contact/contact.db": {"enc_key": "AB" * 32},
            "message/message_0.db": "CD" * 32,
        }}).encode()
        self.assertEqual(parse_keys(data), {"contact/contact.db": "ab" * 32, "message/message_0.db": "cd" * 32})

    def test_rejects_traversal_and_noncanonical_paths(self):
        paths = ("../escape.db", "message/../../escape.db", "/tmp/escape.db", "./message/one.db",
                 "message//one.db", "message\\one.db", "message/one.db/..", "")
        for path in paths:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    parse_keys(json.dumps({"keys": {path: "ab" * 32}}).encode())

    def test_invalid_keys_never_appear_in_errors(self):
        secret = "private-invalid-key-material-" + "ab" * 32
        with self.assertRaises(ValueError) as failure:
            parse_keys(json.dumps({"keys": {"message/message_0.db": {"enc_key": secret}}}).encode())
        self.assertNotIn(secret, str(failure.exception))
        self.assertNotIn("ab" * 32, str(failure.exception))

    def test_rejects_missing_empty_or_invalid_key_objects(self):
        for document in ({}, {"keys": {}}, {"keys": []}, [], "invalid", {"keys": {"db": None}}):
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    parse_keys(json.dumps(document).encode())


@unittest.skipIf(Cipher is None, "Independent encrypted fixtures require cryptography")
class WeChatAccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.account = self.root / "synthetic-account"
        self.database_root = self.account / "db_storage"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        fixture = self.root / "fixture.sqlite3"
        self.plaintext = make_sqlite(fixture)
        fixture.unlink()
        self.keys = {
            "contact/contact.db": "12" * 32,
            "message/message_0.db": "34" * 32,
            "message/message_1.db": "56" * 32,
        }
        for relative, key in self.keys.items():
            path = self.database_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encrypt_fixture(self.plaintext, bytes.fromhex(key)))
        self.reader = ModuleType("vault.wechat_reader")
        self.reader.import_decrypted = Mock(return_value={"messages": 192, "conversations": 3})
        patcher = patch.dict(sys.modules, {"vault.wechat_reader": self.reader})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.library = Mock()

    def key_data(self, keys=None):
        return json.dumps({"keys": {relative: {"enc_key": key} for relative, key in (keys or self.keys).items()}}).encode()

    def save_keys(self):
        return save_verified_keys(self.key_data(), self.account, self.state)

    def assert_staging_clean(self):
        self.assertFalse(list(self.state.glob("wechat-read-*")))

    def test_verified_keys_are_saved_privately_after_all_keys_match(self):
        self.assertEqual(self.save_keys(), {"verified_databases": 3})
        path = self.state / "wechat-keys.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        saved = json.loads(path.read_bytes())
        self.assertEqual(saved["database_root"], str(self.database_root))
        self.assertEqual(parse_keys(path.read_bytes()), self.keys)
        self.assertFalse(list(self.state.glob(".keys-*")))

    def test_wrong_key_does_not_save_any_key_file(self):
        wrong = {**self.keys, "message/message_1.db": "78" * 32}
        with self.assertRaises(ValueError) as failure:
            save_verified_keys(self.key_data(wrong), self.account, self.state)
        self.assertFalse((self.state / "wechat-keys.json").exists())
        self.assertNotIn("78" * 32, str(failure.exception))
        self.assertFalse(list(self.state.glob(".keys-*")))

    def test_wrong_key_preserves_previously_verified_keys(self):
        self.save_keys()
        path = self.state / "wechat-keys.json"
        original = path.read_bytes()
        wrong = {**self.keys, "message/message_0.db": "78" * 32}
        with self.assertRaises(ValueError):
            save_verified_keys(self.key_data(wrong), self.account, self.state)
        self.assertEqual(path.read_bytes(), original)

    def test_key_file_cannot_follow_database_symlink_outside_account(self):
        outside = self.root / "outside.db"
        outside.write_bytes(encrypt_fixture(self.plaintext, bytes.fromhex("ab" * 32)))
        (self.database_root / "message/escape.db").symlink_to(outside)
        with self.assertRaises(ValueError):
            save_verified_keys(self.key_data({"message/escape.db": "ab" * 32}), self.account, self.state)
        self.assertFalse((self.state / "wechat-keys.json").exists())

    def test_import_publishes_only_after_every_database_is_decrypted(self):
        self.save_keys()
        staged_roots = []

        def reader(decrypted_root, library, **kwargs):
            staged_roots.append(decrypted_root)
            self.assertIs(library, self.library)
            self.assertEqual(kwargs["account_id"], str(self.database_root))
            for relative in self.keys:
                path = decrypted_root / relative
                self.assertEqual(path.read_bytes(), self.plaintext)
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
                    self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(database.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 64)
            return {"messages": 192}

        self.reader.import_decrypted.side_effect = reader
        closed = Mock()
        self.assertEqual(import_account(self.account, self.state, self.library, closed), {"messages": 192})
        self.reader.import_decrypted.assert_called_once()
        self.assertGreaterEqual(closed.call_count, 5)
        self.assertFalse(staged_roots[0].exists())
        self.assert_staging_clean()

    def test_late_key_failure_never_calls_reader_or_keeps_staging(self):
        self.save_keys()
        data = json.loads((self.state / "wechat-keys.json").read_bytes())
        data["keys"]["message/message_1.db"]["enc_key"] = "78" * 32
        (self.state / "wechat-keys.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(WeChatCryptoError):
            import_account(self.account, self.state, self.library, Mock())
        self.reader.import_decrypted.assert_not_called()
        self.assert_staging_clean()

    def test_missing_message_key_never_calls_reader(self):
        save_verified_keys(self.key_data({"message/message_0.db": self.keys["message/message_0.db"]}), self.account, self.state)
        with self.assertRaises(ValueError):
            import_account(self.account, self.state, self.library, Mock())
        self.reader.import_decrypted.assert_not_called()
        self.assert_staging_clean()

    def test_ignores_search_databases(self):
        self.save_keys()
        (self.database_root / "message/message_fts.db").write_bytes(b"not a message shard")
        result = import_account(self.account, self.state, self.library, Mock())
        self.assertEqual(result["messages"], 192)
        self.reader.import_decrypted.assert_called_once()

    def test_resource_database_is_staged_for_media_import(self):
        source = self.database_root / "message/message_0.db"
        resource = self.database_root / "message/message_resource.db"
        resource.write_bytes(source.read_bytes())
        self.keys["message/message_resource.db"] = self.keys["message/message_0.db"]
        self.save_keys()
        module = ModuleType("vault.media_import")

        def media(root, account, library, identity, progress):
            self.assertEqual((root / "message/message_resource.db").read_bytes(), self.plaintext)
            self.assertEqual(account, self.account)
            return {"ready": 1}

        module.import_media = Mock(side_effect=media)
        with patch.dict(sys.modules, {"vault.media_import": module}):
            result = import_account(self.account, self.state, self.library, Mock())
        self.assertEqual(result["media"], {"ready": 1})
        module.import_media.assert_called_once()

    def test_existing_wal_uses_sqlcipher_and_requires_unchanged_source(self):
        self.save_keys()
        wal = self.database_root / "message/message_0.db-wal"
        wal.write_bytes(b"synthetic WAL")
        exporter = ModuleType("vault.sqlcipher_export")
        exporter.export_database = Mock(side_effect=lambda source, target, key: target.write_bytes(self.plaintext))
        with patch.dict(sys.modules, {"vault.sqlcipher_export": exporter}):
            import_account(self.account, self.state, self.library, Mock())
        exporter.export_database.assert_called_once()
        self.reader.import_decrypted.assert_called_once()
        self.assert_staging_clean()

    def test_optional_avatar_database_is_staged_and_imported_with_its_saved_key(self):
        avatar = self.database_root / "head_image/head_image.db"
        avatar.parent.mkdir()
        avatar.write_bytes((self.database_root / "message/message_0.db").read_bytes())
        self.keys["head_image/head_image.db"] = self.keys["message/message_0.db"]
        self.save_keys()
        module = ModuleType("vault.wechat_avatars")

        def avatars(root, library, identity, progress):
            self.assertEqual((root / "head_image/head_image.db").read_bytes(), self.plaintext)
            self.assertIs(library, self.library)
            self.assertEqual(identity, str(self.database_root))
            return {"imported": 2, "status": "done"}

        module.import_avatars = Mock(side_effect=avatars)
        with patch.dict(sys.modules, {"vault.wechat_avatars": module}):
            result = import_account(self.account, self.state, self.library, Mock())
        self.assertEqual(result["avatars"], {"imported": 2, "status": "done"})
        module.import_avatars.assert_called_once()
        self.assert_staging_clean()

    def test_optional_avatar_database_without_key_does_not_block_message_import(self):
        avatar = self.database_root / "head_image/head_image.db"
        avatar.parent.mkdir()
        avatar.write_bytes((self.database_root / "message/message_0.db").read_bytes())
        self.save_keys()
        result = import_account(self.account, self.state, self.library, Mock())
        self.assertEqual(result["messages"], 192)
        self.reader.import_decrypted.assert_called_once()
        self.assert_staging_clean()

    def test_existing_wal_changed_after_export_aborts_import(self):
        self.save_keys()
        wal = self.database_root / "contact/contact.db-wal"
        wal.write_bytes(b"synthetic WAL")
        exporter = ModuleType("vault.sqlcipher_export")
        exporter.export_database = Mock(side_effect=lambda source, target, key: target.write_bytes(self.plaintext))

        def progress(value):
            if value["done_files"] == 1:
                wal.write_bytes(b"changed synthetic WAL")

        with patch.dict(sys.modules, {"vault.sqlcipher_export": exporter}):
            with self.assertRaises(ValueError):
                import_account(self.account, self.state, self.library, Mock(), progress=progress)
        self.reader.import_decrypted.assert_not_called()
        self.assert_staging_clean()

    def test_source_mutation_after_its_decryption_aborts_import(self):
        self.save_keys()

        def progress(value):
            if value["done_files"] == 1:
                with (self.database_root / "contact/contact.db").open("ab") as output:
                    output.write(b"synthetic source mutation")

        with self.assertRaises(ValueError):
            import_account(self.account, self.state, self.library, Mock(), progress=progress)
        self.reader.import_decrypted.assert_not_called()
        self.assert_staging_clean()

    def test_wal_created_after_its_database_was_decrypted_aborts_import(self):
        self.save_keys()

        def progress(value):
            if value["done_files"] == 1:
                (self.database_root / "contact/contact.db-wal").write_bytes(b"synthetic pending WAL")

        with self.assertRaises((ValueError, WeChatCryptoError)):
            import_account(self.account, self.state, self.library, Mock(), progress=progress)
        self.reader.import_decrypted.assert_not_called()
        self.assert_staging_clean()

    def test_new_message_database_during_staging_aborts_import(self):
        self.save_keys()

        def progress(value):
            if value["done_files"] == 1:
                data = (self.database_root / "message/message_0.db").read_bytes()
                (self.database_root / "message/message_9.db").write_bytes(data)

        with self.assertRaises(ValueError):
            import_account(self.account, self.state, self.library, Mock(), progress=progress)
        self.reader.import_decrypted.assert_not_called()
        self.assert_staging_clean()

    def test_wechat_starting_between_databases_aborts_import(self):
        self.save_keys()
        closed = Mock(side_effect=[None, None, ValueError("WeChat is running")])
        with self.assertRaisesRegex(ValueError, "WeChat is running"):
            import_account(self.account, self.state, self.library, closed)
        self.reader.import_decrypted.assert_not_called()
        self.assert_staging_clean()


if __name__ == "__main__":
    unittest.main()
