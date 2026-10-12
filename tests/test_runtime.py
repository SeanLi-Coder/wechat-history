"""Check relocatable tool lookup and reuse of the running CPython interpreter."""

from collections import OrderedDict
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import vault.runtime as runtime
import vault.media_codecs as codecs
import vault.sqlcipher_export as exporter
from vault.wechat_media import MediaLocator


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bundle = self.root / "Movable app.app"
        self.resources = self.bundle / "Contents/Resources"
        self.module = self.resources / "app/vault/runtime.py"
        self.module.parent.mkdir(parents=True)
        self.module.write_text("# Synthetic app layout.\n")
        self.tools = self.resources / "tools/bin"
        self.tools.mkdir(parents=True)

    def executable(self, folder, name):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def test_bundle_tools_take_priority_over_environment_and_host(self):
        alternate = self.root / "alternate"
        for name in runtime.TOOL_NAMES:
            expected = self.executable(self.tools, name)
            self.executable(alternate, name)
            with patch.object(runtime, "__file__", str(self.module)), \
                    patch.dict(os.environ, {"SHILIAO_TOOLS_DIR": str(alternate)}), \
                    patch.object(runtime.shutil, "which") as host:
                self.assertEqual(runtime.find_tool(name), str(expected))
                host.assert_not_called()

    def test_missing_or_nonexecutable_bundle_tool_never_uses_host_fallback(self):
        self.executable(self.tools, "ffprobe").chmod(0o644)
        with patch.object(runtime, "__file__", str(self.module)), \
                patch.object(runtime.shutil, "which", return_value="/host/ffmpeg") as host:
            self.assertIsNone(runtime.find_tool("ffmpeg"))
            self.assertIsNone(runtime.find_tool("ffprobe"))
            with self.assertRaises(codecs.MediaCodecError) as missing:
                codecs._tool("ffmpeg")
            self.assertEqual(missing.exception.code, "dependency_missing")
            with self.assertRaises(exporter.SQLCipherExportError) as missing:
                exporter._find_sqlcipher()
            self.assertEqual(missing.exception.code, "sqlcipher_unavailable")
            host.assert_not_called()

    def test_symlinks_must_stay_inside_bundled_tools(self):
        external = self.executable(self.root / "external", "ffmpeg")
        (self.tools / "ffmpeg").symlink_to(external)
        internal = self.executable(self.resources / "tools/libexec", "ffprobe")
        (self.tools / "ffprobe").symlink_to(Path("../libexec/ffprobe"))
        with patch.object(runtime, "__file__", str(self.module)):
            self.assertIsNone(runtime.find_tool("ffmpeg"))
            self.assertEqual(runtime.find_tool("ffprobe"), str(self.tools / "ffprobe"))
            self.assertEqual((self.tools / "ffprobe").resolve(), internal)

    def test_moving_bundle_recomputes_paths_and_does_not_use_stale_environment(self):
        self.executable(self.tools, "sqlcipher")
        moved = self.root / "Another folder" / "Renamed application.app"
        moved.parent.mkdir()
        self.bundle.rename(moved)
        module = moved / "Contents/Resources/app/vault/runtime.py"
        expected = moved / "Contents/Resources/tools/bin/sqlcipher"
        with patch.object(runtime, "__file__", str(module)), \
                patch.dict(os.environ, {"SHILIAO_TOOLS_DIR": str(self.tools)}):
            self.assertEqual(runtime.find_tool("sqlcipher"), str(expected))
            self.assertEqual(exporter._find_sqlcipher(), str(expected))

    def test_source_mode_explicit_absolute_directory_precedes_development_paths(self):
        configured = self.executable(self.root / "configured tools", "ffmpeg")
        host = self.executable(self.root / "development", "ffmpeg")
        with patch.object(runtime, "bundled_resources", return_value=None), \
                patch.object(runtime, "DEVELOPMENT_TOOL_DIRS", (host.parent,)), \
                patch.dict(os.environ, {"SHILIAO_TOOLS_DIR": str(configured.parent)}):
            self.assertEqual(runtime.find_tool("ffmpeg"), str(configured))
        with patch.object(runtime, "bundled_resources", return_value=None), \
                patch.object(runtime, "DEVELOPMENT_TOOL_DIRS", (host.parent,)), \
                patch.dict(os.environ, {"SHILIAO_TOOLS_DIR": "relative/tools"}):
            self.assertEqual(runtime.find_tool("ffmpeg"), str(host))

    def test_source_mode_path_fallback_is_absolute_and_rejects_unknown_tool_names(self):
        selected = self.executable(self.root, "ffmpeg")
        with patch.object(runtime, "bundled_resources", return_value=None), \
                patch.object(runtime, "DEVELOPMENT_TOOL_DIRS", ()), \
                patch.dict(os.environ, {"SHILIAO_TOOLS_DIR": ""}), \
                patch.object(runtime.shutil, "which", return_value=str(selected)):
            self.assertEqual(runtime.find_tool("ffmpeg"), str(selected))
        for name in ("../ffmpeg", "/bin/sh", "python3", "ffmpeg; echo unsafe"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                runtime.find_tool(name)

    def test_video_quality_probe_uses_bundled_tool_and_safe_descriptor(self):
        executable = self.executable(self.tools, "ffprobe")
        source = self.root / "synthetic.bin"
        source.write_bytes(b"synthetic video")
        locator = object.__new__(MediaLocator)
        locator._video_probes = OrderedDict()
        candidate = {"path": source, "signature": (1, 2, 3)}
        response = subprocess.CompletedProcess([], 0, json.dumps({"format": {"duration": "1.0"},
                    "streams": [{"codec_type": "video", "width": 32, "height": 24}]}).encode())
        with patch.object(runtime, "__file__", str(self.module)), \
                patch.object(locator, "open_candidate", side_effect=lambda _: source.open("rb")), \
                patch("vault.wechat_media.subprocess.run", return_value=response) as run:
            self.assertEqual(locator._video_properties(candidate), {"duration": 1.0, "width": 32, "height": 24})
            arguments = run.call_args.args[0]
            self.assertEqual(arguments[0], str(executable))
            self.assertTrue(arguments[-1].startswith("/dev/fd/"))
            self.assertNotIn(str(source), arguments)
            self.assertEqual(len(run.call_args.kwargs["pass_fds"]), 1)

    @unittest.skipUnless(importlib.util.find_spec("pilk"), "The synthetic SILK decoder fixture requires pilk")
    def test_silk_subprocess_reuses_current_interpreter_path_with_spaces(self):
        import pilk
        ffmpeg, ffprobe = runtime.find_tool("ffmpeg"), runtime.find_tool("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("Synthetic SILK conversion requires FFmpeg")
        original_python = sys.executable
        interpreter = self.root / "Moved runtime" / "python/bin/python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(original_python)
        selected_tools = self.root / "selected tools"
        selected_tools.mkdir()
        for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
            (selected_tools / name).symlink_to(path)
        source, pcm, destination = (self.root / name for name in ("source.silk", "samples.pcm", "readable.flac"))
        pcm.write_bytes(b"".join(struct.pack("<h", int(4000 * math.sin(index * .1))) for index in range(4800)))
        pilk.encode(str(pcm), str(source), pcm_rate=24000, tencent=True)
        with patch.object(codecs.sys, "executable", str(interpreter)), \
                patch.object(runtime, "bundled_resources", return_value=None), \
                patch.dict(os.environ, {"SHILIAO_TOOLS_DIR": str(selected_tools), "PATH": "/usr/bin:/bin"}), \
                patch.object(codecs, "_run", wraps=codecs._run) as run:
            info = codecs.convert_audio(source, destination)
            commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(info["source_silk"])
        self.assertTrue(info["lossless"])
        self.assertEqual(commands[0][:2], [str(interpreter), "-c"])
        self.assertTrue(any(command[0] == str(selected_tools / "ffmpeg") for command in commands))
        self.assertTrue(any(command[0] == str(selected_tools / "ffprobe") for command in commands))
        self.assertGreater(destination.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
