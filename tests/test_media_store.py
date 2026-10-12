"""Test private media deduplication, integrity checks, and batch transactions."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vault.library import Library


class MediaStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = Library(self.root / "state")
        self.media = self.library.media
        self.library.import_file(json.dumps({"title": "Synthetic", "messages": [
            {"sender": "Tester", "text": "First"}, {"sender": "Tester", "text": "Second"}]}).encode(), "synthetic.json")
        with self.library.connect() as db:
            self.ids = [row[0] for row in db.execute("SELECT id FROM messages ORDER BY id")]
        self.source = self.root / "media.bin"

    def store(self, data):
        self.source.write_bytes(data)
        return self.media.store_file(self.source, "original", "application/octet-stream", "synthetic.bin")

    def object_path(self, asset):
        with self.library.connect() as db:
            row = db.execute("SELECT object_hash,encoding FROM media_assets WHERE id=?", (asset,)).fetchone()
        return self.media.objects / row[0][:2] / (row[0] + (".gz" if row[1] == "gzip" else ".bin"))

    def test_compressed_objects_deduplicate_and_survive_source_deletion(self):
        data = b"compressible synthetic media" * 10000
        first = self.store(data)
        second = self.store(data)
        self.assertEqual(first, second)
        for message in self.ids:
            self.media.link(message, "original", first)
        self.source.unlink()
        info = self.media.stats()
        self.assertEqual(info["unique_objects"], 1)
        self.assertLess(info["stored_bytes"], len(data) // 20)
        self.assertEqual(info["deduplicated_bytes"], len(data))
        with self.media.open(first) as (stream, metadata):
            self.assertEqual(stream.read(), data)
            self.assertEqual(metadata["encoding"], "gzip")
        for folder in (self.media.root, self.media.objects, self.media.cache):
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        for path in (*self.media.objects.glob("*/*"), *self.media.cache.iterdir()):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_raw_same_size_tampering_is_detected_after_an_earlier_open(self):
        data = os.urandom(4096)
        asset = self.store(data)
        with self.media.open(asset) as (stream, metadata):
            self.assertEqual(stream.read(), data)
            self.assertEqual(metadata["encoding"], "raw")
        path = self.object_path(asset)
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        with self.assertRaisesRegex(ValueError, "integrity"):
            with self.media.open(asset):
                self.fail("Corrupt media was served")

    def test_same_size_decoded_cache_tampering_is_detected(self):
        data = b"x" * 10000
        asset = self.store(data)
        with self.media.open(asset) as (stream, metadata):
            self.assertEqual(stream.read(), data)
        cached = self.media.cache / hashlib.sha256(data).hexdigest()
        cached.write_bytes(b"y" + data[1:])
        with self.assertRaisesRegex(ValueError, "integrity"):
            with self.media.open(asset):
                self.fail("Corrupt decoded media was served")

    def test_duplicate_store_does_not_trust_a_corrupted_existing_object(self):
        data = os.urandom(4096)
        asset = self.store(data)
        path = self.object_path(asset)
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.store(data)

    def test_metadata_path_traversal_and_object_symlinks_are_rejected(self):
        asset = self.store(os.urandom(1000))
        path = self.object_path(asset)
        outside = self.root / "outside.bin"
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            with self.media.open(asset):
                self.fail("A symbolic link was served")
        with self.library.connect() as db:
            db.execute("UPDATE media_assets SET object_hash='../escape' WHERE id=?", (asset,))
        with self.assertRaisesRegex(ValueError, "metadata"):
            with self.media.open(asset):
                self.fail("An unsafe object path was used")

    def test_object_directory_symlinks_cannot_redirect_writes(self):
        data = os.urandom(1000)
        digest = hashlib.sha256(data).hexdigest()
        outside = self.root / "outside"
        outside.mkdir()
        (self.media.objects / digest[:2]).symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            self.store(data)
        self.assertEqual(list(outside.iterdir()), [])

    def test_batch_reuses_one_connection_for_store_and_links(self):
        self.source.write_bytes(b"sample" * 1000)
        with patch.object(self.library, "connect", wraps=self.library.connect) as connections:
            with self.media.batch():
                asset = self.media.store_file(self.source, "original", "application/octet-stream")
                self.media.link(self.ids[0], "original", asset)
                self.media.unavailable(self.ids[1], "audio", "missing", "Not downloaded")
            self.assertEqual(connections.call_count, 1)
        result = self.media.for_messages(self.ids)
        self.assertEqual(result[self.ids[0]][0]["status"], "ready")
        self.assertEqual(result[self.ids[1]][0]["status"], "missing")

    def test_failed_batch_rolls_back_all_metadata_and_links(self):
        self.source.write_bytes(b"sample" * 1000)
        with self.assertRaisesRegex(RuntimeError, "Synthetic rollback"):
            with self.media.batch():
                asset = self.media.store_file(self.source, "original", "application/octet-stream")
                self.media.link(self.ids[0], "original", asset)
                self.media.unavailable(self.ids[1], "audio", "missing")
                raise RuntimeError("Synthetic rollback")
        with self.library.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_media").fetchone()[0], 0)
        with self.media.batch():
            asset = self.media.store_file(self.source, "original", "application/octet-stream")
            self.media.link(self.ids[0], "original", asset)
        self.assertEqual(self.media.stats()["unique_objects"], 1)

    def test_nested_batches_are_explicitly_rejected(self):
        with self.media.batch():
            with self.assertRaisesRegex(ValueError, "Nested"):
                with self.media.batch():
                    pass


if __name__ == "__main__":
    unittest.main()
