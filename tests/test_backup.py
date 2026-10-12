"""Exercise archive consistency and failure safety using synthetic data only."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from vault.backup import BackupEngine, BackupError, CHUNK_SIZE, discover_sources


class BackupEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.destination = self.root / "archives"
        self.state = self.root / "state"
        self.source.mkdir()
        self.engine = BackupEngine(self.state)

    def tearDown(self):
        self.temporary.cleanup()

    def create(self, **kwargs):
        return self.engine.create(str(self.source), str(self.destination), **kwargs)

    def assert_no_partial_or_archive(self):
        self.assertEqual(list(self.destination.glob("*.zip")), [])
        self.assertEqual(list(self.destination.glob("*.partial")), [])

    def test_round_trip_preserves_bytes_and_auditable_manifest(self):
        content = {
            "chat.sqlite": b"SQLite format 3\x00" + b"message data\n" * 500,
            "attachments/photo.bin": bytes(range(256)) * 30,
            "attachments/empty.dat": b"",
            "contacts/alice.txt": "Synthetic conversation\n".encode(),
        }
        for name, value in content.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        (self.source / "empty-directory").mkdir()
        before = {name: (self.source / name).stat() for name in content}
        progress = []
        snapshot = self.create(progress=progress.append)
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual(snapshot["file_count"], len(content))
        self.assertEqual(snapshot["original_bytes"], sum(map(len, content.values())))
        self.assertLess(snapshot["archive_bytes"], snapshot["original_bytes"])
        self.assertEqual(Path(snapshot["path"]).stat().st_mode & 0o777, 0o600)
        manifest = self.engine.read_manifest(snapshot["id"], str(self.destination))
        self.assertEqual(manifest["source"], str(self.source.resolve()))
        self.assertIn("empty-directory", manifest["directories"])
        records = {item["path"]: item for item in manifest["files"]}
        with zipfile.ZipFile(snapshot["path"]) as archive:
            for name, value in content.items():
                self.assertEqual(archive.read(f"data/{name}"), value)
                self.assertEqual(records[name]["sha256"], hashlib.sha256(value).hexdigest())
                self.assertEqual((self.source / name).read_bytes(), value)
                self.assertEqual((self.source / name).stat().st_mtime_ns, before[name].st_mtime_ns)
        result = self.engine.verify(snapshot["id"], str(self.destination))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked_files"], len(content))
        self.assertEqual(result["checked_bytes"], snapshot["original_bytes"])
        self.assertEqual(progress[-1]["done_files"], len(content))
        self.assertEqual(progress[-1]["done_bytes"], snapshot["original_bytes"])
        self.assertEqual(self.engine.list_snapshots(str(self.destination))[0]["id"], snapshot["id"])

    def test_empty_directory_is_a_valid_snapshot(self):
        snapshot = self.create()
        self.assertEqual(snapshot["file_count"], 0)
        self.assertTrue(self.engine.verify(snapshot["id"], str(self.destination))["ok"])

    def test_internal_symlinks_are_recorded_and_never_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("Do not copy this content.")
        (self.source / "file-link").symlink_to(outside / "private.txt")
        (self.source / "directory-link").symlink_to(outside, target_is_directory=True)
        (self.source / "missing-link").symlink_to(outside / "missing")
        (self.source / "chat.txt").write_text("Synthetic chat")
        snapshot = self.create()
        manifest = self.engine.read_manifest(snapshot["id"], str(self.destination))
        self.assertEqual(manifest["skipped_symlinks"],
                         ["directory-link", "file-link", "missing-link"])
        self.assertEqual(snapshot["file_count"], 1)
        with zipfile.ZipFile(snapshot["path"]) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "data/chat.txt"})

    def test_symlink_source_is_rejected(self):
        link = self.root / "source-link"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(BackupError) as error:
            self.engine.create(str(link), str(self.destination))
        self.assertEqual(error.exception.code, "symlink_source")

    def test_nested_destination_and_filesystem_root_are_rejected(self):
        for destination in [self.source, self.source / "backups"]:
            with self.subTest(destination=destination), self.assertRaises(BackupError) as error:
                self.engine.create(str(self.source), str(destination))
            self.assertEqual(error.exception.code, "nested_destination")
        with self.assertRaises(BackupError) as error:
            self.engine.create("/", str(self.destination))
        self.assertEqual(error.exception.code, "unsafe_source")
        self.assertFalse((self.source / "backups").exists())

    def test_destination_symlink_is_rejected(self):
        target = self.root / "target"
        target.mkdir()
        self.destination.symlink_to(target, target_is_directory=True)
        with self.assertRaises(BackupError) as error:
            self.create()
        self.assertEqual(error.exception.code, "symlink_destination")

    def test_file_mutation_during_streaming_removes_partial(self):
        path = self.source / "chat.txt"
        path.write_bytes(b"x" * (CHUNK_SIZE * 2))
        changed = False

        def mutate(progress):
            nonlocal changed
            if progress["done_bytes"] and not changed:
                changed = True
                path.write_bytes(b"y" * (CHUNK_SIZE * 2))

        with self.assertRaises(BackupError) as error:
            self.create(progress=mutate)
        self.assertEqual(error.exception.code, "source_changed")
        self.assert_no_partial_or_archive()

    def test_same_size_mutation_with_restored_mtime_is_detected(self):
        path = self.source / "chat.txt"
        path.write_bytes(b"before")
        original = path.stat()
        changed = False

        def mutate(progress):
            nonlocal changed
            if progress["done_bytes"] and not changed:
                changed = True
                path.write_bytes(b"after!")
                os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

        with self.assertRaises(BackupError) as error:
            self.create(progress=mutate)
        self.assertEqual(error.exception.code, "source_changed")
        self.assert_no_partial_or_archive()

    def test_file_added_after_initial_scan_is_detected(self):
        (self.source / "chat.txt").write_text("Synthetic chat")

        def add_file(progress):
            if progress["done_files"] == progress["total_files"]:
                (self.source / "new-chat.txt").write_text("A concurrent writer")

        with self.assertRaises(BackupError) as error:
            self.create(progress=add_file)
        self.assertEqual(error.exception.code, "source_changed")
        self.assert_no_partial_or_archive()

    def test_file_replaced_by_symlink_does_not_copy_external_data(self):
        original = self.source / "chat.txt"
        original.write_text("Synthetic chat")
        private = self.root / "private.txt"
        private.write_text("Do not copy this content.")

        def replace_file(progress):
            if not progress["current_file"]:
                original.unlink()
                original.symlink_to(private)

        with self.assertRaises(BackupError):
            self.create(progress=replace_file)
        self.assert_no_partial_or_archive()

    def test_directory_replaced_by_symlink_does_not_escape_source(self):
        folder = self.source / "subdir"
        folder.mkdir()
        (folder / "chat.txt").write_text("Synthetic chat")
        private = self.root / "private"
        private.mkdir()
        (private / "chat.txt").write_text("Do not copy this content.")

        def replace_directory(progress):
            if not progress["current_file"]:
                folder.rename(self.source / "renamed")
                folder.symlink_to(private, target_is_directory=True)

        with self.assertRaises(BackupError):
            self.create(progress=replace_directory)
        self.assert_no_partial_or_archive()

    def test_read_permission_failure_never_publishes_a_backup(self):
        (self.source / "chat.txt").write_text("Synthetic chat")
        real_open = os.open

        def deny_chat(path, *args, **kwargs):
            if str(path) == "chat.txt":
                raise PermissionError("Synthetic read permission failure")
            return real_open(path, *args, **kwargs)

        with patch("vault.backup.os.open", side_effect=deny_chat):
            with self.assertRaises(BackupError) as error:
                self.create()
        self.assertEqual(error.exception.code, "permission_denied")
        self.assert_no_partial_or_archive()

    def test_disk_space_preflight_fails_before_writing_an_archive(self):
        (self.source / "chat.txt").write_text("Synthetic chat")
        usage = type("DiskUsage", (), {"free": 0})()
        with patch("vault.backup.shutil.disk_usage", return_value=usage):
            with self.assertRaises(BackupError) as error:
                self.create()
        self.assertEqual(error.exception.code, "insufficient_space")
        self.assert_no_partial_or_archive()

    def test_shared_state_lock_prevents_overlapping_backups(self):
        (self.source / "chat.txt").write_text("Synthetic chat")
        other = BackupEngine(self.state)
        attempted = False

        def attempt_overlap(progress):
            nonlocal attempted
            if not attempted:
                attempted = True
                with self.assertRaises(BackupError) as error:
                    other.create(str(self.source), str(self.destination))
                self.assertEqual(error.exception.code, "backup_busy")

        self.create(progress=attempt_overlap)
        self.assertTrue(attempted)
        self.assertEqual(len(self.engine.list_snapshots(str(self.destination))), 1)

    def test_real_wechat_location_is_blocked_when_wechat_is_running(self):
        self.source = self.root / "xwechat_files"
        self.source.mkdir()
        running = CompletedProcess(["pgrep"], 0, stdout="123\n", stderr="")
        with patch("vault.backup.subprocess.run", return_value=running):
            with self.assertRaises(BackupError) as error:
                self.create()
        self.assertEqual(error.exception.code, "wechat_running")
        self.assert_no_partial_or_archive()

    def test_wechat_starting_during_backup_prevents_publication(self):
        self.source = self.root / "xwechat_files"
        self.source.mkdir()
        (self.source / "chat.txt").write_text("Synthetic chat")
        closed = CompletedProcess(["pgrep"], 1, stdout="", stderr="")
        running = CompletedProcess(["pgrep"], 0, stdout="123\n", stderr="")
        with patch("vault.backup.subprocess.run", side_effect=[closed, closed, closed, running]):
            with self.assertRaises(BackupError) as error:
                self.create()
        self.assertEqual(error.exception.code, "wechat_running")
        self.assert_no_partial_or_archive()

    def test_truncated_archive_is_listed_as_corrupt_and_fails_verification(self):
        (self.source / "chat.txt").write_text("Synthetic chat")
        snapshot = self.create()
        archive = Path(snapshot["path"])
        archive.write_bytes(archive.read_bytes()[:40])
        self.assertEqual(self.engine.list_snapshots(str(self.destination))[0]["status"], "corrupt")
        result = self.engine.verify(snapshot["id"], str(self.destination))
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_valid_zip_with_modified_content_fails_sha256_verification(self):
        (self.source / "chat.txt").write_bytes(b"original")
        snapshot = self.create()
        path = Path(snapshot["path"])
        manifest = self.engine.read_manifest(snapshot["id"], str(self.destination))
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("data/chat.txt", b"tampered")
            archive.writestr("manifest.json", json.dumps(manifest))
        result = self.engine.verify(snapshot["id"], str(self.destination))
        self.assertFalse(result["ok"])
        self.assertIn("Checksum mismatch", result["errors"][0])

    def test_manifest_path_traversal_is_rejected_without_extraction(self):
        (self.source / "chat.txt").write_bytes(b"original")
        snapshot = self.create()
        manifest = self.engine.read_manifest(snapshot["id"], str(self.destination))
        manifest["files"][0]["path"] = "../../outside.txt"
        with zipfile.ZipFile(snapshot["path"], "w") as archive:
            archive.writestr("data/../../outside.txt", b"original")
            archive.writestr("manifest.json", json.dumps(manifest))
        self.assertFalse(self.engine.verify(snapshot["id"], str(self.destination))["ok"])
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertEqual(self.engine.list_snapshots(str(self.destination))[0]["status"], "corrupt")

    def test_snapshot_id_traversal_is_rejected(self):
        with self.assertRaises(BackupError) as error:
            self.engine.read_manifest("../../private", str(self.destination))
        self.assertEqual(error.exception.code, "invalid_snapshot")

    def test_invalid_optional_manifest_metadata_is_listed_as_corrupt(self):
        snapshot = self.create()
        manifest = self.engine.read_manifest(snapshot["id"], str(self.destination))
        manifest["skipped_symlinks"] = None
        with zipfile.ZipFile(snapshot["path"], "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
        self.assertEqual(self.engine.list_snapshots(str(self.destination))[0]["status"], "corrupt")

    def test_callback_failure_cleans_up_and_releases_lock(self):
        (self.source / "chat.txt").write_text("Synthetic chat")

        def abort(progress):
            raise RuntimeError("Synthetic cancellation")

        with self.assertRaisesRegex(RuntimeError, "Synthetic cancellation"):
            self.create(progress=abort)
        self.assert_no_partial_or_archive()
        self.assertEqual(self.create()["file_count"], 1)

    def test_discovery_returns_known_paths_without_reading_chat_content(self):
        known = self.root / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
        known.mkdir(parents=True)
        with patch("vault.backup.Path.home", return_value=self.root):
            sources = discover_sources()
        self.assertEqual(sources[0]["path"], str(known))
        self.assertTrue(any(item["path"] == str(known) and item["exists"] for item in sources))
        self.assertTrue(all(item["kind"] == "wechat" for item in sources))


if __name__ == "__main__":
    unittest.main()
