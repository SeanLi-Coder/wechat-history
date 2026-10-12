"""Exercise portable packaging boundaries using synthetic local files only."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest

from scripts.build_portable import copy_declared_sources, extract_python


class PythonArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-python-archive-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.resources = self.root / "Resources"
        self.resources.mkdir()

    def archive(self, entries):
        path = self.root / "runtime.tar"
        with tarfile.open(path, "w") as archive:
            for name, kind, value in entries:
                item = tarfile.TarInfo(name)
                item.mode = 0o755
                if kind == "file":
                    item.size = len(value)
                    archive.addfile(item, io.BytesIO(value))
                else:
                    item.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                                 "directory": tarfile.DIRTYPE, "fifo": tarfile.FIFOTYPE}[kind]
                    item.linkname = value
                    archive.addfile(item)
        return path

    def test_internal_relative_symlink_survives_directory_move(self):
        archive = self.archive([
            ("python/bin/python3", "symlink", "python3.11"),
            ("python/bin/python3.11", "file", b"synthetic-runtime"),
            ("python/lib/pkg/data.txt", "file", b"synthetic-data"),
            ("python/bin/data", "symlink", "../lib/pkg/data.txt"),
        ])
        extract_python(archive, self.resources)
        moved = self.root / "Moved Resources"
        shutil.move(self.resources, moved)
        launcher = moved / "python/bin/python3"
        self.assertTrue(launcher.is_symlink())
        self.assertEqual(os.readlink(launcher), "python3.11")
        self.assertEqual(launcher.read_bytes(), b"synthetic-runtime")
        self.assertEqual((moved / "python/bin/data").read_bytes(), b"synthetic-data")
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertTrue(launcher.resolve().is_relative_to(moved))

    def test_internal_hardlink_preserves_content(self):
        archive = self.archive([
            ("python/bin/python3.11", "file", b"synthetic-runtime"),
            ("python/bin/python3", "hardlink", "python/bin/python3.11"),
        ])
        extract_python(archive, self.resources)
        original = self.resources / "python/bin/python3.11"
        link = self.resources / "python/bin/python3"
        self.assertEqual(link.read_bytes(), original.read_bytes())
        self.assertEqual(link.stat().st_ino, original.stat().st_ino)

    def test_rejects_parent_traversal_before_extracting_any_member(self):
        for name in ("../outside", "python/../../outside", "python/bin/../outside", "other/file"):
            with self.subTest(name=name):
                archive = self.archive([("python/safe", "file", b"safe"), (name, "file", b"unsafe")])
                with self.assertRaisesRegex(ValueError, "unsafe member"):
                    extract_python(archive, self.resources)
                self.assertEqual(list(self.resources.iterdir()), [])
                self.assertFalse((self.root / "outside").exists())

    def test_rejects_absolute_member(self):
        outside = self.root / "outside"
        archive = self.archive([(str(outside), "file", b"unsafe")])
        with self.assertRaisesRegex(ValueError, "unsafe member"):
            extract_python(archive, self.resources)
        self.assertFalse(outside.exists())

    def test_rejects_duplicate_member_before_extracting_any_member(self):
        archive = self.archive([("python/bin/python3", "file", b"first"),
                                ("python/bin/python3", "file", b"second")])
        with self.assertRaisesRegex(ValueError, "duplicate members"):
            extract_python(archive, self.resources)
        self.assertEqual(list(self.resources.iterdir()), [])

    def test_rejects_escaping_symbolic_and_hard_links(self):
        outside = self.root / "outside"
        outside.write_bytes(b"private-synthetic-sentinel")
        for kind, target in (("symlink", "../../../outside"), ("symlink", str(outside)),
                             ("hardlink", "../outside"), ("hardlink", str(outside))):
            with self.subTest(kind=kind, target=target):
                archive = self.archive([("python/bin/python3", kind, target)])
                with self.assertRaisesRegex(ValueError, "link escapes"):
                    extract_python(archive, self.resources)
                self.assertEqual(list(self.resources.iterdir()), [])
                self.assertEqual(outside.read_bytes(), b"private-synthetic-sentinel")

    def test_rejects_link_chain_that_escapes_after_resolution(self):
        archive = self.archive([("python/a", "symlink", "b/.."),
                                ("python/b", "symlink", ".")])
        with self.assertRaisesRegex(ValueError, "escaping link chain"):
            extract_python(archive, self.resources)

    def test_rejects_special_files(self):
        archive = self.archive([("python/pipe", "fifo", "")])
        with self.assertRaisesRegex(ValueError, "special file"):
            extract_python(archive, self.resources)
        self.assertEqual(list(self.resources.iterdir()), [])


class DeclaredSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-declared-source-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "sources"
        self.source.mkdir()
        self.destination = self.root / "published"

    def manifest(self, entries):
        (self.source / "SOURCES.json").write_text(json.dumps({"version": 1, "files": entries}))

    def entry(self, name, content=b"synthetic-upstream-source"):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"path": name, "sha256": hashlib.sha256(content).hexdigest()}

    def test_copies_only_declared_files_and_exact_manifest(self):
        entries = [self.entry("codec.tar.gz"), self.entry("patches/build.patch", b"synthetic-patch")]
        self.manifest(entries)
        (self.source / "private-chat.sqlite3").write_bytes(b"private-synthetic-sentinel")
        (self.source / "undeclared-source.tar.gz").write_bytes(b"not-declared")
        copy_declared_sources(self.source, self.destination)
        names = {path.relative_to(self.destination).as_posix()
                 for path in self.destination.rglob("*") if path.is_file()}
        self.assertEqual(names, {"codec.tar.gz", "patches/build.patch", "SOURCES.json"})
        self.assertEqual((self.destination / "SOURCES.json").read_bytes(),
                         (self.source / "SOURCES.json").read_bytes())
        for item in entries:
            self.assertEqual((self.destination / item["path"]).read_bytes(),
                             (self.source / item["path"]).read_bytes())

    def test_rejects_changed_content(self):
        entry = self.entry("codec.tar.gz")
        self.manifest([entry])
        (self.source / entry["path"]).write_bytes(b"tampered-source")
        with self.assertRaisesRegex(ValueError, "declared checksum"):
            copy_declared_sources(self.source, self.destination)
        self.assertFalse((self.destination / entry["path"]).exists())

    def test_requires_manifest(self):
        self.entry("codec.tar.gz")
        with self.assertRaises(FileNotFoundError):
            copy_declared_sources(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_rejects_missing_declared_file(self):
        self.manifest([{"path": "missing.tar.gz", "sha256": "0" * 64}])
        with self.assertRaisesRegex(ValueError, "declared checksum"):
            copy_declared_sources(self.source, self.destination)

    def test_rejects_unsafe_manifest_paths(self):
        outside = self.root / "outside.tar.gz"
        outside.write_bytes(b"private-synthetic-sentinel")
        for index, name in enumerate(("../outside.tar.gz", "nested/../../outside.tar.gz", str(outside), "", ".")):
            with self.subTest(name=name):
                self.manifest([{"path": name, "sha256": hashlib.sha256(outside.read_bytes()).hexdigest()}])
                destination = self.root / ("unsafe-output-" + str(index))
                with self.assertRaisesRegex(ValueError, "unsafe paths"):
                    copy_declared_sources(self.source, destination)
                self.assertFalse(any(path.is_file() for path in destination.rglob("*")))
                self.assertEqual(outside.read_bytes(), b"private-synthetic-sentinel")

    def test_rejects_duplicate_normalized_manifest_paths(self):
        entry = self.entry("codec.tar.gz")
        self.manifest([entry, {**entry, "path": "./codec.tar.gz"}])
        with self.assertRaisesRegex(ValueError, "unsafe paths"):
            copy_declared_sources(self.source, self.destination)

    def test_rejects_source_directory_symlink(self):
        self.manifest([self.entry("codec.tar.gz")])
        linked = self.root / "linked-sources"
        linked.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            copy_declared_sources(linked, self.destination)
        self.assertFalse(self.destination.exists())

    def test_rejects_manifest_symlink(self):
        entry = self.entry("codec.tar.gz")
        outside = self.root / "outside-manifest.json"
        outside.write_text(json.dumps({"files": [entry]}))
        (self.source / "SOURCES.json").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            copy_declared_sources(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_rejects_declared_file_symlink_even_with_matching_hash(self):
        outside = self.root / "private-synthetic-source"
        outside.write_bytes(b"private-synthetic-sentinel")
        (self.source / "codec.tar.gz").symlink_to(outside)
        self.manifest([{"path": "codec.tar.gz", "sha256": hashlib.sha256(outside.read_bytes()).hexdigest()}])
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            copy_declared_sources(self.source, self.destination)
        self.assertFalse((self.destination / "codec.tar.gz").exists())

    def test_rejects_symlink_in_declared_parent_directory(self):
        outside = self.root / "private-synthetic-folder"
        outside.mkdir()
        (outside / "codec.tar.gz").write_bytes(b"private-synthetic-sentinel")
        (self.source / "nested").symlink_to(outside, target_is_directory=True)
        self.manifest([{"path": "nested/codec.tar.gz", "sha256": hashlib.sha256(b"private-synthetic-sentinel").hexdigest()}])
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            copy_declared_sources(self.source, self.destination)
        self.assertFalse((self.destination / "nested").exists())


if __name__ == "__main__":
    unittest.main()
