"""Test portable tool source integrity and Mach-O relocation boundaries."""

from io import BytesIO
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_tools, bundle_tools


def commands(*items):
    return "synthetic:\n" + "".join(f"Load command {index}\n{value}\n" for index, value in enumerate(items))


BUILD = "      cmd LC_BUILD_VERSION\n platform 1\n minos 15.0\n sdk 26.0\n version 9999.1"


class NativeMetadataTests(unittest.TestCase):
    def test_build_version_uses_minos_instead_of_sdk_or_linker_version(self):
        info = bundle_tools.parse_load_commands(commands(BUILD))
        self.assertEqual(info.minimum_macos, "15.0")

    def test_legacy_minimum_macos_and_dependency_command_kinds(self):
        data = commands("cmd LC_VERSION_MIN_MACOSX\nversion 10.15\nsdk 15.0",
                        "cmd LC_ID_DYLIB\nname @rpath/libsynthetic.dylib (offset 24)",
                        "cmd LC_LOAD_DYLIB\nname /usr/lib/libSystem.B.dylib (offset 24)",
                        "cmd LC_LOAD_WEAK_DYLIB\nname @rpath/liboptional.dylib (offset 24)",
                        "cmd LC_RPATH\npath @loader_path/../lib (offset 12)")
        info = bundle_tools.parse_load_commands(data)
        self.assertEqual(info.minimum_macos, "10.15")
        self.assertEqual(info.install_id, "@rpath/libsynthetic.dylib")
        self.assertEqual(info.rpaths, ("@loader_path/../lib",))
        self.assertEqual(info.dependencies, ("/usr/lib/libSystem.B.dylib", "@rpath/liboptional.dylib"))

    def test_wrong_platform_and_absent_version_are_rejected(self):
        for text in (commands(BUILD.replace("platform 1", "platform 2")), commands("cmd LC_UUID")):
            with self.subTest(text=text), self.assertRaises(bundle_tools.BundleToolsError):
                bundle_tools.parse_load_commands(text)

    def test_newer_os_or_missing_architecture_is_rejected(self):
        with patch.object(bundle_tools, "run", side_effect=["arm64", commands(BUILD.replace("15.0", "26.0"))]):
            with self.assertRaisesRegex(bundle_tools.BundleToolsError, "newer macOS"):
                bundle_tools.inspect_macho(Path("synthetic"), "arm64", "15.0")
        with patch.object(bundle_tools, "run", return_value="x86_64"):
            with self.assertRaisesRegex(bundle_tools.BundleToolsError, "architecture"):
                bundle_tools.inspect_macho(Path("synthetic"), "arm64", "15.0")

    def test_mac_version_is_validated_before_becoming_compiler_flags(self):
        for value in ("15", "15.0; command", "15.0\n", "x", None):
            with self.subTest(value=value), self.assertRaises(bundle_tools.BundleToolsError):
                bundle_tools.version_tuple(value)
        self.assertEqual(bundle_tools.version_tuple("15.2"), (15, 2, 0))


class NativeDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="shiliao-native-dependency-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "bin").mkdir()
        (self.root / "lib").mkdir()
        self.executable = self.root / "bin" / "ffmpeg"
        self.executable.write_bytes(b"synthetic executable")
        self.library = self.root / "lib" / "libsynthetic.dylib"
        self.library.write_bytes(b"synthetic library")

    def test_loader_executable_and_rpath_dependencies_resolve(self):
        for value, paths in (("@loader_path/../lib/libsynthetic.dylib", ()),
                             ("@executable_path/../lib/libsynthetic.dylib", ()),
                             ("@rpath/libsynthetic.dylib", ("@loader_path/../lib",))):
            with self.subTest(value=value):
                self.assertEqual(bundle_tools.dependency_path(value, self.executable, self.executable, paths), self.library.resolve())
        self.assertIsNone(bundle_tools.dependency_path("/usr/lib/libSystem.B.dylib", self.executable, self.executable, ()))

    def test_unresolved_dependencies_fail_instead_of_searching_the_environment(self):
        with self.assertRaises(bundle_tools.BundleToolsError):
            bundle_tools.dependency_path("@rpath/libmissing.dylib", self.executable, self.executable, ())

    def test_audit_rejects_external_dependencies_and_private_build_paths(self):
        info = bundle_tools.MachO(("/external/libsynthetic.dylib",), (), None, "15.0")
        with patch.object(bundle_tools, "inspect_macho", return_value=info):
            with self.assertRaisesRegex(bundle_tools.BundleToolsError, "external dependency"):
                bundle_tools.audit_tree(self.root, "arm64", "15.0")
        self.executable.write_bytes(b"synthetic " + b"/" + b"Users" + b"/synthetic-build/account.c")
        info = bundle_tools.MachO((), (), None, "15.0")
        with patch.object(bundle_tools, "inspect_macho", return_value=info):
            with self.assertRaisesRegex(bundle_tools.BundleToolsError, "private build path"):
                bundle_tools.audit_tree(self.root, "arm64", "15.0")

    def test_audit_rejects_dependency_escape_even_when_file_exists(self):
        outside = self.root / "outside.dylib"
        outside.write_bytes(b"synthetic outside")
        with tempfile.TemporaryDirectory(prefix="shiliao-tools-escape-test-") as separate:
            outer = Path(separate) / "outside.dylib"
            outer.write_bytes(b"synthetic external")
            (self.root / "lib" / "alias.dylib").symlink_to(outer)
            info = bundle_tools.MachO(("@loader_path/../lib/alias.dylib",), (), None, "15.0")
            with patch.object(bundle_tools, "inspect_macho", return_value=info):
                with self.assertRaises(bundle_tools.BundleToolsError):
                    bundle_tools.audit_tree(self.root, "arm64", "15.0")

    def test_metadata_copy_refuses_symlinks(self):
        (self.root / "lib" / "alias").symlink_to(self.executable)
        with self.assertRaises(bundle_tools.BundleToolsError):
            bundle_tools._copy_tree(self.root / "lib", self.root / "copied")

    def test_existing_bundle_output_is_not_overwritten(self):
        output = self.root / "existing"
        output.mkdir()
        marker = output / "marker"
        marker.write_bytes(b"keep")
        with patch.object(bundle_tools.platform, "system", return_value="Darwin"):
            with self.assertRaisesRegex(bundle_tools.BundleToolsError, "already exists"):
                bundle_tools.bundle_tools(self.root, output, "arm64", "15.0")
        self.assertEqual(marker.read_bytes(), b"keep")


class PinnedSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="shiliao-source-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def specification(self, payload=b"synthetic upstream source"):
        return {"filename": "synthetic.tar.gz", "url": "https://example.invalid/synthetic.tar.gz",
                "sha256": hashlib.sha256(payload).hexdigest()}

    def test_valid_cached_source_avoids_network_and_changed_cache_is_rejected(self):
        specification = self.specification()
        target = self.root / specification["filename"]
        target.write_bytes(b"synthetic upstream source")
        with patch.object(build_tools.urllib.request, "urlopen") as download:
            self.assertEqual(build_tools.download_source(specification, self.root), target)
            download.assert_not_called()
            target.write_bytes(b"modified synthetic source")
            with self.assertRaisesRegex(build_tools.BuildToolsError, "SHA-256"):
                build_tools.download_source(specification, self.root)

    def test_cached_source_symlink_is_rejected(self):
        outside = self.root / "outside"
        outside.write_bytes(b"synthetic upstream source")
        (self.root / "synthetic.tar.gz").symlink_to(outside)
        with self.assertRaises(build_tools.BuildToolsError):
            build_tools.download_source(self.specification(), self.root)

    def tar(self, entries):
        archive = self.root / "fixture.tar"
        with tarfile.open(archive, "w") as output:
            for name, kind in entries:
                entry = tarfile.TarInfo(name)
                if kind == "link":
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = "../../outside"
                    output.addfile(entry)
                else:
                    entry.size = len(b"synthetic")
                    output.addfile(entry, BytesIO(b"synthetic"))
        return archive

    def test_tar_extraction_rejects_traversal_links_and_duplicate_names(self):
        cases = (("../outside", "file"), ("/absolute", "file"), ("upstream/alias", "link"))
        for index, entry in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(build_tools.BuildToolsError):
                build_tools.extract_source(self.tar([entry]), self.root / f"unpack-{index}")
        with self.assertRaises(build_tools.BuildToolsError):
            build_tools.extract_source(self.tar([("upstream/file", "file")] * 2), self.root / "duplicates")
        self.assertFalse((self.root / "outside").exists())

    def test_tar_extraction_accepts_regular_source_files(self):
        result = build_tools.extract_source(self.tar([("upstream/configure", "file")]), self.root / "unpacked")
        self.assertEqual((result / "configure").read_bytes(), b"synthetic")

    def test_tool_configuration_does_not_depend_on_brew_nasm_network_or_gpl(self):
        for arch in ("arm64", "x86_64"):
            arguments = build_tools.ffmpeg_arguments(arch, "15.0")
            for option in ("--disable-network", "--disable-autodetect", "--disable-gpl", "--disable-nonfree", "--disable-x86asm"):
                self.assertIn(option, arguments)
        arguments, environment = build_tools.sqlcipher_configuration("15.0")
        self.assertIn("--disable-shared", arguments)
        self.assertIn("-DSQLCIPHER_CRYPTO_CC", environment["CFLAGS"])
        self.assertNotIn("crypto", environment["LDFLAGS"])

    def test_cross_architecture_build_is_rejected_before_downloading(self):
        with patch.object(build_tools.platform, "system", return_value="Darwin"), \
                patch.object(build_tools.platform, "machine", return_value="arm64"), \
                patch.object(build_tools, "download_source") as download:
            with self.assertRaisesRegex(build_tools.BuildToolsError, "natively"):
                build_tools.build_tools(self.root / "output", "x86_64")
        download.assert_not_called()


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("clang"), "Requires Apple native build tools")
class NativeRelocationIntegrationTests(unittest.TestCase):
    def test_real_transitive_dylibs_work_after_relocation_without_modifying_sources(self):
        with tempfile.TemporaryDirectory(prefix="shiliao-dylib-integration-") as temporary:
            root = Path(temporary).resolve()
            source = root / "source"
            for folder in ("bin", "lib", "licenses"):
                (source / folder).mkdir(parents=True)
            (source / "licenses" / "SYNTHETIC.txt").write_text("Synthetic test sources authored for this test.\n")
            base, dependent = source / "lib" / "libbase.dylib", source / "lib" / "libdependent.dylib"
            base_c, dependent_c, main_c = root / "base.c", root / "dependent.c", root / "main.c"
            base_c.write_text("int base(void) { return 7; }\n")
            dependent_c.write_text("int base(void); int dependent(void) { return base(); }\n")
            main_c.write_text("#include <stdio.h>\nint dependent(void); int main(void) { printf(\"%d\\n\", dependent()); return 0; }\n")
            common = ["/usr/bin/clang", "-mmacosx-version-min=15.0", "-Wl,-headerpad_max_install_names"]
            arguments = ([*common, "-dynamiclib", str(base_c), "-Wl,-install_name," + str(base), "-o", str(base)],
                         [*common, "-dynamiclib", str(dependent_c), str(base), "-Wl,-install_name," + str(dependent), "-o", str(dependent)],
                         [*common, str(main_c), str(dependent), "-o", str(source / "bin" / "ffmpeg")])
            for argument in arguments:
                subprocess.run(argument, capture_output=True, check=True)
            for name in ("ffprobe", "sqlcipher"):
                shutil.copyfile(source / "bin" / "ffmpeg", source / "bin" / name)
            original = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                        for folder in ("bin", "lib") for path in (source / folder).iterdir()}
            output = root / "Moved App.app" / "Contents" / "Resources" / "tools"
            report = bundle_tools.bundle_tools(source, output, platform.machine(), "15.0")
            self.assertEqual(len(report["files"]), 5)
            self.assertEqual(original, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in original})
            shutil.rmtree(source)
            for name in bundle_tools.TOOL_NAMES:
                result = subprocess.run([str(output / "bin" / name)], capture_output=True, check=True,
                                        env={"PATH": "/usr/bin:/bin"})
                self.assertEqual(result.stdout, b"7\n")
            self.assertEqual(len(json.loads((output / "tools-manifest.json").read_text())["files"]), 5)


if __name__ == "__main__":
    unittest.main()
