"""Synthetic media mappings: no real WeChat source or private data is used."""

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vault.wechat_media import MediaLocator, MediaLocatorError, MAX_VOICE_BYTES


CHAT = "fixture-chat"
OTHER = "fixture-other"
CHAT_HASH = hashlib.md5(CHAT.encode()).hexdigest()
TABLE = "Msg_" + CHAT_HASH
STAMP = int(datetime(2026, 6, 15, 12, tzinfo=timezone.utc).timestamp())
MONTH = "2026-06"
DIGEST = "0123456789abcdef0123456789abcdef"
SECOND = "fedcba9876543210fedcba9876543210"
VIDEO = b"\x00\x00\x00\x18ftypisomfixture"
FFMPEG = shutil.which("ffmpeg")


@contextmanager
def fixture_database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class MediaLocatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "decrypted"
        self.account = self.base / "account"
        (self.root / "message").mkdir(parents=True)
        self.account.mkdir()
        self.message_path = self.root / "message/message_0.db"
        with fixture_database(self.message_path) as db:
            db.executescript("CREATE TABLE Name2Id(user_name TEXT); INSERT INTO Name2Id VALUES('fixture-chat');"
                             f"CREATE TABLE {TABLE}(local_id INTEGER,server_id INTEGER,create_time INTEGER,local_type INTEGER,message_content BLOB);")
            db.executemany(f"INSERT INTO {TABLE} VALUES(?,?,?,?,?)",
                           [(1, 101, STAMP, 3, b"unread-image-xml"),
                            (2, 102, STAMP, 34, b"unread-audio-xml"),
                            (3, 103, STAMP, 43, b"unread-video-xml"),
                            (4, 104, STAMP, 1, b"unread-text")])
        self.resource_path = self.root / "message/message_resource.db"
        with fixture_database(self.resource_path) as db:
            db.executescript("""
                CREATE TABLE ChatName2Id(user_name TEXT);
                INSERT INTO ChatName2Id VALUES('fixture-chat'),('fixture-other');
                CREATE TABLE MessageResourceInfo(message_id INTEGER PRIMARY KEY,chat_id INTEGER,
                    message_local_type INTEGER,message_create_time INTEGER,message_local_id INTEGER,
                    message_svr_id INTEGER,packed_info BLOB);
                CREATE TABLE MessageResourceDetail(message_id INTEGER,size INTEGER,status INTEGER,packed_info BLOB);
                CREATE INDEX detail_message_id ON MessageResourceDetail(message_id);
            """)
        self.voice_path = self.root / "message/media_0.db"
        self.make_voice_db(self.voice_path)

    def make_voice_db(self, path):
        with fixture_database(path) as db:
            db.executescript("""
                CREATE TABLE Name2Id(user_name TEXT);
                INSERT INTO Name2Id VALUES('fixture-chat'),('fixture-other');
                CREATE TABLE VoiceInfo(chat_name_id INTEGER,create_time INTEGER,local_id INTEGER,
                    svr_id INTEGER,voice_data BLOB,data_index TEXT);
            """)

    def resource(self, ident=1, kind=3, server=101, local=1, digest=DIGEST, size=7, status=1, chat=1):
        with fixture_database(self.resource_path) as db:
            db.execute("INSERT INTO MessageResourceInfo VALUES(?,?,?,?,?,?,?)",
                       (ident, chat, kind, STAMP, local, server, b"\x0a\x20" + digest.encode()))
            db.execute("INSERT INTO MessageResourceDetail VALUES(?,?,?,?)", (ident, size, status, b""))

    def image(self, digest=DIGEST, suffix=".dat", body=b"fixture"):
        path = self.account / "msg/attach" / CHAT_HASH / MONTH / "Img" / (digest + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def video(self, digest=DIGEST, suffix=".mp4", body=VIDEO):
        path = self.account / "msg/video" / MONTH / (digest + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def voice(self, path=None, chat=1, local=2, server=102, body=b"\x02#!SILK_V3fixture"):
        with fixture_database(path or self.voice_path) as db:
            db.execute("INSERT INTO VoiceInfo VALUES(?,?,?,?,?,?)", (chat, STAMP, local, server, body, ""))

    def ref(self, kind="image", local=1, server=101, chat=CHAT):
        return {"database_name": "message_0.db", "table_name": TABLE.lower(),
                "local_id": str(local), "server_id": server, "create_time": STAMP,
                "chat_username": chat, "kind": kind}

    def test_stream_message_identity_across_shards_without_content(self):
        shard = self.root / "message/message_1.db"
        with fixture_database(shard) as db:
            db.executescript("CREATE TABLE Name2Id(user_name TEXT);"
                             f"CREATE TABLE {TABLE}(local_id,server_id,create_time,local_type);")
            db.execute(f"INSERT INTO {TABLE} VALUES(?,?,?,?)", (1, 999, STAMP, 3))
        with MediaLocator(self.root, self.account) as locator:
            values = list(locator.iter_media_messages())
        self.assertEqual(len(values), 4)
        self.assertEqual([item["kind"] for item in values[:3]], ["image", "audio", "video"])
        self.assertEqual(values[-1]["database_name"], "message_1.db")
        self.assertEqual(values[-1]["chat_username"], CHAT)
        self.assertEqual(values[-1]["table_name"], TABLE.lower())
        self.assertTrue(all("message_content" not in item for item in values))

    def test_image_exact_hash_size_and_multiple_variants(self):
        self.resource()
        original = self.image()
        thumb = self.image(suffix="_t.dat", body=b"tiny")
        self.image(digest=SECOND)
        with fixture_database(self.resource_path) as db:
            db.execute("INSERT INTO MessageResourceDetail VALUES(?,?,?,?)", (1, 4, 1, b""))
        with MediaLocator(self.root, self.account) as locator:
            result = locator.locate(self.ref())
            self.assertEqual(result["status"], "ready")
            self.assertEqual([item["path"] for item in result["candidates"]], [original, thumb])
            with locator.open_candidate(result["candidates"][0]) as stream:
                self.assertEqual(stream.read(), b"fixture")

    def test_wrong_chat_and_wrong_size_do_not_match(self):
        self.resource(chat=2)
        self.image()
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref())["status"], "missing")
        with fixture_database(self.resource_path) as db:
            db.execute("UPDATE MessageResourceInfo SET chat_id=1")
            db.execute("UPDATE MessageResourceDetail SET size=99")
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref())["status"], "missing")

    def test_not_downloaded_and_missing_auxiliary_database(self):
        self.resource(status=0)
        self.image()
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref())["detail"], "media_not_downloaded")
        self.resource_path.unlink()
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref())["detail"], "resource_database_missing")

    def test_video_ignores_thumbnail_and_rejects_multiple_originals(self):
        self.resource(kind=43, server=103, local=3, size=len(VIDEO))
        original = self.video()
        self.video(suffix=".jpg", body=b"\xff\xd8" + b"x" * (len(VIDEO) - 2))
        with MediaLocator(self.root, self.account) as locator:
            result = locator.locate(self.ref("video", 3, 103))
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["candidates"][0]["path"], original)
        self.video(suffix="_duplicate.mp4", body=VIDEO[:-1] + b"X")
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref("video", 3, 103))["status"], "ambiguous")

    def test_identical_video_files_are_safely_deduplicated(self):
        self.resource(kind=43, server=103, local=3, size=len(VIDEO))
        self.video()
        self.video(suffix="_duplicate.mp4")
        with MediaLocator(self.root, self.account) as locator:
            result = locator.locate(self.ref("video", 3, 103))
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["detail"], "identical_video_files")
            self.assertEqual(len(result["candidates"]), 1)

    def quality_pair(self):
        self.resource(kind=43, server=103, local=3, size=len(VIDEO))
        normal = self.video()
        raw = self.video(suffix="_raw.mp4", body=VIDEO[:-1] + b"X")
        return normal, raw

    @staticmethod
    def probe_result(width, height, duration):
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "format": {"duration": str(duration)},
            "streams": [{"codec_type": "video", "width": width, "height": height}],
        }).encode())

    def test_known_quality_pair_uses_highest_resolution_and_caches_probe(self):
        _, raw = self.quality_pair()
        with MediaLocator(self.root, self.account) as locator:
            responses = [self.probe_result(720, 1280, 30), self.probe_result(1080, 1920, 30.05)]
            with patch("vault.wechat_media.subprocess.run", side_effect=responses) as probe:
                first = locator.locate(self.ref("video", 3, 103))
                second = locator.locate(self.ref("video", 3, 103))
                self.assertEqual(probe.call_count, 2)
                self.assertTrue(all(str(arg).startswith("/dev/fd/") for arg in [call.args[0][-1] for call in probe.call_args_list]))
            self.assertEqual(first["status"], "ready")
            self.assertEqual(first["detail"], "video_quality_variant")
            self.assertEqual(first["candidates"][0]["path"], raw)
            self.assertEqual(first, second)
            self.assertTrue(first["candidates"][0]["quality_variant"])

    def test_same_resolution_quality_pair_prefers_original(self):
        _, raw = self.quality_pair()
        with MediaLocator(self.root, self.account) as locator:
            with patch.object(locator, "_video_properties", return_value={"width": 720, "height": 1280, "duration": 30}):
                result = locator.locate(self.ref("video", 3, 103))
            self.assertEqual(result["candidates"][0]["path"], raw)
            self.assertEqual(result["candidates"][0]["variant_role"], "original")

    def test_quality_pair_uses_pixels_instead_of_filename_preference(self):
        normal, _ = self.quality_pair()
        with MediaLocator(self.root, self.account) as locator:
            with patch.object(locator, "_video_properties", side_effect=[
                    {"width": 1920, "height": 1080, "duration": 30},
                    {"width": 720, "height": 1280, "duration": 30}]):
                result = locator.locate(self.ref("video", 3, 103))
            self.assertEqual(result["candidates"][0]["path"], normal)

    def test_quality_pair_duration_mismatch_or_probe_failure_stays_ambiguous(self):
        self.quality_pair()
        with MediaLocator(self.root, self.account) as locator:
            with patch.object(locator, "_video_properties", side_effect=[
                    {"width": 720, "height": 1280, "duration": 30},
                    {"width": 1080, "height": 1920, "duration": 30.2}]):
                self.assertEqual(locator.locate(self.ref("video", 3, 103))["status"], "ambiguous")
            with patch.object(locator, "_video_properties", return_value=None):
                self.assertEqual(locator.locate(self.ref("video", 3, 103))["status"], "ambiguous")

    def test_different_resource_families_do_not_form_quality_pair(self):
        self.resource(kind=43, server=103, local=3, size=len(VIDEO))
        with fixture_database(self.resource_path) as db:
            db.execute("UPDATE MessageResourceDetail SET packed_info=?", (b"\x0a\x20" + SECOND.encode(),))
        self.video()
        self.video(digest=SECOND, suffix="_raw.mp4", body=VIDEO[:-1] + b"X")
        with MediaLocator(self.root, self.account) as locator:
            with patch.object(locator, "_video_properties") as probe:
                self.assertEqual(locator.locate(self.ref("video", 3, 103))["status"], "ambiguous")
                probe.assert_not_called()

    def test_probe_rejects_nonfinite_duration_and_missing_video_stream(self):
        self.quality_pair()
        with MediaLocator(self.root, self.account) as locator:
            with patch("vault.wechat_media.subprocess.run", return_value=self.probe_result(1280, 720, "NaN")):
                self.assertEqual(locator.locate(self.ref("video", 3, 103))["status"], "ambiguous")
            locator._video_probes.clear()
            empty = SimpleNamespace(returncode=0, stdout=b'{"format":{"duration":"30"},"streams":[]}')
            with patch("vault.wechat_media.subprocess.run", return_value=empty):
                self.assertEqual(locator.locate(self.ref("video", 3, 103))["status"], "ambiguous")

    @unittest.skipUnless(FFMPEG, "FFmpeg is required for the descriptor probe integration test")
    def test_real_ffprobe_reads_inherited_descriptor_and_selects_resolution(self):
        normal = self.video()
        raw = self.video(suffix="_raw.mp4")
        for path, dimensions in ((normal, "64x48"), (raw, "128x96")):
            subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={dimensions}:r=10",
                            "-t", "0.3", "-c:v", "mpeg4", "-an", "-y", str(path)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=True)
        self.resource(kind=43, server=103, local=3, size=normal.stat().st_size)
        with fixture_database(self.resource_path) as db:
            db.execute("INSERT INTO MessageResourceDetail VALUES(?,?,?,?)", (1, raw.stat().st_size, 1, b""))
        with MediaLocator(self.root, self.account) as locator:
            result = locator.locate(self.ref("video", 3, 103))
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["detail"], "video_quality_variant")
            self.assertEqual(result["candidates"][0]["path"], raw)
            self.assertEqual((result["candidates"][0]["width"], result["candidates"][0]["height"]), (128, 96))

    def test_voice_unique_conversation_and_server_identity(self):
        self.voice()
        self.voice(chat=2, body=b"wrong-chat")
        with MediaLocator(self.root, self.account) as locator:
            result = locator.locate(self.ref("audio", 2, 102))
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["candidates"][0]["voice_data"], b"\x02#!SILK_V3fixture")

    def test_voice_ambiguity_across_shards_is_not_guessed(self):
        self.voice()
        second = self.root / "message/media_1.db"
        self.make_voice_db(second)
        self.voice(path=second, body=b"different")
        with MediaLocator(self.root, self.account) as locator:
            result = locator.locate(self.ref("audio", 2, 102))
            self.assertEqual(result["status"], "ambiguous")
            self.assertEqual(result["candidates"], [])

    def test_zero_server_id_requires_local_id_and_exact_timestamp(self):
        self.voice(server=0)
        self.resource(server=0)
        self.image()
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref("audio", 2, 0))["status"], "ready")
            self.assertEqual(locator.locate(self.ref(server=0))["status"], "ready")
            ref = self.ref("audio", 2, 0)
            ref["create_time"] += 1
            self.assertEqual(locator.locate(ref)["status"], "missing")

    def test_duplicate_resource_identity_is_ambiguous(self):
        self.resource()
        self.resource(ident=2, digest=SECOND)
        self.image()
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref())["status"], "ambiguous")

    def test_equivalent_resource_duplicates_merge_safely(self):
        self.resource()
        self.resource(ident=2)
        self.image()
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref())["status"], "ready")

    def test_server_conflict_is_disambiguated_by_local_id_and_timestamp(self):
        self.resource()
        self.resource(ident=2, local=99, digest=SECOND)
        self.image()
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref())["status"], "ready")

    def test_video_container_identified_without_filename_extension(self):
        self.resource(kind=43, server=103, local=3, size=len(VIDEO))
        original = self.video(suffix="_raw")
        with MediaLocator(self.root, self.account) as locator:
            result = locator.locate(self.ref("video", 3, 103))
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["candidates"][0]["path"], original)

    def test_voice_oversize_is_rejected_without_materializing_blob(self):
        with fixture_database(self.voice_path) as db:
            db.execute("INSERT INTO VoiceInfo VALUES(?,?,?,?,zeroblob(?),'')", (1, STAMP, 2, 102, MAX_VOICE_BYTES + 1))
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref("audio", 2, 102))["detail"], "voice_size_unsupported")

    def test_directory_symlink_and_file_symlink_are_rejected(self):
        self.resource()
        path = self.image()
        outside = self.base / "outside.dat"
        outside.write_bytes(b"fixture")
        path.unlink()
        path.symlink_to(outside)
        with MediaLocator(self.root, self.account) as locator:
            self.assertNotEqual(locator.locate(self.ref())["status"], "ready")
        path.unlink()
        parent = path.parent
        renamed = parent.with_name("Hidden")
        parent.rename(renamed)
        (renamed / path.name).write_bytes(b"fixture")
        parent.symlink_to(renamed, target_is_directory=True)
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref())["status"], "unsafe")

    def test_database_symlinks_and_pending_wal_are_rejected(self):
        alternate = self.base / "alternate.db"
        self.voice_path.rename(alternate)
        self.voice_path.symlink_to(alternate)
        with self.assertRaises(MediaLocatorError):
            MediaLocator(self.root, self.account)
        self.voice_path.unlink()
        alternate.rename(self.voice_path)
        self.voice_path.with_name("media_0.db-wal").write_bytes(b"pending")
        with self.assertRaises(MediaLocatorError) as raised:
            MediaLocator(self.root, self.account)
        self.assertEqual(raised.exception.code, "pending_journal")

    def test_source_and_candidate_changes_are_detected(self):
        self.resource()
        path = self.image()
        locator = MediaLocator(self.root, self.account)
        self.addCleanup(locator.close, check=False)
        candidate = locator.locate(self.ref())["candidates"][0]
        path.write_bytes(b"changed")
        with self.assertRaises(MediaLocatorError):
            with locator.open_candidate(candidate):
                pass
        self.assertEqual(locator.locate(self.ref())["detail"], "media_source_changed")
        with fixture_database(self.voice_path) as db:
            db.execute("INSERT INTO Name2Id VALUES('new')")
        with self.assertRaises(MediaLocatorError):
            locator.assert_unchanged()

    def test_folder_index_reused_and_source_bytes_unchanged(self):
        self.resource()
        path = self.image()
        before = {p: (hashlib.sha256(p.read_bytes()).digest(), p.stat().st_mtime_ns)
                  for p in [self.message_path, self.resource_path, self.voice_path, path]}
        with MediaLocator(self.root, self.account) as locator:
            with patch("vault.wechat_media.os.scandir", wraps=os.scandir) as scandir:
                for _ in range(3):
                    self.assertEqual(locator.locate(self.ref())["status"], "ready")
                self.assertEqual(scandir.call_count, 1)
        after = {p: (hashlib.sha256(p.read_bytes()).digest(), p.stat().st_mtime_ns) for p in before}
        self.assertEqual(before, after)
        self.assertFalse(list(self.root.rglob("*-wal")))
        self.assertFalse(list(self.root.rglob("*-shm")))

    def test_missing_chat_unsupported_type_and_invalid_metadata(self):
        with MediaLocator(self.root, self.account) as locator:
            self.assertEqual(locator.locate(self.ref(chat=None))["detail"], "conversation_identity_missing")
            self.assertEqual(locator.locate(self.ref(kind="unknown"))["status"], "unsupported")
            self.assertEqual(locator.locate(self.ref(server="bad"))["status"], "unsupported")

    def test_unsupported_schema_and_closed_locator(self):
        with fixture_database(self.voice_path) as db:
            db.execute("DROP TABLE VoiceInfo")
        with self.assertRaises(MediaLocatorError) as raised:
            MediaLocator(self.root, self.account)
        self.assertEqual(raised.exception.code, "unsupported_schema")
        self.voice_path.unlink()
        locator = MediaLocator(self.root, self.account)
        locator.close()
        with self.assertRaises(MediaLocatorError):
            locator.locate(self.ref())


if __name__ == "__main__":
    unittest.main()
