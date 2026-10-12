"""Exercise isolated archive merges, portable identities and failure rollback."""

import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from vault.archive_import import archive_import_info, import_archive
from vault.library import Library
from vault.media_store import signature
from vault.readable_archive import create_archive, export_summary, open_archive


class ArchiveImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = Library(self.root / "source")
        self.target = Library(self.root / "target")
        self.payload = json.dumps({"title": "Same display name", "messages": [
            {"sender": "Same sender", "text": "Identical message", "timestamp": "2024-06-01T10:00:00Z"},
            {"sender": "Same sender", "text": "Identical message", "timestamp": "2024-06-01T10:00:00Z"},
            {"sender": "Other", "text": "Media message", "is_self": True}]}).encode()
        self.source.import_file(self.payload, "synthetic.json")
        with self.source.connect() as db:
            self.cid = db.execute("SELECT id FROM conversations").fetchone()[0]
            self.ids = [row[0] for row in db.execute("SELECT id FROM messages ORDER BY id")]

    def archive(self, library=None):
        return Path(create_archive(library or self.source, self.root / "archives")["path"])

    def media(self, library=None):
        library = library or self.source
        payloads, assets = {}, {}
        for kind, mime, content in (
            ("image", "image/png", b"synthetic image" * 3000),
            ("audio", "audio/flac", b"synthetic audio" * 3000),
            ("video", "video/mp4", bytes(range(256)) * 500),
            ("original", "audio/silk", b"#!SILK_V3\n" + b"original voice" * 10000),
        ):
            path = self.root / (kind + ".bin")
            path.write_bytes(content)
            asset = library.media.store_file(path, kind, mime)
            assets[kind], payloads[asset] = asset, content
            library.media.link(self.ids[2], kind, asset)
        identity = hashlib.sha256(b"account:synthetic-person").hexdigest()
        library.set_avatar(identity, assets["image"])
        with library.connect() as db:
            db.execute("INSERT INTO conversation_avatars VALUES(?,?)", (self.cid, identity))
            db.execute("INSERT INTO message_avatars VALUES(?,?)", (self.ids[0], identity))
        return assets, payloads

    def wechat(self, library=None, digest="a" * 64):
        library = library or self.source
        with library.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS wechat_source_messages (
                account_key TEXT NOT NULL,database_name TEXT NOT NULL,table_name TEXT NOT NULL,
                local_id TEXT NOT NULL,message_id INTEGER NOT NULL UNIQUE,source_digest TEXT NOT NULL,
                PRIMARY KEY(account_key,database_name,table_name,local_id))""")
            db.execute("INSERT INTO wechat_source_messages VALUES(?,?,?,?,?,?)",
                       ("b" * 64, "message_0.db", "msg_" + "c" * 32, "10", self.ids[2], digest))

    def rewrite(self, archive, mutate):
        with zipfile.ZipFile(archive) as source:
            files = {name: source.read(name) for name in source.namelist()}
        manifest = json.loads(files["manifest.json"])
        mutate(files, manifest)
        files["manifest.json"] = json.dumps(manifest).encode()
        path = self.root / "modified.zip"
        with zipfile.ZipFile(path, "w") as target:
            for name, data in files.items():
                target.writestr(name, data)
        return path

    def snapshot(self, library=None):
        library = library or self.target
        with library.connect() as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: [tuple(row) for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')] for name in tables}

    def test_complete_merge_survives_source_removal_and_preserves_local_records(self):
        assets, payloads = self.media()
        self.target.import_file(b'[{"sender":"Local","text":"Keep existing"}]', "local.json")
        path = self.archive()
        before = self.target.messages(self.target.conversations()["conversations"][0]["id"])["messages"][0]
        preview = archive_import_info(path, self.target)
        self.assertEqual(preview["summary"]["messages"], 3)
        self.assertGreater(preview["required_bytes"], 0)
        self.assertFalse((self.target.state_dir / "opened-archives").exists())
        result = import_archive(self.target, path)
        self.assertEqual(result["added"], {"messages": 3, "conversations": 1, "objects": 4, "avatars": 1, "media_links": 4})
        self.assertEqual(result["summary"], preview["summary"])
        self.assertFalse(result["duplicate"])
        path.unlink()
        shutil.rmtree(self.source.state_dir)
        self.assertEqual(self.target.stats(), {"messages": 4, "conversations": 2})
        with self.target.connect() as db:
            self.assertEqual(db.execute("SELECT text FROM messages WHERE id=?", (before["id"],)).fetchone()[0], "Keep existing")
        avatars = [row["avatar_url"] for row in self.target.messages(self.cid)["messages"] if row["avatar_url"]]
        self.assertEqual(avatars, ["/api/media/" + assets["image"]])
        for asset, content in payloads.items():
            with self.target.media.open(asset) as (stream, _):
                self.assertEqual(stream.read(), content)
        self.assertEqual(list(self.target.state_dir.glob(".archive-import-*")), [])

    def test_same_json_in_different_global_id_positions_and_identical_messages_deduplicate(self):
        self.target.import_file(b'[{"text":"Earlier unrelated import"}]', "first.json")
        self.target.import_file(self.payload, "synthetic.json")
        result = import_archive(self.target, self.archive())
        self.assertEqual(result["added"]["messages"], 0)
        self.assertEqual(result["skipped"]["messages"], 3)
        self.assertEqual(self.target.stats()["messages"], 4)
        self.assertTrue(result["duplicate"])

    def test_repeat_import_and_reexport_cycle_back_to_original_do_not_duplicate(self):
        self.media()
        path = self.archive()
        self.target.import_file(b'[{"text":"Other message"}]', "other.json")
        import_archive(self.target, path)
        repeated = import_archive(self.target, path)
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["skipped"]["messages"], 3)
        cycle = self.archive(self.target)
        original = import_archive(self.source, cycle)
        self.assertEqual(original["added"]["messages"], 1)
        self.assertEqual(original["skipped"]["messages"], 3)
        self.assertTrue(import_archive(self.target, self.archive(self.source))["duplicate"])
        self.assertEqual(self.source.stats()["messages"], 4)

    def test_incremental_archive_adds_only_new_rows_and_preserves_same_name_conversations(self):
        import_archive(self.target, self.archive())
        with self.source.connect() as db:
            db.execute("INSERT INTO messages(conversation_id,sender,text,timestamp,is_self) VALUES(?,?,?,?,?)",
                       (self.cid, "Same sender", "Identical message", "2024-06-01T10:00:00+00:00", 0))
        self.source.import_file(b'{"title":"Same display name","messages":[{"text":"Different origin"}]}', "different.json")
        result = import_archive(self.target, self.archive())
        self.assertEqual(result["added"]["messages"], 2)
        self.assertEqual(result["skipped"]["messages"], 3)
        self.assertEqual(self.target.stats()["conversations"], 2)

    def test_same_source_id_different_payload_keeps_both_versions(self):
        import_archive(self.target, self.archive())
        with self.source.connect() as db:
            db.execute("UPDATE messages SET text='Historical alternative' WHERE id=?", (self.ids[0],))
        path = self.archive()
        result = import_archive(self.target, path)
        self.assertEqual(result["added"]["messages"], 1)
        self.assertTrue(import_archive(self.target, path)["duplicate"])
        self.assertEqual(self.target.stats()["messages"], 4)

    def test_wechat_current_revision_stale_links_and_conflicting_revision_remain_separate(self):
        self.media()
        self.wechat()
        with self.source.connect() as db:
            db.execute("INSERT INTO media_import_revisions VALUES(?,?,?)", (self.ids[2], "image", "a" * 64))
            db.execute("INSERT INTO media_import_revisions VALUES(?,?,?)", (self.ids[2], "audio", "d" * 64))
        path = self.archive()
        import_archive(self.target, path)
        with self.target.connect() as db:
            target_id = db.execute("SELECT message_id FROM wechat_source_messages").fetchone()[0]
        self.assertEqual([item["kind"] for item in self.target.media.for_messages([target_id])[target_id]], ["image"])
        self.assertEqual(export_summary(self.target)["media"]["stale_links"], 3)
        with self.source.connect() as db:
            db.execute("UPDATE messages SET text='A later revision' WHERE id=?", (self.ids[2],))
            db.execute("UPDATE wechat_source_messages SET source_digest=?", ("e" * 64,))
        newer = self.archive()
        result = import_archive(self.target, newer)
        self.assertEqual(result["added"]["messages"], 1)
        self.assertTrue(import_archive(self.target, newer)["duplicate"])
        with self.target.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(DISTINCT account_key) FROM wechat_source_messages").fetchone()[0], 2)
        self.assertEqual(import_archive(self.source, self.archive(self.target))["added"]["messages"], 1)
        self.assertTrue(import_archive(self.target, self.archive(self.source))["duplicate"])

    def test_old_archive_after_native_update_retains_prior_provenance(self):
        self.wechat()
        old = self.archive()
        import_archive(self.source, old)
        with self.source.connect() as db:
            prior = [tuple(row) for row in db.execute("SELECT * FROM archive_message_origins ORDER BY origin_key")]
            db.execute("UPDATE messages SET text='Current native text' WHERE id=?", (self.ids[2],))
            db.execute("UPDATE wechat_source_messages SET source_digest=?", ("f" * 64,))
        result = import_archive(self.source, old)
        self.assertEqual(result["added"]["messages"], 1)
        with self.source.connect() as db:
            for row in prior:
                self.assertEqual(tuple(db.execute("SELECT * FROM archive_message_origins WHERE origin_key=?", (row[0],)).fetchone()), row)
        self.assertTrue(import_archive(self.source, old)["duplicate"])
        self.assertEqual(self.source.stats()["messages"], 4)

    def test_import_update_history_reexport_roundtrip_retains_original_lineage(self):
        self.wechat()
        old = self.archive()
        import_archive(self.target, old)
        with self.target.connect() as db:
            target_id = db.execute("SELECT message_id FROM wechat_source_messages").fetchone()[0]
            db.execute("UPDATE messages SET text='Updated native message' WHERE id=?", (target_id,))
            db.execute("UPDATE wechat_source_messages SET source_digest=?", ("f" * 64,))
        self.assertEqual(import_archive(self.target, old)["added"]["messages"], 1)
        result = import_archive(self.source, self.archive(self.target))
        self.assertEqual(result["added"]["messages"], 1)
        self.assertEqual(result["skipped"]["messages"], 3)
        self.assertEqual(self.source.stats()["messages"], 4)
        self.assertTrue(import_archive(self.target, self.archive(self.source))["duplicate"])

    def test_missing_local_objects_are_restored_even_when_all_messages_already_exist(self):
        self.media()
        path = self.archive()
        shutil.rmtree(self.source.media.objects)
        shutil.rmtree(self.source.media.cache)
        result = import_archive(self.source, path)
        self.assertEqual(result["added"]["messages"], 0)
        self.assertEqual(result["added"]["objects"], 4)
        self.assertEqual(len(list(self.source.media.objects.glob("*/*"))), 4)

    def test_existing_gzip_with_different_stored_bytes_is_verified_and_reused(self):
        assets, contents = self.media()
        path = self.archive()
        import_archive(self.target, path)
        with self.target.connect() as db:
            row = db.execute("SELECT * FROM media_assets WHERE id=?", (assets["original"],)).fetchone()
            self.assertEqual(row["encoding"], "gzip")
            target = self.target.media.objects / row["object_hash"][:2] / (row["object_hash"] + ".gz")
            changed = gzip.compress(contents[assets["original"]], compresslevel=1, mtime=123)
            target.write_bytes(changed)
            db.execute("UPDATE media_assets SET stored_size=? WHERE id=?", (len(changed), assets["original"]))
        before = target.read_bytes()
        result = import_archive(self.target, path)
        self.assertEqual(result["added"]["objects"], 0)
        self.assertEqual(target.read_bytes(), before)
        with self.target.media.open(assets["original"]) as (stream, _):
            self.assertEqual(stream.read(), contents[assets["original"]])

    def test_missing_status_asset_ids_cannot_mutate_another_message_after_remapping(self):
        self.source.media.unavailable(self.ids[0], "image", "missing", "Source missing")
        self.target.import_file(b'[{"text":"Keep separate"}]', "other.json")
        self.target.media.unavailable(1, "image", "locked", "Local locked")
        import_archive(self.target, self.archive())
        with self.target.connect() as db:
            mapped = db.execute("SELECT id FROM messages WHERE conversation_id=? ORDER BY id", (self.cid,)).fetchone()[0]
        self.target.media.unavailable(mapped, "image", "error", "Retried")
        self.assertEqual(self.target.media.for_messages([1])[1][0]["status"], "locked")

    def test_legacy_unknown_wechat_revisions_are_preserved_as_stale(self):
        self.media()
        self.wechat()
        with self.source.connect() as db:
            for name in ("avatar_assets", "conversation_avatars", "message_avatars", "media_import_revisions"):
                db.execute(f"DROP TABLE {name}")
        import_archive(self.target, self.archive())
        self.assertEqual(self.target.media.for_messages([self.ids[2]]), {})
        self.assertEqual(export_summary(self.target)["media"]["stale_links"], 4)
        self.assertEqual(export_summary(self.target)["avatars"]["total"], 0)

    def test_generic_legacy_archive_without_optional_tables_still_has_visible_media(self):
        self.media()
        with self.source.connect() as db:
            for table in ("avatar_assets", "conversation_avatars", "message_avatars", "media_import_revisions"):
                db.execute(f"DROP TABLE {table}")
        result = import_archive(self.target, self.archive())
        self.assertEqual(result["unverified_media"], 0)
        self.assertEqual(len(self.target.media.for_messages([self.ids[2]])[self.ids[2]]), 4)

    def test_unknown_wechat_revision_cannot_upgrade_a_current_missing_link(self):
        self.wechat()
        self.source.media.unavailable(self.ids[2], "image")
        with self.source.connect() as db:
            db.execute("INSERT INTO media_import_revisions VALUES(?,?,?)", (self.ids[2], "image", "a" * 64))
        import_archive(self.target, self.archive())
        self.media()
        with self.source.connect() as db:
            db.execute("DROP TABLE media_import_revisions")
        result = import_archive(self.target, self.archive())
        self.assertEqual(result["unverified_media"], 4)
        visible = self.target.media.for_messages([self.ids[2]])[self.ids[2]]
        self.assertEqual([(item["kind"], item["status"]) for item in visible], [("image", "missing")])

    def test_original_gzip_payload_hash_is_checked_even_with_valid_entry_hash(self):
        self.media()
        def corrupt(files, manifest):
            name = next(name for name in files if name.endswith(".gz"))
            data = bytearray(gzip.decompress(files[name]))
            data[0] ^= 1
            replacement = gzip.compress(bytes(data), mtime=0)
            files[name] = replacement
            manifest["files"][name] = {"size": len(replacement), "sha256": hashlib.sha256(replacement).hexdigest()}
            path = self.root / "modified-db.sqlite3"
            path.write_bytes(files["library.sqlite3"])
            with sqlite3.connect(path) as db:
                db.execute("UPDATE media_assets SET stored_size=? WHERE object_hash=?", (len(replacement), Path(name).stem))
            files["library.sqlite3"] = path.read_bytes()
            manifest["files"]["library.sqlite3"] = {"size": len(files["library.sqlite3"]), "sha256": hashlib.sha256(files["library.sqlite3"]).hexdigest()}
        path = self.rewrite(self.archive(), corrupt)
        with self.assertRaisesRegex(ValueError, "integrity"):
            import_archive(self.target, path)
        self.assertEqual(self.target.stats()["messages"], 0)
        self.assertEqual(list(self.target.media.objects.glob("*/*")), [])

    def test_corrupt_media_rolls_back_every_record_and_new_object(self):
        self.media()
        self.target.import_file(b'[{"text":"Keep original"}]', "local.json")
        before = self.snapshot()
        def corrupt(files, manifest):
            name = sorted(name for name in files if name.startswith("media/"))[-1]
            files[name] = bytes([files[name][0] ^ 1]) + files[name][1:]
        path = self.rewrite(self.archive(), corrupt)
        with self.assertRaises(ValueError):
            import_archive(self.target, path)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.target.media.objects.glob("*/*")), [])
        self.assertEqual(list(self.target.state_dir.glob(".archive-import-*")), [])

    def test_late_failure_rolls_back_schema_messages_links_and_objects(self):
        self.media()
        before = self.snapshot()
        def fail(progress):
            if progress["phase"] == "committing":
                raise OSError("Synthetic disk failure")
        with self.assertRaises(OSError):
            import_archive(self.target, self.archive(), fail)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.target.media.objects.glob("*/*")), [])

    def test_source_change_and_preview_signature_change_abort_without_import(self):
        path = self.archive()
        expected = signature(path.stat())
        with path.open("ab") as output:
            output.write(b"extra")
        with self.assertRaisesRegex(ValueError, "preview"):
            import_archive(self.target, path, expected_signature=expected)
        def change(progress):
            if progress["phase"] == "messages":
                with path.open("ab") as output:
                    output.write(b"extra")
        with self.assertRaisesRegex(ValueError, "changed"):
            import_archive(self.target, path, change)
        self.assertEqual(self.target.stats()["messages"], 0)

    def test_non_readable_zip_and_invalid_database_preview_are_value_errors(self):
        path = self.root / "ordinary.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("raw.db", b"database")
        with self.assertRaisesRegex(ValueError, "ZIP"):
            archive_import_info(path, self.target)
        path.write_bytes(b"invalid zip")
        with self.assertRaises(ValueError):
            archive_import_info(path)
        self.assertEqual(list(self.target.state_dir.glob(".archive-import-*")), [])

    def test_existing_corrupt_or_symlink_object_is_never_overwritten(self):
        self.media()
        path = self.archive()
        import_archive(self.target, path)
        target = next(self.target.media.objects.glob("*/*"))
        before = self.snapshot()
        target.write_bytes(b"broken")
        with self.assertRaises(ValueError):
            import_archive(self.target, path)
        self.assertEqual(target.read_bytes(), b"broken")
        self.assertEqual(self.snapshot(), before)
        target.unlink()
        unrelated = self.root / "unrelated.bin"
        unrelated.write_bytes(b"keep")
        target.symlink_to(unrelated)
        with self.assertRaises(ValueError):
            import_archive(self.target, path)
        self.assertEqual(unrelated.read_bytes(), b"keep")

    def test_opened_archive_destination_is_rejected(self):
        state = self.root / "opened"
        state.mkdir()
        path = self.archive()
        opened = open_archive(path, state)
        with self.assertRaisesRegex(ValueError, "read-only"):
            import_archive(opened, path)

    def test_invalid_provenance_alias_does_not_replace_existing_mapping(self):
        path = self.archive()
        import_archive(self.target, path)
        with self.source.connect() as db, self.target.connect() as current:
            db.execute("CREATE TABLE archive_message_origins(origin_key TEXT PRIMARY KEY,message_id INTEGER,payload_digest TEXT)")
            origin = current.execute("SELECT origin_key FROM archive_message_origins WHERE message_id=?", (self.ids[0],)).fetchone()[0]
            db.execute("UPDATE messages SET text='Different content' WHERE id=?", (self.ids[0],))
            row = db.execute("SELECT conversation_id,sender,text,timestamp,is_self FROM messages WHERE id=?", (self.ids[0],)).fetchone()
            digest = hashlib.sha256(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            db.execute("INSERT INTO archive_message_origins VALUES(?,?,?)", (origin, self.ids[0], digest))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "provenance"):
            import_archive(self.target, self.archive())
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
