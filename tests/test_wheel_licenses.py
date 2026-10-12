import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import wheel_licenses as licenses


class WheelLicensesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.python = self.root / "Python runtime"
        self.cache = self.root / "cache"
        self.cache.mkdir()
        for name, version in licenses.EXPECTED.items():
            metadata = self.python / "lib/python3.11/site-packages" / f"{name}-{version}.dist-info"
            metadata.mkdir(parents=True)
            (metadata / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
            (metadata / "LICENSE").write_text("Existing upstream license\n")

    def materials(self):
        text = b"Synthetic native component copyright and permission.\n"
        return [(text, {"path": "component-1.0/LICENSE", "component": "synthetic",
                        "version": "1.0", "source_url": "https://example.com/source.tar.gz",
                        "source_sha256": "a" * 64, "source_member": "component/LICENSE",
                        "sha256": hashlib.sha256(text).hexdigest()})]

    def tar(self, members):
        path = self.root / "source.tar.gz"
        with tarfile.open(path, "w:gz") as output:
            for name, content, kind in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.REGTYPE:
                    info.size = len(content)
                    output.addfile(info, io.BytesIO(content))
                else:
                    info.linkname = "../external"
                    output.addfile(info)
        return path

    def test_collection_has_provenance_is_idempotent_and_preserves_upstream(self):
        with patch.object(licenses, "_collect_materials", return_value=self.materials()):
            first = licenses.collect_wheel_licenses(self.python, self.cache)
            second = licenses.collect_wheel_licenses(self.python, self.cache)
        self.assertEqual(first, second)
        destination = self.python / "licenses/wheels"
        self.assertEqual(json.loads((destination / "LICENSES.json").read_text()), first)
        self.assertEqual((destination / first["files"][0]["path"]).read_bytes(), self.materials()[0][0])
        self.assertEqual(len(list(self.python.glob("lib/python*/site-packages/*/LICENSE"))), 3)
        self.assertFalse(list((self.python / "licenses").glob(".wheel-licenses-*")))

    def test_unknown_or_missing_installed_version_fails_before_download(self):
        metadata = next(self.python.glob("lib/python*/site-packages/pilk*/METADATA"))
        metadata.write_text("Name: pilk\nVersion: 99.0\n")
        with patch.object(licenses, "_collect_materials") as collect:
            with self.assertRaisesRegex(ValueError, "versions differ"):
                licenses.collect_wheel_licenses(self.python, self.cache)
            collect.assert_not_called()

    def test_duplicate_metadata_is_rejected(self):
        extra = self.python / "lib/python3.11/site-packages/duplicate.dist-info"
        extra.mkdir()
        (extra / "METADATA").write_text("Name: pilk\nVersion: 0.2.4\n")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            licenses.collect_wheel_licenses(self.python, self.cache)

    def test_existing_differing_notices_are_not_overwritten(self):
        with patch.object(licenses, "_collect_materials", return_value=self.materials()):
            licenses.collect_wheel_licenses(self.python, self.cache)
            target = self.python / "licenses/wheels/component-1.0/LICENSE"
            target.write_text("Preserve this existing material")
            with self.assertRaisesRegex(ValueError, "differ"):
                licenses.collect_wheel_licenses(self.python, self.cache)
        self.assertEqual(target.read_text(), "Preserve this existing material")

    def test_output_links_are_rejected(self):
        (self.python / "licenses").symlink_to(self.cache, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            licenses.collect_wheel_licenses(self.python, self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_failure_does_not_publish_partial_directory(self):
        materials = self.materials()
        materials.append((b"Untrusted", {"path": "../outside"}))
        with patch.object(licenses, "_collect_materials", return_value=materials):
            with self.assertRaisesRegex(ValueError, "output path"):
                licenses.collect_wheel_licenses(self.python, self.cache)
        self.assertFalse((self.python / "licenses/wheels").exists())
        self.assertFalse(list((self.python / "licenses").iterdir()))

    def test_tar_reader_only_reads_the_named_regular_member(self):
        archive = self.tar([("wanted/LICENSE", b"License", tarfile.REGTYPE),
                            ("../../outside", b"Do not extract", tarfile.REGTYPE)])
        self.assertEqual(licenses._member(archive, "wanted/LICENSE"), b"License")
        self.assertFalse((self.root / "outside").exists())

    def test_tar_reader_rejects_duplicate_symlink_and_missing_license(self):
        for members in [
            [("LICENSE", b"a", tarfile.REGTYPE), ("LICENSE", b"b", tarfile.REGTYPE)],
            [("LICENSE", b"", tarfile.SYMTYPE)],
            [("OTHER", b"a", tarfile.REGTYPE)],
        ]:
            with self.subTest(members=members):
                with self.assertRaisesRegex(ValueError, "missing or unsafe"):
                    licenses._member(self.tar(members), "LICENSE")

    def test_tar_reader_enforces_text_size_and_utf8(self):
        archive = self.tar([("LICENSE", b"long license", tarfile.REGTYPE)])
        with patch.object(licenses, "MAX_TEXT_BYTES", 3):
            with self.assertRaisesRegex(ValueError, "size"):
                licenses._member(archive, "LICENSE")
        archive = self.tar([("LICENSE", b"\xff", tarfile.REGTYPE)])
        with self.assertRaises(UnicodeDecodeError):
            licenses._member(archive, "LICENSE")

    def test_cached_source_is_checksum_verified_and_not_redownloaded(self):
        path = self.cache / "source.tar.gz"
        path.write_bytes(b"Synthetic source")
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        with patch.object(licenses.subprocess, "run") as run:
            self.assertEqual(licenses._fetch("https://example.com/source.tar.gz", expected, self.cache), path)
            run.assert_not_called()
            with self.assertRaisesRegex(ValueError, "checksum"):
                licenses._fetch("https://example.com/source.tar.gz", "0" * 64, self.cache)

    def test_download_hash_failure_cleans_temp_and_does_not_publish(self):
        def download(arguments, **kwargs):
            Path(arguments[-1]).write_bytes(b"Wrong source")
            return subprocess.CompletedProcess(arguments, 0)
        with patch.object(licenses.subprocess, "run", side_effect=download):
            with self.assertRaisesRegex(ValueError, "checksum"):
                licenses._fetch("https://example.com/source.tar.gz", "0" * 64, self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_download_success_is_verified_and_atomically_cached(self):
        content = b"Pinned source"
        def download(arguments, **kwargs):
            Path(arguments[-1]).write_bytes(content)
            return subprocess.CompletedProcess(arguments, 0)
        with patch.object(licenses.subprocess, "run", side_effect=download):
            path = licenses._fetch("https://example.com/source.tar.gz",
                                   hashlib.sha256(content).hexdigest(), self.cache)
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(list(self.cache.iterdir()), [path])

    def test_runtime_crate_scope_excludes_build_tools_and_wrong_target(self):
        self.assertIn("asn1", licenses.RUST_LICENSES)
        self.assertIn("pyo3", licenses.RUST_LICENSES)
        self.assertIn("itoa", licenses.RUST_LICENSES)
        for excluded in ("cc", "pkg-config", "vcpkg", "syn", "portable-atomic", "pyo3-build-config"):
            self.assertNotIn(excluded, licenses.RUST_LICENSES)


if __name__ == "__main__":
    unittest.main()
