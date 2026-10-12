"""Test self-contained readable archives without any real conversation data."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from vault.library import Library
import vault.readable_archive as archives
from vault.readable_archive import ArchiveSource, create_archive, open_archive


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


if __name__ == "__main__":
    unittest.main()
