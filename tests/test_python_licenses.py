"""Verify PBS license collection using synthetic archives and local files only."""

import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import python_licenses


class PythonLicenseArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-python-license-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.destination = self.root / "collected"
        self.licenses = {
            "licenses/LICENSE.cpython.txt": b"Synthetic CPython license.\n",
            "licenses/LICENSE.openssl.txt": b"Synthetic OpenSSL license.\n",
            "licenses/LICENSE.libffi.txt": b"Synthetic libffi license.\n",
            "licenses/LICENSE.tcl.txt": b"Synthetic Tcl license.\n",
            "licenses/LICENSE.tk.txt": b"Synthetic Tk license.\n",
        }
        self.metadata = {
            "version": "8", "python_version": "3.11.17", "target_triple": "aarch64-apple-darwin",
            "build_options": "pgo+lto", "license_path": "licenses/LICENSE.cpython.txt",
            "licenses": ["Python-2.0"],
            "build_info": {"core": {"objs": [], "links": []}, "extensions": {
                "_ssl": [{"licenses": ["Apache-2.0"], "license_paths": ["licenses/LICENSE.openssl.txt"]}],
                "_hashlib": [{"licenses": ["Apache-2.0"], "license_paths": ["licenses/LICENSE.openssl.txt"]}],
                "_ctypes": [{"licenses": ["MIT"], "license_paths": ["licenses/LICENSE.libffi.txt"]}],
                "_tkinter": [{"licenses": ["TCL"], "license_paths": ["licenses/LICENSE.tcl.txt", "licenses/LICENSE.tk.txt"]}],
            }},
        }

    def tar_stream(self, *, metadata=None, licenses=None, extra=(), metadata_bytes=None):
        metadata = self.metadata if metadata is None else metadata
        licenses = self.licenses if licenses is None else licenses
        raw = metadata_bytes if metadata_bytes is not None else json.dumps(metadata).encode()
        entries = [("python/PYTHON.json", "file", raw)]
        entries.extend(("python/" + name, "file", data) for name, data in licenses.items())
        entries.extend(extra)
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode="w") as archive:
            for name, kind, value in entries:
                item = tarfile.TarInfo(name)
                item.mode = 0o644
                if kind == "file":
                    item.size = len(value)
                    archive.addfile(item, io.BytesIO(value))
                else:
                    item.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                                 "directory": tarfile.DIRTYPE, "fifo": tarfile.FIFOTYPE}[kind]
                    item.linkname = value
                    archive.addfile(item)
        result.seek(0)
        return result

    def collect(self, stream=None, architecture="arm64", destination=None):
        return python_licenses.extract_license_tar(stream or self.tar_stream(),
                                                   destination or self.destination, architecture)

    def test_retains_metadata_and_all_declared_native_dependency_licenses(self):
        raw = json.dumps(self.metadata, indent=2).encode()
        result = self.collect(self.tar_stream(metadata_bytes=raw))
        self.assertEqual((self.destination / "PYTHON.json").read_bytes(), raw)
        for name, data in self.licenses.items():
            self.assertEqual((self.destination / name).read_bytes(), data)
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["python_version"], "3.11.17")
        self.assertEqual(result["architecture"], "arm64")
        self.assertEqual(result["release"], "20261009")
        self.assertEqual(result["omitted_system_references"], [])
        stored = json.loads((self.destination / "LICENSES.json").read_text())
        self.assertEqual(stored, result)
        records = {item["path"]: item for item in result["files"]}
        self.assertEqual(set(records), {"PYTHON.json", *self.licenses})
        self.assertEqual(len(records), len(result["files"]))
        for name, record in records.items():
            data = (self.destination / name).read_bytes()
            self.assertEqual(record["bytes"], len(data))
            self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())

    def test_does_not_copy_undeclared_sources_or_runtime_files(self):
        self.collect(self.tar_stream(extra=[
            ("python/install/bin/python3", "symlink", "python3.11"),
            ("python/install/bin/python3.11", "file", b"synthetic-runtime"),
            ("python/private-synthetic-sentinel.db", "file", b"not-for-release"),
            ("python/licenses/LICENSE.unreferenced.txt", "file", b"not-declared"),
        ]))
        output = {path.relative_to(self.destination).as_posix()
                  for path in self.destination.rglob("*") if path.is_file()}
        self.assertEqual(output, {"PYTHON.json", "LICENSES.json", *self.licenses})

    def test_supports_intel_with_matching_metadata(self):
        metadata = {**self.metadata, "target_triple": "x86_64-apple-darwin"}
        result = self.collect(self.tar_stream(metadata=metadata), architecture="x86_64")
        self.assertEqual(result["architecture"], "x86_64")

    def test_accepts_numeric_schema_version_eight(self):
        self.collect(self.tar_stream(metadata={**self.metadata, "version": 8}))
        self.assertTrue((self.destination / "LICENSES.json").is_file())

    def test_rejects_wrong_python_version_target_build_or_schema(self):
        for index, (key, value) in enumerate([
            ("python_version", "3.11.16"), ("python_version", "3.12.0"),
            ("target_triple", "x86_64-apple-darwin"), ("target_triple", "aarch64-unknown-linux-gnu"),
            ("build_options", "debug"), ("build_options", "pgo"),
            ("version", "7"), ("version", 9), ("version", None),
        ]):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    self.collect(self.tar_stream(metadata={**self.metadata, key: value}),
                                 destination=self.root / ("mismatch-" + str(index)))

    def test_rejects_unknown_architecture(self):
        with self.assertRaises(ValueError):
            self.collect(architecture="synthetic-unsupported")

    def test_requires_python_metadata(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w"):
            pass
        data.seek(0)
        with self.assertRaises(ValueError):
            self.collect(data)

    def test_rejects_invalid_json_and_nonobject_metadata(self):
        for index, data in enumerate((b"{not-json}", b"null", b"[]", b'"text"')):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    self.collect(self.tar_stream(metadata_bytes=data), destination=self.root / ("invalid-" + str(index)))

    def test_rejects_duplicate_metadata_keys(self):
        raw = json.dumps(self.metadata).replace('"version": "8"', '"version": "8", "version": "8"').encode()
        with self.assertRaisesRegex(ValueError, "duplicate keys"):
            self.collect(self.tar_stream(metadata_bytes=raw))

    def test_requires_root_and_native_extension_license_declarations(self):
        variants = []
        metadata = copy.deepcopy(self.metadata)
        del metadata["license_path"]
        variants.append(metadata)
        for name in ("_ssl", "_ctypes", "_tkinter"):
            for value in (None, [], [{}], [{"license_paths": []}]):
                metadata = copy.deepcopy(self.metadata)
                metadata["build_info"]["extensions"][name] = value
                variants.append(metadata)
        for index, metadata in enumerate(variants):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    self.collect(self.tar_stream(metadata=metadata), destination=self.root / ("declaration-" + str(index)))

    def test_rejects_missing_declared_license(self):
        for index, missing in enumerate(self.licenses):
            with self.subTest(missing=missing):
                licenses = {name: data for name, data in self.licenses.items() if name != missing}
                with self.assertRaises(ValueError):
                    self.collect(self.tar_stream(licenses=licenses), destination=self.root / ("missing-" + str(index)))

    def test_rejects_parent_and_absolute_license_references(self):
        outside = self.root / "outside-license"
        outside.write_bytes(b"private-synthetic-sentinel")
        for index, name in enumerate(("../outside-license", str(outside), "licenses/../../outside-license",
                                      "install/private.db", "licenses/../LICENSE.txt", "")):
            with self.subTest(name=name):
                metadata = {**self.metadata, "license_path": name}
                with self.assertRaises(ValueError):
                    self.collect(self.tar_stream(metadata=metadata), destination=self.root / ("unsafe-" + str(index)))
                self.assertEqual(outside.read_bytes(), b"private-synthetic-sentinel")

    def test_rejects_unsafe_extension_license_references(self):
        metadata = copy.deepcopy(self.metadata)
        metadata["build_info"]["extensions"]["_ssl"][0]["license_paths"] = ["../outside-license"]
        with self.assertRaises(ValueError):
            self.collect(self.tar_stream(metadata=metadata))

    def test_rejects_unsafe_tar_members_even_when_not_selected(self):
        outside = self.root / "outside"
        for index, name in enumerate(("../outside", str(outside), "python/../../outside", "python/licenses/../outside")):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.collect(self.tar_stream(extra=[(name, "file", b"unsafe")]),
                                 destination=self.root / ("unsafe-member-" + str(index)))
                self.assertFalse(outside.exists())

    def test_rejects_duplicate_tar_metadata_and_license_members(self):
        for index, name in enumerate(("python/PYTHON.json", "python/licenses/LICENSE.openssl.txt")):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.collect(self.tar_stream(extra=[(name, "file", b"duplicate")]),
                                 destination=self.root / ("duplicate-" + str(index)))

    def test_rejects_declared_license_symlink_hardlink_and_directory(self):
        name = "licenses/LICENSE.openssl.txt"
        licenses = {key: value for key, value in self.licenses.items() if key != name}
        for index, kind in enumerate(("symlink", "hardlink", "directory", "fifo")):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    self.collect(self.tar_stream(licenses=licenses, extra=[("python/" + name, kind, "LICENSE.cpython.txt")]),
                                 destination=self.root / ("special-" + str(index)))

    def test_refuses_existing_destination_without_changing_it(self):
        self.destination.mkdir()
        sentinel = self.destination / "keep.txt"
        sentinel.write_bytes(b"existing-synthetic-material")
        with self.assertRaises((ValueError, FileExistsError)):
            self.collect()
        self.assertEqual(sentinel.read_bytes(), b"existing-synthetic-material")
        self.assertEqual(list(self.destination.iterdir()), [sentinel])

    def test_refuses_destination_symlink(self):
        outside = self.root / "private-synthetic-directory"
        outside.mkdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((ValueError, FileExistsError)):
            self.collect()
        self.assertEqual(list(outside.iterdir()), [])

    def test_enforces_archive_and_license_size_limits_before_publication(self):
        for name in ("MAX_MEMBERS", "MAX_TAR_BYTES", "MAX_METADATA_BYTES", "MAX_LICENSE_BYTES", "MAX_LICENSE_TOTAL"):
            with self.subTest(limit=name), patch.object(python_licenses, name, 1):
                with self.assertRaises(ValueError):
                    self.collect()
                self.assertFalse(self.destination.exists())

    def system_zlib_metadata(self):
        metadata = copy.deepcopy(self.metadata)
        metadata["build_info"]["extensions"]["zlib"] = [{
            "init_fn": "PyInit_zlib", "links": [{"name": "z", "system": True}],
            "license_paths": ["licenses/LICENSE.zlib.txt", "licenses/LICENSE.zlib-ng.txt"],
        }]
        return metadata

    def test_records_exact_system_zlib_missing_notice_exception(self):
        licenses = {**self.licenses, "licenses/LICENSE.zlib.txt": b"Synthetic system zlib notice.\n"}
        result = self.collect(self.tar_stream(metadata=self.system_zlib_metadata(), licenses=licenses))
        self.assertEqual(len(result["omitted_system_references"]), 1)
        self.assertEqual(result["omitted_system_references"][0]["path"], "licenses/LICENSE.zlib-ng.txt")
        self.assertTrue(result["omitted_system_references"][0]["reason"])
        self.assertTrue((self.destination / "licenses/LICENSE.zlib.txt").is_file())
        self.assertFalse((self.destination / "licenses/LICENSE.zlib-ng.txt").exists())

    def test_does_not_relax_missing_notice_rule_for_nonsystem_zlib(self):
        licenses = {**self.licenses, "licenses/LICENSE.zlib.txt": b"Synthetic zlib notice.\n"}
        for index, mutation in enumerate((
            {"links": [{"name": "z", "system": False}]},
            {"links": [{"name": "z", "system": True}, {"name": "z-ng", "system": False}]},
            {"init_fn": "PyInit_other"},
            {"license_paths": ["licenses/LICENSE.zlib-ng.txt"]},
        )):
            with self.subTest(mutation=mutation):
                metadata = self.system_zlib_metadata()
                metadata["build_info"]["extensions"]["zlib"][0].update(mutation)
                with self.assertRaisesRegex(ValueError, "missing a declared license"):
                    self.collect(self.tar_stream(metadata=metadata, licenses=licenses),
                                 destination=self.root / ("not-system-" + str(index)))

    def test_requires_missing_notice_when_another_component_references_it(self):
        metadata = self.system_zlib_metadata()
        metadata["build_info"]["extensions"]["_ssl"][0]["license_paths"].append("licenses/LICENSE.zlib-ng.txt")
        licenses = {**self.licenses, "licenses/LICENSE.zlib.txt": b"Synthetic zlib notice.\n"}
        with self.assertRaisesRegex(ValueError, "missing a declared license"):
            self.collect(self.tar_stream(metadata=metadata, licenses=licenses))

    def test_retains_present_zlib_ng_notice_without_using_exception(self):
        licenses = {**self.licenses, "licenses/LICENSE.zlib.txt": b"Synthetic zlib notice.\n",
                    "licenses/LICENSE.zlib-ng.txt": b"Synthetic zlib-ng notice.\n"}
        result = self.collect(self.tar_stream(metadata=self.system_zlib_metadata(), licenses=licenses))
        self.assertEqual(result["omitted_system_references"], [])
        self.assertEqual((self.destination / "licenses/LICENSE.zlib-ng.txt").read_bytes(),
                         licenses["licenses/LICENSE.zlib-ng.txt"])

    @unittest.skipUnless(importlib.util.find_spec("zstandard"), "Zstandard is required to exercise a compressed full archive")
    def test_gather_reads_only_pinned_cached_archive_and_generates_source_record(self):
        import zstandard
        compressed = zstandard.ZstdCompressor().compress(self.tar_stream().getvalue())
        expected = hashlib.sha256(compressed).hexdigest()
        cache = self.root / "cache"
        cache.mkdir()
        with patch.dict(python_licenses.FULL_ARCHIVES, {"arm64": ("aarch64", expected)}):
            info = python_licenses.source_info("arm64")
            (cache / info["filename"]).write_bytes(compressed)
            with patch.object(python_licenses.subprocess, "run", side_effect=AssertionError("Network access is forbidden")) as network:
                result = python_licenses.gather_python_licenses("arm64", self.destination, cache)
            network.assert_not_called()
        self.assertEqual(result["source"], {"url": info["url"], "sha256": expected})
        self.assertEqual(result["files"], json.loads((self.destination / "LICENSES.json").read_text())["files"])


class PythonLicenseDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-python-license-cache-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def test_cached_digest_mismatch_is_rejected_without_network(self):
        archive = self.cache / python_licenses.source_info("arm64")["filename"]
        archive.write_bytes(b"synthetic-wrong-archive")
        with patch.object(python_licenses.subprocess, "run", side_effect=AssertionError("Network access is forbidden")) as network:
            with self.assertRaisesRegex(ValueError, "checksum"):
                python_licenses.download_full_archive("arm64", self.cache)
        network.assert_not_called()
        self.assertEqual(archive.read_bytes(), b"synthetic-wrong-archive")

    def test_rejects_symbolic_link_cache_and_cached_archive(self):
        info = python_licenses.source_info("arm64")
        linked_cache = self.root / "linked-cache"
        linked_cache.symlink_to(self.cache, target_is_directory=True)
        outside = self.root / "private-synthetic-sentinel"
        outside.write_bytes(b"not-an-archive")
        (self.cache / info["filename"]).symlink_to(outside)
        with patch.object(python_licenses.subprocess, "run", side_effect=AssertionError("Network access is forbidden")) as network:
            for cache in (linked_cache, self.cache):
                with self.subTest(cache=cache.name), self.assertRaisesRegex(ValueError, "symbolic link"):
                    python_licenses.download_full_archive("arm64", cache)
        network.assert_not_called()
        self.assertEqual(outside.read_bytes(), b"not-an-archive")

    def test_failed_download_checksum_never_publishes_a_cached_archive(self):
        def fake_download(arguments, **_kwargs):
            Path(arguments[arguments.index("--output") + 1]).write_bytes(b"synthetic-tampered-download")
            class Result:
                returncode = 0
            return Result()

        with patch.object(python_licenses.subprocess, "run", side_effect=fake_download) as download:
            with self.assertRaisesRegex(ValueError, "checksum"):
                python_licenses.download_full_archive("arm64", self.cache)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_sources_pin_both_architectures_to_same_runtime_release_and_profile(self):
        for architecture, target in (("arm64", "aarch64"), ("x86_64", "x86_64")):
            with self.subTest(architecture=architecture):
                info = python_licenses.source_info(architecture)
                self.assertEqual(info["filename"], f"cpython-3.11.17+20261009-{target}-apple-darwin-pgo+lto-full.tar.zst")
                self.assertTrue(info["url"].startswith("https://github.com/astral-sh/python-build-standalone/releases/download/20261009/"))
                self.assertRegex(info["sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
