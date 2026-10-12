"""Verify source-only release boundaries with disposable, synthetic projects."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SPEC = importlib.util.spec_from_file_location("package_release", Path(__file__).resolve().parents[1] / "scripts/package_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class PackageReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-release-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "source"
        for name in release.RELEASE_FILES:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Synthetic public source.\n")
        (self.project / "vault/__init__.py").write_text('__version__ = "0.2.0"\n')
        (self.project / "assets/AppIcon.icns").write_bytes(b"icns" + (8).to_bytes(4, "big"))

    def build(self, suffix="one", **kwargs):
        return release.build_release(self.project, self.root / suffix, **kwargs)

    def test_only_explicit_sources_are_included_with_content_checksums(self):
        for name in ("wechat-keys.json", "library.sqlite3", ".test-artifacts/private.txt", "dist/Private.app/secret",
                     "vault/forgotten_private.py", "tests/private_fixture.json", "assets/chat-photo.jpg"):
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"Synthetic excluded private payload")
        result = self.build()
        archive = Path(result["archive"])
        self.assertEqual(result["version"], "0.2.0")
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), result["sha256"])
        self.assertEqual(Path(result["checksum"]).read_text(), f"{result['sha256']}  {archive.name}\n")
        prefix = "shiliao-0.2.0/"
        with zipfile.ZipFile(archive) as package:
            names = set(package.namelist())
            self.assertEqual(names, {prefix + name for name in release.RELEASE_FILES} | {prefix + "RELEASE-MANIFEST.json"})
            manifest = json.loads(package.read(prefix + "RELEASE-MANIFEST.json"))
            self.assertEqual(manifest["version"], "0.2.0")
            for entry in manifest["files"]:
                data = package.read(prefix + entry["path"])
                self.assertEqual(len(data), entry["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
            self.assertEqual((package.getinfo(prefix + "Start.command").external_attr >> 16) & 0o777, 0o755)
            self.assertEqual((package.getinfo(prefix + "app.py").external_attr >> 16) & 0o777, 0o644)

    def test_output_is_deterministic_across_mtimes_and_source_permissions(self):
        first = self.build("first")
        for name in release.RELEASE_FILES:
            path = self.project / name
            os.utime(path, (1700000000, 1700000000))
            path.chmod(0o600)
        second = self.build("second", version="v0.2.0")
        self.assertEqual(Path(first["archive"]).read_bytes(), Path(second["archive"]).read_bytes())
        self.assertEqual(Path(first["checksum"]).read_bytes(), Path(second["checksum"]).read_bytes())

    def test_secret_literals_and_personal_paths_abort_without_echoing_values(self):
        payloads = (
            "token = '" + "ghp_" + "A" * 36 + "'\n",
            'data = {"enc_key": "' + "a1" * 32 + '"}\n',
            'folder = "' + "/" + "Users" + "/PrivateOwner/Documents" + '"\n',
            "pem = '" + "-----BEGIN " + "PRIVATE KEY-----" + "'\n",
            'authorization = "' + "Bearer " + "A" * 43 + '"\n',
            'account = "' + "wxid_" + "privateunreviewedaccount" + '"\n',
        )
        for index, payload in enumerate(payloads):
            with self.subTest(rule=index):
                (self.project / "app.py").write_text(payload)
                with self.assertRaises(release.ReleaseError) as error:
                    self.build()
                self.assertIn("app.py", str(error.exception))
                self.assertNotIn(payload.strip(), str(error.exception))
                self.assertFalse((self.root / "one").exists())

    def test_symlinked_source_and_parent_directories_are_rejected(self):
        path = self.project / "app.py"
        outside = self.root / "outside"
        outside.write_text("Synthetic unrelated file")
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(release.ReleaseError, "Symbolic links"):
            self.build()
        path.unlink()
        path.write_text("Synthetic public source")
        original = self.project / "static"
        moved = self.root / "other-static"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(release.ReleaseError, "Symbolic links"):
            self.build()

    def test_required_sources_cannot_silently_disappear(self):
        (self.project / "vendor/wechat-access/LICENSE").unlink()
        with self.assertRaisesRegex(release.ReleaseError, "Missing release source"):
            self.build()

    def test_source_version_must_match_requested_release(self):
        with self.assertRaisesRegex(release.ReleaseError, "differs"):
            self.build(version="0.3.0")

    def test_binary_payload_cannot_be_hidden_in_a_source_file(self):
        (self.project / "app.py").write_bytes(b"SQLite format 3\x00synthetic data")
        with self.assertRaisesRegex(release.ReleaseError, "Binary content"):
            self.build()

    def test_existing_outputs_are_never_overwritten(self):
        result = self.build()
        before = Path(result["archive"]).read_bytes()
        with self.assertRaisesRegex(release.ReleaseError, "already exist"):
            self.build()
        self.assertEqual(Path(result["archive"]).read_bytes(), before)

    def test_source_changed_during_build_is_not_published(self):
        original = release._entry
        changed = False

        def mutate(name, executable=False):
            nonlocal changed
            if not changed:
                changed = True
                (self.project / "README.md").write_text("Changed public content")
            return original(name, executable)

        with patch.object(release, "_entry", side_effect=mutate), self.assertRaisesRegex(release.ReleaseError, "changed before publication"):
            self.build()
        self.assertEqual(list((self.root / "one").iterdir()), [])

    def test_second_output_collision_does_not_leave_half_a_release(self):
        link = release.os.link

        def collision(source, target):
            if str(target).endswith(".sha256"):
                Path(target).write_text("Existing unrelated checksum")
            return link(source, target)

        with patch.object(release.os, "link", side_effect=collision), self.assertRaises(release.ReleaseError):
            self.build()
        self.assertFalse((self.root / "one/shiliao-0.2.0-source.zip").exists())
        self.assertEqual((self.root / "one/shiliao-0.2.0-source.zip.sha256").read_text(), "Existing unrelated checksum")


if __name__ == "__main__":
    unittest.main()
