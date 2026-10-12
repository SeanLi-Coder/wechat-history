"""Test self-contained readable archives without any real conversation data."""

import hashlib
import gzip
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from vault.library import Library
import vault.readable_archive as archives
from vault.readable_archive import ArchiveSource, create_archive, export_summary, open_archive


class ReadableArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = Library(self.root / "original-state")
        self.library.import_file(json.dumps({"title": "Synthetic conversation", "messages": [
            {"sender": "Tester", "text": "Offline test"}, {"sender": "Tester", "text": "Duplicate media"}]}).encode(), "synthetic.json")
        with self.library.connect() as db:
            self.ids = [row[0] for row in db.execute("SELECT id FROM messages ORDER BY id")]
        self.data = b"independent synthetic media" * 10000
        self.source = self.root / "source.bin"
        self.source.write_bytes(self.data)
        self.asset = self.library.media.store_file(self.source, "original", "application/octet-stream")
        for message in self.ids:
            self.library.media.link(message, "original", self.asset)
        self.open_state = self.root / "opened-state"
        self.open_state.mkdir()

    def archive(self):
        return Path(create_archive(self.library, self.root / "archives")["path"])

    def rewrite(self, path, mutate):
        with zipfile.ZipFile(path) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(files["manifest.json"])
        mutate(files, manifest)
        files["manifest.json"] = json.dumps(manifest).encode()
        target = self.root / "modified.zip"
        with zipfile.ZipFile(target, "w") as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        return target

    def test_archive_is_deduplicated_self_contained_and_lazily_extracted(self):
        path = self.archive()
        with zipfile.ZipFile(path) as archive:
            media_files = [name for name in archive.namelist() if name.startswith("media/")]
            self.assertEqual(len(media_files), 1)
            self.assertIn("library.sqlite3", archive.namelist())
            self.assertNotIn("wechat-keys.json", archive.namelist())
        self.source.unlink()
        shutil.rmtree(self.library.state_dir)
        opened = open_archive(path, self.open_state)
        self.assertEqual(opened.stats()["messages"], 2)
        self.assertEqual(list(opened.media.objects.glob("*/*")), [])
        with opened.media.open(self.asset) as (stream, metadata):
            self.assertEqual(stream.read(), self.data)
        self.assertEqual(len(list(opened.media.objects.glob("*/*"))), 1)
        self.assertEqual(opened.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(opened.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_opened_library_is_read_only_and_original_library_is_unchanged(self):
        before = hashlib.sha256(self.library.path.read_bytes()).digest()
        opened = open_archive(self.archive(), self.open_state)
        self.assertNotEqual(opened.path, self.library.path)
        with self.assertRaises(sqlite3.OperationalError):
            with opened.connect() as db:
                db.execute("DELETE FROM messages")
        with self.assertRaisesRegex(ValueError, "read-only"):
            opened.media.store_file(self.source, "original", "application/octet-stream")
        self.assertEqual(hashlib.sha256(self.library.path.read_bytes()).digest(), before)
        self.assertEqual(self.library.stats()["messages"], 2)

    def test_same_size_archive_entry_tampering_is_rejected_on_lazy_read(self):
        path = self.archive()

        def corrupt(files, manifest):
            name = next(name for name in files if name.startswith("media/"))
            data = files[name]
            files[name] = bytes([data[0] ^ 1]) + data[1:]

        modified = self.rewrite(path, corrupt)
        opened = open_archive(modified, self.open_state)
        with self.assertRaisesRegex(ValueError, "integrity"):
            with opened.media.open(self.asset):
                self.fail("A corrupted archive entry was served")
        self.assertEqual(list(opened.media.objects.glob("*/*")), [])

    def test_unlisted_path_traversal_entry_is_rejected(self):
        def add_escape(files, manifest):
            files["../escape"] = b"untrusted"
        with self.assertRaisesRegex(ValueError, "unexpected"):
            ArchiveSource(self.rewrite(self.archive(), add_escape))
        self.assertFalse((self.root / "escape").exists())

    def test_manifest_path_traversal_is_rejected(self):
        def add_escape(files, manifest):
            files["../escape"] = b"untrusted"
            manifest["files"]["../escape"] = {"size": 9, "sha256": hashlib.sha256(b"untrusted").hexdigest()}
        with self.assertRaisesRegex(ValueError, "invalid object path"):
            ArchiveSource(self.rewrite(self.archive(), add_escape))

    def test_symbolic_link_zip_entry_is_rejected(self):
        path = self.archive()
        target = self.root / "symlink.zip"
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(target, "w") as output:
            for member in source.infolist():
                if member.filename.startswith("media/"):
                    member.external_attr = 0o120777 << 16
                output.writestr(member, source.read(member))
        with self.assertRaisesRegex(ValueError, "regular files"):
            ArchiveSource(target)

    def test_same_size_corrupt_raw_object_cannot_be_archived(self):
        raw = os.urandom(4096)
        self.source.write_bytes(raw)
        self.library.media.store_file(self.source, "original", "application/octet-stream")
        digest = hashlib.sha256(raw).hexdigest()
        path = self.library.media.objects / digest[:2] / (digest + ".bin")
        path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.archive()
        self.assertEqual(list((self.root / "archives").iterdir()), [])

    def test_missing_referenced_media_is_rejected_before_open(self):
        def remove_media(files, manifest):
            name = next(name for name in files if name.startswith("media/"))
            del files[name]
            del manifest["files"][name]
        with self.assertRaisesRegex(ValueError, "missing a referenced"):
            open_archive(self.rewrite(self.archive(), remove_media), self.open_state)
        self.assertEqual(list((self.open_state / "opened-archives").iterdir()), [])

    def test_malicious_sqlite_views_and_triggers_are_rejected(self):
        for statement in (
            "CREATE VIEW hostile AS SELECT load_extension('untrusted-extension')",
            "CREATE TRIGGER hostile AFTER INSERT ON messages BEGIN DELETE FROM messages; END",
        ):
            with self.subTest(statement=statement):
                with self.library.connect() as db:
                    db.execute(statement)
                with self.assertRaisesRegex(ValueError, "views, triggers"):
                    open_archive(self.archive(), self.open_state)
                with self.library.connect() as db:
                    db.execute("DROP " + ("VIEW" if "VIEW" in statement else "TRIGGER") + " hostile")

    def test_extract_never_overwrites_existing_file(self):
        source = ArchiveSource(self.archive())
        existing = self.root / "existing.db"
        existing.write_bytes(b"Preserve existing content")
        with self.assertRaisesRegex(ValueError, "already exists"):
            source.extract("library.sqlite3", existing)
        self.assertEqual(existing.read_bytes(), b"Preserve existing content")

    def test_archive_replacement_after_opening_is_rejected(self):
        path = self.archive()
        source = ArchiveSource(path)
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "changed after opening"):
            source.extract("library.sqlite3", self.root / "extracted.db")
        self.assertFalse((self.root / "extracted.db").exists())

    def test_source_mutation_during_archive_copy_does_not_publish_zip(self):
        actual_copy = archives._copy
        changed = False
        object_path = next(self.library.media.objects.glob("*/*"))
        object_inode = object_path.stat().st_ino

        def mutate(inp, out, expected, digest=None):
            nonlocal changed
            actual_copy(inp, out, expected, digest)
            if not changed and os.fstat(inp.fileno()).st_ino == object_inode:
                with object_path.open("ab") as source:
                    source.write(b"changed")
                changed = True

        with patch.object(archives, "_copy", side_effect=mutate), self.assertRaises((ValueError, OSError)):
            self.archive()
        self.assertEqual(list((self.root / "archives").iterdir()), [])

    def add_complete_media_fixture(self):
        self.library.import_file(json.dumps({"title": "Second synthetic conversation", "messages": [
            {"sender": "Other", "text": f"Other message {index}", "timestamp": 1700000000 + index}
            for index in range(5)]}).encode(), "second.json")
        with self.library.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM messages ORDER BY id")]
        expected = {self.asset: self.data}
        for kind, mime, data, message_ids in (
                ("image", "image/png", b"Synthetic image bytes", ids[:2]),
                ("audio", "audio/wav", b"Synthetic audio samples" * 1000, [ids[2]]),
                ("video", "video/mp4", b"Synthetic video container" + os.urandom(8192), [ids[3]]),
                ("original", "audio/silk", b"#!SILK_V3 Synthetic retained source" * 100, [])):
            source = self.root / f"synthetic-{kind}.bin"
            source.write_bytes(data)
            asset = self.library.media.store_file(source, kind, mime)
            expected[asset] = data
            for message_id in message_ids:
                self.library.media.link(message_id, kind, asset)
        for message_id, kind, status in ((ids[4], "image", "missing"), (ids[5], "audio", "locked"),
                                         (ids[6], "video", "unsupported"), (ids[0], "audio", "error")):
            self.library.media.unavailable(message_id, kind, status, "Synthetic unavailable media")
        source = self.root / "synthetic-avatar.png"
        source.write_bytes(b"Synthetic avatar image")
        avatar = self.library.media.store_file(source, "image", "image/png")
        identity = hashlib.sha256(b"synthetic-account:synthetic-identity").hexdigest()
        self.library.set_avatar(identity, avatar)
        with self.library.connect() as db:
            cid = db.execute("SELECT conversation_id FROM messages WHERE id=?", (ids[0],)).fetchone()[0]
            db.execute("INSERT INTO conversation_avatars VALUES(?,?)", (cid, identity))
            db.execute("INSERT INTO message_avatars VALUES(?,?)", (ids[0], identity))
            db.execute("INSERT INTO avatar_assets VALUES(?,?)", (hashlib.sha256(b"synthetic-missing-identity").hexdigest(), "f" * 64))
        expected[avatar] = source.read_bytes()
        return expected, ids

    def test_opened_archive_reexports_all_messages_media_and_avatars_without_cache_extraction(self):
        expected, ids = self.add_complete_media_fixture()
        initial = create_archive(self.library, self.root / "archives")
        source = Path(initial["path"])
        source_hash = hashlib.sha256(source.read_bytes()).digest()
        opened = open_archive(source, self.open_state)
        self.assertEqual(opened.search("Other message 0")["total"], 1)
        preview = export_summary(opened)
        self.assertEqual((preview["conversations"], preview["messages"]), (2, 7))
        self.assertEqual(preview["avatars"], {"total": 2, "ready": 1, "unavailable": 1})
        self.assertEqual(preview["unavailable_media"], 4)
        statuses = {(item["kind"], item["status"]): item["count"] for item in preview["media"]["items"]}
        self.assertEqual(statuses, {("original", "ready"): 2, ("image", "ready"): 2,
            ("audio", "ready"): 1, ("video", "ready"): 1, ("image", "missing"): 1,
            ("audio", "locked"): 1, ("video", "unsupported"): 1, ("audio", "error"): 1})
        self.assertEqual(preview["media"]["unique_objects"], 6)
        self.assertEqual(preview["media"]["original_bytes"], sum(map(len, expected.values())))
        self.assertLess(preview["media"]["stored_bytes"], preview["media"]["original_bytes"])
        self.source.unlink()
        shutil.rmtree(self.library.state_dir)
        self.assertEqual(list(opened.media.objects.glob("*/*")), [])
        with patch.object(opened.media, "open", side_effect=AssertionError("Unexpected decoded media read")), \
                patch.object(opened.media.archive_source, "extract", side_effect=AssertionError("Unexpected media extraction")):
            result = create_archive(opened, self.root / "reexport")
        self.assertEqual(result["summary"], preview)
        self.assertEqual(result["objects"], 6)
        self.assertEqual(list(opened.media.objects.glob("*/*")), [])
        self.assertEqual(list(opened.media.cache.iterdir()), [])
        target = Path(result["path"])
        with zipfile.ZipFile(source) as before, zipfile.ZipFile(target) as after:
            names = set(before.namelist()) - {"manifest.json"}
            self.assertEqual(names, set(after.namelist()) - {"manifest.json"})
            for name in names:
                self.assertEqual(before.read(name), after.read(name))
            self.assertEqual(json.loads(after.read("manifest.json"))["summary"], preview)
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), source_hash)
        source.unlink()
        shutil.rmtree(opened.state_dir)
        restored = open_archive(target, self.open_state)
        self.assertEqual(restored.stats(), {"conversations": 2, "messages": len(ids)})
        self.assertEqual(export_summary(restored), preview)
        for asset, body in expected.items():
            with restored.media.open(asset) as (stream, info):
                self.assertEqual(stream.read(), body)
                self.assertEqual(info["byte_size"], len(body))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_legacy_archive_summary_is_recomputed_instead_of_trusting_manifest(self):
        with self.library.connect() as db:
            for table in ("avatar_assets", "message_avatars", "conversation_avatars", "media_import_revisions"):
                db.execute(f"DROP TABLE {table}")
        original = self.archive()

        def invent_summary(files, manifest):
            manifest["summary"] = {"messages": 999999, "avatars": {"ready": 999999}}

        opened = open_archive(self.rewrite(original, invent_summary), self.open_state)
        result = create_archive(opened, self.root / "reexport")
        self.assertEqual(result["summary"], export_summary(opened))
        self.assertEqual(result["summary"]["messages"], 2)
        self.assertEqual(result["summary"]["avatars"], {"total": 0, "ready": 0, "unavailable": 0})

    def test_corrupt_unread_archive_media_aborts_export_and_cleans_output(self):
        original = self.archive()

        def corrupt(files, manifest):
            name = next(name for name in files if name.startswith("media/"))
            files[name] = bytes([files[name][0] ^ 1]) + files[name][1:]

        modified = self.rewrite(original, corrupt)
        opened = open_archive(modified, self.open_state)
        shutil.rmtree(self.library.state_dir)
        with self.assertRaisesRegex(ValueError, "integrity"):
            create_archive(opened, self.root / "failed")
        self.assertEqual(list((self.root / "failed").iterdir()), [])
        self.assertEqual(list(opened.media.objects.glob("*/*")), [])

    def test_changed_gzip_payload_with_repaired_entry_hash_still_fails_content_validation(self):
        original = self.archive()

        def corrupt_decoded_content(files, manifest):
            name = next(name for name in files if name.endswith(".gz"))
            modified = gzip.compress(b"x" * len(self.data), mtime=0)
            files[name] = modified
            manifest["files"][name] = {"size": len(modified), "sha256": hashlib.sha256(modified).hexdigest()}
            database = self.root / "mutated.sqlite3"
            database.write_bytes(files["library.sqlite3"])
            with sqlite3.connect(database) as db:
                db.execute("UPDATE media_assets SET stored_size=? WHERE id=?", (len(modified), self.asset))
            files["library.sqlite3"] = database.read_bytes()
            manifest["files"]["library.sqlite3"] = {"size": len(files["library.sqlite3"]),
                "sha256": hashlib.sha256(files["library.sqlite3"]).hexdigest()}

        opened = open_archive(self.rewrite(original, corrupt_decoded_content), self.open_state)
        with self.assertRaisesRegex(ValueError, "content integrity"):
            create_archive(opened, self.root / "failed")
        self.assertEqual(list((self.root / "failed").iterdir()), [])

    def test_compressed_object_decoded_size_is_verified(self):
        with self.library.connect() as db:
            db.execute("UPDATE media_assets SET byte_size=byte_size+1 WHERE id=?", (self.asset,))
        with self.assertRaisesRegex(ValueError, "content integrity"):
            self.archive()
        self.assertEqual(list((self.root / "archives").iterdir()), [])

    def test_input_zip_mutation_is_detected_before_reexport_publication(self):
        original = self.archive()
        opened = open_archive(original, self.open_state)
        changed = False

        def progress(value):
            nonlocal changed
            if value["done_files"] == value["total_files"] and not changed:
                with original.open("ab") as stream:
                    stream.write(b"Synthetic concurrent change")
                changed = True

        with self.assertRaisesRegex(ValueError, "changed"):
            create_archive(opened, self.root / "failed", progress)
        self.assertTrue(changed)
        self.assertEqual(list((self.root / "failed").iterdir()), [])

    def test_reexport_never_overwrites_its_input_even_when_generated_name_collides(self):
        with patch.object(archives, "datetime") as clock, patch.object(archives.os, "urandom", return_value=b"abc"):
            clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
            original = self.archive()
            before = original.read_bytes()
            opened = open_archive(original, self.open_state)
            with self.assertRaises(FileExistsError):
                create_archive(opened, original.parent)
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(list(original.parent.iterdir()), [original])

    def test_shared_object_metadata_must_be_consistent(self):
        duplicate = self.library.media.store_file(self.source, "original", "application/octet-stream", detail="Synthetic variant")
        with self.library.connect() as db:
            db.execute("UPDATE media_assets SET stored_size=stored_size+1 WHERE id=?", (duplicate,))
        with self.assertRaisesRegex(ValueError, "inconsistent metadata"):
            self.archive()
        self.assertEqual(list((self.root / "archives").iterdir()), [])

    def test_summary_uses_one_read_transaction_and_excludes_stale_media_links(self):
        with self.library.connect() as db:
            db.execute("CREATE TABLE wechat_source_messages(message_id INTEGER UNIQUE, source_digest TEXT)")
            db.execute("INSERT INTO wechat_source_messages VALUES(?,?)", (self.ids[0], "a" * 64))
            db.execute("INSERT INTO media_import_revisions VALUES(?,?,?)", (self.ids[0], "original", "b" * 64))
        original_connect = self.library.connect
        statements = []

        from contextlib import contextmanager

        @contextmanager
        def observed_connection():
            with original_connect() as db:
                db.set_trace_callback(statements.append)
                yield db

        with patch.object(self.library, "connect", observed_connection), \
                patch.object(self.library.media, "open", side_effect=AssertionError("Unexpected media read")):
            summary = export_summary(self.library)
        self.assertEqual(sum(statement == "BEGIN" for statement in statements), 1)
        self.assertEqual(summary["media"]["items"], [{"kind": "original", "status": "ready", "count": 1}])
        self.assertEqual(summary["media"]["stale_links"], 1)
        self.assertEqual(summary["media"]["unique_objects"], 1)
        self.assertEqual(set(self.library.media.for_messages(self.ids)), {self.ids[1]})
        self.assertEqual(create_archive(self.library, self.root / "archives")["summary"], summary)


if __name__ == "__main__":
    unittest.main()
